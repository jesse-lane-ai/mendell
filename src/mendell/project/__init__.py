"""Project lifecycle: scaffolding, project.toml read/write, `set`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import paths
from ..errors import BadInputError, NotFoundError
from ..timing import parse_time_sig
from ..toml_io import read_toml, write_toml

VALID_KEYS = {"C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"}
VALID_SCALES = {"major", "minor"}

DEFAULTS = {
    "bpm": 120.0,
    "key": "C",
    "scale": "minor",
    "sample_rate": 44100,
    "time_sig": "4/4",
}
MASTER_DEFAULTS = {
    "vol": 100,
    "limiter_ceiling": -0.3,
}

SCAFFOLD_DIRS = ("tracks", "clips", "samplers", "samples")


def _validate_key(key: str) -> str:
    if key not in VALID_KEYS:
        raise BadInputError(f"invalid key '{key}' (expected one of {sorted(VALID_KEYS)})")
    return key


def _validate_scale(scale: str) -> str:
    if scale not in VALID_SCALES:
        raise BadInputError(f"invalid scale '{scale}' (expected one of {sorted(VALID_SCALES)})")
    return scale


def create(
    parent: Path,
    name: str,
    *,
    bpm: float = DEFAULTS["bpm"],
    key: str = DEFAULTS["key"],
    scale: str = DEFAULTS["scale"],
    sample_rate: int = DEFAULTS["sample_rate"],
    time_sig: str = DEFAULTS["time_sig"],
) -> Path:
    """Scaffold a new project directory with project.toml and empty subdirs."""
    project_dir = parent / name
    if (project_dir / paths.PROJECT_FILE).exists():
        raise BadInputError(f"project '{name}' already exists at {project_dir}")

    _validate_key(key)
    _validate_scale(scale)
    parse_time_sig(time_sig)
    if sample_rate <= 0:
        raise BadInputError(f"invalid sample rate '{sample_rate}'")
    if bpm <= 0:
        raise BadInputError(f"invalid bpm '{bpm}'")

    project_dir.mkdir(parents=True, exist_ok=True)
    for sub in SCAFFOLD_DIRS:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    data = {
        "project": {
            "name": name,
            "bpm": bpm,
            "key": key,
            "scale": scale,
            "sample_rate": sample_rate,
            "time_sig": time_sig,
        },
        "master": dict(MASTER_DEFAULTS),
    }
    write_toml(paths.project_toml(project_dir), data)

    # Empty timeline scaffold for the arrangement.
    write_toml(paths.arrangement_toml(project_dir), {"clips": [], "arrangement": {}})

    return project_dir


def load(project_dir: Path) -> dict[str, Any]:
    toml_path = paths.project_toml(project_dir)
    if not toml_path.is_file():
        raise NotFoundError(f"project not found at {project_dir}")
    return read_toml(toml_path)


def save(project_dir: Path, data: dict[str, Any]) -> None:
    write_toml(paths.project_toml(project_dir), data)


def timing_params(project_dir: Path) -> dict[str, Any]:
    """Convenience accessor for the values the timing engine needs."""
    proj = load(project_dir)["project"]
    beats_per_bar, beat_unit = parse_time_sig(proj.get("time_sig", DEFAULTS["time_sig"]))
    return {
        "bpm": float(proj.get("bpm", DEFAULTS["bpm"])),
        "sample_rate": int(proj.get("sample_rate", DEFAULTS["sample_rate"])),
        "beats_per_bar": beats_per_bar,
        "beat_unit": beat_unit,
    }


def info(project_dir: Path) -> dict[str, Any]:
    data = load(project_dir)
    proj = data.get("project", {})
    master = data.get("master", {})

    track_count = sum(1 for _ in paths.tracks_dir(project_dir).glob("*.toml"))
    clip_count = sum(1 for _ in paths.clips_dir(project_dir).glob("*.toml"))
    sampler_count = sum(1 for _ in paths.samplers_dir(project_dir).glob("*.toml"))

    return {
        "name": proj.get("name"),
        "path": str(project_dir),
        "bpm": proj.get("bpm"),
        "key": proj.get("key"),
        "scale": proj.get("scale"),
        "sample_rate": proj.get("sample_rate"),
        "time_sig": proj.get("time_sig"),
        "master": {
            "vol": master.get("vol", MASTER_DEFAULTS["vol"]),
            "limiter_ceiling": master.get("limiter_ceiling", MASTER_DEFAULTS["limiter_ceiling"]),
        },
        "tracks": track_count,
        "clips": clip_count,
        "samplers": sampler_count,
    }


SETTABLE = {
    "bpm",
    "key",
    "scale",
    "time_sig",
    "master_vol",
    "limiter_ceiling",
}


def set_params(project_dir: Path, **params: Any) -> dict[str, Any]:
    """Apply a set of `mendell set` parameter updates. Unspecified params are
    left untouched (idempotent partial update)."""
    data = load(project_dir)
    proj = data.setdefault("project", {})
    master = data.setdefault("master", {})

    changed: dict[str, Any] = {}
    for name, value in params.items():
        if value is None:
            continue
        if name not in SETTABLE:
            raise BadInputError(f"unknown or immutable project parameter '{name}'")

        if name == "bpm":
            value = float(value)
            if value <= 0:
                raise BadInputError(f"invalid bpm '{value}'")
            proj["bpm"] = value
        elif name == "key":
            proj["key"] = _validate_key(value)
        elif name == "scale":
            proj["scale"] = _validate_scale(value)
        elif name == "time_sig":
            parse_time_sig(value)
            proj["time_sig"] = value
        elif name == "master_vol":
            value = int(value)
            if not (0 <= value <= 100):
                raise BadInputError(f"master_vol must be 0-100, got {value}")
            master["vol"] = value
        elif name == "limiter_ceiling":
            master["limiter_ceiling"] = float(value)

        changed[name] = value

    save(project_dir, data)
    return {"changed": changed, **info(project_dir)}
