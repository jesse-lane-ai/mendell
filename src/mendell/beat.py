"""`mendell beat new` — scaffold a ready-to-go beat project from a style preset.

Collapses the cold-start boilerplate (project -> tracks -> sampler -> routing
-> a clip to trigger it) into a single command, seeded with style-appropriate
tempo/key defaults and a small starter MIDI drum pattern. Patterns are written
using General MIDI percussion notes — the same convention `mendell kit load`
maps one-shot filenames onto — so loading a kit afterwards lines up out of
the box.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

from . import arrangement as arrangement_mod
from . import clips as clips_mod
from . import engine as engine_mod
from . import kit as kit_mod
from . import project as project_mod
from . import routing as routing_mod
from . import sampler as sampler_mod
from . import tracks as tracks_mod
from .errors import BadInputError
from .midi_gen import write_pattern_midi

BEATS_PER_BAR = 4.0
MELODY_TRACK = "melody"
BASS_TRACK = "bass"

ARRANGEMENT_BARS = 8.0

DRUM_TRACK = "drums"
KIT_TRACK = "kit"
PATTERN_CLIP = "starter-pattern"
PATTERN_FILENAME = "starter-pattern.mid"

# General MIDI percussion key map notes used by the starter patterns below —
# matches the notes `mendell kit load` assigns to kick/snare/clap/hat filenames.
_GM_KICK, _GM_SNARE, _GM_CLAP, _GM_CLOSED_HAT, _GM_OPEN_HAT = 36, 38, 39, 42, 46

# Notes that count as "groove fill" — safe to thin out or velocity-jitter
# between variations without disturbing the kick/snare backbone.
_HAT_NOTES = {_GM_CLOSED_HAT, _GM_OPEN_HAT}

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

    project_dir = project_mod.create(parent, name, bpm=preset["bpm"], key=preset["key"], scale=preset["scale"], genre=style)

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


def _seconds_per_bar(bpm: float) -> float:
    return BEATS_PER_BAR * 60.0 / bpm


def _parse_duration_seconds(value: str | float | int) -> float:
    """Accept '60s', '90', 1.5, etc. -> seconds (float)."""
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().lower()
        if text.endswith("s"):
            text = text[:-1]
        try:
            seconds = float(text)
        except ValueError as exc:
            raise BadInputError(f"invalid duration '{value}' (expected e.g. '60s')") from exc
    if seconds <= 0:
        raise BadInputError(f"duration must be positive (got '{value}')")
    return seconds


def _humanize_pattern(
    base: list[tuple[float, int, int, float]], rng: random.Random
) -> list[tuple[float, int, int, float]]:
    """Derive a distinct-but-coherent variation of a one-bar drum pattern:
    jitter velocities, occasionally drop a hat, and now and then add a ghost
    kick. Kick/snare hits are always kept so the groove stays recognizable.
    """
    out: list[tuple[float, int, int, float]] = []
    for offset, note, vel, length in base:
        if note in _HAT_NOTES and rng.random() < 0.18:
            continue  # thin out the hats for movement
        jitter = rng.randint(-8, 8) if note in _HAT_NOTES else rng.randint(-5, 5)
        new_vel = max(1, min(127, vel + jitter))
        out.append((offset, note, new_vel, length))
    # ~40% of variations get a syncopated ghost kick
    if rng.random() < 0.4:
        out.append((rng.choice([1.5, 2.25, 3.5]), _GM_KICK, rng.randint(60, 80), 0.5))
    out.sort(key=lambda e: e[0])
    return out


def _add_loop_track(
    project_dir: Path, track_name: str, sample_path: str, *, warp: str
) -> None:
    """Add an audio track carrying a single looped, tempo-warped sample clip
    placed across the whole arrangement. Falls back to no warp if the loop
    can't be analyzed/warped so the build still completes."""
    tracks_mod.add(project_dir, track_name, "audio")
    clip_name = f"{track_name}-loop"
    try:
        clips_mod.import_clip(project_dir, track_name, clip_name, sample_path=sample_path, warp=warp)
    except Exception:
        clips_mod.import_clip(project_dir, track_name, clip_name, sample_path=sample_path)
    clips_mod.set_params(project_dir, track_name, clip_name, loop=True)
    arrangement_mod.place(project_dir, track_name, clip_name, bar=1)


