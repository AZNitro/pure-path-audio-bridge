#!/bin/bash
# Unattended long-run Pure CRC: 105,840,078 B of long.wav (~10 min) straight
# through bridge-tx, no enhance in the path. Writes a one-line verdict to
# $HOME_DIR/longcrc.result and the full transcript to longcrc.log.
HOME_DIR="${BRIDGE_HOME:-/home/arduino}"
LOG=$HOME_DIR/longcrc.log
RES=$HOME_DIR/longcrc.result
{ echo "RUNNING  started $(date -Is)"; } > "$RES"
: > "$LOG"

# no other source may touch audio.fifo while the services are down
python3 - <<'PY' >> "$LOG" 2>&1
import socket
s=socket.create_connection(("localhost",6600),5); f=s.makefile("rw",newline="\n"); f.readline()
f.write("stop\n"); f.flush(); print("mpd stopped:", f.readline().strip())
PY

sudo -n systemctl stop enhance bridge-tx librespot >> "$LOG" 2>&1
sleep 2
$HOME_DIR/bridge-tx $HOME_DIR/long.wav >> "$LOG" 2>&1
RC=$?
sudo -n systemctl start bridge-tx enhance librespot >> "$LOG" 2>&1
sleep 4

LINE=$(grep -h 'sent_bytes' "$LOG" | tail -1)
UNITS=$(for u in bridge-tx bridge-api enhance mpd librespot upmpdcli; do printf "%s=%s " "$u" "$(systemctl is-active $u)"; done)
{
  echo "$LINE"
  echo "exit=$RC  finished=$(date -Is)"
  echo "units: $UNITS"
  echo "stm32: $(cat /run/bridge/status.json)"
} > "$RES"
