# Pure-Path Audio Bridge

A Hi-Fi network audio streamer on the **Arduino UNO Q**: Spotify Connect, UPnP/DLNA and local FLAC on the Linux side (Qualcomm QRB2210), a custom Zephyr firmware on the STM32U585 driving a PCM5102A DAC over I2S, and a CRC-32 at both ends of the link that proves the path is bit-exact.

Two modes:

- **Pure** — bytes pass from the decoder to the DAC's I2S pins untouched. Verified: `sent_crc32 == stm32_crc32` over 105,840,128 bytes.
- **Enhanced** — six EQ presets, a 5-band custom EQ, and an **Auto** setting where Google's YAMNet classifier picks the preset from what it hears. The model only chooses; the EQ curves are ours.

Built for Hackster's *Invent the Future with Arduino UNO Q* (Home Automation). Full write-up: `docs/HACKSTER_STORY.md`.

## How it works

```
Spotify ─▶ librespot ─┐
UPnP    ─▶ upmpdcli ─▶ MPD ─┼─▶ audio.fifo ─▶ enhance.py ─▶ bridge.fifo ─▶ bridge-tx
Local   ──────────────▶ MPD ─┘      (s16le 44.1 kHz)  (Pure/EQ/AI)          (frames, pacing)
                                                                               │
                                              LPUART1 · 3 Mbaud · RTS/CTS · DMA
                                                                               │
                      STM32U585 / Zephyr: DMA RX ─▶ 64 KB ring ─▶ SAI2 master ─▶ PCM5102A
```

Key numbers: PLL2 gives 256 × 44.1 kHz with −0.035 ppm error; 3 Mbaud carries 222 kB/s clean against 176.4 kB/s needed; the sender paces on the STM32's ring fill (target 50 %) so the SAI never stops; STATUS every 100 ms carries counters plus a running CRC-32 of every byte handed to the DAC.

## Repository layout

| path | contents |
|---|---|
| `firmware/` | Zephyr app for the STM32U585: `src/main.c`, board overlay (PLL2, SAI2, LPUART1 DMA), `prj.conf`, and `BUILD.md` with build/flash steps and the gotchas |
| `linux/bridge-tx/` | C sender: framing, CRC-16, rate pacing, P-control on ring fill, CRC-32 comparison |
| `linux/enhance/` | `enhance.py` — Pure passthrough / EQ presets / custom EQ / crossfade / YAMNet Auto; `models/` holds the YAMNet files with NOTICE and hashes |
| `linux/webui/` | `bridge_api.py` (stdlib HTTP API on :8080) and `index.html` (status/control page) |
| `systemd/` | our four units, plus `mpd.conf` and `upmpdcli.conf` for the stock services, and install notes |
| `tools/` | `purecrc.sh` / `longcrc.sh` (bit-exactness checks), click/slew capture, link bandwidth probe, VU calibration |
| `docs/` | Hackster story, BOM, wiring |

## Quick start

1. **Wire the DAC** — six wires, see `docs/WIRING.md`. SCK on the module goes to GND; XSMT jumper must be high.
2. **Build and flash the firmware** — `firmware/BUILD.md`. Board target is `arduino_uno_q/stm32u585xx`. Power-cycle after flashing; never `reset`.
3. **Install the Linux side** — `systemd/README.md`. Disable `arduino-router.service` (it owns `/dev/ttyHS1`), install librespot / MPD / upmpdcli, create the Python venv from `linux/enhance/requirements.txt`, enable the units.
4. Open `http://<board-ip>:8080`. Pick "UNO Q" in Spotify, or cast to "UNO Q" from any DLNA controller.

## Verify it yourself

```sh
# on the board
tools/purecrc.sh some_16bit_44k1.wav
# sent_bytes=5292032 sent_crc32=5c906e27  stm32_consumed=5292032 stm32_crc32=5c906e27  MATCH
```

Keep MPD's mixer and the Spotify client's volume at 100 % — any software attenuation is still correct audio but no longer byte-exact, and the check will say MISMATCH.

## Status

Verified working (Sept 2026): Spotify Connect from phone and desktop, FLAC via BubbleUPnP, Windows Cast to Device, NetEase Cloud Music DLNA, local files; 10-minute CRC run MATCH; six services survive a power cycle; Enhanced/Auto at ~30 % of one A53 core.

Not done / next: restoration model for lossy streams, Rust port of the enhancer, 24-bit at 4 Mbaud with DMA-only RX, USB Audio gadget (needs `f_uac2`), PCM5122 with hardware volume.

## Licence

MIT for everything here except the redistributed YAMNet files (Apache-2.0) — see `LICENSE` and `THIRD_PARTY.md`.
