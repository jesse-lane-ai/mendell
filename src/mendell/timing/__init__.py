"""Unified timing engine — single source of truth for all time conversions.

Every time position in Mendell ultimately resolves to sample frames, the
canonical internal unit (see SPEC.md "Timing Engine"). This module implements
the conversions between the four representations:

    bar.beat  <->  seconds  <->  sample frames  <->  MIDI ticks

Beats are 1-indexed: beat 1 is the first beat of a bar, so bar 1 beat 1 sits
at frame 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import BadInputError


@dataclass(frozen=True)
class BarBeat:
    bar: int
    beat: float

    def __str__(self) -> str:
        # Render an integral beat without a trailing ".0" (e.g. "5.3", not "5.3.0").
        if float(self.beat).is_integer():
            return f"{self.bar}.{int(self.beat)}"
        return f"{self.bar}.{self.beat}"


def parse_time_sig(time_sig: str) -> tuple[int, int]:
    """Parse a "4/4"-style time signature into (beats_per_bar, beat_unit)."""
    try:
        num, den = time_sig.split("/")
        beats_per_bar, beat_unit = int(num), int(den)
        if beats_per_bar <= 0 or beat_unit <= 0:
            raise ValueError
    except ValueError:
        raise BadInputError(f"invalid time signature '{time_sig}' (expected e.g. '4/4')")
    return beats_per_bar, beat_unit


def parse_bar_beat(value: str | int | float) -> BarBeat:
    """Parse user-facing ``bar.beat`` notation, e.g. "5.3" -> bar 5, beat 3.

    The bar and beat are separate 1-indexed integer components separated by a
    literal '.', *not* a decimal fraction — "2.10" means bar 2 beat 10, not
    bar 2.1. A bare bar number ("5") implies beat 1.
    """
    text = str(value).strip()
    if not text:
        raise BadInputError("empty bar.beat value")

    parts = text.split(".")
    if len(parts) == 1:
        bar_str, beat_str = parts[0], "1"
    elif len(parts) == 2:
        bar_str, beat_str = parts
    else:
        raise BadInputError(f"invalid bar.beat value '{value}' (expected e.g. '5.3')")

    try:
        bar = int(bar_str)
        beat = float(beat_str) if beat_str else 1.0
    except ValueError:
        raise BadInputError(f"invalid bar.beat value '{value}' (expected e.g. '5.3')")

    if bar < 1:
        raise BadInputError(f"bar must be >= 1, got {bar}")
    if beat < 1:
        raise BadInputError(f"beat must be >= 1, got {beat}")

    return BarBeat(bar=bar, beat=beat)


def seconds_per_beat(bpm: float) -> float:
    return 60.0 / bpm


def seconds_per_bar(bpm: float, beats_per_bar: int) -> float:
    return seconds_per_beat(bpm) * beats_per_bar


def frames_per_beat(bpm: float, sample_rate: int) -> float:
    return seconds_per_beat(bpm) * sample_rate


def frames_per_bar(bpm: float, sample_rate: int, beats_per_bar: int) -> float:
    return seconds_per_bar(bpm, beats_per_bar) * sample_rate


def bar_beat_to_seconds(bar: int, beat: float, bpm: float, beats_per_bar: int) -> float:
    return ((bar - 1) * beats_per_bar + (beat - 1)) * seconds_per_beat(bpm)


def bar_beat_to_frames(
    bar: int, beat: float, bpm: float, sample_rate: int, beats_per_bar: int
) -> int:
    frames = ((bar - 1) * beats_per_bar + (beat - 1)) * frames_per_beat(bpm, sample_rate)
    return round(frames)


def seconds_to_frames(seconds: float, sample_rate: int) -> int:
    return round(seconds * sample_rate)


def frames_to_seconds(frames: int, sample_rate: int) -> float:
    return frames / sample_rate


def frames_to_bar_beat(
    frames: int, bpm: float, sample_rate: int, beats_per_bar: int
) -> BarBeat:
    fpb = frames_per_beat(bpm, sample_rate)
    total_beats = frames / fpb
    bar = int(total_beats // beats_per_bar) + 1
    beat = (total_beats % beats_per_bar) + 1
    return BarBeat(bar=bar, beat=beat)


def seconds_to_bar_beat(seconds: float, bpm: float, beats_per_bar: int) -> BarBeat:
    spb = seconds_per_beat(bpm)
    total_beats = seconds / spb
    bar = int(total_beats // beats_per_bar) + 1
    beat = (total_beats % beats_per_bar) + 1
    return BarBeat(bar=bar, beat=beat)


def ticks_to_seconds(ticks: int, ppq: int, bpm: float) -> float:
    """MIDI ticks -> seconds, per SPEC.md: seconds = ticks / (ppq * (bpm / 60))"""
    return ticks / (ppq * (bpm / 60.0))


def ticks_to_frames(ticks: int, ppq: int, bpm: float, sample_rate: int) -> int:
    return seconds_to_frames(ticks_to_seconds(ticks, ppq, bpm), sample_rate)


@dataclass(frozen=True)
class TimingResult:
    bar: int
    beat: float
    seconds: float
    frames: int
    ms: float

    def to_dict(self) -> dict:
        return {
            "bar": self.bar,
            "beat": self.beat,
            "seconds": round(self.seconds, 6),
            "frames": self.frames,
            "ms": round(self.ms, 3),
        }


def resolve_timing(
    *,
    bpm: float,
    sample_rate: int,
    beats_per_bar: int,
    bar: int | None = None,
    beat: float | None = None,
    seconds: float | None = None,
    frames: int | None = None,
) -> TimingResult:
    """Resolve a position given in any one representation into all four.

    Exactly one of (bar[, beat]), seconds, or frames should be supplied.
    """
    given = [v is not None for v in (bar, seconds, frames)]
    if sum(given) != 1:
        raise BadInputError("provide exactly one of --bar/--beat, --seconds, or --frames")

    if bar is not None:
        b = beat if beat is not None else 1.0
        secs = bar_beat_to_seconds(bar, b, bpm, beats_per_bar)
        frm = seconds_to_frames(secs, sample_rate)
        bb = BarBeat(bar=bar, beat=b)
    elif seconds is not None:
        secs = seconds
        frm = seconds_to_frames(secs, sample_rate)
        bb = seconds_to_bar_beat(secs, bpm, beats_per_bar)
    else:
        frm = frames
        secs = frames_to_seconds(frames, sample_rate)
        bb = frames_to_bar_beat(frames, bpm, sample_rate, beats_per_bar)

    return TimingResult(bar=bb.bar, beat=bb.beat, seconds=secs, frames=frm, ms=secs * 1000.0)
