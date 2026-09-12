#!/usr/bin/env python3
"""
bridge_api.py — status + control for the UNO Q streamer. Stdlib only.

  GET  /                 web page (index.html next to this file)
  GET  /status           JSON: bridge stats, MPD state, now playing, services, source, mode
  POST /mpd/<cmd>        play | pause | stop | next | previous
  POST /volume/<0-100>   MPD software volume
  POST /mode/<pure|enhanced>   read by enhance.py
  POST /preset/<auto|flat|speech|warm|bright|bass|acoustic|custom>
                         while Auto is on this sets an OVERRIDE; tapping the
                         same preset again pins it as the sticky preset
  POST /custom/<g0>/<g1>/<g2>/<g3>/<g4>   band gains dB, 0.5 steps, -12..+12
  POST /custom/pregain/<auto|-6..0>
  POST /custom/reset
  POST /feedback/<warmer|brighter|bass_up|bass_down>   +-1 dB nudge, per context
  POST /feedback/reset
  GET  /level            live L/R RMS dBFS + the meter range (poll me, I am cheap)
  POST /vu/<-6..40>      meter sensitivity: dB of gain added before display
  POST /spotify/restart  restart librespot (drops the current Connect session)

Run as user arduino: python3 bridge_api.py [port=8080]
"""
import json, os, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = "/run/bridge/status.json"
SPOTIFY_FILE = "/run/bridge/spotify.json"
MODE_FILE = "/run/bridge/mode"
PRESET_FILE = "/run/bridge/preset"
ENHANCE_FILE = "/run/bridge/enhance.json"
OVERRIDE_FILE = "/run/bridge/override"
LEVEL_FILE = "/run/bridge/level.json"
VU_FILE = "/var/lib/bridge/vu.json"
VU_GAIN_MIN, VU_GAIN_MAX, VU_GAIN_DEF = -6.0, 40.0, 12.0
VU_SPAN_DB = 30.0                    # window width; gain slides it up the scale
STATE_DIR = "/var/lib/bridge"
USER_EQ = STATE_DIR + "/user_eq.json"
PRESETS = ("auto", "flat", "speech", "warm", "bright", "bass", "acoustic", "custom")
BAND_MAX, OFFSET_MAX, PREGAIN_MIN = 12.0, 6.0, -6.0
NUDGE = {"warmer":    (0.0, +1.0, 0.0, 0.0, -1.0),
         "brighter":  (0.0, -1.0, 0.0, 0.0, +1.0),
         "bass_up":   (+1.0, 0.0, 0.0, 0.0, 0.0),
         "bass_down": (-1.0, 0.0, 0.0, 0.0, 0.0)}
MPD_ADDR = ("127.0.0.1", 6600)
SERVICES = ["bridge-tx", "enhance", "librespot", "mpd", "upmpdcli"]


def mpd(*cmds):
    """Send commands to MPD, return dict of key: value for each response."""
    out = {}
    try:
        with socket.create_connection(MPD_ADDR, timeout=1.0) as s:
            s.recv(256)  # OK MPD x.y.z
            for c in cmds:
                s.sendall((c + "\n").encode())
                buf = b""
                while not (buf.endswith(b"OK\n") or b"\nACK" in buf or buf.startswith(b"ACK")):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                for line in buf.decode(errors="replace").splitlines():
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        out[k] = v
    except OSError:
        pass
    return out


def service_active(name):
    r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
    return r.stdout.strip() == "active"


# Five systemctl spawns cost ~124 ms, which was most of every /status and made
# the page feel slow to react to a tap. Service state changes on the order of
# minutes, so sample it off the request path and serve the last answer.
_svc = {s: True for s in SERVICES}


def _svc_poll():
    global _svc
    while True:
        _svc = {s: service_active(s) for s in SERVICES}
        time.sleep(2.0)


def bridge_status():
    try:
        with open(STATUS_FILE) as f:
            d = json.load(f)
        age = time.time() - os.path.getmtime(STATUS_FILE)
        d["age_s"] = round(age, 1)
        if age > 3:                      # bridge-tx blocked on an empty FIFO = idle
            d["streaming"] = 0
            d["rate_bps"] = 0
        return d
    except (OSError, ValueError):
        return {"streaming": 0, "link": 0, "age_s": None}


