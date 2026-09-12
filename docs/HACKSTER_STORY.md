# Pure-Path Audio Bridge

**A Hi-Fi network streamer on the Arduino UNO Q: every sample that leaves Linux reaches the DAC unchanged — and I can prove it with a checksum. Plus an AI mode that listens to what's playing and picks the EQ for you.**

> Category: Home Automation · Board: Arduino UNO Q · Repo: https://github.com/AZNitro/pure-path-audio-bridge

`[VIDEO: one-take demo — boot → matrix VU → Spotify → FLAC cast → Pure/Enhanced A/B → CRC MATCH]`

`[IMAGE: cover — UNO Q + PCM5102 module on the bench, matrix lit, headphones/amp in frame]`

---

## Why

A "proper" network streamer plus a separate DAC costs around NT$17,000 (about US$500) for something like an iFi Zen Stream + Zen DAC. What you're paying for is two things: a box that speaks Spotify Connect / UPnP / AirPlay, and a clean, correctly-clocked I2S path into a DAC chip.

The UNO Q already has both halves of that on one board: a quad-core Qualcomm QRB2210 running Debian for the network side, and an STM32U585 microcontroller with a real SAI (Serial Audio Interface) peripheral for the I2S side. Add a US$5 PCM5102A DAC module and, on paper, you have a streamer.

I wanted to find out whether "on paper" survives contact with reality — and whether I could *prove* the audio path is transparent rather than just say it sounds fine.

## What it does

- **Sources:** Spotify Connect (shows up as "UNO Q" in the Spotify app), UPnP/DLNA renderer (BubbleUPnP, Windows "Cast to Device", NetEase Cloud Music, any DLNA controller), and local FLAC/WAV files via MPD. All of them feed one shared pipe, one at a time.
- **Pure mode:** bytes go from the decoder to the DAC's I2S pins untouched. Verified with a CRC-32 at both ends (details below).
- **Enhanced mode:** six EQ presets I designed (Flat, Speech, Warm, Bright, Bass, Acoustic), a 5-band Custom EQ, and an **Auto** setting where Google's YAMNet audio classifier listens to the stream and chooses the preset. You can override it, and "Warmer / Brighter / More bass / Less bass" buttons let you nudge each preset toward your taste — the nudges are remembered per content type.
- **Status page** on the LAN: buffer fill, link counters, now-playing, MPD transport controls, mode/preset, live level meter, service health.
- **LED matrix VU meter** on the UNO Q's own 13×8 matrix, driven from the STM32 from the exact bytes going to the DAC.
- Six systemd services, survives power cycles, no keyboard or screen needed.

`[IMAGE: status page screenshot — Enhanced/Auto, "Detected: Speech 0.87 → Speech"]`

## Architecture

```
  Spotify app ──Connect──▶ librespot ─┐
  Phone/PC   ──UPnP/DLNA─▶ upmpdcli ─▶ MPD ─┤
  Local FLAC ─────────────────────────▶ MPD ─┤
                                            ▼
                                   audio.fifo  (s16le / 44.1 kHz / stereo)
                                            │
                          enhance.py  (Pure: passthrough · Enhanced: EQ, YAMNet)
                                            │
                                   bridge.fifo
                                            │
                          bridge-tx  (C: frames + CRC-16, rate-paced, P-control)
                                            │
                    ══════ LPUART1 · 3 Mbaud · RTS/CTS · DMA ══════
                                            │
        STM32U585 (Zephyr): DMA RX → parser → 64 KB PCM ring → SAI2 (DMA, master)
                                            │
                                   I2S: BCK / LRCK / DATA
                                            ▼
                                     PCM5102A DAC → line out
```

Linux side: QRB2210, Debian 13. STM32 side: custom Zephyr firmware, ~50 kB flash. The two talk over the on-board UART that normally carries Arduino's own Bridge protocol.

`[IMAGE: block diagram (same as above, rendered)]`

## Why custom firmware instead of App Lab

This was the first real decision, and it was forced by two measurements.

