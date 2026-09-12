#!/usr/bin/env python3
"""
enhance.py — Enhanced mode for the UNO Q audio bridge.

    sources -> /home/arduino/audio.fifo -> enhance.py -> /home/arduino/bridge.fifo -> bridge-tx

Pure mode:      bytes pass through untouched (bit-perfect).
Enhanced mode:  one of the EQ presets below is applied. Preset "auto" lets a
                YAMNet classifier (Google, Apache-2.0) pick the preset from what
                it hears, once per second. The EQ curves are ours; the model
                only chooses between them.

Control files (written by bridge_api.py):
    /run/bridge/mode     pure | enhanced
    /run/bridge/preset   auto | flat | speech | warm | bright | bass | acoustic
Status out:
    /run/bridge/enhance.json   {"mode","preset","active","label","score","model"}

Audio format is fixed: s16le, stereo, 44100 Hz (what the STM32 expects).
"""
import json, math, os, select, sys, threading, time
import numpy as np

try:
    from scipy.signal import sosfilt, sosfreqz, resample_poly
except ImportError:
    sys.exit("need python3-scipy (sudo apt install python3-scipy python3-numpy)")

IN_FIFO  = "/home/arduino/audio.fifo"
OUT_FIFO = "/home/arduino/bridge.fifo"
RUN      = "/run/bridge"
STATE    = "/var/lib/bridge"         # persists across reboots, unlike RUN
USER_EQ  = STATE + "/user_eq.json"   # custom band gains + per-context tweaks
OVERRIDE = RUN + "/override"         # transient: manual preset held over Auto
LEVEL    = RUN + "/level.json"       # live L/R RMS for the web meter
LEVEL_MS = 50                        # how often that is refreshed
FS       = 44100
FRAMES   = 512                       # per block (matches bridge-tx frame)
BLOCK    = FRAMES * 4                # bytes, s16 stereo
IDLE_MS  = 200                       # input poll timeout: bounds tail flush + status
CLASSIFY_S = 2.0                     # seconds between YAMNet inferences
FADE_FRAMES = 4096                   # ~93 ms crossfade on any chain change

# Custom EQ / feedback nudges: 5 peaking bands, shared by both features.
BANDS      = (60.0, 250.0, 1000.0, 4000.0, 12000.0)
BAND_Q     = 1.0
BAND_MAX   = 12.0                    # user band gain limit, dB
OFFSET_MAX = 6.0                     # feedback nudge limit per band, dB
PREGAIN_MIN = -6.0                   # master pre-gain floor, dB
NUDGE = {                            # button -> per-band delta, dB
    "warmer":    (0.0, +1.0, 0.0, 0.0, -1.0),
    "brighter":  (0.0, -1.0, 0.0, 0.0, +1.0),
    "bass_up":   (+1.0, 0.0, 0.0, 0.0, 0.0),
    "bass_down": (-1.0, 0.0, 0.0, 0.0, 0.0),
}

# ---------------------------------------------------------------------------
# RBJ cookbook biquads -> second-order sections
# ---------------------------------------------------------------------------
def _norm(b, a):
    return np.array([[b[0]/a[0], b[1]/a[0], b[2]/a[0], 1.0, a[1]/a[0], a[2]/a[0]]])

def peaking(f0, gain_db, q):
    A = 10 ** (gain_db / 40); w = 2*np.pi*f0/FS; al = np.sin(w)/(2*q); c = np.cos(w)
    return _norm([1+al*A, -2*c, 1-al*A], [1+al/A, -2*c, 1-al/A])

def low_shelf(f0, gain_db, s=0.9):
    A = 10 ** (gain_db / 40); w = 2*np.pi*f0/FS; c = np.cos(w)
    al = np.sin(w)/2*np.sqrt((A+1/A)*(1/s-1)+2); sa = 2*np.sqrt(A)*al
    return _norm([A*((A+1)-(A-1)*c+sa), 2*A*((A-1)-(A+1)*c), A*((A+1)-(A-1)*c-sa)],
                 [(A+1)+(A-1)*c+sa, -2*((A-1)+(A+1)*c), (A+1)+(A-1)*c-sa])

def high_shelf(f0, gain_db, s=0.9):
    A = 10 ** (gain_db / 40); w = 2*np.pi*f0/FS; c = np.cos(w)
    al = np.sin(w)/2*np.sqrt((A+1/A)*(1/s-1)+2); sa = 2*np.sqrt(A)*al
    return _norm([A*((A+1)+(A-1)*c+sa), -2*A*((A-1)+(A+1)*c), A*((A+1)+(A-1)*c-sa)],
                 [(A+1)-(A-1)*c+sa, 2*((A-1)-(A+1)*c), (A+1)-(A-1)*c-sa])

