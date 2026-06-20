"""Track management — add/remove/list/show, and the track TOML format.

A track TOML file holds everything that belongs to the track itself:
type, mixer settings (vol/pan/mute/solo), FX chain, and track/mixer/FX
automation (see SPEC.md "Automation" — each entity owns its own automation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import paths
from ..errors import BadInputError, NotFoundError
from ..toml_io import read_toml, write_toml

VALID_TYPES = {"midi", "audio"}

# Instrument types a MIDI track can host. Only "sampler" exists today; "synth"
# and "vst" are reserved for future work.
VALID_INSTRUMENTS = {"sampler"}

MIXER_DEFAULTS = {
    "vol": 100,
    "pan": 0,
    "mute": False,
    "solo": False,
}


def _validate_type(track_type: str) -> str:
    if track_type not in VALID_TYPES:
        raise BadInputError(
            f"invalid track type '{track_type}' (expected one of {sorted(VALID_TYPES)})"
        )
    return track_type


def _new_track_data(name: str, track_type: str) -> dict[str, Any]:
    return {
        "track": {"name": name, "type": track_type},
        "mixer": dict(MIXER_DEFAULTS),
        "fx": [],
        "fx_next_id": 0,
        "automation": [],
        "clips": [],
    }


def exists(project_dir: Path, name: str) -> bool:
    return paths.track_toml(project_dir, name).is_file()


def load(project_dir: Path, name: str) -> dict[str, Any]:
    toml_path = paths.track_toml(project_dir, name)
    if not toml_path.is_file():
        raise NotFoundError(f"track '{name}' not found")
    return read_toml(toml_path)


def save(project_dir: Path, name: str, data: dict[str, Any]) -> None:
    write_toml(paths.track_toml(project_dir, name), data)


def add(project_dir: Path, name: str, track_type: str) -> dict[str, Any]:
    """Create-or-update: idempotent. Re-adding an existing track updates its type."""
    _validate_type(track_type)
    if exists(project_dir, name):
        data = load(project_dir, name)
        data["track"]["type"] = track_type
        save(project_dir, name, data)
    else:
        data = _new_track_data(name, track_type)
        save(project_dir, name, data)
    return summary(project_dir, name)


def remove(project_dir: Path, name: str) -> dict[str, Any]:
    toml_path = paths.track_toml(project_dir, name)
    if not toml_path.is_file():
        raise NotFoundError(f"track '{name}' not found")
    toml_path.unlink()
    return {"removed": name}


def list_tracks(project_dir: Path) -> list[dict[str, Any]]:
    names = sorted(p.stem for p in paths.tracks_dir(project_dir).glob("*.toml"))
    return [summary(project_dir, name) for name in names]


def summary(project_dir: Path, name: str) -> dict[str, Any]:
    data = load(project_dir, name)
    track = data.get("track", {})
    mixer = {**MIXER_DEFAULTS, **data.get("mixer", {})}
    return {
        "name": track.get("name", name),
        "type": track.get("type"),
        "instrument": track.get("instrument"),
        "vol": mixer["vol"],
        "pan": mixer["pan"],
        "mute": mixer["mute"],
        "solo": mixer["solo"],
        "fx": len(data.get("fx", [])),
        "clips": len(data.get("clips", [])),
    }


def show(project_dir: Path, name: str) -> dict[str, Any]:
    data = load(project_dir, name)
    track = data.get("track", {})
    mixer = {**MIXER_DEFAULTS, **data.get("mixer", {})}
    return {
        "name": track.get("name", name),
        "type": track.get("type"),
        "instrument": track.get("instrument"),
        "mixer": mixer,
        "fx": data.get("fx", []),
        "clips": data.get("clips", []),
        "automation": data.get("automation", []),
    }


def set_params(project_dir: Path, name: str, *, new_name: str | None = None,
               new_type: str | None = None) -> dict[str, Any]:
    data = load(project_dir, name)
    changed: dict[str, Any] = {}

    if new_type is not None:
        _validate_type(new_type)
        data["track"]["type"] = new_type
        changed["type"] = new_type

    if new_name is not None and new_name != name:
        if exists(project_dir, new_name):
            raise BadInputError(f"track '{new_name}' already exists")
        data["track"]["name"] = new_name
        save(project_dir, new_name, data)
        paths.track_toml(project_dir, name).unlink()
        changed["name"] = new_name
        name = new_name
    else:
        save(project_dir, name, data)

    return {"changed": changed, **summary(project_dir, name)}


# ---------------------------------------------------------------------------
# instruments — a MIDI track hosts an instrument (today only "sampler")
# ---------------------------------------------------------------------------

def set_instrument(project_dir: Path, name: str, inst_type: str = "sampler") -> dict[str, Any]:
    """Attach an instrument of ``inst_type`` to a MIDI track (idempotent)."""
    if inst_type not in VALID_INSTRUMENTS:
        raise BadInputError(
            f"invalid instrument '{inst_type}' (expected one of {sorted(VALID_INSTRUMENTS)})"
        )
    data = load(project_dir, name)
    if data.get("track", {}).get("type") != "midi":
        raise BadInputError(
            f"instruments attach to MIDI tracks; '{name}' is type "
            f"'{data.get('track', {}).get('type')}'"
        )
    data["track"]["instrument"] = {"type": inst_type}
    save(project_dir, name, data)
    return summary(project_dir, name)


def get_instrument(project_dir: Path, name: str) -> dict[str, Any] | None:
    """Return the track's instrument descriptor, or ``None`` if it has none."""
    return load(project_dir, name).get("track", {}).get("instrument")


def has_instrument(project_dir: Path, name: str) -> bool:
    return bool(get_instrument(project_dir, name))


def clear_instrument(project_dir: Path, name: str) -> dict[str, Any]:
    data = load(project_dir, name)
    data.get("track", {}).pop("instrument", None)
    save(project_dir, name, data)
    return summary(project_dir, name)
