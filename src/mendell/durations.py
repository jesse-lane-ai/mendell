"""Parsing for short duration strings used throughout the spec, e.g. "5ms",
"100ms", "-10ms", "1.5s" — always normalized to milliseconds (float)."""

from __future__ import annotations

import re

from .errors import BadInputError

_DURATION_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(ms|s)$", re.I)


def parse_duration_ms(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    m = _DURATION_RE.match(text)
    if not m:
        raise BadInputError(f"invalid duration '{value}' (expected e.g. '5ms' or '1.5s')")

    amount, unit = float(m.group(1)), m.group(2).lower()
    return amount * 1000.0 if unit == "s" else amount


def format_duration_ms(ms: float) -> str:
    if ms != 0 and abs(ms) >= 1000 and ms % 1000 == 0:
        return f"{int(ms // 1000)}s"
    if float(ms).is_integer():
        return f"{int(ms)}ms"
    return f"{ms}ms"
