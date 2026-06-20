"""Drum-kit loading — auto-map a folder of one-shot samples onto a sampler
track using General MIDI percussion note conventions.

Collapses the tedious part of the sampler + one-shot workflow: instead of
mapping each kick/snare/hat by hand (`sampler map add ... --note ...` per
file), point `kit load` at a folder and it guesses sensible note assignments
from filenames, falling back to sequential mapping for anything it doesn't
recognize.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import sampler as sampler_mod
from . import tracks as tracks_mod
from .errors import BadInputError
from .notes import midi_to_note_name, note_name_to_midi

AUDIO_EXTS = sampler_mod.AUDIO_EXTS

DEFAULT_START_NOTE = "C5"

# General MIDI percussion key map (channel 10) — first matching keyword wins,
# so more specific names (closed/open hat) must precede generic ones (hat).
_GM_DRUM_KEYWORDS: list[tuple[tuple[str, ...], int]] = [
    (("kick", "bd", "kd", "bassdrum", "bass_drum", "bass-drum", "kik", "808"), 36),  # Bass Drum 1
    (("rim", "rimshot", "rim_shot", "sidestick", "side_stick", "side-stick"), 37),  # Side Stick
    (("snare", "sd", "snr"), 38),                                               # Acoustic Snare
    (("clap", "clp"), 39),                                                      # Hand Clap
    (("closedhat", "closed_hat", "closed-hat", "hatclosed", "hat_closed", "hat-closed", "chh"), 42),  # Closed Hi-Hat
    (("openhat", "open_hat", "open-hat", "hatopen", "hat_open", "hat-open", "ohh"), 46),              # Open Hi-Hat
    (("hat", "hh", "hihat", "hi-hat", "hi_hat"), 42),                           # generic -> closed hat
    (("tom",), 45),                                                             # Low Tom
    (("crash", "cym", "cymbal"), 49),                                           # Crash Cymbal 1
    (("ride",), 51),                                                            # Ride Cymbal 1
    (("perc", "prc", "shaker", "tamb", "cowbell", "conga", "bongo", "clave"), 54),  # Tambourine
]


def _guess_note(stem: str) -> int | None:
    lowered = stem.lower()
    for keywords, note in _GM_DRUM_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return note
    return None


def _ensure_sampler_track(project_dir: Path, track_name: str) -> None:
    """Create-or-reuse a MIDI track hosting a sampler instrument (idempotent, so
    re-running `kit load` is safe). The track's own clips trigger the sampler."""
    if tracks_mod.exists(project_dir, track_name):
        ttype = tracks_mod.load(project_dir, track_name).get("track", {}).get("type")
        if ttype != "midi":
            raise BadInputError(
                f"track '{track_name}' exists but is type '{ttype}', not midi — "
                "a sampler instrument is hosted on a MIDI track"
            )
    else:
        tracks_mod.add(project_dir, track_name, "midi")
    sampler_mod.create(project_dir, track_name)


def load_kit(
    project_dir: Path, track_name: str, folder: str, *, start_note: str = DEFAULT_START_NOTE
) -> dict[str, Any]:
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise BadInputError(f"folder not found: {folder}")

    files = sorted(
        (p for p in folder_path.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS),
        key=lambda p: (str(p.parent).lower(), p.name.lower()),
    )
    if not files:
        raise BadInputError(f"no audio files found in {folder}")

    _ensure_sampler_track(project_dir, track_name)

    used_notes: set[int] = set()
    assignments: list[tuple[Path, int]] = []
    unmatched: list[Path] = []

    for file_path in files:
        note = _guess_note(file_path.stem)
        if note is not None:
            # Resolve collisions (e.g. two files both named "kick") by nudging
            # upward to the next free note rather than clobbering the slot.
            while note in used_notes and note < 127:
                note += 1
        if note is None or note in used_notes:
            unmatched.append(file_path)
            continue
        used_notes.add(note)
        assignments.append((file_path, note))

    sequential = note_name_to_midi(start_note)
    for file_path in unmatched:
        while sequential in used_notes and sequential < 127:
            sequential += 1
        if sequential in used_notes or sequential > 127:
            raise BadInputError(
                f"ran out of MIDI notes mapping '{folder}' — too many one-shots "
                f"to place sequentially from {start_note}"
            )
        used_notes.add(sequential)
        assignments.append((file_path, sequential))

    assignments.sort(key=lambda pair: pair[1])

    mapped = []
    for file_path, note in assignments:
        slot = sampler_mod.map_add(project_dir, track_name, note=midi_to_note_name(note), sample=str(file_path))
        mapped.append({"note": slot["note"], "sample": file_path.name})

    return {
        "track": track_name,
        "folder": str(folder_path.resolve()),
        "mapped": mapped,
        "count": len(mapped),
    }