**1. The stock Arduino Bridge is 15× too slow.** 16-bit stereo at 44.1 kHz is 176,400 bytes per second. The Bridge link between the Qualcomm and the STM32 runs at 115,200 baud ≈ 11.5 kB/s. There is no way to stream audio through it.

**2. App Lab can't turn on the SAI peripheral.** The devicetree is fixed at Arduino's build time; a sketch cannot enable SAI2 or its DMA channel. So I built the STM32 side as a normal Zephyr application with `west`, and re-configured the same UART (LPUART1) for 3 Mbaud with hardware flow control and DMA in both directions.

I measured the link before locking the rate:

| Baud | Clean payload | Result |
|---|---|---|
| 2 M | 161 kB/s | clean, but below the 176 kB/s needed |
| 3 M | 222 kB/s | clean — **chosen**, ~26 % headroom |
| 4 M | — | RX overflow: the interrupt path can't keep up |

The rules don't require App Lab, so this write-up tells the custom-firmware story directly. Flashing is done with OpenOCD bit-banging SWD from the QRB2210's GPIO, reached over `adb` — no external probe. (One rule learned the hard way: after flashing, power-cycle the board; a debugger `reset` leaves the STM32U5 stuck in its boot ROM.)

## Getting the clock exactly right

A DAC only sounds right if the bit clock is exactly 64 × Fs. The STM32's 16 MHz HSI can't divide down to 44.1 kHz — the nearest integer divider is 3 % sharp, which a tuner app catches instantly. The fix is PLL2 with a fractional multiplier, fed from the 4 MHz MSI clock:

```
N = 56 + 3670/8192, P = 20  →  PLL2P = 11,289,599.61 Hz
ideal 256 × 44,100          =  11,289,600.00 Hz
error                       =  −0.035 ppm
```

An integer N is arithmetically impossible at this input frequency (11,289,600 / 4,000,000 = 1764/625), so FRACN is mandatory. I confirmed a 440 Hz test tone with a tuner app before moving on.

`[IMAGE: tuner app showing 440.0 Hz]`

## The link protocol

Every 512-byte PCM payload is wrapped in a small frame:

```
[A5 5A] [seq u16] [type u8] [fmt u8] [len u16] [512 bytes PCM] [CRC-16/CCITT]
```

The STM32 answers every 100 ms with a STATUS frame: ring fill, underruns, bad frames, dropped sequence numbers, RX overflows — and, since Phase 5, a running **CRC-32 of every PCM byte it has handed to the SAI** plus the byte count.

`bridge-tx` (C) sends at the nominal byte rate and applies a proportional correction on the STM32's ring fill (target 50 %, Kp = 0.6). No start/stop — the fill line stays flat at 50 % and the SAI never stops clocking, so there are no clicks between tracks. A 64 KB ring is about 370 ms of audio; playback starts once it is half full.

`[IMAGE: bridge-tx log — fill=50.0% underrun=0 bad=0 drop=0 rxovf=0]`

## Proof, not vibes: the CRC check

"Bit-perfect" is a claim people make a lot. Here's what I actually verified.

The sender CRC-32s every byte it writes to the UART. The firmware CRC-32s every byte it takes out of the ring and hands to the SAI DMA (silence inserted on underrun is excluded). Both use the zlib polynomial. At the end of a file, `bridge-tx` compares counts and CRCs:

```
sent_bytes=5292032   sent_crc32=5c906e27
stm32_consumed=5292032 stm32_crc32=5c906e27   MATCH
```

Then, unattended over a 10-minute file:

```
sent_bytes=105840128   sent_crc32=887b40e4
stm32_consumed=105840128 stm32_crc32=887b40e4   MATCH
```

The same check run **through `enhance.py` in Pure mode** gives the same `5c906e27` — so the Pure path really is a byte-exact passthrough, not "close enough". An earlier 10-minute stress run logged 206,719 frames with zero bad/drop/underrun.

**What this does and doesn't mean.** The CRC proves the transport: from the Linux audio pipeline to the DAC's I2S pins, nothing changes a sample. It does *not* make a lossy source lossless — Spotify Connect delivers 320 kbps Ogg Vorbis, and I transport that decoded stream bit-exact, but the stream itself is lossy. For a 16-bit / 44.1 kHz FLAC sent over UPnP with the software mixer at 100 %, the chain is bit-perfect from file to DAC. Hi-res files get resampled by MPD (soxr, dithered) to the locked 16/44.1 format first.

