#!/usr/bin/env python3
"""VU sensitivity calibration.

Only applies to the FIXED-SCALE firmware revision, which took a sensitivity
value over POST /vu/<dB>. The shipped firmware does its own auto-gain and
ignores that endpoint, so this is kept for reference and for anyone running
the earlier build.

Play music at your normal listening volume, run this, leave it 20 s.
It reads the live level and tells you the sensitivity to set.
"""
import json, os, sys, time, urllib.request

API = os.environ.get("BRIDGE_API", "http://localhost:8080")
SPAN = 30.0
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
vals, t0 = [], time.time()
while time.time() - t0 < dur:
    try:
        d = json.load(urllib.request.urlopen(API + "/level", timeout=1))
        if d["l"] > -99: vals.append(max(d["l"], d["r"]))
    except Exception:
        pass
    time.sleep(0.1)
if len(vals) < 10:
    sys.exit("no audio seen -- start playback first, then run this again")
vals.sort()
p50 = vals[len(vals)//2]
p90 = vals[int(len(vals)*0.9)]
gain = round(-4.6 - p90)
gain = max(-6, min(40, gain))
print("  samples %d over %.0f s" % (len(vals), dur))
print("  your level: median %.1f dBFS, loud passages (p90) %.1f dBFS" % (p50, p90))
print()
print("  recommended sensitivity: +%d dB" % gain if gain >= 0 else "  recommended: %d dB" % gain)
print("  at that setting the loud parts reach ~11 of 13 pixels and the median sits at ~%d."
      % max(0, min(13, int((p50 + gain + SPAN) / SPAN * 13))))
print()
print("  apply it with:  curl -s -X POST %s/vu/%d" % (API, gain))
