"""Export engine — renders the full arrangement to a stereo buffer and writes
WAV/MP3, with NDJSON progress events and optional per-track stems.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .. import arrangement as arrangement_mod
from .. import clips as clips_mod
from .. import migrate as migrate_mod
from .. import project as project_mod
from .. import sampler as sampler_mod
from .. import tracks as tracks_mod
from ..errors import BadInputError, EngineError
from ..fx import processors as fx_processors
from ..output import emit_event
from ..timing import bar_beat_to_frames
from . import render
from .render import (
    RenderContext,
    apply_track_processing,
    gather_midi_events,
    render_audio_track,
    render_sampler_track,
)

EXPORT_FORMATS = {".wav": "WAV", ".mp3": "MP3"}
DEFAULT_EXPORT_DIR = "export"
DEFAULT_EXPORT_FORMAT = "wav"


def _produces_audio(project_dir: Path, name: str, data: dict[str, Any]) -> bool:
    """A track produces audio if it's an audio track, or a MIDI track that hosts
    a sampler instrument (its notes play through the sampler)."""
    ttype = data["track"]["type"]
    if ttype == "audio":
        return True
    if ttype == "midi" and data["track"].get("instrument") and sampler_mod.exists(project_dir, name):
        return True
    return False


def _clip_length_frames(clip_data: dict[str, Any], tp: dict[str, Any]) -> int:
    clip = clip_data["clip"]
    if clip["type"] == "midi":
        return int(clip_data.get("length_frame", 0))

    length_seconds = float(clip_data.get("length_seconds", 0.0))
    if clip.get("warp", "off") != "off":
        ratio = tp["bpm"] / float(clip.get("native_bpm", tp["bpm"]) or tp["bpm"])
        if ratio:
            length_seconds = length_seconds / ratio
    return int(round(length_seconds * tp["sample_rate"]))


def _total_frames(project_dir: Path, tp: dict[str, Any], placements: list[dict[str, Any]]) -> int:
    arrangement_data = arrangement_mod.load(project_dir)
    length_bars = float(arrangement_data.get("arrangement", {}).get("length", 0.0) or 0.0)
    if length_bars > 0:
        return bar_beat_to_frames(int(length_bars) + 1, 1.0, tp["bpm"], tp["sample_rate"], tp["beats_per_bar"])

    if not placements:
        return 0

    max_end = 0
    for placement in placements:
        clip_data = clips_mod.load(project_dir, placement["clip"])
        max_end = max(max_end, placement["frame"] + _clip_length_frames(clip_data, tp))
    return max_end


def _write_audio(path: Path, buf: np.ndarray, sample_rate: int) -> None:
    fmt = EXPORT_FORMATS.get(path.suffix.lower())
    clipped = np.clip(buf, -1.0, 1.0)
    try:
        sf.write(str(path), clipped, sample_rate, format=fmt)
    except Exception as err:
        raise EngineError(f"could not write audio file '{path}': {err}")

    # Guard against silent failures (wrong cwd, race with another process, a
    # subagent reporting a path that was never actually written) — a reported
    # export path must point at a real, non-empty file.
    if not path.is_file() or path.stat().st_size == 0:
        raise EngineError(
            f"export reported success but '{path}' is missing or empty on disk — "
            f"check the working directory and that the path is writable, then re-run export"
        )


def _resolve_out_path(project_dir: Path, proj_data: dict[str, Any], *, out: str | None, fmt: str | None) -> Path:
    if out is not None:
        out_path = Path(out)
        if out_path.suffix.lower() not in EXPORT_FORMATS:
            raise BadInputError(f"unsupported export format '{out_path.suffix}' (expected .wav or .mp3)")
        return out_path

    fmt = (fmt or DEFAULT_EXPORT_FORMAT).lower()
    suffix = f".{fmt}"
    if suffix not in EXPORT_FORMATS:
        raise BadInputError(f"unsupported export format '{fmt}' (expected wav or mp3)")

    project_name = proj_data.get("project", {}).get("name") or project_dir.name
    return project_dir / DEFAULT_EXPORT_DIR / f"{project_name}{suffix}"


def preview(project_dir: Path, *, out: str | None = None, format: str | None = None,
            stems: bool = False) -> dict[str, Any]:
    """Build the same render plan `export()` would execute — duration, active
    tracks, FX chains, where the file would land — without rendering any audio
    or touching disk. Lets an agent sanity-check a long render before paying
    for it, and surfaces missing-file/missing-binary problems up front."""
    migrate_mod.ensure_migrated(project_dir)
    tp = project_mod.timing_params(project_dir)
    proj_data = project_mod.load(project_dir)

    out_path = _resolve_out_path(project_dir, proj_data, out=out, fmt=format)

    arrangement_data = arrangement_mod.load(project_dir)
    placements = arrangement_data.get("clips", [])
    track_names = [t["name"] for t in tracks_mod.list_tracks(project_dir)]
    if not track_names:
        raise EngineError("nothing to export — project has no tracks")

    total_frames = _total_frames(project_dir, tp, placements)

    track_data = {name: tracks_mod.load(project_dir, name) for name in track_names}
    mixers = {name: {**tracks_mod.MIXER_DEFAULTS, **data.get("mixer", {})} for name, data in track_data.items()}
    audio_producing = {name for name, data in track_data.items() if _produces_audio(project_dir, name, data)}
    soloed = [name for name in audio_producing if mixers[name]["solo"]]
    active = set(soloed) if soloed else {name for name in audio_producing if not mixers[name]["mute"]}

    warnings: list[str] = []
    needs_rubberband = False

    tracks_plan: list[dict[str, Any]] = []
    for name in track_names:
        data = track_data[name]
        ttype = data["track"]["type"]
        track_placements = [p for p in placements if p["track"] == name]
        is_renderable = _produces_audio(project_dir, name, data)

        tracks_plan.append({
            "name": name,
            "type": ttype,
            "active": (name in active) if is_renderable else None,
            "muted": mixers[name]["mute"] if name in mixers else False,
            "soloed": mixers[name]["solo"] if name in mixers else False,
            "placements": len(track_placements),
            "fx_chain": [{"id": s["id"], "type": s["type"]} for s in data.get("fx", [])],
        })

        if ttype == "audio":
            for placement in track_placements:
                clip_data = clips_mod.load(project_dir, placement["clip"])
                clip = clip_data.get("clip", {})
                source = clip.get("source", "")
                if not Path(source).is_file():
                    warnings.append(
                        f"audio track '{name}', clip '{clip.get('name')}': source file missing — "
                        f"'{source}' (re-import it with `mendell clip import ... --sample <path>`)"
                    )
                if clip.get("warp", "off") != "off":
                    needs_rubberband = True
        elif ttype == "midi" and data["track"].get("instrument") and sampler_mod.exists(project_dir, name):
            sampler_data = sampler_mod.load(project_dir, name)
            for slot in sampler_data.get("slots", []):
                if not Path(slot["sample"]).is_file():
                    warnings.append(
                        f"track '{name}' sampler, note {slot.get('note')}: sample file missing — "
                        f"'{slot['sample']}' (re-map it with `mendell sampler map add`)"
                    )

    if needs_rubberband and shutil.which("rubberband") is None:
        warnings.append(
            "this render needs the 'rubberband' CLI tool for time-stretching/pitch-shifting "
            f"warped audio clips, but it isn't installed or isn't on PATH; {render.RUBBERBAND_INSTALL_HINT}"
        )
    if total_frames <= 0:
        warnings.append("arrangement is empty — export would produce no audio")
    elif not any(t["active"] for t in tracks_plan if t["active"] is not None):
        warnings.append("no audio-producing track is active (everything muted, or solo set elsewhere) — export would produce silence")

    return {
        "project": proj_data.get("project", {}).get("name") or project_dir.name,
        "bpm": tp["bpm"],
        "sample_rate": tp["sample_rate"],
        "duration_s": round(total_frames / tp["sample_rate"], 3) if total_frames > 0 else 0.0,
        "out_path": str(out_path),
        "would_write_stems": bool(stems),
        "tracks": tracks_plan,
        "warnings": warnings,
    }


def export(project_dir: Path, *, out: str | None = None, format: str | None = None,
           stems: bool = False, progress: bool = False) -> dict[str, Any]:
    migrate_mod.ensure_migrated(project_dir)
    tp = project_mod.timing_params(project_dir)
    proj_data = project_mod.load(project_dir)
    master = {**project_mod.MASTER_DEFAULTS, **proj_data.get("master", {})}

    out_path = _resolve_out_path(project_dir, proj_data, out=out, fmt=format)

    arrangement_data = arrangement_mod.load(project_dir)
    placements = arrangement_data.get("clips", [])
    track_names = [t["name"] for t in tracks_mod.list_tracks(project_dir)]
    if not track_names:
        raise EngineError("nothing to export — project has no tracks")

    total_frames = _total_frames(project_dir, tp, placements)
    if total_frames <= 0:
        raise EngineError("nothing to export — arrangement is empty")

    ctx = RenderContext(
        project_dir=project_dir, bpm=tp["bpm"], sample_rate=tp["sample_rate"],
        beats_per_bar=tp["beats_per_bar"], placements=placements, total_frames=total_frames,
        track_order=track_names,
    )

    track_data = {name: tracks_mod.load(project_dir, name) for name in track_names}
    mixers = {name: {**tracks_mod.MIXER_DEFAULTS, **data.get("mixer", {})} for name, data in track_data.items()}

    # Mute/solo gate the audio-*producing* tracks: audio tracks, and MIDI tracks
    # that host a sampler instrument (their own notes play through that sampler).
    audio_producing = {name for name, data in track_data.items() if _produces_audio(project_dir, name, data)}
    soloed = [name for name in audio_producing if mixers[name]["solo"]]
    active = set(soloed) if soloed else {name for name in audio_producing if not mixers[name]["mute"]}

    if progress:
        emit_event({"event": "export_progress", "pct": 0})

    # A MIDI track's notes play through its own hosted sampler instrument.
    sampler_inputs: dict[str, list] = {}
    for name in audio_producing:
        if track_data[name]["track"]["type"] == "midi":
            sampler_inputs[name] = gather_midi_events(ctx, name)

    if progress:
        emit_event({"event": "export_progress", "pct": 15})

    active_renderable = [name for name in track_names if name in active]

    track_buffers: dict[str, np.ndarray] = {}
    n = max(len(active_renderable), 1)
    for i, name in enumerate(active_renderable):
        if track_data[name]["track"]["type"] == "audio":
            raw = render_audio_track(ctx, name)
        else:
            raw = render_sampler_track(ctx, name, sampler_inputs.get(name, []))
        track_buffers[name] = apply_track_processing(ctx, name, raw)
        if progress:
            emit_event({"event": "export_progress", "pct": 15 + int(65 * (i + 1) / n)})

    master_buf = np.zeros((total_frames, 2))
    for buf in track_buffers.values():
        master_buf += buf

    master_buf = master_buf * (master["vol"] / 100.0)
    master_buf = fx_processors.process(
        "limiter", master_buf,
        {"ceiling": master["limiter_ceiling"], "lookahead": 5.0},
        sample_rate=tp["sample_rate"],
    )

    if progress:
        emit_event({"event": "export_progress", "pct": 90})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_audio(out_path, master_buf, tp["sample_rate"])

    result: dict[str, Any] = {
        "path": str(out_path),
        "duration_s": round(total_frames / tp["sample_rate"], 3),
        "tracks_rendered": len(track_buffers),
    }

    if stems:
        stems_dir = out_path.parent / f"{out_path.stem}_stems"
        stems_dir.mkdir(parents=True, exist_ok=True)
        stem_paths = []
        for name, buf in track_buffers.items():
            stem_path = stems_dir / f"{name}{out_path.suffix}"
            _write_audio(stem_path, buf, tp["sample_rate"])
            stem_paths.append(str(stem_path))
        result["stems"] = stem_paths

    if progress:
        emit_event({"event": "export_progress", "pct": 100})
        emit_event({"event": "export_complete", "path": str(out_path), "duration_s": result["duration_s"]})

    return result
