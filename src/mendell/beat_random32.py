"""`mendell beat random32` — build a full 32-bar Mendell project from the library.

Creates a real, editable project (drums + bass + melody tracks, clips, an
arrangement, and an exported mix) rather than a flat rendered WAV — so the
result can be re-mixed, re-arranged, and tweaked like any other Mendell project.

Layout (4 sections x 8 bars = 32 bars):
    | 8     | 8         | 8        | 8        |
    Melody  clean       octave-up  lowpass    reverse   <- same loop, mutated
    Bass    same loop across all sections
    Drums   same one-shot pattern across all sections

The four melody clips live on one track, placed at bars 1/9/17/25; the engine
loops each until the next placement, so each 8-bar section gets its own mutation.
"""

import os
import random
import shutil
import sqlite3
import subprocess
import wave
from pathlib import Path

import numpy as np

from . import arrangement as arrangement_mod
from . import clips as clips_mod
from . import engine as engine_mod
from . import project as project_mod
from . import routing as routing_mod
from . import sampler as sampler_mod
from . import tracks as tracks_mod
from .midi_gen import write_pattern_midi

SR = 44100
KEYS = ["A", "B", "C", "D", "E", "F", "G"]
# Semitone offset of each key from C, used to transpose toward the chosen key.
_KEY_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
DEFAULT_DB = os.path.expanduser("~/.config/mendell/library.db")

# General MIDI percussion notes — match what `kit load` / `midi generate` use.
_GM = {"kick": "C2", "snare": "D2", "clap": "D#2", "hat": "F#2"}
_GM_NUM = {"kick": 36, "snare": 38, "clap": 39, "hat": 42}

DRUM_TRACK, KIT_TRACK, BASS_TRACK, MELODY_TRACK = "drums", "kit", "bass", "melody"
ARRANGEMENT_BARS = 32
SECTION_BARS = 8

# One constant 4/4 drum bar (beat_offset, note, velocity, length_beats); loops.
_DRUM_PATTERN = [
    (0.0, _GM_NUM["kick"], 112, 0.5), (2.0, _GM_NUM["kick"], 104, 0.5),
    (1.0, _GM_NUM["snare"], 100, 0.5), (3.0, _GM_NUM["snare"], 100, 0.5),
    (1.0, _GM_NUM["clap"], 78, 0.5), (3.0, _GM_NUM["clap"], 78, 0.5),
    *[(i * 0.5, _GM_NUM["hat"], 58 if i % 2 else 68, 0.25) for i in range(8)],
]


def _have_warp():
    """True if the rubberband CLI + pyrubberband are usable for clean warp."""
    if shutil.which("rubberband") is None:
        return False
    try:
        import pyrubberband  # noqa: F401
        return True
    except Exception:
        return False


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


def _write_wav(path, audio):
    audio = np.clip(audio, -1.0, 1.0)
    w = wave.open(str(path), "w")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes((audio * 32767).astype(np.int16).tobytes())
    w.close()


def _add_melody_clip(project_dir, clip_name, sample_path, *, bar, pitch,
                     native_bpm, warp_mode):
    """Import a melody sample as a looping clip placed at `bar` with `pitch`."""
    clips_mod.import_clip(project_dir, MELODY_TRACK, clip_name,
                          sample_path=sample_path,
                          native_bpm=native_bpm, warp=warp_mode)
    clips_mod.set_params(project_dir, MELODY_TRACK, clip_name, pitch=pitch, loop=True)
    arrangement_mod.place(project_dir, MELODY_TRACK, clip_name, bar=bar)


