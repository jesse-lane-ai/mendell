"""MIDI -> Sampler routing — a many-to-many relation stored on the MIDI
track's TOML (the `routes` list of destination sampler-track names)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import paths
from . import tracks as tracks_mod
from .errors import BadInputError, NotFoundError


def _require_type(project_dir: Path, track_name: str, expected: str, role: str) -> dict[str, Any]:
    data = tracks_mod.load(project_dir, track_name)
    actual = data.get("track", {}).get("type")
    if actual != expected:
        raise BadInputError(
            f"--{role} track '{track_name}' must be type '{expected}' (it is '{actual}')"
        )
    return data


def set_route(project_dir: Path, from_track: str, to_track: str) -> dict[str, Any]:
    _require_type(project_dir, from_track, "midi", "from")
    _require_type(project_dir, to_track, "sampler", "to")

    data = tracks_mod.load(project_dir, from_track)
    routes = data.setdefault("routes", [])
    if to_track not in routes:
        routes.append(to_track)
    tracks_mod.save(project_dir, from_track, data)
    return {"from": from_track, "to": to_track}


def remove_route(project_dir: Path, from_track: str, to_track: str) -> dict[str, Any]:
    if not tracks_mod.exists(project_dir, from_track):
        raise NotFoundError(f"track '{from_track}' not found")

    data = tracks_mod.load(project_dir, from_track)
    routes = data.get("routes", [])
    if to_track not in routes:
        raise NotFoundError(f"no route from '{from_track}' to '{to_track}'")
    routes.remove(to_track)
    tracks_mod.save(project_dir, from_track, data)
    return {"removed": {"from": from_track, "to": to_track}}


def list_routes(project_dir: Path) -> list[dict[str, Any]]:
    out = []
    for toml_path in sorted(paths.tracks_dir(project_dir).glob("*.toml")):
        name = toml_path.stem
        data = tracks_mod.load(project_dir, name)
        if data.get("track", {}).get("type") != "midi":
            continue
        for to_track in data.get("routes", []):
            out.append({"from": name, "to": to_track})
    return out