One thing this taught me: **volume must live in the analog domain**. Both MPD and librespot attenuate in software. At a comfortable listening level MPD's mixer sat at 23 %, which is −31 dB — five bits thrown away before the DAC. Set both to 100 % (verified bit-transparent: −24.1 dBFS in, −24.1 dBFS out) and use the amplifier. That's also why a DAC with hardware volume is on the next-steps list.

## The AI part, honestly

The contest asks for AI, and it's easy to bolt on something that sounds impressive and does nothing. Here's exactly what the model does and doesn't do.

**YAMNet** (Google, Apache-2.0, 521 AudioSet classes) runs on the QRB2210 via TFLite. Every 2 seconds it gets the last 1 second of audio, downsampled to 16 kHz mono, and returns a class label with a score. A small table maps labels to *my* presets — speech and narration → Speech; classical, piano, jazz → Acoustic; hip-hop, electronic → Bass; rock and metal → Bright; pop and vocal → Warm; anything else → Flat.

**The model never touches the audio.** It listens to a side copy and returns a label. The sound is shaped by ordinary DSP — second-order IIR sections designed from the RBJ cookbook, run in float, with a headroom pre-gain per preset so boosts can't clip. AI chooses; DSP shapes. On the status page you can see the decision live: "Detected: Speech 0.87 → Speech".

Cost: the classifier alone is about 2.5 % of one A53 core. The whole Enhanced pipeline in Python — EQ, crossfades, classifier, level meter — is ~26 % of one core, ~7.5 % of the four-core chip.

**You stay in charge.** Tap a preset while Auto is on and it becomes an override, held until the detected content changes. Tap it again to pin it. The five-slider Custom EQ (60 / 250 / 1k / 4k / 12k Hz, ±12 dB) uses a small solver so that the *combined* response hits your requested gains — neighbouring Q = 1 bands leak into each other by up to 2 dB otherwise. And the "How does it feel?" buttons add ±1 dB nudges that are stored **per detected class**, so over time Auto's "Warm" becomes *your* Warm.

`[IMAGE: presets row with Auto active and "Override: Warm (Auto detected Speech)"]`
`[IMAGE: custom EQ sliders + feedback buttons]`

I evaluated Qualcomm's SNPE runtime and Edge Impulse first. Neither was needed: the QRB2210 has no NPU, inference is 0.5 Hz, and YAMNet needs no training. Simplest thing that works.

## The bug I found by measuring

Switching presets clicked. The obvious fix is a crossfade, so I added a 93 ms (4096-frame) crossfade between the old and new filter chains — Pure is just a chain with no sections, so Pure↔Enhanced and preset↔preset go through one code path.

To check it, I captured the output during a 440 Hz tone while toggling Pure → Bass → Warm → Pure, and measured the sample-to-sample slew against the tone's own natural slew:

```
                            peak (× tone)   max slew (× natural)
no crossfade                2.00  CLIPS     21.2
crossfade, shipped state    2.00  CLIPS      1.37
crossfade + zero state      1.00            1.00
```

The crossfade removed the step (21× → 1.4×). But the output was still slamming into full scale on every switch — a smooth low-frequency swell that the slew metric didn't see. The cause was `scipy.signal.sosfilt_zi`: it returns the filter's steady state for a *unit step* input, which is right for `filtfilt` edge handling and wrong for a filter taking over mid-song. Each new chain started as if full-scale DC had been applied forever, and that phantom DC bled off as a swell. With the crossfade gone, the louder click had simply been hiding it.

A filter that fades in should start from rest. Zero initial state, phase-swept over 16 switch points: 1.00× peak, 1.00× slew, every preset. Pure CRC re-checked: still `5c906e27`.

`[IMAGE: capture plot — before/after switch transient]`

## The LED matrix VU meter