def render(parent, name, *, db_path=DEFAULT_DB, tempo=None, key=None, seed=None,
           export_format="mp3", warp=None):
    parent = Path(parent)
    if seed is not None:
        random.seed(seed)
    tempo = float(tempo) if tempo else float(random.randint(70, 160))
    key = key.upper() if key else random.choice(KEYS)
    if key not in _KEY_SEMITONE:
        raise ValueError(f"key must be one of {KEYS}")
    use_warp = _have_warp() if warp is None else bool(warp)
    warp_mode = "melodic" if use_warp else None
    bass_warp = "beats" if use_warp else None
    # nearest transpose to the chosen key, kept within +/-6 semitones
    key_semi = ((_KEY_SEMITONE[key] + 6) % 12) - 6

    con = _connect(db_path)

    # --- pick samples ------------------------------------------------------
    picks = {c: _pick(con, c)[0] for c in ("kick", "snare", "hat", "clap")}
    bpath = _pick(con, "bass", bpm=tempo)[0]
    mpath = (_pick(con, "melody", bpm=tempo) or _pick(con, "loop", bpm=tempo)
             or _pick(con, "melody"))[0]
    bbpm = _bpm_of(con, bpath)
    mbpm = _bpm_of(con, mpath)

    # --- project + drums (midi -> sampler kit) -----------------------------
    project_dir = project_mod.create(parent, name, bpm=tempo, key=key, scale="minor")
    tracks_mod.add(project_dir, DRUM_TRACK, "midi")
    tracks_mod.add(project_dir, KIT_TRACK, "sampler")
    sampler_mod.create(project_dir, KIT_TRACK)
    routing_mod.set_route(project_dir, DRUM_TRACK, KIT_TRACK)
    for cat, sample in picks.items():
        sampler_mod.map_add(project_dir, KIT_TRACK, note=_GM[cat], sample=sample, link=True)

    pattern_path = project_dir / "midi" / "drum-pattern.mid"
    write_pattern_midi(pattern_path, _DRUM_PATTERN)
    clips_mod.import_clip(project_dir, DRUM_TRACK, "drum-loop", midi_path=str(pattern_path))
    clips_mod.set_params(project_dir, DRUM_TRACK, "drum-loop", loop=True)
    arrangement_mod.place(project_dir, DRUM_TRACK, "drum-loop", bar=1)

    # --- bass: one looped loop across all 32 bars, transposed to key -------
    tracks_mod.add(project_dir, BASS_TRACK, "audio")
    clips_mod.import_clip(project_dir, BASS_TRACK, "bass-loop",
                          sample_path=bpath, native_bpm=bbpm, warp=bass_warp)
    clips_mod.set_params(project_dir, BASS_TRACK, "bass-loop", pitch=key_semi, loop=True)
    arrangement_mod.place(project_dir, BASS_TRACK, "bass-loop", bar=1)

    # --- melody: same loop, mutated per 8-bar section ----------------------
    tracks_mod.add(project_dir, MELODY_TRACK, "audio")
    # section 1: clean
    _add_melody_clip(project_dir, "mel-clean", mpath, bar=1, pitch=key_semi,
                     native_bpm=mbpm, warp_mode=warp_mode)
    # section 2: octave up (+12 semitones)
    _add_melody_clip(project_dir, "mel-octave", mpath, bar=1 + SECTION_BARS,
                     pitch=key_semi + 12, native_bpm=mbpm, warp_mode=warp_mode)
    # sections 3 & 4 need processed audio — pre-render lowpass + reverse variants
    mel = _decode(mpath)
    lp_path = project_dir / "samples" / "mel-lowpass.wav"
    rev_path = project_dir / "samples" / "mel-reverse.wav"
    lp_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(lp_path, _lowpass(mel))
    _write_wav(rev_path, mel[::-1].copy())
    # section 3: lowpass
    _add_melody_clip(project_dir, "mel-lowpass", str(lp_path), bar=1 + 2 * SECTION_BARS,
                     pitch=key_semi, native_bpm=mbpm, warp_mode=warp_mode)
    # section 4: reverse
    _add_melody_clip(project_dir, "mel-reverse", str(rev_path), bar=1 + 3 * SECTION_BARS,
                     pitch=key_semi, native_bpm=mbpm, warp_mode=warp_mode)

    arrangement_mod.set_params(project_dir, length=float(ARRANGEMENT_BARS))

    # --- render / export ---------------------------------------------------
    export_info = engine_mod.export(project_dir, format=export_format)

    return {
        "project": project_mod.info(project_dir),
        "engine": "rubberband" if use_warp else "none",
        "tempo": tempo,
        "key": key,
        "bars": ARRANGEMENT_BARS,
        "sections": 4,
        "tracks": [DRUM_TRACK, KIT_TRACK, BASS_TRACK, MELODY_TRACK],
        "bass": os.path.basename(bpath),
        "melody": os.path.basename(mpath),
        "kit": {c: os.path.basename(p) for c, p in picks.items()},
        "mutations": ["clean", "octave-up", "lowpass", "reverse"],
        "export": export_info,
    }