def spotify():
    """Now playing from librespot's --onevent hook. {} when nothing is known."""
    try:
        with open(SPOTIFY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def mode():
    try:
        return open(MODE_FILE).read().strip() or "pure"
    except OSError:
        return "pure"


def preset():
    try:
        return open(PRESET_FILE).read().strip() or "auto"
    except OSError:
        return "auto"


def clampf(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def q(v):                                # 0.5 dB steps
    return round(float(v) * 2.0) / 2.0


def load_eq():
    """Defaults on anything unreadable -- the page must still come up."""
    try:
        with open(USER_EQ) as f:
            d = json.load(f)
        c = d.get("custom") or {}
        b = [clampf(q(g), -BAND_MAX, BAND_MAX) for g in (c.get("bands") or [])][:5]
        b += [0.0] * (5 - len(b))
        pg = c.get("pregain")
        pg = None if pg is None else clampf(float(pg), PREGAIN_MIN, 0.0)
        o = {}
        for k, v in (d.get("offsets") or {}).items():
            x = [clampf(float(i), -OFFSET_MAX, OFFSET_MAX) for i in (v or [])][:5]
            o[str(k)] = x + [0.0] * (5 - len(x))
        return {"custom": {"bands": b, "pregain": pg}, "offsets": o}
    except (OSError, ValueError, TypeError):
        return {"custom": {"bands": [0.0] * 5, "pregain": None}, "offsets": {}}


def save_eq(d):
    """Atomic: enhance.py reloads on mtime and must never see a half file."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = USER_EQ + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, USER_EQ)


def vu_gain():
    """dB added to the measured level before it is mapped to pixels. Volume on
    this board is digital and upstream of us, so at a comfortable listening
    level the PCM itself is quiet -- this is what lets the meter still move."""
    try:
        with open(VU_FILE) as f:
            return clampf(float(json.load(f)["gain_db"]), VU_GAIN_MIN, VU_GAIN_MAX)
    except (OSError, ValueError, TypeError, KeyError):
        return VU_GAIN_DEF


def level():
    try:
        with open(LEVEL_FILE) as f:
            d = json.load(f)
        if time.time() - d.get("ts", 0) > 1.0:       # enhance.py stopped writing
            d["l"] = d["r"] = -99.0
        return d
    except (OSError, ValueError):
        return {"l": -99.0, "r": -99.0, "ts": 0}


def override():
    try:
        return open(OVERRIDE_FILE).read().strip() or None
    except OSError:
        return None


def enhance():
    try:
        with open(ENHANCE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def full_status():
    b = bridge_status()
    st = mpd("status", "currentsong")
    mpd_state = st.get("state", "unknown")
    if mpd_state == "play":
        source = "upnp/mpd"
    elif b.get("streaming"):
        source = "spotify"
    else:
        source = "idle"
    return {
        "bridge": b,
        "mpd": {
            "state": mpd_state,
            "volume": st.get("volume"),
            "title": st.get("Title") or st.get("Name") or (st.get("file", "").rsplit("/", 1)[-1] if st.get("file") else None),
            "artist": st.get("Artist"),
            "album": st.get("Album"),
            "elapsed": st.get("elapsed"),
            "duration": st.get("duration"),
            "audio": st.get("audio"),
        },
        "spotify": spotify(),
        "source": source,
        "mode": mode(),
        "preset": preset(),
        "enhance": enhance(),
        "user_eq": load_eq(),
        "vu_gain": vu_gain(),
        "vu_span": VU_SPAN_DB,
        "services": dict(_svc),
        "time": time.time(),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"error": "index.html missing"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            self._json(full_status())
        elif self.path == "/level":
            d = level(); d["gain_db"] = vu_gain(); d["span_db"] = VU_SPAN_DB
            self._json(d)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.strip("/").split("/")
        if len(p) == 2 and p[0] == "mpd" and p[1] in ("play", "pause", "stop", "next", "previous"):
            mpd(p[1] if p[1] != "pause" else "pause 1")
            return self._json({"ok": True})
        if len(p) == 2 and p[0] == "volume" and p[1].isdigit() and 0 <= int(p[1]) <= 100:
            mpd(f"setvol {int(p[1])}")
            return self._json({"ok": True})
        if len(p) == 2 and p[0] == "mode" and p[1] in ("pure", "enhanced"):
            with open(MODE_FILE, "w") as f:
                f.write(p[1])
            return self._json({"ok": True, "mode": p[1]})
        if len(p) == 2 and p[0] == "preset" and p[1] in PRESETS:
            name = p[1]
            if name == "auto":                       # back to Auto, drop any override
                with open(PRESET_FILE, "w") as f:
                    f.write("auto")
                try:
                    os.unlink(OVERRIDE_FILE)
                except OSError:
                    pass
                return self._json({"ok": True, "preset": "auto", "override": None})
            if preset() == "auto" and override() != name:
                # Auto is on: hold this preset over it. enhance.py releases the
                # override when the detected class changes.
                with open(OVERRIDE_FILE, "w") as f:
                    f.write(name)
                return self._json({"ok": True, "preset": "auto", "override": name})
            # Not in Auto, or the same preset tapped twice: pin it.
            with open(PRESET_FILE, "w") as f:
                f.write(name)
            try:
                os.unlink(OVERRIDE_FILE)
            except OSError:
                pass
            return self._json({"ok": True, "preset": name, "override": None})
        if len(p) == 6 and p[0] == "custom":
            try:
                g = [clampf(q(v), -BAND_MAX, BAND_MAX) for v in p[1:]]
            except ValueError:
                return self._json({"error": "bad gains"}, 400)
            d = load_eq(); d["custom"]["bands"] = g; save_eq(d)
            return self._json({"ok": True, "bands": g})
        if p == ["custom", "reset"]:
            d = load_eq(); d["custom"] = {"bands": [0.0] * 5, "pregain": None}; save_eq(d)
            return self._json({"ok": True, "bands": [0.0] * 5})
        if len(p) == 3 and p[:2] == ["custom", "pregain"]:
            try:
                v = None if p[2] == "auto" else clampf(float(p[2]), PREGAIN_MIN, 0.0)
            except ValueError:
                return self._json({"error": "bad pregain"}, 400)
            d = load_eq(); d["custom"]["pregain"] = v; save_eq(d)
            return self._json({"ok": True, "pregain": v})
        if len(p) == 2 and p[0] == "feedback":
            ctx = (enhance() or {}).get("context")
            if not ctx:
                return self._json({"error": "no active context (idle or pure)"}, 409)
            d = load_eq()
            cur = d["offsets"].get(ctx) or [0.0] * 5
            if p[1] == "reset":
                d["offsets"].pop(ctx, None)
                save_eq(d)
                return self._json({"ok": True, "context": ctx, "offset": [0.0] * 5})
            if p[1] not in NUDGE:
                return self._json({"error": "bad nudge"}, 400)
            new = [clampf(a + b, -OFFSET_MAX, OFFSET_MAX) for a, b in zip(cur, NUDGE[p[1]])]
            d["offsets"][ctx] = new
            save_eq(d)
            return self._json({"ok": True, "context": ctx, "offset": new})
        if len(p) == 2 and p[0] == "vu":
            try:
                v = clampf(float(p[1]), VU_GAIN_MIN, VU_GAIN_MAX)
            except ValueError:
                return self._json({"error": "bad gain"}, 400)
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = VU_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"gain_db": v}, f)
            os.replace(tmp, VU_FILE)
            return self._json({"ok": True, "gain_db": v})
        if p == ["spotify", "restart"]:
            subprocess.run(["sudo", "systemctl", "restart", "librespot"])
            return self._json({"ok": True})
        self._json({"error": "bad request"}, 400)


if __name__ == "__main__":
    threading.Thread(target=_svc_poll, daemon=True).start()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
