"""Arrangement view — read/aggregate snapshot of a project's arrangement as a
track×bar grid, plus action helpers for random fill operations.

``view(project_dir)`` returns a grid-friendly JSON snapshot built from the
existing arrangement + tracks modules.  The three ``random_*`` helpers pick a
random loop/kit/clip and wire it into the arrangement using the same
existing APIs (kit, midi_gen, arrangement, clips) that the CLI uses.

Optional sibling modules (kits, midi_catalog) are guarded with try/except so
this module works standalone when they haven't been committed yet.
"""

from __future__ import annotations

import hashlib
import math
import random as _random
from pathlib import Path
from typing import Any

from . import arrangement as arrangement_mod
from . import clips as clips_mod
from . import midi_gen as midi_gen_mod
from . import project as project_mod
from . import tracks as tracks_mod
from .errors import BadInputError, NotFoundError
from .timing import frames_per_bar


def _clip_color(clip_name: str) -> str:
    """Stable hex colour derived from a hash of the clip name.

    Same clip name always yields the same colour. We clamp each channel into a
    mid-bright band so white block text stays legible on the result.
    """
    digest = hashlib.md5(clip_name.encode("utf-8")).digest()
    r = 70 + digest[0] % 130
    g = 70 + digest[1] % 130
    b = 70 + digest[2] % 130
    return f"#{r:02x}{g:02x}{b:02x}"


def _clip_length_bars(project_dir: Path, clip_name: str, tp: dict[str, Any]) -> tuple[int, str]:
    """Compute a clip's TRUE rendered length in bars (ceil, min 1) and its type.

    MIDI clips use ``length_frame`` directly. Audio clips convert
    ``length_seconds`` to bars at the project BPM, accounting for warp: a warped
    clip is time-stretched from its ``native_bpm`` to the project BPM, so its
    rendered duration scales by ``native_bpm / project_bpm``.
    """
    try:
        data = clips_mod.load(project_dir, clip_name)
    except Exception:
        return 1, "unknown"

    clip = data.get("clip", {})
    ctype = clip.get("type", "unknown")
    bpm = tp["bpm"]
    sr = tp["sample_rate"]
    bpb = tp["beats_per_bar"]
    fpb = frames_per_bar(bpm, sr, bpb)

    if ctype == "midi":
        length_frame = float(data.get("length_frame", 0) or 0)
        bars = math.ceil(length_frame / fpb) if fpb > 0 else 1
        return max(1, bars), ctype

    # audio
    length_seconds = float(data.get("length_seconds", 0.0) or 0.0)
    native_bpm = float(clip.get("native_bpm") or bpm)
    warp = clip.get("warp")
    rendered_seconds = length_seconds
    if warp and warp != "off" and native_bpm > 0:
        rendered_seconds = length_seconds * (native_bpm / bpm)
    seconds_per_bar = (60.0 / bpm) * bpb if bpm > 0 else 0.0
    bars = math.ceil(rendered_seconds / seconds_per_bar) if seconds_per_bar > 0 else 1
    return max(1, bars), ctype


def _ensure_track(project_dir: Path, name: str, track_type: str) -> None:
    """Create ``name`` as a ``track_type`` track if it doesn't exist yet.

    Lets random-fill work on a freshly scaffolded project (which has no tracks).
    """
    if not tracks_mod.exists(project_dir, name):
        tracks_mod.add(project_dir, name, track_type)


# ---------------------------------------------------------------------------
# view snapshot
# ---------------------------------------------------------------------------

