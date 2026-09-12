#!/usr/bin/env python3
"""
Bridge bandwidth test, Linux side. Run ON the UNO Q (QRB2210):

    sudo systemctl stop arduino-router      # it owns /dev/ttyHS1
    python3 bw_test.py 2000000 30           # baud, seconds

Sends [A5 5A][seq][len][256 B payload][crc16] frames as fast as the port
accepts, and prints the STM32's "S good=.. bad=.. drop=.." lines next to
the local send count. Audio needs ~176,400 B/s payload (44.1k/16/2).
"""
import binascii, os, struct, sys, time
import serial

PAYLOAD = 256
PORT = "/dev/ttyHS1"
BATCH = 16            # frames per write()

def frame(seq, payload):
    body = struct.pack("<HH", seq & 0xFFFF, PAYLOAD) + payload
    crc = binascii.crc_hqx(body, 0xFFFF)          # CRC-16/CCITT-FALSE
    return b"\xA5\x5A" + body + struct.pack("<H", crc)

def main():
    baud = int(sys.argv[1]) if len(sys.argv) > 1 else 1000000
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 30
    payload = os.urandom(PAYLOAD)

    ser = serial.Serial(PORT, baud, rtscts=True, timeout=0, write_timeout=5)
    ser.reset_input_buffer()
    print(f"port={PORT} baud={baud} frame={len(frame(0, payload))}B "
          f"raw_max={baud/10:.0f} B/s")

    seq, sent_bytes, sent_frames = 0, 0, 0
    t0 = tlast = time.monotonic()
    rxbuf = b""

    while time.monotonic() - t0 < secs:
        chunk = b"".join(frame(seq + i, payload) for i in range(BATCH))
        seq += BATCH
        ser.write(chunk)
        sent_bytes += len(chunk)
        sent_frames += BATCH

        rxbuf += ser.read(4096)
        while b"\n" in rxbuf:
            line, rxbuf = rxbuf.split(b"\n", 1)
            el = time.monotonic() - t0
            print(f"[{el:5.1f}s] sent={sent_frames} ({sent_bytes/el:,.0f} B/s)  "
                  f"stm32: {line.decode(errors='replace').strip()}")

    ser.flush()
    time.sleep(1.5)
    rxbuf += ser.read(4096)
    for line in rxbuf.split(b"\n"):
        if line.strip():
            print("final:", line.decode(errors="replace").strip())
    el = time.monotonic() - t0
    print(f"\nSENT {sent_frames} frames, {sent_bytes} bytes, "
          f"{sent_bytes/el:,.0f} B/s over {el:.1f}s  "
          f"(payload {sent_frames*PAYLOAD/el:,.0f} B/s)")
    ser.close()

if __name__ == "__main__":
    main()
