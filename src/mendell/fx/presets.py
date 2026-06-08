"""Named FX-chain presets — apply a curated chain of slots to a track in one
shot instead of building it slot-by-slot via `mix fx add`.

Each preset is an ordered list of (fx_type, params); `mixer.fx_apply_preset`
appends them via the normal `fx_add` path (so they get stable per-track ids
and validated/coerced params, exactly like a hand-built chain).
"""

from __future__ import annotations

from typing import Any

FX_PRESETS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "lofi-vinyl": [
        ("filter", {"type": "lp", "cutoff": 4000.0, "resonance": 0.2}),
        ("bitcrusher", {"bits": 10, "rate_reduction": 2}),
        ("eq", {"low_shelf": 2.0, "high_shelf": -6.0}),
        ("reverb", {"room": 0.3, "damping": 0.6, "wet": 0.15}),
    ],
    "tape-warmth": [
        ("eq", {"low_shelf": 1.5, "mid_freq": 200.0, "mid_gain": 1.0, "high_shelf": -3.0}),
        ("compressor", {"threshold": -18.0, "ratio": 2.5, "attack": 15.0, "release": 120.0}),
        ("chorus", {"rate": 0.3, "depth": 0.15, "wet": 0.12}),
    ],
    "radio": [
        ("filter", {"type": "bp", "cutoff": 1800.0, "resonance": 0.4}),
        ("compressor", {"threshold": -12.0, "ratio": 6.0, "attack": 5.0, "release": 60.0}),
    ],
    "telephone": [
        ("filter", {"type": "bp", "cutoff": 1200.0, "resonance": 0.5}),
        ("bitcrusher", {"bits": 8, "rate_reduction": 3}),
    ],
    "spacious": [
        ("reverb", {"room": 0.7, "damping": 0.4, "wet": 0.35}),
        ("delay", {"time": 0.75, "feedback": 0.25, "wet": 0.2}),
    ],
    "punch": [
        ("compressor", {"threshold": -16.0, "ratio": 4.0, "attack": 3.0, "release": 80.0}),
        ("eq", {"low_shelf": 3.0, "mid_freq": 3000.0, "mid_gain": 2.0, "high_shelf": 1.0}),
    ],
}

PRESET_NAMES = tuple(sorted(FX_PRESETS))
