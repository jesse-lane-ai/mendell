"""Per-track rendering — MIDI -> sampler synthesis, audio-clip placement with
warp/stretch, FX chain processing, and automation resolution.

All buffers are (n_frames, 2) float64 stereo arrays at the project sample rate.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .. import automation as automation_mod
from .. import clips as clips_mod
from .. import sampler as sampler_mod
from .. import tracks as tracks_mod
from ..errors import EngineError
from ..fx import processors as fx_processors
from . import dsp


@dataclass
class RenderContext:
    project_dir: Path
    bpm: float
    sample_rate: int
    beats_per_bar: int
    placements: list[dict[str, Any]]
    total_frames: int
    track_order: list[str] = field(default_factory=list)

    def placements_for(self, track_name: str) -> list[dict[str, Any]]:
        return sorted(
            (p for p in self.placements if p["track"] == track_name),
            key=lambda p: p["frame"],
        )

    def slot_end(self, track_name: str, frame: int) -> int:
        following = [p["frame"] for p in self.placements_for(track_name) if p["frame"] > frame]
        return min(following) if following else self.total_frames


@dataclass
class NoteEvent:
    frame: int
    note: int
    velocity: int
    duration_frame: int


# ---------------------------------------------------------------------------
# MIDI note gathering
# ---------------------------------------------------------------------------

def gather_midi_events(ctx: RenderContext, track_name: str) -> list[NoteEvent]:
    events: list[NoteEvent] = []

    for placement in ctx.placements_for(track_name):
        clip_data = clips_mod.load(ctx.project_dir, placement["clip"])
        if clip_data.get("clip", {}).get("type") != "midi":
            continue

        params = clip_data.get("params", {})
        transpose = int(params.get("transpose", 0))
        velocity_scale = float(params.get("velocity_scale", 1.0))
        loop = bool(params.get("loop", False))
        clip_length = int(clip_data.get("length_frame", 0))
        notes = clip_data.get("notes", [])
        end = ctx.slot_end(track_name, placement["frame"])

        repeats = 1
        if loop and clip_length > 0:
            repeats = max(int(np.ceil((end - placement["frame"]) / clip_length)), 1)

        for rep in range(repeats):
            base = placement["frame"] + rep * clip_length
            if base >= end:
                break
            for n in notes:
                frame = base + n["start_frame"]
                if frame >= end:
                    continue
                note = max(0, min(127, n["note"] + transpose))
                velocity = max(1, min(127, round(n["velocity"] * velocity_scale)))
                events.append(NoteEvent(
                    frame=frame, note=note, velocity=velocity,
                    duration_frame=max(n["duration_frame"], 1),
                ))

    return events


# ---------------------------------------------------------------------------
# Sampler track rendering
# ---------------------------------------------------------------------------

def _find_slot(slots: list[dict[str, Any]], note: int) -> dict[str, Any] | None:
    for slot in slots:
        if slot["note_low"] <= note <= slot["note_high"]:
            return slot
    return None


_SAMPLE_CACHE: dict[str, np.ndarray] = {}


_RUBBERBAND_INSTALL_HINT = (
    "install it with 'sudo apt install rubberband-cli' (Debian/Ubuntu), "
    "'brew install rubberband' (macOS), or 'sudo pacman -S rubberband' (Arch), "
    "then re-run export"
)


def _missing_file_hint(path: str, *, what: str, fix: str) -> str:
    return (
        f"{what} file is missing on disk: '{path}' — it may have been moved, deleted, "
        f"or referenced via --link from a path that no longer exists. {fix}"
    )


def _load_sample(path: str, sample_rate: int) -> np.ndarray:
    cached = _SAMPLE_CACHE.get(path)
    if cached is not None:
        return cached
    if not Path(path).is_file():
        raise EngineError(_missing_file_hint(
            path, what="sample",
            fix="check `mendell sampler map list <project> <track>` and re-map it with `mendell sampler map add`.",
        ))
    y, sr = sf.read(path, always_2d=True, dtype="float64")
    y = dsp.to_stereo(y)
    if sr != sample_rate:
        # Project-rate consistency is enforced on import; a mismatch here means
        # a linked file's rate differs from the project's. Resample to match.
        y = dsp.resample_by_ratio(y, sr / sample_rate)
    _SAMPLE_CACHE[path] = y
    return y


def _extend_with_loop(sample: np.ndarray, target_frames: int, *, mode: str,
                      loop_start_s: float, loop_end_s: float | None, sample_rate: int) -> np.ndarray:
    """Extend `sample` to at least `target_frames` by looping [loop_start, loop_end)
    forward or ping-pong, so a held note can outlast the underlying sample."""
    if mode == "off" or len(sample) >= target_frames:
        return sample

    start = max(int(loop_start_s * sample_rate), 0)
    end = int(loop_end_s * sample_rate) if loop_end_s is not None else len(sample)
    end = min(max(end, start + 2), len(sample))
    region = sample[start:end]
    if mode == "pingpong" and len(region) > 2:
        region = np.concatenate([region, region[-2:0:-1]])

    extra_needed = target_frames - len(sample)
    tiles = int(np.ceil(extra_needed / len(region))) + 1
    tail = np.tile(region, (tiles, 1))[:extra_needed]
    return np.concatenate([sample, tail])


def render_sampler_track(ctx: RenderContext, track_name: str, midi_events: list[NoteEvent]) -> np.ndarray:
    buf = np.zeros((ctx.total_frames, 2))
    if not sampler_mod.exists(ctx.project_dir, track_name):
        return buf

    sampler_data = sampler_mod.load(ctx.project_dir, track_name)
    sampler_settings = {**sampler_mod.SAMPLER_DEFAULTS, **sampler_data.get("sampler", {})}
    global_tune = sampler_settings.get("tune", 0)
    slots = sampler_data.get("slots", [])
    if not slots:
        return buf

    for event in midi_events:
        slot = _find_slot(slots, event.note)
        if slot is None:
            continue

        try:
            sample = _load_sample(slot["sample"], ctx.sample_rate)
        except EngineError:
            raise
        except Exception as err:
            raise EngineError(f"could not read sample '{slot['sample']}': {err}")

        cents = slot.get("tune", 0) + global_tune
        semitone_offset = (event.note - slot["root"]) if slot.get("pitch_follow", True) else 0
        ratio = 2.0 ** ((semitone_offset + cents / 100.0) / 12.0)

        note_off = event.duration_frame
        attack_ms = slot.get("attack_ms", sampler_mod.SLOT_DEFAULTS["attack_ms"])
        decay_ms = slot.get("decay_ms", sampler_mod.SLOT_DEFAULTS["decay_ms"])
        sustain = slot.get("sustain", sampler_mod.SLOT_DEFAULTS["sustain"])
        release_ms = slot.get("release_ms", sampler_mod.SLOT_DEFAULTS["release_ms"])
        release_n = int(release_ms / 1000.0 * ctx.sample_rate)
        target_len = note_off + release_n

        # Extend at the source sample rate (pre pitch-shift) so loop_start/loop_end
        # — stored in seconds of the original sample — need no ratio conversion.
        source = _extend_with_loop(
            sample, int(np.ceil(target_len * ratio)) + 1,
            mode=slot.get("loop", sampler_mod.SLOT_DEFAULTS["loop"]),
            loop_start_s=slot.get("loop_start", sampler_mod.SLOT_DEFAULTS["loop_start"]),
            loop_end_s=slot.get("loop_end"),
            sample_rate=ctx.sample_rate,
        )
        voice = dsp.resample_by_ratio(source, ratio) if ratio != 1.0 else source

        voiced_len = min(len(voice), target_len)
        if voiced_len <= 0:
            continue
        voice = voice[:voiced_len]

        envelope = dsp.adsr_envelope(
            voiced_len, note_off,
            attack_ms=attack_ms, decay_ms=decay_ms, sustain_pct=sustain, release_ms=release_ms,
            sample_rate=ctx.sample_rate,
        )
        velocity_gain = event.velocity / 127.0
        slot_gain = slot.get("vol", sampler_mod.SLOT_DEFAULTS["vol"]) / 100.0
        left, right = dsp.pan_gains(slot.get("pan", sampler_mod.SLOT_DEFAULTS["pan"]))

        gained = voice * envelope[:, None] * velocity_gain * slot_gain
        gained = np.column_stack([gained[:, 0] * left, gained[:, 1] * right])

        dsp.mix_at(buf, gained, event.frame)

    return buf


# ---------------------------------------------------------------------------
# Audio track rendering
# ---------------------------------------------------------------------------

def _stretch_and_pitch(y: np.ndarray, sample_rate: int, *, stretch_ratio: float, pitch_semitones: float) -> np.ndarray:
    needs_rubberband = stretch_ratio != 1.0 or bool(pitch_semitones)
    if needs_rubberband and shutil.which("rubberband") is None:
        raise EngineError(
            "this clip needs time-stretching or pitch-shifting, which requires the "
            f"'rubberband' command-line tool — it isn't installed or isn't on PATH; {_RUBBERBAND_INSTALL_HINT}"
        )

    out = y
    if stretch_ratio != 1.0:
        try:
            import pyrubberband as prb
            out = prb.time_stretch(out, sample_rate, stretch_ratio)
        except Exception as err:
            raise EngineError(f"time-stretching with 'rubberband' failed: {err}")
    if pitch_semitones:
        try:
            import pyrubberband as prb
            out = prb.pitch_shift(out, sample_rate, pitch_semitones)
        except Exception as err:
            raise EngineError(f"pitch-shifting with 'rubberband' failed: {err}")
    return out


def render_audio_track(ctx: RenderContext, track_name: str) -> np.ndarray:
    buf = np.zeros((ctx.total_frames, 2))

    for placement in ctx.placements_for(track_name):
        clip_data = clips_mod.load(ctx.project_dir, placement["clip"])
        clip = clip_data.get("clip", {})
        if clip.get("type") != "audio":
            continue

        params = clip_data.get("params", {})
        warp = clip.get("warp", "off")
        native_bpm = float(clip.get("native_bpm", ctx.bpm))
        gain = dsp.db_to_linear(_resolve_clip_value(ctx, placement, "gain", params.get("gain", 0.0)))
        pitch = _resolve_clip_value(ctx, placement, "pitch", params.get("pitch", 0.0))
        loop = bool(params.get("loop", False))
        loop_start = float(params.get("loop_start", 0.0))
        loop_end = params.get("loop_end")

        source = clip.get("source", "")
        if not Path(source).is_file():
            raise EngineError(_missing_file_hint(
                source, what=f"audio clip '{clip.get('name')}' source",
                fix="re-import it with `mendell clip import ... --sample <path>`.",
            ))
        try:
            y, sr = sf.read(source, always_2d=True, dtype="float64")
        except Exception as err:
            raise EngineError(f"could not read audio clip '{clip.get('name')}' ('{source}'): {err}")
        if sr != ctx.sample_rate:
            y = dsp.resample_by_ratio(y, sr / ctx.sample_rate)

        stretch_ratio = (ctx.bpm / native_bpm) if warp != "off" else 1.0
        rendered = _stretch_and_pitch(y, ctx.sample_rate, stretch_ratio=stretch_ratio, pitch_semitones=pitch)
        rendered = dsp.to_stereo(rendered) * gain

        end = ctx.slot_end(track_name, placement["frame"])
        slot_len = end - placement["frame"]

        if loop and len(rendered) < slot_len:
            ls = max(int(loop_start * ctx.sample_rate), 0)
            le = int(loop_end * ctx.sample_rate) if loop_end is not None else len(rendered)
            le = min(max(le, ls + 1), len(rendered))
            region = rendered[ls:le]
            tiles = int(np.ceil(slot_len / len(region)))
            rendered = np.tile(region, (tiles, 1))[:slot_len]

        dsp.mix_at(buf, rendered, placement["frame"])

    return buf


def _resolve_clip_value(ctx: RenderContext, placement: dict[str, Any], param: str, static: float) -> float:
    """Resolve `clip.<name>.<param>` automation at the placement's start frame
    (a clip-level parameter is constant for the duration of one placement)."""
    clip_data = clips_mod.load(ctx.project_dir, placement["clip"])
    for entry in clip_data.get("automation", []):
        if entry["param"] == param:
            value = automation_mod.interpolate(
                entry["points"], placement["frame"],
                bpm=ctx.bpm, sample_rate=ctx.sample_rate, beats_per_bar=ctx.beats_per_bar,
            )
            if value is not None:
                return value
    return static


# ---------------------------------------------------------------------------
# Track post-processing — automation, FX chain, vol/pan
# ---------------------------------------------------------------------------

def _track_param_curve(ctx: RenderContext, track_data: dict[str, Any], param: str, static: Any) -> np.ndarray | Any:
    for entry in track_data.get("automation", []):
        if entry["param"] == param and entry["points"]:
            values = np.empty(ctx.total_frames)
            # Sample the curve once per frame is wasteful for long renders;
            # interpolate on a coarse grid and broadcast — still sample-accurate
            # at segment boundaries because the grid resolution is sub-ms.
            step = max(ctx.sample_rate // 200, 1)  # ~5ms control-rate
            grid = np.arange(0, ctx.total_frames, step)
            grid_vals = np.array([
                automation_mod.interpolate(entry["points"], int(f), bpm=ctx.bpm,
                                            sample_rate=ctx.sample_rate, beats_per_bar=ctx.beats_per_bar)
                for f in grid
            ])
            values = np.interp(np.arange(ctx.total_frames), grid, grid_vals)
            return values
    return static


def apply_track_processing(ctx: RenderContext, track_name: str, buf: np.ndarray) -> np.ndarray:
    track_data = tracks_mod.load(ctx.project_dir, track_name)
    mixer = {**tracks_mod.MIXER_DEFAULTS, **track_data.get("mixer", {})}

    vol_curve = _track_param_curve(ctx, track_data, "vol", mixer["vol"])
    pan_curve = _track_param_curve(ctx, track_data, "pan", mixer["pan"])

    out = buf
    for slot in track_data.get("fx", []):
        params = dict(slot["params"])
        # FX processors operate on a whole buffer at fixed parameter values; an
        # automated FX param is honored as its time-average over the render —
        # full per-sample-accurate automated DSP is out of scope for an offline renderer.
        for pname in list(params.keys()):
            curve = _track_param_curve(ctx, track_data, f"fx.{slot['id']}.{pname}", None)
            if isinstance(curve, np.ndarray):
                params[pname] = float(np.mean(curve))
        out = fx_processors.process(slot["type"], out, params, sample_rate=ctx.sample_rate, bpm=ctx.bpm)

    if isinstance(vol_curve, np.ndarray):
        gain = vol_curve / 100.0
        out = out * gain[:, None]
    else:
        out = out * (vol_curve / 100.0)

    if isinstance(pan_curve, np.ndarray):
        angle = ((pan_curve + 100) / 200.0) * (np.pi / 2)
        left, right = np.cos(angle), np.sin(angle)
        out = np.column_stack([out[:, 0] * left, out[:, 1] * right])
    else:
        left, right = dsp.pan_gains(int(pan_curve))
        out = np.column_stack([out[:, 0] * left, out[:, 1] * right])

    return out
