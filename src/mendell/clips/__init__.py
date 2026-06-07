"""Clip management — MIDI clip import (mido), audio clip import + warp
detection, inspection, parameter updates, and warp markers.

Each clip owns its own TOML file under clips/, holding either a parsed
MIDI note list or an audio reference plus warp/stretch settings (and, per
SPEC.md "Automation", that clip's own clip.<name>.<param> automation).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .. import paths
from .. import project as project_mod
from .. import tracks as tracks_mod
from ..durations import format_duration_ms, parse_duration_ms
from ..errors import BadInputError, NotFoundError
from ..timing import bar_beat_to_seconds, frames_to_seconds, parse_bar_beat, seconds_to_frames
from ..toml_io import read_toml, write_toml
from . import audio_analysis, midi_import

WARP_MODES = (*audio_analysis.WARP_MODES, "off")

AUDIO_DEFAULTS = {
    "gain": 0.0,
    "pitch": 0.0,
    "loop": False,
    "loop_start": 0.0,
}

MIDI_DEFAULTS = {
    "transpose": 0,
    "velocity_scale": 1.0,
    "loop": False,
}

AUDIO_EXTS = {".wav", ".aiff", ".aif", ".flac", ".mp3", ".ogg"}


# ---------------------------------------------------------------------------
# load / save / lookups
# ---------------------------------------------------------------------------

def exists(project_dir: Path, name: str) -> bool:
    return paths.clip_toml(project_dir, name).is_file()


def load(project_dir: Path, name: str) -> dict[str, Any]:
    toml_path = paths.clip_toml(project_dir, name)
    if not toml_path.is_file():
        raise NotFoundError(f"clip '{name}' not found")
    return read_toml(toml_path)


def save(project_dir: Path, name: str, data: dict[str, Any]) -> None:
    write_toml(paths.clip_toml(project_dir, name), data)


def _require_track(project_dir: Path, track_name: str) -> dict[str, Any]:
    return tracks_mod.load(project_dir, track_name)


def _register_clip_on_track(project_dir: Path, track_name: str, clip_name: str) -> None:
    track_data = tracks_mod.load(project_dir, track_name)
    clips = track_data.setdefault("clips", [])
    if clip_name not in clips:
        clips.append(clip_name)
    tracks_mod.save(project_dir, track_name, track_data)


def _unregister_clip_from_track(project_dir: Path, track_name: str, clip_name: str) -> None:
    if not tracks_mod.exists(project_dir, track_name):
        return
    track_data = tracks_mod.load(project_dir, track_name)
    clips = track_data.get("clips", [])
    if clip_name in clips:
        clips.remove(clip_name)
        tracks_mod.save(project_dir, track_name, track_data)


def _clip_belongs_to_track(data: dict[str, Any], track_name: str) -> bool:
    return data.get("clip", {}).get("track") == track_name


def _get_for_track(project_dir: Path, track_name: str, clip_name: str) -> dict[str, Any]:
    data = load(project_dir, clip_name)
    if not _clip_belongs_to_track(data, track_name):
        raise NotFoundError(f"clip '{clip_name}' not found on track '{track_name}'")
    return data


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def import_clip(
    project_dir: Path,
    track_name: str,
    clip_name: str,
    *,
    midi_path: str | None = None,
    midi_track_index: int | None = None,
    sample_path: str | None = None,
    link: bool = False,
    native_bpm: float | None = None,
    warp: str | None = None,
) -> dict[str, Any]:
    if (midi_path is None) == (sample_path is None):
        raise BadInputError("provide exactly one of --midi or --sample")

    track_data = _require_track(project_dir, track_name)
    track_type = track_data.get("track", {}).get("type")

    if exists(project_dir, clip_name):
        existing = load(project_dir, clip_name)
        if existing.get("clip", {}).get("track") != track_name:
            raise BadInputError(
                f"clip '{clip_name}' already exists on track "
                f"'{existing.get('clip', {}).get('track')}'"
            )

    if midi_path is not None:
        if track_type != "midi":
            raise BadInputError(
                f"track '{track_name}' is type '{track_type}'; MIDI clips require a 'midi' track"
            )
        result = _import_midi_clip(project_dir, track_name, clip_name, midi_path, midi_track_index)
    else:
        if track_type != "audio":
            raise BadInputError(
                f"track '{track_name}' is type '{track_type}'; audio clips require an 'audio' track"
            )
        result = _import_audio_clip(
            project_dir, track_name, clip_name, sample_path,
            link=link, native_bpm=native_bpm, warp=warp,
        )

    _register_clip_on_track(project_dir, track_name, clip_name)
    return result


def _import_midi_clip(
    project_dir: Path,
    track_name: str,
    clip_name: str,
    midi_path: str,
    midi_track_index: int | None,
) -> dict[str, Any]:
    src = Path(midi_path)
    if not src.is_file():
        raise BadInputError(f"MIDI file not found: {midi_path}")

    midi_file = midi_import.load_midi_file(str(src))
    track_index = midi_track_index if midi_track_index is not None else 0
    notes = midi_import.extract_notes(midi_file, track_index)

    tp = project_mod.timing_params(project_dir)
    converted = midi_import.notes_to_frames(
        notes, ppq=midi_file.ticks_per_beat, bpm=tp["bpm"], sample_rate=tp["sample_rate"]
    )
    length_frame = max((n["start_frame"] + n["duration_frame"] for n in converted), default=0)

    data = {
        "clip": {
            "name": clip_name,
            "type": "midi",
            "track": track_name,
            "source": str(src.resolve()),
            "midi_track": track_index,
            "ppq": midi_file.ticks_per_beat,
        },
        "params": dict(MIDI_DEFAULTS),
        "notes": converted,
        "length_frame": length_frame,
        "automation": [],
    }
    save(project_dir, clip_name, data)

    return {
        "clip": clip_name,
        "track": track_name,
        "type": "midi",
        "notes": len(converted),
        "length_frame": length_frame,
        "midi_track": track_index,
    }


def _import_audio_clip(
    project_dir: Path,
    track_name: str,
    clip_name: str,
    sample_path: str,
    *,
    link: bool,
    native_bpm: float | None,
    warp: str | None,
) -> dict[str, Any]:
    src = Path(sample_path)
    if not src.is_file():
        raise BadInputError(f"audio file not found: {sample_path}")
    if src.suffix.lower() not in AUDIO_EXTS:
        raise BadInputError(f"unsupported audio file type '{src.suffix}'")
    if warp is not None and warp not in WARP_MODES:
        raise BadInputError(f"invalid warp mode '{warp}' (expected one of {WARP_MODES})")

    if link:
        stored_path = src.resolve()
    else:
        dest_dir = paths.samples_dir(project_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.resolve() != src.resolve():
            shutil.copy2(src, dest)
        stored_path = dest.resolve()

    detected = audio_analysis.analyze_clip(str(stored_path), src.name)
    resolved_bpm = native_bpm if native_bpm is not None else detected["native_bpm"]
    resolved_warp = warp if warp is not None else detected["warp"]
    source = "manual" if (native_bpm is not None or warp is not None) else detected["source"]

    import soundfile as sf
    info = sf.info(str(stored_path))
    length_seconds = info.frames / info.samplerate

    data = {
        "clip": {
            "name": clip_name,
            "type": "audio",
            "track": track_name,
            "source": str(stored_path),
            "linked": link,
            "native_bpm": float(resolved_bpm),
            "warp": resolved_warp,
            "detected_source": source,
        },
        "params": {
            **AUDIO_DEFAULTS,
            "loop_end": round(length_seconds, 6),
        },
        "length_seconds": round(length_seconds, 6),
        "warp_markers": [],
        "automation": [],
    }
    save(project_dir, clip_name, data)

    return {
        "clip": clip_name,
        "track": track_name,
        "native_bpm": float(resolved_bpm),
        "warp": resolved_warp,
        "source": source,
        "linked": link,
    }


# ---------------------------------------------------------------------------
# inspect / remove
# ---------------------------------------------------------------------------

def show(project_dir: Path, track_name: str, clip_name: str) -> dict[str, Any]:
    data = _get_for_track(project_dir, track_name, clip_name)
    clip = data.get("clip", {})
    out: dict[str, Any] = {"name": clip.get("name", clip_name), "type": clip.get("type"),
                           "track": clip.get("track")}
    if clip.get("type") == "midi":
        out.update({
            "source": clip.get("source"),
            "midi_track": clip.get("midi_track"),
            "params": data.get("params", dict(MIDI_DEFAULTS)),
            "notes": data.get("notes", []),
            "length_frame": data.get("length_frame", 0),
        })
    else:
        out.update({
            "source": clip.get("source"),
            "linked": clip.get("linked", False),
            "native_bpm": clip.get("native_bpm"),
            "warp": clip.get("warp"),
            "detected_source": clip.get("detected_source"),
            "params": data.get("params", dict(AUDIO_DEFAULTS)),
            "length_seconds": data.get("length_seconds"),
            "warp_markers": data.get("warp_markers", []),
        })
    out["automation"] = data.get("automation", [])
    return out


def list_clips(project_dir: Path, track_name: str) -> list[dict[str, Any]]:
    _require_track(project_dir, track_name)
    track_data = tracks_mod.load(project_dir, track_name)
    names = track_data.get("clips", [])
    out = []
    for name in names:
        if exists(project_dir, name):
            data = load(project_dir, name)
            clip = data.get("clip", {})
            out.append({"name": name, "type": clip.get("type")})
    return out


def remove(project_dir: Path, track_name: str, clip_name: str) -> dict[str, Any]:
    _get_for_track(project_dir, track_name, clip_name)
    paths.clip_toml(project_dir, clip_name).unlink()
    _unregister_clip_from_track(project_dir, track_name, clip_name)
    return {"removed": clip_name, "track": track_name}


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

AUDIO_SETTABLE = {"gain", "native_bpm", "warp", "pitch", "loop", "loop_start", "loop_end"}
MIDI_SETTABLE = {"transpose", "velocity_scale", "loop"}


def set_params(project_dir: Path, track_name: str, clip_name: str, **kwargs: Any) -> dict[str, Any]:
    data = _get_for_track(project_dir, track_name, clip_name)
    clip = data["clip"]
    params = data.setdefault("params", {})
    is_midi = clip.get("type") == "midi"
    settable = MIDI_SETTABLE if is_midi else AUDIO_SETTABLE

    changed: dict[str, Any] = {}
    for name, value in kwargs.items():
        if value is None:
            continue
        if name not in settable:
            raise BadInputError(
                f"'{name}' is not settable on a {'MIDI' if is_midi else 'audio'} clip"
            )

        if name == "warp":
            if value not in WARP_MODES:
                raise BadInputError(f"invalid warp mode '{value}' (expected one of {WARP_MODES})")
            clip["warp"] = value
            clip["detected_source"] = "manual"
            changed[name] = value
        elif name == "native_bpm":
            value = float(value)
            if value <= 0:
                raise BadInputError(f"invalid native_bpm '{value}'")
            clip["native_bpm"] = value
            clip["detected_source"] = "manual"
            changed[name] = value
        elif name == "transpose":
            params["transpose"] = int(value)
            changed[name] = int(value)
        elif name == "velocity_scale":
            params["velocity_scale"] = float(value)
            changed[name] = float(value)
        elif name == "gain":
            params["gain"] = float(value)
            changed[name] = float(value)
        elif name == "pitch":
            params["pitch"] = float(value)
            changed[name] = float(value)
        elif name == "loop":
            params["loop"] = bool(value)
            changed[name] = bool(value)
        elif name in ("loop_start", "loop_end"):
            params[name] = float(value)
            changed[name] = float(value)

    save(project_dir, clip_name, data)
    return {"changed": changed, **show(project_dir, track_name, clip_name)}


# ---------------------------------------------------------------------------
# warp markers (Beats mode only)
# ---------------------------------------------------------------------------

def _require_beats_mode(clip: dict[str, Any]) -> None:
    if clip.get("type") != "audio":
        raise BadInputError("warp markers are only valid on audio clips")
    if clip.get("warp") != "beats":
        raise BadInputError(
            f"warp markers require warp mode 'beats' (clip is currently '{clip.get('warp')}')"
        )


def warp_marker_add(
    project_dir: Path, track_name: str, clip_name: str, *, beat: float, offset: str | float
) -> dict[str, Any]:
    data = _get_for_track(project_dir, track_name, clip_name)
    _require_beats_mode(data["clip"])

    offset_ms = parse_duration_ms(offset)
    markers = data.setdefault("warp_markers", [])
    markers = [m for m in markers if m["beat"] != beat]
    markers.append({"beat": float(beat), "offset_ms": offset_ms})
    markers.sort(key=lambda m: m["beat"])
    data["warp_markers"] = markers
    save(project_dir, clip_name, data)

    return {"clip": clip_name, "beat": float(beat), "offset": format_duration_ms(offset_ms)}


def warp_marker_list(project_dir: Path, track_name: str, clip_name: str) -> list[dict[str, Any]]:
    data = _get_for_track(project_dir, track_name, clip_name)
    return [
        {"beat": m["beat"], "offset": format_duration_ms(m["offset_ms"])}
        for m in data.get("warp_markers", [])
    ]


def warp_marker_remove(
    project_dir: Path, track_name: str, clip_name: str, *, beat: float
) -> dict[str, Any]:
    data = _get_for_track(project_dir, track_name, clip_name)
    _require_beats_mode(data["clip"])

    markers = data.get("warp_markers", [])
    remaining = [m for m in markers if m["beat"] != beat]
    if len(remaining) == len(markers):
        raise NotFoundError(f"no warp marker at beat {beat}")
    data["warp_markers"] = remaining
    save(project_dir, clip_name, data)
    return {"removed_beat": float(beat), "clip": clip_name}
