"""`mendell beat new` — scaffold a ready-to-go beat project from a style preset.

Collapses the cold-start boilerplate (project -> tracks -> sampler -> routing
-> a clip to trigger it) into a single command, seeded with style-appropriate
tempo/key defaults and a small starter MIDI drum pattern. Patterns are written
using General MIDI percussion notes — the same convention `mendell kit load`
maps one-shot filenames onto — so loading a kit afterwards lines up out of
the box.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import arrangement as arrangement_mod
from . import clips as clips_mod
from . import project as project_mod
from . import routing as routing_mod
from . import sampler as sampler_mod
from . import tracks as tracks_mod
from .errors import BadInputError
from .midi_gen import write_pattern_midi

ARRANGEMENT_BARS = 8.0

DRUM_TRACK = "drums"
KIT_TRACK = "kit"
PATTERN_CLIP = "starter-pattern"
PATTERN_FILENAME = "starter-pattern.mid"

# General MIDI percussion key map notes used by the starter patterns below —
# matches the notes `mendell kit load` assigns to kick/snare/clap/hat filenames.
_GM_KICK, _GM_SNARE, _GM_CLAP, _GM_CLOSED_HAT, _GM_OPEN_HAT = 36, 38, 39, 42, 46

# Each pattern is a flat list of (beat_offset, note, velocity, length_beats)
# events for one 4/4 bar; the clip loops to fill the arrangement.
STYLES: dict[str, dict[str, Any]] = {
    "lofi": {
        "bpm": 78.0,
        "key": "A",
        "scale": "minor",
        "pattern": [
            (0.0, _GM_KICK, 95, 0.5), (2.5, _GM_KICK, 80, 0.5),
            (1.0, _GM_SNARE, 88, 0.5), (3.0, _GM_SNARE, 88, 0.5),
            (0.0, _GM_CLOSED_HAT, 55, 0.4), (0.5, _GM_CLOSED_HAT, 42, 0.4),
            (1.0, _GM_CLOSED_HAT, 55, 0.4), (1.5, _GM_CLOSED_HAT, 42, 0.4),
            (2.0, _GM_CLOSED_HAT, 55, 0.4), (2.5, _GM_CLOSED_HAT, 42, 0.4),
            (3.0, _GM_CLOSED_HAT, 55, 0.4), (3.5, _GM_CLOSED_HAT, 42, 0.4),
        ],
    },
    "dark": {
        "bpm": 140.0,
        "key": "F",
        "scale": "minor",
        "pattern": [
            (0.0, _GM_KICK, 115, 0.5), (1.75, _GM_KICK, 95, 0.5), (2.5, _GM_KICK, 90, 0.5),
            (2.0, _GM_CLAP, 105, 0.5),
            (0.0, _GM_CLOSED_HAT, 75, 0.25), (0.5, _GM_CLOSED_HAT, 60, 0.25),
            (1.0, _GM_CLOSED_HAT, 75, 0.25), (1.25, _GM_CLOSED_HAT, 50, 0.25),
            (1.5, _GM_CLOSED_HAT, 65, 0.25), (2.0, _GM_CLOSED_HAT, 75, 0.25),
            (2.5, _GM_CLOSED_HAT, 65, 0.25), (3.0, _GM_CLOSED_HAT, 75, 0.25),
            (3.25, _GM_CLOSED_HAT, 50, 0.25), (3.5, _GM_CLOSED_HAT, 65, 0.25),
            (3.75, _GM_CLOSED_HAT, 50, 0.25),
        ],
    },
    "energetic": {
        "bpm": 128.0,
        "key": "C",
        "scale": "major",
        "pattern": [
            (0.0, _GM_KICK, 120, 0.5), (1.0, _GM_KICK, 110, 0.5),
            (2.0, _GM_KICK, 120, 0.5), (3.0, _GM_KICK, 110, 0.5),
            (1.0, _GM_SNARE, 110, 0.5), (3.0, _GM_SNARE, 110, 0.5),
            (0.5, _GM_OPEN_HAT, 85, 0.4), (1.5, _GM_OPEN_HAT, 85, 0.4),
            (2.5, _GM_OPEN_HAT, 85, 0.4), (3.5, _GM_OPEN_HAT, 85, 0.4),
        ],
    },
}


def new(parent: Path, name: str, *, style: str) -> dict[str, Any]:
    style = style.lower()
    if style not in STYLES:
        raise BadInputError(f"unknown style '{style}' (expected one of {sorted(STYLES)})")
    preset = STYLES[style]

    project_dir = project_mod.create(parent, name, bpm=preset["bpm"], key=preset["key"], scale=preset["scale"])

    tracks_mod.add(project_dir, DRUM_TRACK, "midi")
    tracks_mod.add(project_dir, KIT_TRACK, "sampler")
    sampler_mod.create(project_dir, KIT_TRACK)
    routing_mod.set_route(project_dir, DRUM_TRACK, KIT_TRACK)

    pattern_path = project_dir / "midi" / PATTERN_FILENAME
    write_pattern_midi(pattern_path, preset["pattern"])
    clips_mod.import_clip(project_dir, DRUM_TRACK, PATTERN_CLIP, midi_path=str(pattern_path))
    clips_mod.set_params(project_dir, DRUM_TRACK, PATTERN_CLIP, loop=True)

    arrangement_mod.place(project_dir, DRUM_TRACK, PATTERN_CLIP, bar=1)
    arrangement_mod.set_params(project_dir, length=ARRANGEMENT_BARS)

    return {
        "project": project_mod.info(project_dir),
        "style": style,
        "tracks": [DRUM_TRACK, KIT_TRACK],
        "pattern_clip": PATTERN_CLIP,
        "next_steps": [
            f"mendell kit load {name} {KIT_TRACK} <folder-of-one-shot-samples>",
            f"mendell export {name}",
        ],
    }
