#!/bin/bash
# $1 = enhance script to exercise, $2 = capture path
set -e
HOME_DIR="${BRIDGE_HOME:-/home/arduino}"
rm -f "$2"
echo pure > /run/bridge/mode; echo bass > /run/bridge/preset
cat $HOME_DIR/bridge.fifo > "$2" &
CAT=$!
$HOME_DIR/venv/bin/python "$1" > /tmp/enh.log 2>&1 &
EN=$!
sleep 3
python3 $HOME_DIR/feed440.py &
FEED=$!
sleep 5; echo enhanced > /run/bridge/mode
sleep 5; echo warm     > /run/bridge/preset
sleep 5; echo pure     > /run/bridge/mode
wait $FEED
sleep 1
kill $EN 2>/dev/null; sleep 0.3; kill $CAT 2>/dev/null
wait 2>/dev/null || true