def make(
    parent: Path,
    name: str,
    *,
    style: str,
    bpm: float | None = None,
    key: str | None = None,
    duration: str | float = "60s",
    variations: int = 8,
    kit: str | None = None,
    melody: str | None = None,
    bass: str | None = None,
    export_format: str = "mp3",
    seed: int | None = None,
) -> dict[str, Any]:
    """One-shot beat build: scaffold -> kit -> N distinct variation sections
    tiled to fill the target duration -> optional warped melody/bass loops ->
    render/export. Returns an envelope with the exported file path.
    """
    style = style.lower()
    if style not in STYLES:
        raise BadInputError(f"unknown style '{style}' (expected one of {sorted(STYLES)})")
    if variations < 1:
        raise BadInputError("variations must be >= 1")
    preset = STYLES[style]

    eff_bpm = float(bpm) if bpm is not None else float(preset["bpm"])
    eff_key = key if key is not None else preset["key"]

    # Scaffold: project + midi drum track routed into a sampler "kit" track.
    project_dir = project_mod.create(parent, name, bpm=eff_bpm, key=eff_key, scale=preset["scale"], genre=style)
    tracks_mod.add(project_dir, DRUM_TRACK, "midi")
    tracks_mod.add(project_dir, KIT_TRACK, "sampler")
    sampler_mod.create(project_dir, KIT_TRACK)
    routing_mod.set_route(project_dir, DRUM_TRACK, KIT_TRACK)

    # Optional one-shot kit (minimal load — caller curates the folder).
    kit_info = None
    if kit:
        kit_info = kit_mod.load_kit(project_dir, KIT_TRACK, kit)

    # How many 8-bar sections do we need to cover the requested duration?
    seconds = _parse_duration_seconds(duration)
    total_bars = max(int(math.ceil(seconds / _seconds_per_bar(eff_bpm))), int(ARRANGEMENT_BARS))
    sections = max(int(math.ceil(total_bars / ARRANGEMENT_BARS)), 1)
    arrangement_bars = sections * ARRANGEMENT_BARS

    # Generate `variations` distinct one-bar variants of the style pattern.
    rng = random.Random(seed if seed is not None else f"{name}:{style}")
    variant_clips: list[str] = []
    for i in range(variations):
        clip_name = f"var-{i + 1}"
        pattern = preset["pattern"] if i == 0 else _humanize_pattern(preset["pattern"], rng)
        midi_path = project_dir / "midi" / f"{clip_name}.mid"
        write_pattern_midi(midi_path, pattern)
        clips_mod.import_clip(project_dir, DRUM_TRACK, clip_name, midi_path=str(midi_path))
        clips_mod.set_params(project_dir, DRUM_TRACK, clip_name, loop=True)
        variant_clips.append(clip_name)

    # Tile the variants across the arrangement, one per 8-bar section, cycling.
    for s in range(sections):
        bar = int(s * ARRANGEMENT_BARS) + 1
        arrangement_mod.place(project_dir, DRUM_TRACK, variant_clips[s % len(variant_clips)], bar=bar)
    arrangement_mod.set_params(project_dir, length=arrangement_bars)

    extra_tracks: list[str] = []
    if melody:
        _add_loop_track(project_dir, MELODY_TRACK, melody, warp="melodic")
        extra_tracks.append(MELODY_TRACK)
    if bass:
        _add_loop_track(project_dir, BASS_TRACK, bass, warp="beats")
        extra_tracks.append(BASS_TRACK)

    export_info = engine_mod.export(project_dir, format=export_format)

    return {
        "project": project_mod.info(project_dir),
        "style": style,
        "bpm": eff_bpm,
        "key": eff_key,
        "tracks": [DRUM_TRACK, KIT_TRACK, *extra_tracks],
        "variations": len(variant_clips),
        "sections": sections,
        "arrangement_bars": arrangement_bars,
        "kit": kit_info,
        "export": export_info,
    }
