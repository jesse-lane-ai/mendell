"""Shared shapes for the recognizer seam: ``FileProbe`` in, ``Recognition`` out.

Two-axis taxonomy on top of the existing ``kind`` (one-shot / loop / unknown):

  * ``category``    — single coarse role, vocabulary depends on ``kind``
                       (see ``ONESHOT_CATEGORIES`` / ``LOOP_CATEGORIES``).
  * ``instruments`` — 0..N instrument tags from ``INSTRUMENT_VOCAB``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Coarse category vocabulary, split by `kind` — a recognizer picks from the
# set matching the file's already-known `kind` (one-shot vs loop). `unknown`
# kind files are treated as one-shots for category purposes (the safer,
# smaller vocabulary), since a recognizer's `category` is only used on the
# fallback path anyway (see `library._fuse_category`).
ONESHOT_CATEGORIES: tuple[str, ...] = (
    "kick", "snare", "clap", "hat", "tom", "crash", "ride", "rim", "perc",
    "bass", "808", "stab", "vocal", "fx", "melody",
)
LOOP_CATEGORIES: tuple[str, ...] = (
    "drum", "perc", "bass", "melodic", "chord", "vocal", "fx", "full",
)

# Multi-valued instrument vocabulary — shared across one-shots and loops.
INSTRUMENT_VOCAB: tuple[str, ...] = (
    "drums", "bass", "piano", "keys", "guitar", "strings", "brass", "synth",
    "vocal", "fx",
)


@dataclass(frozen=True)
class FileProbe:
    """Already-computed per-file facts handed to a recognizer — backends must
    not re-probe duration/kind themselves."""

    path: Path
    filename: str
    duration: float | None
    kind: str  # "one-shot" | "loop" | "unknown"


@dataclass(frozen=True)
class Recognition:
    """One backend's verdict for a single file."""

    category: str
    instruments: list[str]
    source: str  # "heuristic" | "clap" | "gemini-embedding" | "gemini-generative"
    confidence: float  # 0..1


class Recognizer(Protocol):
    """A pluggable content-based recognition backend.

    Batch-first: ``recognize`` takes the whole folder's probes at once so
    model backends can amortize load/round-trips. Returning ``None`` for an
    item means "defer to the filename guess" (e.g. the backend couldn't form
    an opinion for that file).
    """

    name: str

    def recognize(self, items: list[FileProbe]) -> list[Recognition | None]: ...
