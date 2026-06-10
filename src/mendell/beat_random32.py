"""32-bar beat from the sample library — the 'napkin' arrangement.

Layout (4 sections x 8 bars = 32 bars):
    | 8     | 8     | 8     | 8     |
    Melody  Melody  Melody  Melody   <- same melody, mutated each section (pitch/fx)
    Bass    Bass    Bass    Bass     <- stays the same
    Drums   Drums   Drums   Drums    <- stays the same

Rules:
  * Pull loops/one-shots from the registered library.db.
  * Random tempo 70-160 BPM and random key (A-G).
  * Time-stretch selected loops to tempo (cheap resample).
  * Transpose bass + melody to the selected key; mutate melody every 8 bars.
"""

import os
import random
import shutil
import sqlite3
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 44100


def _have_warp():
    """True if the rubberband CLI + pyrubberband are usable for clean warp."""
    if shutil.which("rubberband") is None:
        return False
    try:
        import pyrubberband  # noqa: F401
        return True
    except Exception:
        return False
KEYS = ["A", "B", "C", "D", "E", "F", "G"]
# Semitone offset of each key from C, used to transpose toward the chosen key.
_KEY_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
DEFAULT_DB = os.path.expanduser("~/.config/mendell/library.db")


def _connect(db_path):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"library db not found: {db_path}")
    return sqlite3.connect(db_path)


def _pick(con, cat, n=1, bpm=None, tol=8):
    """Build the full candidate list for a category, then choose randomly."""
    base = ("SELECT l.path||'/'||f.rel_path FROM files f "
            "JOIN libraries l ON l.name=f.library_name WHERE f.category=?")
    if bpm is not None:
        rows = [r[0] for r in con.execute(
            base + " AND f.bpm BETWEEN ? AND ?", (cat, bpm - tol, bpm + tol)).fetchall()]
        if rows:
            return random.sample(rows, min(n, len(rows)))
    rows = [r[0] for r in con.execute(base, (cat,)).fetchall()]
    if not rows:
        raise ValueError(f"no samples in library for category '{cat}'")
    return random.sample(rows, min(n, len(rows)))


def _bpm_of(con, path):
    r = con.execute(
        "SELECT f.bpm FROM files f JOIN libraries l ON l.name=f.library_name "
        "WHERE l.path||'/'||f.rel_path=?", (path,)).fetchone()
    return r[0] if r and r[0] else None


def _decode(path):
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, 2).copy()


def _resample(src, ratio):
    """Speed up/slow down by `ratio` (>1 = faster/higher) via index resample."""
    if ratio == 1.0 or len(src) == 0:
        return src
    idx = (np.arange(int(len(src) / ratio)) * ratio).astype(int)
    idx = idx[idx < len(src)]
    return src[idx]


