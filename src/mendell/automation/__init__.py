"""Automation — (bar.beat, value, curve) point lists for any numeric param.

Per SPEC.md, each entity owns and stores the automation for its own
parameters: track/mixer/FX automation lives in the track's TOML, and clip
automation (`clip.<name>.<param>`) lives in that clip's TOML. `mendell auto`
commands always address automation through the track and route writes to the
correct file transparently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import clips as clips_mod
from .. import tracks as tracks_mod
from ..errors import BadInputError, NotFoundError
from ..fx import schema as fx_schema
from ..timing import parse_bar_beat

CURVES = ("linear", "ease-in", "ease-out", "ease-in-out", "step")
DEFAULT_CURVE = "linear"

TRACK_PARAMS = {"vol", "pan", "mute"}
CLIP_PARAMS = {"gain", "pitch", "warp"}
ZERO_OR_ONE_PARAMS = {"mute", "warp"}


@dataclass(frozen=True)
class ParamRef:
    kind: str          # "track" | "clip" | "fx"
    full: str          # the param string as the agent wrote it, e.g. "fx.0.wet"
    sub_param: str     # the leaf parameter name, e.g. "wet", "gain", "vol"
    clip_name: str | None = None
    fx_id: int | None = None


def parse_param_ref(project_dir: Path, track_name: str, param: str) -> ParamRef:
    parts = param.split(".")

    if parts[0] == "clip":
        if len(parts) != 3:
            raise BadInputError(f"invalid clip automation target '{param}' (expected 'clip.<name>.<param>')")
        _, clip_name, sub_param = parts
        if sub_param not in CLIP_PARAMS:
            raise BadInputError(
                f"'{sub_param}' is not an automatable clip parameter (expected one of {sorted(CLIP_PARAMS)})"
            )
        clip_data = clips_mod.load(project_dir, clip_name)
        if clip_data.get("clip", {}).get("track") != track_name:
            raise BadInputError(f"clip '{clip_name}' is not on track '{track_name}'")
        return ParamRef(kind="clip", full=param, sub_param=sub_param, clip_name=clip_name)

    if parts[0] == "fx":
        if len(parts) != 3:
            raise BadInputError(f"invalid FX automation target '{param}' (expected 'fx.<id>.<param>')")
        _, fx_id_str, sub_param = parts
        try:
            fx_id = int(fx_id_str)
        except ValueError:
            raise BadInputError(f"invalid FX id '{fx_id_str}' in '{param}'")
        track_data = tracks_mod.load(project_dir, track_name)
        slot = next((s for s in track_data.get("fx", []) if s["id"] == fx_id), None)
        if slot is None:
            raise NotFoundError(f"no FX slot with id {fx_id} on track '{track_name}'")
        fx_schema.coerce_param(slot["type"], sub_param, slot["params"].get(sub_param, 0))
        return ParamRef(kind="fx", full=param, sub_param=sub_param, fx_id=fx_id)

    if parts[0] == "send":
        if len(parts) != 2:
            raise BadInputError(f"invalid send automation target '{param}' (expected 'send.<fx-name>')")
        return ParamRef(kind="track", full=param, sub_param=param)

    if param in TRACK_PARAMS:
        return ParamRef(kind="track", full=param, sub_param=param)

    raise BadInputError(f"'{param}' is not an automatable parameter")


def _validate_curve(curve: str) -> str:
    if curve not in CURVES:
        raise BadInputError(f"invalid curve '{curve}' (expected one of {CURVES})")
    return curve


def _validate_value(sub_param: str, value: float) -> float:
    if sub_param in ZERO_OR_ONE_PARAMS and value not in (0, 1):
        raise BadInputError(f"'{sub_param}' automation value must be 0 or 1, got {value}")
    return float(value)


# ---------------------------------------------------------------------------
# storage helpers — read/write the (param, points) lists on the owning entity
# ---------------------------------------------------------------------------

def _track_points(project_dir: Path, track_name: str, full_param: str) -> tuple[dict, list]:
    data = tracks_mod.load(project_dir, track_name)
    automation = data.setdefault("automation", [])
    entry = next((e for e in automation if e["param"] == full_param), None)
    if entry is None:
        entry = {"param": full_param, "points": []}
        automation.append(entry)
    return data, entry["points"]


def _clip_points(project_dir: Path, clip_name: str, sub_param: str) -> tuple[dict, list]:
    data = clips_mod.load(project_dir, clip_name)
    automation = data.setdefault("automation", [])
    entry = next((e for e in automation if e["param"] == sub_param), None)
    if entry is None:
        entry = {"param": sub_param, "points": []}
        automation.append(entry)
    return data, entry["points"]


def _save(project_dir: Path, ref: ParamRef, track_name: str, data: dict[str, Any]) -> None:
    if ref.kind == "clip":
        clips_mod.save(project_dir, ref.clip_name, data)
    else:
        tracks_mod.save(project_dir, track_name, data)


# ---------------------------------------------------------------------------
# add / list / remove / clear
# ---------------------------------------------------------------------------

def add(
    project_dir: Path, track_name: str, param: str, *, at: str | float, value: float,
    curve: str = DEFAULT_CURVE,
) -> dict[str, Any]:
    ref = parse_param_ref(project_dir, track_name, param)
    curve = _validate_curve(curve)
    value = _validate_value(ref.sub_param, value)
    bb = parse_bar_beat(at)
    at_str = str(bb)

    if ref.kind == "clip":
        data, points = _clip_points(project_dir, ref.clip_name, ref.sub_param)
    else:
        data, points = _track_points(project_dir, track_name, ref.full)

    points[:] = [p for p in points if p["at"] != at_str]
    points.append({"at": at_str, "value": value, "curve": curve})
    points.sort(key=lambda p: (lambda bb: (bb.bar, bb.beat))(parse_bar_beat(p["at"])))

    _save(project_dir, ref, track_name, data)
    return {"track": track_name, "param": param, "at": at_str, "value": value, "curve": curve}


# ---------------------------------------------------------------------------
# curve interpolation (used by the export engine to resolve values over time)
# ---------------------------------------------------------------------------

def _ease_in(t: float) -> float:
    return t * t


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) * (1.0 - t)


def _ease_in_out(t: float) -> float:
    return 3 * t * t - 2 * t * t * t


_CURVE_FNS = {
    "linear": lambda t: t,
    "ease-in": _ease_in,
    "ease-out": _ease_out,
    "ease-in-out": _ease_in_out,
}


def interpolate(
    points: list[dict[str, Any]], frame: int, *, bpm: float, sample_rate: int, beats_per_bar: int
) -> float | None:
    """Resolve a parameter's value at a given sample-frame position.

    Each point's `at` (bar.beat) is converted to frames — the canonical unit —
    via the timing engine, then the sorted list is scanned and the active
    segment's curve is applied. Returns None if there are no points (caller
    should fall back to the entity's static parameter value).
    """
    if not points:
        return None

    from ..timing import bar_beat_to_frames  # local import avoids a cycle at module load

    resolved = []
    for p in points:
        bb = parse_bar_beat(p["at"])
        pos = bar_beat_to_frames(bb.bar, bb.beat, bpm, sample_rate, beats_per_bar)
        resolved.append((pos, p["value"], p.get("curve", DEFAULT_CURVE)))
    resolved.sort(key=lambda r: r[0])

    if frame <= resolved[0][0]:
        return resolved[0][1]
    if frame >= resolved[-1][0]:
        return resolved[-1][1]

    for (pos_a, val_a, _), (pos_b, val_b, curve_b) in zip(resolved, resolved[1:]):
        if pos_a <= frame <= pos_b:
            if curve_b == "step" or pos_b == pos_a:
                return val_a
            t = (frame - pos_a) / (pos_b - pos_a)
            shaped = _CURVE_FNS.get(curve_b, _CURVE_FNS["linear"])(t)
            return val_a + (val_b - val_a) * shaped

    return resolved[-1][1]


def _gather(project_dir: Path, track_name: str, param: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    track_data = tracks_mod.load(project_dir, track_name)
    for entry in track_data.get("automation", []):
        if param is None or entry["param"] == param:
            out.append({"param": entry["param"], "points": entry["points"]})

    for clip_name in track_data.get("clips", []):
        if not clips_mod.exists(project_dir, clip_name):
            continue
        clip_data = clips_mod.load(project_dir, clip_name)
        for entry in clip_data.get("automation", []):
            full = f"clip.{clip_name}.{entry['param']}"
            if param is None or full == param:
                out.append({"param": full, "points": entry["points"]})

    return out


def list_points(project_dir: Path, track_name: str, param: str | None = None) -> list[dict[str, Any]]:
    return _gather(project_dir, track_name, param)


def remove(project_dir: Path, track_name: str, param: str, *, at: str | float) -> dict[str, Any]:
    ref = parse_param_ref(project_dir, track_name, param)
    at_str = str(parse_bar_beat(at))

    if ref.kind == "clip":
        data, points = _clip_points(project_dir, ref.clip_name, ref.sub_param)
    else:
        data, points = _track_points(project_dir, track_name, ref.full)

    remaining = [p for p in points if p["at"] != at_str]
    if len(remaining) == len(points):
        raise NotFoundError(f"no automation point for '{param}' at {at_str}")
    points[:] = remaining

    _save(project_dir, ref, track_name, data)
    return {"track": track_name, "param": param, "removed_at": at_str}


def clear(project_dir: Path, track_name: str, param: str | None = None) -> dict[str, Any]:
    cleared: list[str] = []

    track_data = tracks_mod.load(project_dir, track_name)
    automation = track_data.get("automation", [])
    if param is None:
        cleared.extend(e["param"] for e in automation)
        track_data["automation"] = []
    else:
        remaining = [e for e in automation if e["param"] != param]
        if len(remaining) != len(automation):
            cleared.append(param)
        track_data["automation"] = remaining
    tracks_mod.save(project_dir, track_name, track_data)

    for clip_name in track_data.get("clips", []):
        if not clips_mod.exists(project_dir, clip_name):
            continue
        clip_data = clips_mod.load(project_dir, clip_name)
        clip_automation = clip_data.get("automation", [])
        if param is None:
            cleared.extend(f"clip.{clip_name}.{e['param']}" for e in clip_automation)
            clip_data["automation"] = []
            clips_mod.save(project_dir, clip_name, clip_data)
        elif param.startswith(f"clip.{clip_name}."):
            sub = param.split(".", 2)[2]
            remaining = [e for e in clip_automation if e["param"] != sub]
            if len(remaining) != len(clip_automation):
                cleared.append(param)
                clip_data["automation"] = remaining
                clips_mod.save(project_dir, clip_name, clip_data)

    return {"track": track_name, "cleared": cleared}