def view(project_dir: Path) -> dict[str, Any]:
    """Return a grid-friendly snapshot of ``project_dir``'s arrangement.

    Shape::

        {
            "bpm": 120.0,
            "time_sig": "4/4",
            "beats_per_bar": 4,
            "total_bars": 32,
            "tracks": [
                {
                    "name": "drums",
                    "type": "midi",
                    "placements": [
                        {"clip": "drum-loop", "start_bar": 1, "length_bars": 4,
                         "clip_type": "midi", "color": "#3b5bdb"}
                    ]
                },
                ...
            ]
        }
    """
    project_dir = Path(project_dir)
    from . import migrate as migrate_mod
    migrate_mod.ensure_migrated(project_dir)
    tp = project_mod.timing_params(project_dir)
    arr_data = arrangement_mod.load(project_dir)
    arr_settings = {**arrangement_mod.ARRANGEMENT_DEFAULTS, **arr_data.get("arrangement", {})}
    placements = arr_data.get("clips", [])

    # Build per-track placement maps
    by_track: dict[str, list[dict]] = {}
    for p in placements:
        by_track.setdefault(p["track"], []).append(p)

    # Total bars: use explicit length if set, else derive from where the last
    # block (start + its TRUE length) actually ends, with a little headroom.
    total_bars_raw = arr_settings.get("length", 0.0)
    if total_bars_raw:
        total_bars = int(math.ceil(float(total_bars_raw)))
    elif placements:
        last_end = 1
        for p in placements:
            length_bars, _ = _clip_length_bars(project_dir, p["clip"], tp)
            last_end = max(last_end, p["bar"] + length_bars - 1)
        total_bars = last_end + 4  # headroom for dropping new blocks
    else:
        total_bars = 8  # default

    track_rows = []
    for t in tracks_mod.list_tracks(project_dir):
        name = t["name"]
        track_placements = sorted(by_track.get(name, []), key=lambda x: x["bar"])
        grid_placements = []
        for idx, p in enumerate(track_placements):
            # TRUE length from the clip's real content (no gap-to-next estimate).
            true_len, ctype = _clip_length_bars(project_dir, p["clip"], tp)
            # Enforce NO OVERLAP: clamp the rendered length so this block ends
            # at-or-before the next block's start on the same track.
            if idx + 1 < len(track_placements):
                next_start = track_placements[idx + 1]["bar"]
                max_len = max(1, next_start - p["bar"])
                length_bars = min(true_len, max_len)
            else:
                length_bars = true_len
            grid_placements.append({
                "clip": p["clip"],
                "start_bar": p["bar"],
                "length_bars": length_bars,
                "clip_type": ctype,
                "color": _clip_color(p["clip"]),
            })
        track_rows.append({
            "name": name,
            "type": t["type"],
            "placements": grid_placements,
        })

    return {
        "bpm": tp["bpm"],
        "time_sig": tp.get("time_sig", "4/4"),
        "beats_per_bar": tp.get("beats_per_bar", 4),
        "total_bars": total_bars,
        "tracks": track_rows,
    }


# ---------------------------------------------------------------------------
# random action helpers
# ---------------------------------------------------------------------------

def _seed_rng(seed: int | None) -> None:
    if seed is not None:
        _random.seed(seed)


def random_loop(
    project_dir: Path,
    track: str,
    *,
    bars: int = 8,
    library: str | None = None,
    seed: int | None = None,
    start_bar: int = 1,
) -> dict[str, Any]:
    """Pick a random audio loop from the library and place it on ``track``.

    The loop is imported as clip ``random-loop-<N>`` (incrementing to avoid
    collisions) and placed at ``start_bar``.  The clip is marked looping.

    Returns the placement record.
    """
    _seed_rng(seed)
    project_dir = Path(project_dir)

    # Try the optional sibling midi_catalog / kits modules for richer queries;
    # fall back to the core library module.
    loop_path: str | None = None
    try:
        from . import kits as kits_mod  # optional sibling
        loop_path = kits_mod.random_loop_path(library=library)
    except Exception:
        pass

    if loop_path is None:
        try:
            from . import library as library_mod
            import sqlite3
            db = library_mod.db_path()
            if db.exists():
                con = sqlite3.connect(str(db))
                q = ("SELECT l.path||'/'||f.rel_path FROM files f "
                     "JOIN libraries l ON l.name=f.library_name "
                     "WHERE f.category IN ('loop','melody','bass')"
                     + (" AND l.name=?" if library else ""))
                params = (library,) if library else ()
                rows = [r[0] for r in con.execute(q, params).fetchall()]
                con.close()
                if rows:
                    loop_path = _random.choice(rows)
        except Exception:
            pass

    if loop_path is None:
        raise ValueError(
            "No loop samples found in library.  "
            "Run `mendell library add` to register a sample library first."
        )

    # Pick a project-unique clip name
    clip_name = _unique_clip_name(project_dir, "random-loop")

    _ensure_track(project_dir, track, "audio")
    clips_mod.import_clip(project_dir, track, clip_name, sample_path=loop_path)
    clips_mod.set_params(project_dir, track, clip_name, loop=True)
    placement = arrangement_mod.place(project_dir, track, clip_name, bar=start_bar)
    return {"clip": clip_name, "source": loop_path, "placement": placement}