def high_pass(f0, q=0.707):
    w = 2*np.pi*f0/FS; al = np.sin(w)/(2*q); c = np.cos(w)
    return _norm([(1+c)/2, -(1+c), (1+c)/2], [1+al, -2*c, 1-al])

# ---------------------------------------------------------------------------
# Presets: (list of sections, headroom dB applied before EQ to avoid clipping)
# ---------------------------------------------------------------------------
PRESETS = {
    # Pure is a chain like any other: unity gain, no sections. That way
    # Pure<->Enhanced crossfades through exactly the same code path as
    # preset<->preset, and there is no special case to get wrong.
    "pure":     ([], 0.0),
    "flat":     ([], 0.0),
    "speech":   ([high_pass(80), peaking(250, -3.0, 1.0), peaking(3000, 3.0, 1.0),
                  high_shelf(9000, -2.0)], -3.5),
    "warm":     ([low_shelf(120, 2.5), high_shelf(8000, -1.5)], -3.0),
    "bright":   ([peaking(3000, 1.5, 1.2), high_shelf(6000, 2.5)], -3.0),
    "bass":     ([low_shelf(100, 4.0), peaking(60, 1.5, 0.8)], -5.0),
    "acoustic": ([peaking(500, -1.0, 1.0), high_shelf(10000, 1.0)], -1.5),
}

# YAMNet label -> preset. Anything not listed -> "flat".
LABEL_MAP = [
    (("Speech", "Narration, monologue", "Conversation", "Male speech, man speaking",
      "Female speech, woman speaking", "Child speech, kid speaking"), "speech"),
    (("Classical music", "Orchestra", "Piano", "Acoustic guitar", "Violin, fiddle",
      "Choir", "Opera", "Chamber music", "Jazz"), "acoustic"),
    (("Hip hop music", "Electronic music", "Techno", "House music", "Dubstep",
      "Drum and bass", "Trance music", "Electronic dance music", "Rapping"), "bass"),
    (("Rock music", "Heavy metal", "Punk rock", "Grunge", "Progressive rock",
      "Electric guitar", "Rock and roll"), "bright"),
    (("Pop music", "Singing", "Vocal music", "Soul music", "Rhythm and blues",
      "Reggae", "Folk music", "Country"), "warm"),
]

# ---------------------------------------------------------------------------
# Classifier (optional). Falls back to "flat" if no runtime/model.
# ---------------------------------------------------------------------------
class Classifier:
    MODEL = "/home/arduino/models/yamnet.tflite"
    LABELS = "/home/arduino/models/yamnet_class_map.csv"

    def __init__(self):
        self.ok = False; self.label = None; self.score = 0.0; self.preset = "flat"
        self.ts = 0.0                               # when that result was produced
        self.buf = np.zeros(FS, dtype=np.float32)   # last 1 s, mono @44.1k
        self.lock = threading.Lock()
        try:
            try:
                from ai_edge_litert.interpreter import Interpreter
            except ImportError:
                from tflite_runtime.interpreter import Interpreter
            self.it = Interpreter(model_path=self.MODEL, num_threads=2)
            self.it.allocate_tensors()
            self.inp = self.it.get_input_details()[0]
            self.out = self.it.get_output_details()[0]
            import csv
            with open(self.LABELS) as f:
                self.names = [r[2] for r in list(csv.reader(f))[1:]]
            self.ok = True
            self.model = "YAMNet (TFLite)"
        except Exception as e:                      # noqa
            self.model = f"unavailable: {e.__class__.__name__}"
        threading.Thread(target=self._loop, daemon=True).start()

    def feed(self, mono):
        n = len(mono)
        with self.lock:
            self.buf = np.roll(self.buf, -n); self.buf[-n:] = mono

    def _loop(self):
        while True:
            # One inference per CLASSIFY_S. The window is still the last 1 s of
            # audio, so Auto reacts to a change within ~2 s either way; halving
            # the rate just halves the CPU it costs.
            time.sleep(CLASSIFY_S)
            if not self.ok:
                continue
            with self.lock:
                x = self.buf.copy()
            x16 = resample_poly(x, 160, 441).astype(np.float32)   # 44.1k -> 16k
            x16 = x16[-15600:] if len(x16) >= 15600 else np.pad(x16, (15600 - len(x16), 0))
            try:
                self.it.resize_tensor_input(self.inp["index"], [len(x16)])
                self.it.allocate_tensors()
                self.it.set_tensor(self.inp["index"], x16)
                self.it.invoke()
                scores = self.it.get_tensor(self.out["index"]).mean(axis=0)
            except Exception:                        # noqa
                continue
            top = int(np.argmax(scores)); name = self.names[top]
            preset = "flat"
            for names, p in LABEL_MAP:
                if any(name.startswith(n) for n in names):
                    preset = p; break
            self.label, self.score, self.preset = name, float(scores[top]), preset
            self.ts = time.time()

