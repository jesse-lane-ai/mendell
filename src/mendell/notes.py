"""Scientific pitch notation <-> MIDI note number.

Per SPEC.md, middle C is **C4** (MIDI note 60): MIDI = 12*(octave+1) + index,
where index runs C=0 .. B=11. Octaves span C<n>-B<n>.
"""

from __future__ import annotations

import re

from .errors import BadInputError

_NOTE_INDEX = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
               "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}
_INDEX_NOTE = {v: k for k, v in _NOTE_INDEX.items()}

_NOTE_RE = re.compile(r"^([A-Ga-g])(#?)(-?\d+)$")


def note_name_to_midi(name: str) -> int:
    m = _NOTE_RE.match(name.strip())
    if not m:
        raise BadInputError(f"invalid note name '{name}' (expected scientific pitch notation, e.g. 'C4')")

    letter, sharp, octave = m.group(1).upper(), m.group(2), int(m.group(3))
    key = letter + sharp
    if key not in _NOTE_INDEX:
        raise BadInputError(f"invalid note name '{name}'")

    midi = 12 * (octave + 1) + _NOTE_INDEX[key]
    if not (0 <= midi <= 127):
        raise BadInputError(f"note '{name}' is out of MIDI range (0-127)")
    return midi


def midi_to_note_name(midi: int) -> str:
    if not (0 <= midi <= 127):
        raise BadInputError(f"MIDI note {midi} out of range (0-127)")
    octave = midi // 12 - 1
    index = midi % 12
    return f"{_INDEX_NOTE[index]}{octave}"


def parse_note_range(text: str) -> tuple[int, int]:
    """Parse a "C4-B5"-style range into (low_midi, high_midi)."""
    parts = text.split("-")
    # Handle ranges where the low note itself contains a '-' for negative
    # octaves, e.g. "C-1-G0" -> ["C", "1", "G0"].
    if len(parts) == 3:
        low_str, high_str = f"{parts[0]}-{parts[1]}", parts[2]
    elif len(parts) == 2:
        low_str, high_str = parts
    else:
        raise BadInputError(f"invalid note range '{text}' (expected e.g. 'C4-B5')")

    low, high = note_name_to_midi(low_str), note_name_to_midi(high_str)
    if low > high:
        raise BadInputError(f"invalid note range '{text}' (low note is above high note)")
    return low, high
