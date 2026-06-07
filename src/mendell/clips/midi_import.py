"""MIDI clip import — parse a .mid file into a flat list of note events.

Per SPEC.md, note positions are resolved onto the *project's* tempo grid:
ticks are converted to frames using the project BPM (not the file's own
tempo), so a clip's musical position in beats is what's preserved on import,
and stays locked to the project's beat grid thereafter.
"""

from __future__ import annotations

from typing import Any

import mido

from ..errors import BadInputError
from ..timing import frames_per_beat


def load_midi_file(path: str) -> mido.MidiFile:
    try:
        return mido.MidiFile(path)
    except (OSError, EOFError, ValueError) as err:
        raise BadInputError(f"could not read MIDI file '{path}': {err}")


def extract_notes(midi_file: mido.MidiFile, track_index: int) -> list[dict[str, Any]]:
    """Extract (note, velocity, channel, start_tick, duration_tick) events from
    one track of a parsed MIDI file, pairing note_on/note_off messages."""
    if track_index < 0 or track_index >= len(midi_file.tracks):
        raise BadInputError(
            f"MIDI file has {len(midi_file.tracks)} track(s); "
            f"--midi-track {track_index} is out of range"
        )

    track = midi_file.tracks[track_index]

    notes: list[dict[str, Any]] = []
    open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    abs_tick = 0

    for msg in track:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            key = (msg.channel, msg.note)
            open_notes.setdefault(key, []).append((abs_tick, msg.velocity))
        elif msg.type in ("note_off", "note_on"):
            # note_on with velocity 0 behaves as note_off
            key = (msg.channel, msg.note)
            stack = open_notes.get(key)
            if stack:
                start_tick, velocity = stack.pop(0)
                notes.append({
                    "note": msg.note,
                    "velocity": velocity,
                    "channel": msg.channel,
                    "start_tick": start_tick,
                    "duration_tick": max(abs_tick - start_tick, 1),
                })

    notes.sort(key=lambda n: (n["start_tick"], n["note"]))
    return notes


def notes_to_frames(
    notes: list[dict[str, Any]], *, ppq: int, bpm: float, sample_rate: int
) -> list[dict[str, Any]]:
    """Convert tick-based note events onto the project's frame grid."""
    fpb = frames_per_beat(bpm, sample_rate)
    ticks_to_frames_factor = fpb / ppq

    converted = []
    for n in notes:
        start_frame = round(n["start_tick"] * ticks_to_frames_factor)
        duration_frame = round(n["duration_tick"] * ticks_to_frames_factor)
        converted.append({
            "note": n["note"],
            "velocity": n["velocity"],
            "channel": n["channel"],
            "start_frame": start_frame,
            "duration_frame": max(duration_frame, 1),
        })
    return converted
