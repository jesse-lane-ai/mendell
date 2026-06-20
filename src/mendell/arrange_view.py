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

import math
import random as _random
from pathlib import Path
from typing import Any

from . import arrangement as arrangement_mod
from . import clips as clips_mod
from . import midi_gen as midi_gen_mod
from . import project as project_mod
from . import tracks as tracks_mod


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
                        {"clip": "drum-loop", "start_bar": 1, "length_bars": 32}
                    ]
                },
                ...
            ]
        }
    """
    project_dir = Path(project_dir)
    tp = project_mod.timing_params(project_dir)
    arr_data = arrangement_mod.load(project_dir)
    arr_settings = {**arrangement_mod.ARRANGEMENT_DEFAULTS, **arr_data.get("arrangement", {})}
    placements = arr_data.get("clips", [])

    # Total bars: use explicit length if set, else derive from placements.
    total_bars_raw = arr_settings.get("length", 0.0)
    if total_bars_raw:
        total_bars = int(math.ceil(float(total_bars_raw)))
    elif placements:
        # last placement bar — we don't track end bar, so just report max start+1
        total_bars = max(p["bar"] for p in placements) + 7  # rough estimate
    else:
        total_bars = 8  # default

    # Build per-track placement maps
    by_track: dict[str, list[dict]] = {}
    for p in placements:
        by_track.setdefault(p["track"], []).append(p)

    track_rows = []
    for t in tracks_mod.list_tracks(project_dir):
        name = t["name"]
        track_placements = by_track.get(name, [])
        grid_placements = []
        for p in sorted(track_placements, key=lambda x: x["bar"]):
            # Estimate length_bars: distance to next placement on same track,
            # or to arrangement end, capped at total_bars.
            others = [q["bar"] for q in track_placements if q["bar"] > p["bar"]]
            if others:
                length_bars = min(others) - p["bar"]
            else:
                length_bars = max(1, total_bars - p["bar"] + 1)
            grid_placements.append({
                "clip": p["clip"],
                "start_bar": p["bar"],
                "length_bars": length_bars,
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

    # Pick a unique clip name
    existing_clips = set()
    try:
        track_data = tracks_mod.load(project_dir, track)
        existing_clips = {c.get("name", "") for c in track_data.get("clips", [])}
    except Exception:
        pass
    n = 1
    while f"random-loop-{n}" in existing_clips:
        n += 1
    clip_name = f"random-loop-{n}"

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
    existing_clips: set[str] = set()
    try:
        track_data = tracks_mod.load(project_dir, track)
        existing_clips = {c.get("name", "") for c in track_data.get("clips", [])}
    except Exception:
        pass
    n = 1
    while f"random-clip-{n}" in existing_clips:
        n += 1
    clip_name = f"random-clip-{n}"

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


def _unique_clip_name(project_dir: Path, track: str, prefix: str) -> str:
    existing: set[str] = set()
    try:
        existing = {c.get("name", "") for c in tracks_mod.load(project_dir, track).get("clips", [])}
    except Exception:
        pass
    n = 1
    while f"{prefix}-{n}" in existing:
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
    clip_name = name or _unique_clip_name(project_dir, track, "clip")

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


def add_kit_to_track(
    project_dir: Path, track: str, *,
    kit: str | None = None, library: str | None = None, seed: int | None = None,
) -> dict[str, Any]:
    """Attach a drum kit so a MIDI track's notes make sound.

    Creates (or reuses) a sampler track, routes ``track`` → it, then fills it
    from a named catalog ``kit`` or from random library one-shots.
    """
    project_dir = Path(project_dir)
    ttype = _track_type(project_dir, track)
    if ttype != "midi":
        raise ValueError(f"kits attach to MIDI tracks; '{track}' is type '{ttype}'")

    from . import routing as routing_mod
    from . import sampler as sampler_mod

    # Reuse an existing route target if the track already feeds a sampler.
    routes = [r["to"] for r in routing_mod.list_routes(project_dir) if r["from"] == track]
    sampler_track = routes[0] if routes else f"{track}-kit"
    if not tracks_mod.exists(project_dir, sampler_track):
        tracks_mod.add(project_dir, sampler_track, "sampler")
        sampler_mod.create(project_dir, sampler_track)
    routing_mod.set_route(project_dir, track, sampler_track)

    if kit:
        from . import kits as kits_mod
        result = kits_mod.apply_to_project(kit, project_dir, sampler_track)
        mapped = result.get("count", 0)
    else:
        from . import beat as beat_mod
        rng = _random.Random(seed)
        out = beat_mod._fill_kit_from_library(
            project_dir, sampler_track, set(_GM_KIT_NOTES), library=library, rng=rng
        )
        if not out:
            raise ValueError(
                "no usable one-shots in the library to build a kit — register a "
                "sample library first, or pass a saved kit name"
            )
        mapped = len(out[0])

    return {"track": track, "sampler_track": sampler_track, "kit": kit, "mapped": mapped}
