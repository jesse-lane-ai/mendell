"""Mixer — per-track vol/pan/mute/solo and FX chain (stable per-track ids).

FX slots get a monotonically increasing id from a per-track counter that
never repeats and is never reused (see SPEC.md "Mixer" — FX chain). This
keeps `fx.<id>.<param>` automation references valid for the slot's lifetime,
even after other slots are removed. Processing order follows insertion order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import paths
from .. import tracks as tracks_mod
from ..errors import BadInputError, NotFoundError
from ..fx import schema as fx_schema


# ---------------------------------------------------------------------------
# vol / pan / mute / solo
# ---------------------------------------------------------------------------

def set_mixer(project_dir: Path, track_name: str, *, vol: int | None = None,
              pan: int | None = None) -> dict[str, Any]:
    data = tracks_mod.load(project_dir, track_name)
    mixer = data.setdefault("mixer", dict(tracks_mod.MIXER_DEFAULTS))

    changed: dict[str, Any] = {}
    if vol is not None:
        vol = int(vol)
        if not (0 <= vol <= 100):
            raise BadInputError(f"vol must be 0-100, got {vol}")
        mixer["vol"] = vol
        changed["vol"] = vol
    if pan is not None:
        pan = int(pan)
        if not (-100 <= pan <= 100):
            raise BadInputError(f"pan must be -100..100, got {pan}")
        mixer["pan"] = pan
        changed["pan"] = pan

    tracks_mod.save(project_dir, track_name, data)
    return {"changed": changed, **tracks_mod.summary(project_dir, track_name)}


def set_mute(project_dir: Path, track_name: str, *, on: bool = True) -> dict[str, Any]:
    data = tracks_mod.load(project_dir, track_name)
    mixer = data.setdefault("mixer", dict(tracks_mod.MIXER_DEFAULTS))
    mixer["mute"] = bool(on)
    tracks_mod.save(project_dir, track_name, data)
    return tracks_mod.summary(project_dir, track_name)


def set_solo(project_dir: Path, track_name: str, *, on: bool = True) -> dict[str, Any]:
    data = tracks_mod.load(project_dir, track_name)
    mixer = data.setdefault("mixer", dict(tracks_mod.MIXER_DEFAULTS))
    mixer["solo"] = bool(on)
    tracks_mod.save(project_dir, track_name, data)
    return tracks_mod.summary(project_dir, track_name)


def show(project_dir: Path) -> list[dict[str, Any]]:
    return tracks_mod.list_tracks(project_dir)


# ---------------------------------------------------------------------------
# FX chain — stable per-track ids
# ---------------------------------------------------------------------------

def _find_fx(chain: list[dict[str, Any]], fx_id: int) -> dict[str, Any] | None:
    for slot in chain:
        if slot["id"] == fx_id:
            return slot
    return None


def fx_add(project_dir: Path, track_name: str, fx_type: str, **params: Any) -> dict[str, Any]:
    fx_schema.validate_type(fx_type)
    data = tracks_mod.load(project_dir, track_name)
    chain = data.setdefault("fx", [])

    next_id = data.get("fx_next_id", 0)
    resolved = {**fx_schema.defaults(fx_type), **fx_schema.coerce_params(fx_type, params)}
    slot = {"id": next_id, "type": fx_type, "params": resolved}
    chain.append(slot)
    data["fx_next_id"] = next_id + 1

    tracks_mod.save(project_dir, track_name, data)
    return {"id": slot["id"], "type": slot["type"], "params": slot["params"]}


def fx_set(project_dir: Path, track_name: str, fx_id: int, **params: Any) -> dict[str, Any]:
    data = tracks_mod.load(project_dir, track_name)
    chain = data.get("fx", [])
    slot = _find_fx(chain, fx_id)
    if slot is None:
        raise NotFoundError(f"no FX slot with id {fx_id} on track '{track_name}'")

    coerced = fx_schema.coerce_params(slot["type"], params)
    slot["params"].update(coerced)

    tracks_mod.save(project_dir, track_name, data)
    return {"id": slot["id"], "type": slot["type"], "params": slot["params"], "changed": coerced}


def fx_remove(project_dir: Path, track_name: str, fx_id: int) -> dict[str, Any]:
    data = tracks_mod.load(project_dir, track_name)
    chain = data.get("fx", [])
    slot = _find_fx(chain, fx_id)
    if slot is None:
        raise NotFoundError(f"no FX slot with id {fx_id} on track '{track_name}'")
    chain.remove(slot)

    tracks_mod.save(project_dir, track_name, data)
    return {"removed": fx_id, "track": track_name}


def fx_list(project_dir: Path, track_name: str) -> list[dict[str, Any]]:
    data = tracks_mod.load(project_dir, track_name)
    return [{"id": s["id"], "type": s["type"], "params": s["params"]} for s in data.get("fx", [])]