# ---------------------------------------------------------------------------
def log(msg):
    print("enhance: " + msg, file=sys.stderr, flush=True)

def clampf(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)

def default_eq():
    return {"custom": {"bands": [0.0] * 5, "pregain": None}, "offsets": {}}

def load_user_eq():
    """Never raise and never crash the audio path: bad file -> defaults + a log."""
    try:
        with open(USER_EQ) as f:
            d = json.load(f)
        c = d.get("custom") or {}
        bands = [clampf(float(g), -BAND_MAX, BAND_MAX) for g in (c.get("bands") or [])][:5]
        bands += [0.0] * (5 - len(bands))
        pg = c.get("pregain")
        pg = None if pg is None else clampf(float(pg), PREGAIN_MIN, 0.0)
        offs = {}
        for k, v in (d.get("offsets") or {}).items():
            o = [clampf(float(x), -OFFSET_MAX, OFFSET_MAX) for x in (v or [])][:5]
            offs[str(k)] = o + [0.0] * (5 - len(o))
        return {"custom": {"bands": bands, "pregain": pg}, "offsets": offs}
    except FileNotFoundError:
        return default_eq()
    except Exception as e:                                  # noqa: corrupt/unreadable
        log("%s unreadable (%s) -- using defaults" % (USER_EQ, e.__class__.__name__))
        return default_eq()

def auto_pregain(gains):
    """Headroom so band boosts cannot clip: half the largest boost, floored."""
    return clampf(-max(0.0, max(gains)) / 2.0, PREGAIN_MIN, 0.0)

def band_sections(gains):
    return [peaking(f, g, BAND_Q) for f, g in zip(BANDS, gains) if abs(g) >= 0.05]

_solve_memo = {}

def solve_bands(target):
    """Section gains whose COMBINED response hits `target` at the band centres.

    The five centres are only ~2 octaves apart and a Q=1 peaking section is
    ~1.4 octaves wide, so neighbours leak into each other: setting each section
    to the requested gain overshoots the centres by up to 2.1 dB. Correcting the
    error converges in a few passes because the coupling is weak, and it keeps
    Q at the specified 1.0 instead of narrowing the bands to force separation."""
    memo = tuple(target)
    if memo in _solve_memo:                  # ~4 ms a solve, and the audio loop
        return _solve_memo[memo]             # rebuilds chains for other reasons too
    tgt = np.asarray(target, dtype=float)
    if not np.any(np.abs(tgt) >= 0.05):
        return [0.0] * 5
    w = np.asarray(BANDS) * 2 * np.pi / FS
    g = tgt.copy()
    for _ in range(12):
        secs = band_sections(g)
        if not secs:
            break
        _, h = sosfreqz(np.vstack(secs), worN=w)
        err = tgt - 20 * np.log10(np.abs(h) + 1e-12)
        if np.max(np.abs(err)) < 0.02:
            break
        g = np.clip(g + err, -24.0, 24.0)
    out = [float(v) for v in g]
    if len(_solve_memo) < 256:
        _solve_memo[memo] = out
    return out

def build_sections(name, cfg, ctx):
    """(sections, headroom_db) for a chain, including the context's tweak."""
    if name == "pure":
        return [], 0.0                                      # Pure ignores all of this
    if name == "custom":
        g = cfg["custom"]["bands"]
        pg = cfg["custom"]["pregain"]
        secs = band_sections(solve_bands(g))     # g is what the user asked for
        head = auto_pregain(g) if pg is None else pg
    else:
        base, head = PRESETS.get(name, PRESETS["flat"])
        secs = list(base)
    off = cfg["offsets"].get(ctx) or [0.0] * 5
    if any(abs(o) >= 0.05 for o in off):
        secs = secs + band_sections(off)
        head += auto_pregain(off)                           # same anti-clip rule
    return secs, head

def chain_key(name, cfg, ctx):
    """Everything that changes the coefficients. A new key means a crossfade."""
    if name == "pure":
        return ("pure",)
    c = cfg["custom"]
    return (name,
            tuple(c["bands"]) if name == "custom" else (),
            c["pregain"] if name == "custom" else None,
            tuple(cfg["offsets"].get(ctx) or [0.0] * 5))

