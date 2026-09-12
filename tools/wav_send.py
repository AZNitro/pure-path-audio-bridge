#!/usr/bin/env python3
"""
Phase 2A test sender. Run ON the UNO Q with arduino-router stopped:

    python3 wav_send.py song.wav            # 16-bit stereo 44.1 kHz WAV only

Streams AUDIO frames at 3 Mbaud, reads STATUS frames back, and paces to hold
the STM32 PCM ring near 50 %. Python manages ~220 kB/s here, audio needs 176.
"""
import binascii, struct, sys, time, wave
import serial

PORT, BAUD = "/dev/ttyHS1", 3000000
PAYLOAD = 512
FMT = 0x11                      # 16-bit, 44.1k
T_AUDIO, T_STATUS = 1, 3
TARGET = 0.50                   # ring fill to hold
HI, LO = 0.60, 0.40

def frame(seq, typ, payload):
    body = struct.pack("<HBBH", seq & 0xFFFF, typ, FMT, len(payload)) + payload
    return b"\xA5\x5A" + body + struct.pack("<H", binascii.crc_hqx(body, 0xFFFF))

class Parser:
    def __init__(self): self.buf = b""
    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"\xA5\x5A")
            if i < 0: self.buf = self.buf[-1:]; break
            if len(self.buf) < i + 8: self.buf = self.buf[i:]; break
            seq, typ, fmt, ln = struct.unpack_from("<HBBH", self.buf, i + 2)
            if ln > 512: self.buf = self.buf[i + 2:]; continue
            end = i + 8 + ln + 2
            if len(self.buf) < end: self.buf = self.buf[i:]; break
            body = self.buf[i + 2:i + 8 + ln]
            crc, = struct.unpack_from("<H", self.buf, i + 8 + ln)
            self.buf = self.buf[end:]
            if binascii.crc_hqx(body, 0xFFFF) == crc:
                out.append((typ, body[6:]))
        return out

def main():
    w = wave.open(sys.argv[1], "rb")
    assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 2, 44100), \
        "need 16-bit stereo 44.1 kHz WAV"
    ser = serial.Serial(PORT, BAUD, rtscts=True, timeout=0, write_timeout=5)
    ser.reset_input_buffer()
    p = Parser()
    seq, fill_ratio, t0 = 0, 0.0, time.monotonic()
    last_print, status = 0, None

    while True:
        pcm = w.readframes(PAYLOAD // 4)
        if not pcm:
            break
        if len(pcm) < PAYLOAD:
            pcm += b"\0" * (PAYLOAD - len(pcm))

        # pacing: if the STM32 ring is above HI, wait until it drains
        while fill_ratio > HI:
            time.sleep(0.005)
            for typ, body in p.feed(ser.read(4096)):
                if typ == T_STATUS:
                    status = struct.unpack("<6I", body)
                    fill_ratio = status[0] / status[1]

        ser.write(frame(seq, T_AUDIO, pcm))
        seq += 1

        for typ, body in p.feed(ser.read(4096)):
            if typ == T_STATUS:
                status = struct.unpack("<6I", body)
                fill_ratio = status[0] / status[1]

        now = time.monotonic()
        if status and now - last_print >= 1.0:
            last_print = now
            fill, size, under, bad, drop, ovf = status
            print(f"[{now - t0:6.1f}s] seq={seq} fill={fill*100//size:3d}% "
                  f"underrun={under} bad={bad} drop={drop} rxovf={ovf}")

    ser.flush()
    time.sleep(0.5)
    print("done")

if __name__ == "__main__":
    main()
