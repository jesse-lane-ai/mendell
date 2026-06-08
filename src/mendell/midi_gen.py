"""`mendell midi generate` — write a starter drum pattern from a style preset
straight onto a MIDI track as a clip.

Mendell deliberately has no step sequencer (MIDI clips come exclusively from
imported `.mid` files) — this collapses the "write a pattern by hand in a DAW,
export it, import it" loop into one command. Patterns use General MIDI
percussion notes, the same convention `kit load` maps one-shot filenames onto
and `beat new` seeds its starter patterns with, so a generated pattern, a
loaded kit, and an export all line up out of the box.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mido

from . import clips as clips_mod
from .errors import BadInputError

PATTERN_PPQ = 480
BEATS_PER_BAR = 4.0

_GM_KICK, _GM_SNARE, _GM_CLAP, _GM_RIM = 36, 38, 39, 37
_GM_CLOSED_HAT, _GM_OPEN_HAT = 42, 46

# Each pattern is a flat list of (beat_offset, note, velocity, length_beats)
# events for one 4/4 bar; `generate()` tiles it across `--bars` bars.
DRUM_PATTERNS: dict[str, list[tuple[float, int, int, float]]] = {
    "boom-bap": [
        (0.0, _GM_KICK, 110, 0.5), (2.75, _GM_KICK, 90, 0.5),
        (1.0, _GM_SNARE, 105, 0.5), (3.0, _GM_SNARE, 105, 0.5),
        (1.75, _GM_RIM, 80, 0.3),
        (0.0, _GM_CLOSED_HAT, 70, 0.4), (0.5, _GM_CLOSED_HAT, 55, 0.4),
        (1.0, _GM_CLOSED_HAT, 70, 0.4), (1.5, _GM_CLOSED_HAT, 55, 0.4),
        (2.0, _GM_CLOSED_HAT, 70, 0.4), (2.5, _GM_CLOSED_HAT, 55, 0.4),
        (3.0, _GM_CLOSED_HAT, 70, 0.4), (3.5, _GM_CLOSED_HAT, 55, 0.4),
    ],
    "lofi": [
        (0.0, _GM_KICK, 95, 0.5), (2.5, _GM_KICK, 80, 0.5),
        (1.0, _GM_SNARE, 88, 0.5), (3.0, _GM_SNARE, 88, 0.5),
        (0.0, _GM_CLOSED_HAT, 55, 0.4), (0.5, _GM_CLOSED_HAT, 42, 0.4),
        (1.0, _GM_CLOSED_HAT, 55, 0.4), (1.5, _GM_CLOSED_HAT, 42, 0.4),
        (2.0, _GM_CLOSED_HAT, 55, 0.4), (2.5, _GM_CLOSED_HAT, 42, 0.4),
        (3.0, _GM_CLOSED_HAT, 55, 0.4), (3.5, _GM_CLOSED_HAT, 42, 0.4),
    ],
    "trap": [
        (0.0, _GM_KICK, 120, 0.5), (1.5, _GM_KICK, 100, 0.5), (2.75, _GM_KICK, 110, 0.5),
        (2.0, _GM_CLAP, 110, 0.5),
        (3.5, _GM_OPEN_HAT, 75, 0.5),
        (0.0, _GM_CLOSED_HAT, 80, 0.2), (0.25, _GM_CLOSED_HAT, 60, 0.2),
        (0.5, _GM_CLOSED_HAT, 80, 0.2), (0.625, _GM_CLOSED_HAT, 50, 0.2), (0.75, _GM_CLOSED_HAT, 65, 0.2),
        (1.0, _GM_CLOSED_HAT, 80, 0.2), (1.25, _GM_CLOSED_HAT, 60, 0.2),
        (1.5, _GM_CLOSED_HAT, 80, 0.2), (1.75, _GM_CLOSED_HAT, 65, 0.2),
        (2.0, _GM_CLOSED_HAT, 80, 0.2), (2.25, _GM_CLOSED_HAT, 60, 0.2), (2.375, _GM_CLOSED_HAT, 50, 0.2),
        (2.5, _GM_CLOSED_HAT, 80, 0.2), (2.75, _GM_CLOSED_HAT, 65, 0.2),
        (3.0, _GM_CLOSED_HAT, 80, 0.2), (3.25, _GM_CLOSED_HAT, 60, 0.2),
        (3.5, _GM_CLOSED_HAT, 80, 0.2), (3.625, _GM_CLOSED_HAT, 50, 0.2), (3.75, _GM_CLOSED_HAT, 65, 0.2),
    ],
}

DRUM_STYLES = tuple(sorted(DRUM_PATTERNS))


def write_pattern_midi(path: Path, pattern: list[tuple[float, int, int, float]], *, bars: int = 1) -> None:
    """Render PATTERN — one 4/4 bar of (beat, note, velocity, length_beats)
    events — tiled across BARS bars to a single-track .mid file at PATTERN_PPQ
    ticks per beat."""
    bars = max(int(bars), 1)
    bar_ticks = round(BEATS_PER_BAR * PATTERN_PPQ)

    events: list[tuple[int, int, int, int]] = []  # (abs_tick, kind, note, velocity); kind 1=on, 0=off
    for rep in range(bars):
        offset = rep * bar_ticks
        for beat, note, velocity, length_beats in pattern:
            start_tick = offset + round(beat * PATTERN_PPQ)
            end_tick = max(offset + round((beat + length_beats) * PATTERN_PPQ), start_tick + 1)
            events.append((start_tick, 1, note, velocity))
            events.append((end_tick, 0, note, 0))
    events.sort(key=lambda e: (e[0], e[1]))  # note_off (0) before note_on (1) at the same tick

    mid = mido.MidiFile(ticks_per_beat=PATTERN_PPQ)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    last_tick = 0
    for abs_tick, kind, note, velocity in events:
        msg_type = "note_on" if kind == 1 else "note_off"
        track.append(mido.Message(msg_type, note=note, velocity=velocity, time=abs_tick - last_tick))
        last_tick = abs_tick

    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(path))


def generate(project_dir: Path, track_name: str, clip_name: str, *,
             style: str, bars: int = 1, loop: bool = True) -> dict[str, Any]:
    style = style.lower()
    if style not in DRUM_PATTERNS:
        raise BadInputError(f"unknown drum style '{style}' (expected one of {DRUM_STYLES})")
    bars = max(int(bars), 1)

    pattern_path = project_dir / "midi" / f"{clip_name}.mid"
    write_pattern_midi(pattern_path, DRUM_PATTERNS[style], bars=bars)

    clips_mod.import_clip(project_dir, track_name, clip_name, midi_path=str(pattern_path))
    if loop:
        clips_mod.set_params(project_dir, track_name, clip_name, loop=True)

    return {
        "clip": clip_name,
        "track": track_name,
        "style": style,
        "bars": bars,
        "loop": loop,
        "source": str(pattern_path),
    }