The UNO Q's 13×8 matrix is the board's signature, and a dark matrix looks broken. The firmware now shows two RMS bars (left / right) with a peak-hold pixel, drawn at 20 fps from the same thread that feeds the SAI, computed over the same bytes the CRC covers — nothing was added to any ISR or DMA callback.

One twist: because volume is digital and applied upstream, an absolute-scale meter shows your *listening volume*, not the music — at a quiet level the bars barely lit. So the meter is relative to an auto-gain reference (fast attack, ~3 s release) with an absolute −60 dBFS floor so silence stays dark and a +40 dB gain cap. It fills at any volume and still shows the dynamics. Everything is integer maths on mean-square values — no square roots, no floats, in the loop that also sends STATUS.

If the link drops or errors accumulate, the diagnostics take over the matrix; when idle, a single dim heartbeat pixel blinks so you can tell "booted, waiting" from "dead".

`[IMAGE / GIF: matrix VU bouncing]`

## Things that didn't work, and decisions

- **4 Mbaud** overflowed the STM32's interrupt-driven RX. 3 M is the stable ceiling with this driver; a DMA-only RX path could go higher.
- **Spotify Lossless** is unavailable to every third-party Connect device, including commercial streamers like the Zen Stream. 320 kbps Vorbis is the ceiling for anyone outside Spotify's certification program. Not my bug; worth knowing.
- **AirPlay** (shairport-sync) was planned and deferred — UPnP already covers lossless from a phone.
- **App Lab / Arduino Bridge** — see above; measured, not assumed.
- **Digital volume** throws away resolution. Keep it at 100 % and use the amp.

## What's next

- A restoration model for lossy streams (bring back what 320 kbps Vorbis loses) — this is the interesting AI problem, and now there's a bit-exact path to A/B it against.
- Port `enhance.py` to Rust: the Python EQ is ~26 % of a core; a native version should be a few percent.
- 24-bit audio over a DMA-only RX at 4 Mbaud.
- USB Audio gadget mode (UAC2) so the board is also a USB DAC for a PC — needs the `f_uac2` kernel module, which the stock kernel doesn't ship.
- A PCM5122 with hardware volume, so the digital path can stay at unity all the time.
- Signal-adaptive dynamic EQ and a feedback loop that learns your taste beyond the current ±6 dB nudges.

## Build it

### Wiring (6 wires, no soldering beyond the module's own jumpers)

| PCM5102A module | Signal | UNO Q header | STM32 pin |
|---|---|---|---|
| VIN | 5 V | 5V | — |
| GND | ground | GND | — |
| BCK | bit clock | D13 | PB13 = SAI2_SCK_A |
| LCK | word clock | D19 / A5 | PC0 = SAI2_FS_A |
| DIN | data | D11 | PB15 = SAI2_SD_A |
| SCK | system clock | **GND** (enables the module's internal PLL) | — |

Module solder jumpers: FLT = L, DEMP = L, **XSMT = H** (no sound otherwise), FMT = L (I2S). Keep the wires under 10 cm. The STM32 is the I2S master, generating BCK and LRCK; the PCM5102A's on-chip PLL derives its master clock from BCK.

`[IMAGE: Fritzing / labelled photo of the six wires]`

### Software

Everything is in the repo: Zephyr firmware (`firmware/`, with a `BUILD.md` covering the four gotchas that cost me days), the C sender, `enhance.py`, the web UI, systemd units and configs, and the test tools including `purecrc.sh` so you can reproduce the bit-perfect check yourself.

```
git clone https://github.com/AZNitro/pure-path-audio-bridge
```

Then follow `firmware/BUILD.md` to build and flash, and `systemd/README.md` to install the Linux side. You need a Zephyr workspace, `adb`, and on the board: librespot, MPD, upmpdcli, Python 3 with NumPy/SciPy and the `ai-edge-litert` TFLite runtime.

### Credits

YAMNet — Google, Apache-2.0 (redistributed unmodified with NOTICE). librespot (MIT), MPD and upmpdcli (GPL-2.0), Zephyr RTOS (Apache-2.0), NumPy and SciPy (BSD). My own code is MIT.

---

*Built by AZ (Yu-Shen Chen), computer science student, for Hackster's "Invent the Future with Arduino UNO Q".*