def random_kit(
    project_dir: Path,
    *,
    name: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Load a randomly chosen kit folder onto a sampler track.

    If the optional ``kits`` sibling module is available, uses it to pick a
    random kit folder.  Otherwise falls back to scanning library root paths.
    Returns the kit-load result.
    """
    _seed_rng(seed)
    project_dir = Path(project_dir)
    track_name = name or "kit"

    kit_folder: str | None = None
    try:
        from . import kits as kits_mod  # optional sibling
        kit_folder = kits_mod.random_kit_folder()
    except Exception:
        pass

    if kit_folder is None:
        # Fallback: look for subdirs under registered library roots that contain
        # wav/aif files, pick one at random.
        try:
            from . import library as library_mod
            import sqlite3
            db = library_mod.db_path()
            if db.exists():
                con = sqlite3.connect(str(db))
                roots = [r[0] for r in con.execute(
                    "SELECT path FROM libraries").fetchall()]
                con.close()
                candidates: list[str] = []
                for root in roots:
                    rp = Path(root)
                    if rp.is_dir():
                        for sub in rp.iterdir():
                            if sub.is_dir():
                                wavs = list(sub.glob("*.wav")) + list(sub.glob("*.aif"))
                                if wavs:
                                    candidates.append(str(sub))
                if candidates:
                    kit_folder = _random.choice(candidates)
        except Exception:
            pass

    if kit_folder is None:
        raise ValueError(
            "No kit folders found.  Register a sample library with drum one-shots first."
        )

    from . import kit as kit_mod
    result = kit_mod.load_kit(project_dir, track_name, kit_folder)
    return {"track": track_name, "kit_folder": kit_folder, "result": result}


def random_clip(
    project_dir: Path,
    track: str,
    *,
    style: str = "lofi",
    bars: int = 4,
    seed: int | None = None,
    start_bar: int = 1,
) -> dict[str, Any]:
    """Generate a random MIDI drum clip (using midi_gen) and place it on ``track``.

    ``style`` is one of the midi_gen DRUM_STYLES (boom-bap / lofi / trap).
    Returns the placement record.
    """
    _seed_rng(seed)
    project_dir = Path(project_dir)

    valid_styles = set(midi_gen_mod.DRUM_STYLES)
    if style not in valid_styles:
        style = "lofi"

    # Unique clip name
    clip_name = _unique_clip_name(project_dir, "random-clip")

    _ensure_track(project_dir, track, "midi")
    result = midi_gen_mod.generate(project_dir, track, clip_name, style=style, bars=bars)
    placement = arrangement_mod.place(project_dir, track, clip_name, bar=start_bar)
    return {"clip": clip_name, "style": style, "bars": bars, "placement": placement,
            "midi_result": result}


# ---------------------------------------------------------------------------
# selection-driven edit helpers (used by the arrange UI's clip/track actions)
# ---------------------------------------------------------------------------

# Standard GM drum notes a "fill a kit" operation targets.
_GM_KIT_NOTES = {36, 37, 38, 39, 42, 45, 46, 49, 51}


def _track_type(project_dir: Path, track: str) -> str:
    return tracks_mod.load(project_dir, track).get("track", {}).get("type", "")


def _unique_clip_name(project_dir: Path, prefix: str) -> str:
    """A clip name unique across the *whole project* (clip files live in a
    single ``clips/`` namespace, not per-track), so blocks dropped on different
    tracks don't collide on ``<prefix>-1``."""
    n = 1
    while clips_mod.exists(project_dir, f"{prefix}-{n}"):
        n += 1
    return f"{prefix}-{n}"


def randomize_clip(
    project_dir: Path, track: str, bar: int, *,
    library: str | None = None, seed: int | None = None,
) -> dict[str, Any]:
    """Swap whatever sits at ``(track, bar)`` for a fresh random clip of the
    same track type. Audio → random library loop; MIDI → generated pattern.

    ``place`` is keyed by ``(track, bar)``, so the new clip overwrites the old
    placement in-place.
    """
    project_dir = Path(project_dir)
    bar = int(bar)
    ttype = _track_type(project_dir, track)
    if ttype == "audio":
        return random_loop(project_dir, track, start_bar=bar, library=library, seed=seed)
    if ttype == "midi":
        return random_clip(project_dir, track, start_bar=bar, seed=seed)
    raise ValueError(
        f"cannot randomize a clip on a '{ttype}' track — select an audio or midi track"
    )


def replace_clip(
    project_dir: Path, track: str, bar: int, source: str, *, name: str | None = None,
) -> dict[str, Any]:
    """Replace the clip at ``(track, bar)`` with an explicit ``source``.

    For audio tracks ``source`` is a library ref or file path; for MIDI tracks
    it is a MIDI-catalog clip name.
    """
    project_dir = Path(project_dir)
    bar = int(bar)
    ttype = _track_type(project_dir, track)
    clip_name = name or _unique_clip_name(project_dir, "clip")

    if ttype == "audio":
        from . import library as library_mod
        resolved = library_mod.resolve_path_arg(source) or source
        clips_mod.import_clip(project_dir, track, clip_name, sample_path=str(resolved))
        clips_mod.set_params(project_dir, track, clip_name, loop=True)
        placement = arrangement_mod.place(project_dir, track, clip_name, bar=bar)
        return {"clip": clip_name, "source": str(resolved), "placement": placement}

    if ttype == "midi":
        from . import midi_catalog as midi_catalog_mod
        clip = midi_catalog_mod.show(source)
        clips_mod.import_clip(project_dir, track, clip_name, midi_path=clip["path"])
        clips_mod.set_params(project_dir, track, clip_name, loop=True)
        placement = arrangement_mod.place(project_dir, track, clip_name, bar=bar)
        return {"clip": clip_name, "source": clip["path"], "placement": placement}

    raise ValueError(
        f"cannot place a clip on a '{ttype}' track — select an audio or midi track"
    )


def remove_clip(project_dir: Path, track: str, bar: int) -> dict[str, Any]:
    """Remove the placement at ``(track, bar)`` from the arrangement."""
    return arrangement_mod.remove(Path(project_dir), track, int(bar))


def move_clip(project_dir: Path, track: str, from_bar: int, to_bar: int) -> dict[str, Any]:
    """Move the placement at ``(track, from_bar)`` to ``to_bar`` (snap-to-bar).

    Rejects the move when it would overlap another block on the same track:
    the moved block occupies ``[to_bar, to_bar + length - 1]`` and must not
    intersect any other placement's occupied span. Returns the updated
    placement record.
    """
    project_dir = Path(project_dir)
    from_bar = int(from_bar)
    to_bar = int(to_bar)
    if to_bar < 1:
        raise BadInputError(f"to_bar must be >= 1, got {to_bar}")

    arr = arrangement_mod.load(project_dir)
    placements = arr.get("clips", [])
    moving = next(
        (p for p in placements if p["track"] == track and p["bar"] == from_bar), None
    )
    if moving is None:
        raise NotFoundError(f"no clip placed on track '{track}' at bar {from_bar}")

    if to_bar == from_bar:
        return {"track": track, "clip": moving["clip"], "bar": from_bar, "moved": False}

    tp = project_mod.timing_params(project_dir)
    moved_len, _ = _clip_length_bars(project_dir, moving["clip"], tp)
    moved_start, moved_end = to_bar, to_bar + moved_len - 1

    for p in placements:
        if p["track"] != track or p["bar"] == from_bar:
            continue
        other_len, _ = _clip_length_bars(project_dir, p["clip"], tp)
        other_start, other_end = p["bar"], p["bar"] + other_len - 1
        if moved_start <= other_end and other_start <= moved_end:
            raise BadInputError(
                f"cannot move '{moving['clip']}' to bar {to_bar}: it would overlap "
                f"'{p['clip']}' (bars {other_start}-{other_end}) on track '{track}'"
            )

    clip_name = moving["clip"]
    arrangement_mod.remove(project_dir, track, from_bar)
    placement = arrangement_mod.place(project_dir, track, clip_name, bar=to_bar)
    return {**placement, "moved": True, "from_bar": from_bar}


# ---------------------------------------------------------------------------
# clip content (consumed by the per-block editor phase)
# ---------------------------------------------------------------------------

def clip_midi_content(project_dir: Path, track: str, clip: str) -> dict[str, Any]:
    """Return a MIDI clip's notes as ``(pitch, start_beat, length_beats, velocity)``.

    Frames in the stored note list are converted to beats at the project tempo,
    so the editor works in musical units. Also reports ``length_bars``,
    ``beats_per_bar`` and ``bpm``.
    """
    project_dir = Path(project_dir)
    data = clips_mod._get_for_track(project_dir, track, clip)
    if data.get("clip", {}).get("type") != "midi":
        raise BadInputError(f"clip '{clip}' is not a MIDI clip")

    tp = project_mod.timing_params(project_dir)
    bpm, sr, bpb = tp["bpm"], tp["sample_rate"], tp["beats_per_bar"]
    fpb = (60.0 / bpm) * sr if bpm > 0 else 1.0  # frames per beat

    notes_out = []
    for n in data.get("notes", []):
        start_frame = float(n.get("start_frame", 0) or 0)
        dur_frame = float(n.get("duration_frame", 0) or 0)
        notes_out.append({
            "pitch": int(n.get("pitch", n.get("note", 60))),
            "start_beat": round(start_frame / fpb, 6) if fpb else 0.0,
            "length_beats": round(dur_frame / fpb, 6) if fpb else 0.0,
            "velocity": int(n.get("velocity", 100)),
        })

    length_bars, _ = _clip_length_bars(project_dir, clip, tp)
    return {
        "notes": notes_out,
        "length_bars": length_bars,
        "beats_per_bar": bpb,
        "bpm": bpm,
    }


def clip_audio_peaks(
    project_dir: Path, track: str, clip: str, *, buckets: int = 800
) -> dict[str, Any]:
    """Return downsampled min/max peaks for an audio clip's source wav.

    The source is read mono-summed and reduced to ``buckets`` (~600-1000)
    ``[min, max]`` pairs suitable for drawing a waveform. Also reports
    ``length_seconds`` and the clip ``params``.
    """
    project_dir = Path(project_dir)
    data = clips_mod._get_for_track(project_dir, track, clip)
    clip_meta = data.get("clip", {})
    if clip_meta.get("type") != "audio":
        raise BadInputError(f"clip '{clip}' is not an audio clip")

    source = clip_meta.get("source")
    if not source or not Path(source).is_file():
        raise NotFoundError(f"audio source for clip '{clip}' not found: {source}")

    import numpy as np
    import soundfile as sf

    audio, _sr = sf.read(source, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    n = len(mono)
    buckets = max(1, min(int(buckets), 1000))
    peaks: list[list[float]] = []
    if n:
        edges = np.linspace(0, n, buckets + 1, dtype=int)
        for i in range(buckets):
            a, b = edges[i], edges[i + 1]
            if b <= a:
                peaks.append([0.0, 0.0])
                continue
            seg = mono[a:b]
            peaks.append([float(seg.min()), float(seg.max())])

    return {
        "peaks": peaks,
        "length_seconds": data.get("length_seconds"),
        "params": data.get("params", {}),
    }


def set_clip_audio_params(
    project_dir: Path, track: str, clip: str, **params: Any
) -> dict[str, Any]:
    """Persist audio-clip param edits (gain/pitch/loop/...) via ``clips.set_params``."""
    return clips_mod.set_params(Path(project_dir), track, clip, **params)


def add_kit_to_track(
    project_dir: Path, track: str, *,
    kit: str | None = None, library: str | None = None, seed: int | None = None,
) -> dict[str, Any]:
    """Attach a drum kit so a MIDI track's notes make sound.

    Hosts a sampler instrument directly on ``track`` (in "kit" mode) and fills
    it from a named catalog ``kit`` or from random library one-shots.
    """
    project_dir = Path(project_dir)
    ttype = _track_type(project_dir, track)
    if ttype != "midi":
        raise ValueError(f"kits attach to MIDI tracks; '{track}' is type '{ttype}'")

    from . import sampler as sampler_mod
    sampler_mod.create(project_dir, track, mode="kit")

    if kit:
        from . import kits as kits_mod
        result = kits_mod.apply_to_project(kit, project_dir, track)
        mapped = result.get("count", 0)
    else:
        from . import beat as beat_mod
        rng = _random.Random(seed)
        out = beat_mod._fill_kit_from_library(
            project_dir, track, set(_GM_KIT_NOTES), library=library, rng=rng
        )
        if not out:
            raise ValueError(
                "no usable one-shots in the library to build a kit — register a "
                "sample library first, or pass a saved kit name"
            )
        mapped = len(out[0])

    return {"track": track, "sampler_track": track, "kit": kit, "mapped": mapped}
