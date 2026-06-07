"""Arrangement — clip placements at bar positions, loop points, length.

No scenes: clips place directly into the timeline at bar positions (always on
a bar boundary — beat 1). Stored in arrangement.toml as the union of every
track's placements plus the arrangement-wide loop/length settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import clips as clips_mod
from .. import paths
from .. import project as project_mod
from .. import tracks as tracks_mod
from ..errors import BadInputError, NotFoundError
from ..timing import BarBeat, bar_beat_to_frames, frames_to_bar_beat, parse_bar_beat
from ..toml_io import read_toml, write_toml

ARRANGEMENT_DEFAULTS = {"loop": False, "loop_in": "1.1", "loop_out": "1.1", "length": 0.0}


def load(project_dir: Path) -> dict[str, Any]:
    toml_path = paths.arrangement_toml(project_dir)
    if not toml_path.is_file():
        return {"clips": [], "arrangement": dict(ARRANGEMENT_DEFAULTS)}
    data = read_toml(toml_path)
    data.setdefault("clips", [])
    data.setdefault("arrangement", {})
    return data


def save(project_dir: Path, data: dict[str, Any]) -> None:
    write_toml(paths.arrangement_toml(project_dir), data)


def _timing(project_dir: Path) -> dict[str, Any]:
    return project_mod.timing_params(project_dir)


def _frame_for_bar(project_dir: Path, bar: int) -> int:
    tp = _timing(project_dir)
    return bar_beat_to_frames(bar, 1.0, tp["bpm"], tp["sample_rate"], tp["beats_per_bar"])


# ---------------------------------------------------------------------------
# place / remove / list
# ---------------------------------------------------------------------------

def place(project_dir: Path, track_name: str, clip_name: str, bar: int) -> dict[str, Any]:
    if bar < 1:
        raise BadInputError(f"bar must be >= 1, got {bar}")

    tracks_mod.load(project_dir, track_name)  # raises NotFoundError if missing
    clip_data = clips_mod.load(project_dir, clip_name)
    if clip_data.get("clip", {}).get("track") != track_name:
        raise BadInputError(f"clip '{clip_name}' is not on track '{track_name}'")

    frame = _frame_for_bar(project_dir, bar)

    data = load(project_dir)
    placements = data.setdefault("clips", [])
    placements = [p for p in placements if not (p["track"] == track_name and p["bar"] == bar)]
    placements.append({"track": track_name, "clip": clip_name, "bar": bar, "beat": 1, "frame": frame})
    placements.sort(key=lambda p: (p["frame"], p["track"]))
    data["clips"] = placements
    save(project_dir, data)

    return {"track": track_name, "clip": clip_name, "bar": bar, "beat": 1, "frame": frame}


def remove(project_dir: Path, track_name: str, bar: int) -> dict[str, Any]:
    data = load(project_dir)
    placements = data.get("clips", [])
    remaining = [p for p in placements if not (p["track"] == track_name and p["bar"] == bar)]
    if len(remaining) == len(placements):
        raise NotFoundError(f"no clip placed on track '{track_name}' at bar {bar}")
    data["clips"] = remaining
    save(project_dir, data)
    return {"removed": {"track": track_name, "bar": bar}}


def list_placements(project_dir: Path) -> list[dict[str, Any]]:
    return load(project_dir).get("clips", [])


# ---------------------------------------------------------------------------
# loop / settings
# ---------------------------------------------------------------------------

def _format_bar_beat(value: Any) -> str:
    if isinstance(value, BarBeat):
        return str(value)
    return str(parse_bar_beat(value))


def set_loop(project_dir: Path, *, loop_in: str | float, loop_out: str | float) -> dict[str, Any]:
    bb_in, bb_out = parse_bar_beat(loop_in), parse_bar_beat(loop_out)

    data = load(project_dir)
    arr = data.setdefault("arrangement", dict(ARRANGEMENT_DEFAULTS))
    arr["loop"] = True
    arr["loop_in"] = str(bb_in)
    arr["loop_out"] = str(bb_out)
    save(project_dir, data)
    return show(project_dir)


ARRANGEMENT_SETTABLE = {"loop", "loop_in", "loop_out", "length"}


def set_params(project_dir: Path, **kwargs: Any) -> dict[str, Any]:
    data = load(project_dir)
    arr = data.setdefault("arrangement", dict(ARRANGEMENT_DEFAULTS))

    changed: dict[str, Any] = {}
    for name, value in kwargs.items():
        if value is None:
            continue
        if name not in ARRANGEMENT_SETTABLE:
            raise BadInputError(f"unknown arrangement parameter '{name}'")

        if name == "loop":
            arr["loop"] = bool(value)
            changed[name] = bool(value)
        elif name in ("loop_in", "loop_out"):
            arr[name] = str(parse_bar_beat(value))
            changed[name] = arr[name]
        elif name == "length":
            value = float(value)
            if value < 0:
                raise BadInputError(f"length must be >= 0, got {value}")
            arr["length"] = value
            changed[name] = value

    save(project_dir, data)
    return {"changed": changed, **show(project_dir)}


def show(project_dir: Path) -> dict[str, Any]:
    data = load(project_dir)
    arr = {**ARRANGEMENT_DEFAULTS, **data.get("arrangement", {})}
    return {
        "loop": arr["loop"],
        "loop_in": arr["loop_in"],
        "loop_out": arr["loop_out"],
        "length": arr["length"],
        "clips": data.get("clips", []),
    }
