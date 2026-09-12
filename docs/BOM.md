# Bill of materials

## Hardware

| # | item | qty | notes | price |
|---|---|---|---|---|
| 1 | Arduino UNO Q | 1 | Qualcomm QRB2210 (4× Cortex-A53, Debian) + STM32U585AIIxQ (Cortex-M33). The 13×8 LED matrix is used for the VU meter. | `[fill in]` |
| 2 | GY-PCM5102 / PCM5102A I2S DAC module | 1 | TI PCM5102A, 3.5 mm line out. Solder jumpers: FLT=L, DEMP=L, XSMT=H, FMT=L. | ~US$5 |
| 3 | Dupont jumper wires (M–F or M–M) | 6 | keep under 10 cm | — |
| 4 | Breadboard (optional) | 1 | only to hold the module | — |
| 5 | USB-C cable + 5 V supply | 1 | powers the UNO Q; the DAC module takes 5 V from the header | — |
| 6 | Headphones or amplifier with line input | 1 | volume is set here, not in software | — |

No soldering beyond the module's own jumper pads. No external clock, no level shifters, no debug probe.

## Software on the board (QRB2210, Debian 13)

| component | version used | licence | role |
|---|---|---|---|
| librespot | 0.8.0 | MIT | Spotify Connect receiver, `--backend pipe` into `audio.fifo` |
| MPD | Debian package | GPL-2.0-or-later | local playback + UPnP target; `fifo` output at 44100:16:2, soxr resampler |
| upmpdcli | Debian package | GPL-2.0-or-later | UPnP/DLNA renderer front-end for MPD |
| Python 3 | 3.13 | PSF | `enhance.py`, `bridge_api.py` |
| NumPy | 2.2.4 | BSD-3 | sample buffers |
| SciPy | 1.15.3 | BSD-3 | `sosfilt`, `sosfreqz`, `resample_poly` |
| ai-edge-litert | 2.2.0 | Apache-2.0 | TFLite interpreter for YAMNet |
| YAMNet model + class map | upstream | Apache-2.0 | audio classifier (Google); redistributed unmodified, see `linux/enhance/models/NOTICE` |
| systemd | Debian | LGPL | six units |
| adb | platform-tools | Apache-2.0 | host ↔ board access and port forwarding |

## Firmware (STM32U585)

| component | version used | licence | role |
|---|---|---|---|
| Zephyr RTOS | west workspace, board `arduino_uno_q/stm32u585xx` | Apache-2.0 | SAI/I2S, UART async DMA, display, ring buffer |
| Zephyr SDK (arm-zephyr-eabi) | — | Apache-2.0 | toolchain + gdb |
| OpenOCD (`linuxgpiod`, via `arduino-debug`) | on-board | GPL-2.0 | bit-banged SWD from the QRB2210's GPIO; no external probe |

Firmware footprint: ~50 kB flash, ~119 kB RAM.

## Tools used during development

| tool | purpose |
|---|---|
| tuner app (phone) | confirm 440 Hz test tone → clock accuracy |
| BubbleUPnP (Android) | UPnP control point, FLAC casting |
| Spotify (phone + Windows) | Spotify Connect source |
| Windows "Cast to Device" | DLNA source test |
| `tools/bw_test.py` | link bandwidth measurement (2 M / 3 M / 4 M baud) |
| `tools/purecrc.sh`, `tools/longcrc.sh` | bit-exactness proof |
| `tools/clicktest.sh` + NumPy | switch-transient capture and slew analysis |
| Fritzing / photos | wiring diagram |

## Not included (by choice)

- Arduino App Lab — cannot enable SAI2; replaced by vanilla Zephyr.
- Qualcomm SNPE / Edge Impulse — evaluated, not needed (no NPU, 0.5 Hz inference, no training required).
- shairport-sync (AirPlay) — deferred; UPnP already covers lossless from a phone.
