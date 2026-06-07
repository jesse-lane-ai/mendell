"""Export engine — renders the full arrangement to a stereo buffer and writes
WAV/MP3, with NDJSON progress events and optional per-track stems.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .. import arrangement as arrangement_mod
from .. import clips as clips_mod
from .. import project as project_mod
from .. import tracks as tracks_mod
from ..errors import BadInputError, EngineError
from ..fx import processors as fx_processors
from ..output import emit_event
from ..timing import bar_beat_to_frames
from .render import (
    RenderContext,
    apply_track_processing,
    gather_midi_events,
    render_audio_track,
    render_sampler_track,
)

EXPORT_FORMATS = {".wav": "WAV", ".mp3": "MP3"}


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


def export(project_dir: Path, *, out: str, stems: bool = False, progress: bool = False) -> dict[str, Any]:
    out_path = Path(out)
    if out_path.suffix.lower() not in EXPORT_FORMATS:
        raise BadInputError(f"unsupported export format '{out_path.suffix}' (expected .wav or .mp3)")

    tp = project_mod.timing_params(project_dir)
    proj_data = project_mod.load(project_dir)
    master = {**project_mod.MASTER_DEFAULTS, **proj_data.get("master", {})}

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

    # Mute/solo gate the audio-*producing* tracks only (audio, sampler). MIDI
    # tracks carry no audio of their own — gating note-gathering by their solo
    # state would mean soloing a Sampler track goes silent unless its upstream
    # MIDI track is *also* soloed, which is surprising. Notes always flow along
    # routes; mute/solo decides which rendered buffers reach the master mix.
    audio_producing = {name for name, data in track_data.items() if data["track"]["type"] in ("audio", "sampler")}
    soloed = [name for name in audio_producing if mixers[name]["solo"]]
    active = set(soloed) if soloed else {name for name in audio_producing if not mixers[name]["mute"]}

    if progress:
        emit_event({"event": "export_progress", "pct": 0})

    # Gather MIDI note events and route them into every Sampler track they're
    # connected to (many-to-many): one MIDI track can drive several samplers
    # (layering), one sampler can receive from several MIDI tracks.
    sampler_inputs: dict[str, list] = {name: [] for name in audio_producing if track_data[name]["track"]["type"] == "sampler"}
    for name, data in track_data.items():
        if data["track"]["type"] != "midi":
            continue
        events = gather_midi_events(ctx, name)
        for dest in data.get("routes", []):
            if dest in sampler_inputs:
                sampler_inputs[dest].extend(events)

    if progress:
        emit_event({"event": "export_progress", "pct": 15})

    renderable = [name for name in track_names if track_data[name]["track"]["type"] in ("audio", "sampler")]
    active_renderable = [name for name in renderable if name in active]

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
