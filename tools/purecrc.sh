#!/bin/bash
# Pure-path CRC check.
#
# Takes the services out of the way, sends a 16-bit stereo 44.1 kHz WAV straight
# through bridge-tx, and prints the comparison. The firmware CRC-32s every byte it
# hands to the SAI; bridge-tx CRC-32s every byte it sent. Equal CRCs with equal byte
# counts prove the path is bit-perfect.
#
#   ./purecrc.sh song.wav
#   sent_bytes=5292032 sent_crc32=5c906e27  stm32_consumed=5292032 stm32_crc32=5c906e27  MATCH
#
# A MISMATCH means something is altering samples. The usual cause is a volume
# control below unity (MPD's software mixer, or a Spotify client's slider driving
# librespot's softvol) rather than a fault in the link.
set -euo pipefail
HOME_DIR="${BRIDGE_HOME:-/home/arduino}"
WAV="${1:-$HOME_DIR/song.wav}"

[ -r "$WAV" ] || { echo "no such file: $WAV" >&2; exit 1; }

# One source at a time: enhance owns bridge.fifo, bridge-tx owns the UART.
sudo systemctl stop enhance bridge-tx
trap 'sudo systemctl start bridge-tx enhance' EXIT
sleep 1

"$HOME_DIR/bridge-tx" "$WAV"
