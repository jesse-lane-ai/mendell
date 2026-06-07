"""Built-in FX parameter schema — types, ranges/choices, and defaults.

Shared by the mixer (validating `mix fx add/set`) and the DSP processors
(reading resolved parameter values at render time).
"""

from __future__ import annotations

from typing import Any

from ..errors import BadInputError

# name -> {type, range: (lo, hi) | None, choices: (...) | None, default}
FX_SCHEMA: dict[str, dict[str, dict[str, Any]]] = {
    "reverb": {
        "room": {"type": float, "range": (0.0, 1.0), "default": 0.5},
        "damping": {"type": float, "range": (0.0, 1.0), "default": 0.5},
        "wet": {"type": float, "range": (0.0, 1.0), "default": 0.3},
    },
    "delay": {
        "time": {"type": float, "range": (0.0, None), "default": 0.5},
        "feedback": {"type": float, "range": (0.0, 1.0), "default": 0.3},
        "wet": {"type": float, "range": (0.0, 1.0), "default": 0.3},
    },
    "compressor": {
        "threshold": {"type": float, "range": (None, 0.0), "default": -20.0},
        "ratio": {"type": float, "range": (1.0, 20.0), "default": 4.0},
        "attack": {"type": float, "range": (0.0, None), "default": 10.0},
        "release": {"type": float, "range": (0.0, None), "default": 100.0},
    },
    "eq": {
        "low_shelf": {"type": float, "range": (None, None), "default": 0.0},
        "mid_freq": {"type": float, "range": (20.0, 20000.0), "default": 1000.0},
        "mid_gain": {"type": float, "range": (None, None), "default": 0.0},
        "high_shelf": {"type": float, "range": (None, None), "default": 0.0},
    },
    "chorus": {
        "rate": {"type": float, "range": (0.0, None), "default": 1.0},
        "depth": {"type": float, "range": (0.0, 1.0), "default": 0.3},
        "wet": {"type": float, "range": (0.0, 1.0), "default": 0.3},
    },
    "bitcrusher": {
        "bits": {"type": int, "range": (1, 16), "default": 8},
        "rate_reduction": {"type": int, "range": (1, 16), "default": 1},
    },
    "filter": {
        "type": {"type": str, "choices": ("lp", "hp", "bp"), "default": "lp"},
        "cutoff": {"type": float, "range": (20.0, 20000.0), "default": 1000.0},
        "resonance": {"type": float, "range": (0.0, 1.0), "default": 0.3},
    },
    "limiter": {
        "ceiling": {"type": float, "range": (None, 0.0), "default": -0.3},
        "lookahead": {"type": float, "range": (0.0, None), "default": 5.0},
    },
}

FX_TYPES = tuple(FX_SCHEMA.keys())


def defaults(fx_type: str) -> dict[str, Any]:
    return {name: spec["default"] for name, spec in FX_SCHEMA[fx_type].items()}


def validate_type(fx_type: str) -> str:
    if fx_type not in FX_SCHEMA:
        raise BadInputError(f"unknown FX type '{fx_type}' (expected one of {FX_TYPES})")
    return fx_type


def coerce_param(fx_type: str, name: str, value: Any) -> Any:
    schema = FX_SCHEMA[fx_type]
    if name not in schema:
        raise BadInputError(f"'{name}' is not a parameter of '{fx_type}' FX")
    spec = schema[name]

    if spec["type"] is str:
        choices = spec.get("choices")
        if choices and value not in choices:
            raise BadInputError(f"'{name}' must be one of {choices}, got '{value}'")
        return str(value)

    value = spec["type"](value)
    lo, hi = spec.get("range", (None, None))
    if lo is not None and value < lo:
        raise BadInputError(f"'{name}' must be >= {lo}, got {value}")
    if hi is not None and value > hi:
        raise BadInputError(f"'{name}' must be <= {hi}, got {value}")
    return value


def coerce_params(fx_type: str, params: dict[str, Any]) -> dict[str, Any]:
    return {name: coerce_param(fx_type, name, value) for name, value in params.items() if value is not None}