def write_level(ms_l, ms_r):
    """Live output level for the page meter. Measurement only -- this never
    touches the samples, so Pure stays byte-exact."""
    def db(ms):
        return -99.0 if ms < 1.0 else round(10.0 * math.log10(ms / (32768.0 * 32768.0)), 1)
    try:
        tmp = LEVEL + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"l": db(ms_l), "r": db(ms_r), "ts": round(time.time(), 3)}, f)
        os.replace(tmp, LEVEL)
    except OSError:
        pass

def eq_mtime():
    try:
        return os.stat(USER_EQ).st_mtime_ns
    except OSError:
        return 0

def read_override():
    try:
        return open(OVERRIDE).read().strip() or None
    except OSError:
        return None

def clear_override():
    try:
        os.unlink(OVERRIDE)
    except OSError:
        pass

def read_ctl(name, default):
    try:
        return open(os.path.join(RUN, name)).read().strip() or default
    except OSError:
        return default

def write_status(d):
    try:
        tmp = os.path.join(RUN, "enhance.json.tmp")
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, os.path.join(RUN, "enhance.json"))
    except OSError:
        pass

class EQ:
    """Stateful stereo SOS chain. Immutable once built: any coefficient change
    means a new chain and a crossfade, never a mutation under a running filter."""
    def __init__(self, key, secs, headroom):
        self.key = key
        self.name = key[0]
        self.gain = 10 ** (headroom / 20)
        self.sos = None
        if secs:
            self.sos = np.vstack(secs)
            # Start from rest. sosfilt_zi gives the steady state for a UNIT
            # step: right for filtfilt edges, wrong for a chain taking over
            # mid-stream, where it seeds a phantom DC that bleeds off as an LF
            # swell -- measured up to 6.3x full scale on a -6 dBFS tone, hard
            # clipping, on every preset change. Zero state plus the crossfade
            # measures 1.00x peak and 1.00x slew at every preset and every
            # switch phase.
            self.zi = np.zeros((self.sos.shape[0], 2, 2))   # (nsec, 2ch, 2)
    def process(self, x):                                   # x: (frames, 2) float32
        if self.sos is None:
            return x if self.gain == 1.0 else x * self.gain
        y, self.zi = sosfilt(self.sos, x * self.gain, axis=0, zi=self.zi)
        return y

def make_chain(name, cfg, ctx):
    """A fresh EQ, so the new chain starts with clean filter state."""
    secs, head = build_sections(name, cfg, ctx)
    return EQ(chain_key(name, cfg, ctx), secs, head)

