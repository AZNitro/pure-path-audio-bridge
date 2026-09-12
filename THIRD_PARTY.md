# Third-party components

Everything in this repository is MIT-licensed (see `LICENSE`) **except** the files
listed under "Redistributed" below, which carry their own licences and are included
unmodified. Everything under "Runtime dependencies" is used but not redistributed —
install it from your distribution or from upstream.

## Redistributed in this repository

### YAMNet — `linux/enhance/models/`

| file | licence | source |
|---|---|---|
| `yamnet.tflite` | Apache-2.0 | Google, via the MediaPipe model bucket |
| `yamnet_class_map.csv` | Apache-2.0 | `tensorflow/models`, `research/audioset/yamnet` |

Both files are unmodified upstream artifacts. `models/NOTICE` records the exact
source URLs, SHA-256 hashes and retrieval date; `models/fetch-yamnet.sh` re-fetches
and verifies them. Apache-2.0 permits redistribution with attribution, which is what
this section and `NOTICE` provide.

YAMNet is used **only to choose between EQ presets authored in this repository**. The
model never processes the audio that reaches the DAC — it reads a downsampled mono
copy and returns a class label. Full licence text: https://www.apache.org/licenses/LICENSE-2.0

## Runtime dependencies (not redistributed)

| project | licence | role |
|---|---|---|
| [Zephyr RTOS](https://github.com/zephyrproject-rtos/zephyr) | Apache-2.0 | firmware RTOS, drivers, build system |
| [librespot](https://github.com/librespot-org/librespot) | MIT | Spotify Connect receiver, pipe backend |
| [MPD](https://github.com/MusicPlayerDaemon/MPD) | GPL-2.0-or-later | local playback, FIFO output |
| [upmpdcli](https://github.com/medoc92/upmpdcli) | GPL-2.0-or-later | UPnP/DLNA renderer front-end to MPD |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | sample buffers and arithmetic |
| [SciPy](https://github.com/scipy/scipy) | BSD-3-Clause | `sosfilt`, `sosfreqz`, `resample_poly` |
| [ai-edge-litert](https://github.com/google-ai-edge/LiteRT) | Apache-2.0 | TFLite interpreter for YAMNet |

The systemd units for MPD and upmpdcli are the stock ones from their Debian packages
and are deliberately **not** copied into this repository — only our own configuration
for them (`systemd/mpd.conf`, `systemd/upmpdcli.conf`) is included.

## Hardware

Arduino UNO Q (Qualcomm QRB2210 + STMicroelectronics STM32U585AIIxQ) and a PCM5102A
I2S DAC module. No vendor code from either is included here.