def _tile(src, length):
    if len(src) == 0:
        return np.zeros((length, 2), np.float32)
    return np.tile(src, (length // len(src) + 1, 1))[:length]


def _prep(src, stretch_ratio, semitones, use_warp):
    """Time-stretch (toward target tempo) and pitch-shift one loop.

    Warp path uses rubberband for independent tempo/pitch; the fallback resamples
    (tempo and pitch are coupled). `stretch_ratio` = target_bpm / native_bpm.
    """
    if len(src) == 0:
        return src
    if use_warp:
        import pyrubberband as prb
        out = src
        if stretch_ratio != 1.0:
            out = prb.time_stretch(out, SR, stretch_ratio)
        if semitones:
            out = prb.pitch_shift(out, SR, semitones)
        return np.ascontiguousarray(out, dtype=np.float32)
    # fallback: resample couples speed + pitch
    ratio = stretch_ratio * (2 ** (semitones / 12.0))
    return _resample(src, ratio)


def _lowpass(x, a=0.16):
    y = np.copy(x)
    for c in range(2):
        acc = 0.0
        col = x[:, c]
        out = y[:, c]
        for i in range(len(col)):
            acc += a * (col[i] - acc)
            out[i] = acc
    return y


def render(out_path, db_path=DEFAULT_DB, tempo=None, key=None, seed=None, warp=None):
    if seed is not None:
        random.seed(seed)
    tempo = float(tempo) if tempo else float(random.randint(70, 160))
    key = key.upper() if key else random.choice(KEYS)
    if key not in _KEY_SEMITONE:
        raise ValueError(f"key must be one of {KEYS}")
    use_warp = _have_warp() if warp is None else bool(warp)

    con = _connect(db_path)

    beat = 60.0 / tempo
    spb = int(round(beat * 4 * SR))          # samples per bar (4/4)
    sec = spb * 8                            # samples per 8-bar section
    total = sec * 4
    step = int(round(beat * SR / 4))         # 16th-note step
    mix = np.zeros((total, 2), np.float32)

    def place(src, at, g=1.0):
        n = min(len(src), len(mix) - at)
        if n > 0:
            mix[at:at + n] += src[:n] * g

    # --- one-shot drum kit, same across all sections -----------------------
    kpath, spath, hpath, cpath = (_pick(con, "kick")[0], _pick(con, "snare")[0],
                                  _pick(con, "hat")[0], _pick(con, "clap")[0])
    kick, snare, hat, clap = _decode(kpath), _decode(spath), _decode(hpath), _decode(cpath)

    def drums(off):
        for b in range(8):
            bo = off + b * spb
            for bt in range(4):
                t = bo + bt * int(round(beat * SR))
                if bt in (0, 2):
                    place(kick, t, 0.95)
                if bt in (1, 3):
                    place(snare, t, 0.8)
                    place(clap, t, 0.45)
            for s in range(16):
                place(hat, bo + s * step, 0.3 if s % 2 else 0.45)

    # nearest transpose to the chosen key, kept within +/-6 semitones
    key_semi = ((_KEY_SEMITONE[key] + 6) % 12) - 6

    # --- bass: same loop every section, stretched to tempo + to key --------
    bpath = _pick(con, "bass", bpm=tempo)[0]
    bratio = (tempo / _bpm_of(con, bpath)) if _bpm_of(con, bpath) else 1.0
    bass_seg = _tile(_prep(_decode(bpath), bratio, key_semi, use_warp), sec)

    # --- melody: same loop, mutated every 8 bars ---------------------------
    mpath = (_pick(con, "melody", bpm=tempo) or _pick(con, "loop", bpm=tempo)
             or _pick(con, "melody"))[0]
    mratio = (tempo / _bpm_of(con, mpath)) if _bpm_of(con, mpath) else 1.0
    mel_native = _decode(mpath)

    mutations = ["clean", "octave-up", "lowpass", "reverse"]
    for s in range(4):
        off = s * sec
        drums(off)
        place(bass_seg, off, 0.5)
        mut = mutations[s]
        # +12 semitones for the octave (clean pitch under warp; speed under resample)
        msemi = key_semi + (12 if mut == "octave-up" else 0)
        mseg = _tile(_prep(mel_native, mratio, msemi, use_warp), sec)
        if mut == "lowpass":
            mseg = _lowpass(mseg)
        elif mut == "reverse":
            mseg = mseg[::-1].copy()
        place(mseg, off, 0.5)

    # --- master: normalize + soft clip -------------------------------------
    peak = np.max(np.abs(mix))
    if peak > 0:
        mix *= 0.97 / peak
    mix = np.tanh(mix * 1.2) * 0.9

    out_path = str(Path(out_path).expanduser())
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    w = wave.open(out_path, "w")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes((mix * 32767).astype(np.int16).tobytes())
    w.close()

    return {
        "out": out_path,
        "engine": "rubberband" if use_warp else "resample",
        "tempo": tempo,
        "key": key,
        "bars": 32,
        "sections": 4,
        "duration_sec": round(total / SR, 2),
        "bass": os.path.basename(bpath),
        "melody": os.path.basename(mpath),
        "kit": {k: os.path.basename(v) for k, v in
                (("kick", kpath), ("snare", spath), ("hat", hpath), ("clap", cpath))},
        "mutations": mutations,
    }