def main():
    os.makedirs(RUN, exist_ok=True)
    fin = os.open(IN_FIFO, os.O_RDWR)                       # never EOF
    fout = None
    clf = Classifier()
    cfg = load_user_eq()         # custom bands + per-context tweaks
    cfg_mtime = eq_mtime()
    cur = make_chain("pure", cfg, None)   # chain in force
    prev = None                  # chain being faded out; None == silence
    fade_pos = 0                 # frames of crossfade still owed
    streaming = False
    last_status = 0.0
    ovr_name = None              # manual preset held over Auto
    ovr_class = None             # class detected when that override was set
    lv_l = lv_r = 0.0            # running sum of squares for the level meter
    lv_n = 0
    lv_next = 0.0

    def open_out():
        nonlocal fout
        while fout is None:
            try:
                fout = os.open(OUT_FIFO, os.O_WRONLY)      # blocks until bridge-tx reads
            except OSError:
                time.sleep(0.5)

    open_out()
    poller = select.poll()
    poller.register(fin, select.POLLIN)
    pending = b""

    while True:
        # Fill one block, but never wait forever for it. A source that stops
        # mid-block used to leave the remainder stuck here until the next track
        # started: the tail was never played and was then glued onto the front
        # of the following stream. Blocking here also froze the status write
        # below, so enhance.json went stale whenever nothing was playing.
        while len(pending) < BLOCK:
            if not poller.poll(IDLE_MS):
                break                                       # sources are quiet
            chunk = os.read(fin, BLOCK - len(pending))
            if not chunk:                                   # can't happen on O_RDWR
                break
            pending += chunk

        if len(pending) == BLOCK:
            data, pending = pending, b""
        elif pending:
            # Zero-pad the short tail out to a whole block and send it. bridge-tx
            # pads its own last frame the same way, so this stays sample-aligned
            # and costs at most 11 ms of silence at the end of a track.
            data, pending = pending + bytes(BLOCK - len(pending)), b""
        else:
            data = None                                     # idle: status only
            streaming = False

        mode = read_ctl("mode", "pure")
        preset = read_ctl("preset", "auto")
        active = None
        ctx = None

        m = eq_mtime()
        if m != cfg_mtime:                                  # the API saved new gains
            cfg_mtime = m
            cfg = load_user_eq()

        if data is not None:
            out = data
            if mode == "enhanced":
                detected = clf.preset
                if preset == "auto":
                    # An override is a manual preset laid over Auto. It is held
                    # until the detected class moves on, or the API clears it.
                    o = read_override()
                    if o != ovr_name:                       # newly set or cleared
                        ovr_name, ovr_class = o, detected
                    elif ovr_name and detected != ovr_class:
                        clear_override(); ovr_name = None
                    active = ovr_name or detected
                    ctx = detected                          # tweaks key off the class
                else:
                    if ovr_name:
                        clear_override(); ovr_name = None
                    active = preset
                    ctx = preset                            # ...or off the preset
            chain = active if mode == "enhanced" else "pure"
            key = chain_key(chain, cfg, ctx)

            if not streaming:
                # New stream. Rebuild unconditionally so the last track's filter
                # state can't ring into this one, and ramp up from silence to
                # kill the pop at track start -- except in Pure, where any ramp
                # would alter the bytes and break the bit-perfect guarantee.
                cur = make_chain(chain, cfg, ctx)
                prev = None
                fade_pos = FADE_FRAMES if chain != "pure" else 0
            elif key != cur.key:
                # Covers preset changes, override, slider moves and feedback
                # nudges alike -- all of them are just a different chain.
                prev = cur
                cur = make_chain(chain, cfg, ctx)
                fade_pos = FADE_FRAMES
            streaming = True

            if chain == "pure" and fade_pos == 0:
                out = data                              # bit-exact passthrough
            else:
                x = np.frombuffer(data, dtype=np.int16).reshape(-1, 2).astype(np.float32) / 32768.0
                if mode == "enhanced":
                    clf.feed(x.mean(axis=1))
                y = cur.process(x)
                if fade_pos > 0:
                    # prev must see every block too, or its filter state would
                    # be stale where the crossfade is still reading from it.
                    y_old = prev.process(x) if prev is not None else np.zeros_like(y)
                    n = min(len(x), fade_pos)
                    ramp = np.linspace(1 - (FADE_FRAMES - fade_pos) / FADE_FRAMES,
                                       1 - (FADE_FRAMES - fade_pos + n) / FADE_FRAMES, n,
                                       endpoint=False)[:, None]      # old weight 1 -> 0
                    y = y.copy()
                    y[:n] = y_old[:n] * ramp + y[:n] * (1 - ramp)
                    fade_pos -= n
                    if fade_pos == 0:
                        prev = None
                out = np.clip(np.rint(y * 32768.0), -32768, 32767).astype(np.int16).tobytes()

            # Level of what actually leaves here, so the page meter matches the
            # matrix. Read-only: `out` is written to the FIFO untouched.
            a = np.frombuffer(out, dtype=np.int16).reshape(-1, 2).astype(np.float32)
            lv_l += float(np.dot(a[:, 0], a[:, 0]))
            lv_r += float(np.dot(a[:, 1], a[:, 1]))
            lv_n += len(a)

            try:
                os.write(fout, out)
            except BrokenPipeError:                         # bridge-tx restarted
                os.close(fout); fout = None; open_out()

        now = time.time()
        if now >= lv_next:
            lv_next = now + LEVEL_MS / 1000.0
            if lv_n:
                write_level(lv_l / lv_n, lv_r / lv_n)
            else:
                write_level(0.0, 0.0)
            lv_l = lv_r = 0.0; lv_n = 0
        if now - last_status >= 1.0:
            last_status = now
            idle = data is None
            enh = (mode == "enhanced") and not idle
            cb = cfg["custom"]["bands"]
            write_status({"mode": mode, "preset": preset,
                          "active": active if enh else None,
                          "label": clf.label if enh else None,
                          "score": round(clf.score, 3) if enh else None,
                          "class_ts": round(clf.ts, 1) if enh and clf.ts else None,
                          "chosen": clf.preset if enh else None,
                          "override": ovr_name,   # user state: survives a pause
                          "context": ctx,
                          "offset": (cfg["offsets"].get(ctx) or [0.0] * 5) if ctx else None,
                          "custom": {"bands": cb,
                                     "pregain": cfg["custom"]["pregain"],
                                     "pregain_eff": round(auto_pregain(cb)
                                         if cfg["custom"]["pregain"] is None
                                         else cfg["custom"]["pregain"], 2)},
                          "bands_hz": list(BANDS),
                          "model": clf.model})

if __name__ == "__main__":
    main()
