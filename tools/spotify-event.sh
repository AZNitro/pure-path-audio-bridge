#!/bin/bash
# librespot --onevent hook -> /run/bridge/spotify.json, read by bridge_api.py.
#
# librespot 0.8 passes the event name in PLAYER_EVENT. Only track_changed
# carries metadata (NAME, ARTISTS, ALBUM, URI, DURATION_MS); playing/paused/
# stopped carry just TRACK_ID/POSITION_MS, so the title is carried forward from
# the last track_changed instead of being blanked on every pause.
exec python3 - <<'PY'
import json, os, sys

OUT = "/run/bridge/spotify.json"
LOG = "/run/bridge/spotify-events.log"

if not os.path.isdir("/run/bridge"):
    sys.exit(0)                     # bridge-tx down; nothing reads this

ev = os.environ.get("PLAYER_EVENT", "")

# Keep the last 50 events with the variables librespot actually supplied, so
# the names above can be checked against a real session.
try:
    keep = ["NAME", "ARTISTS", "ALBUM", "URI", "TRACK_ID", "ITEM_TYPE", "POSITION_MS"]
    line = " ".join([ev] + ["%s=%s" % (k, os.environ[k].replace("\n", "|"))
                            for k in keep if os.environ.get(k)])
    old = open(LOG).read().splitlines()[-49:] if os.path.exists(LOG) else []
    open(LOG, "w").write("\n".join(old + [line]) + "\n")
except OSError:
    pass

STATE = {"track_changed": "playing", "playing": "playing",
         "paused": "paused", "stopped": "stopped", "loading": "loading"}
if ev not in STATE:
    sys.exit(0)                     # volume_changed, preloading, seeked, ...

try:
    prev = json.load(open(OUT))
except (OSError, ValueError):
    prev = {}

def artists(raw):
    # ARTISTS is newline-separated when a track has several.
    return ", ".join(a.strip() for a in (raw or "").splitlines() if a.strip()) or None

if ev == "track_changed":           # new track: never inherit the old title
    title, artist = os.environ.get("NAME") or None, artists(os.environ.get("ARTISTS"))
else:                               # state-only event: keep what we know
    title, artist = prev.get("title"), prev.get("artist")

tmp = OUT + ".tmp"
with open(tmp, "w") as f:
    json.dump({"title": title, "artist": artist, "state": STATE[ev]}, f)
os.replace(tmp, OUT)
PY
