"""ACE-Step subsystem — generation, editing, separation, and audio
understanding wired into Mendell projects.

This is the *project-facing* layer over ``ace.engine``: it decides where ACE
outputs land inside a project (``<project>/generated/`` for new audio,
``<project>/stems/`` for separation) and can auto-import a freshly generated
file as an audio clip on a track in one shot, so an agent can go from a text
prompt to a placeable clip without a second command.

Every function returns a plain dict suitable for a ``--json`` envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import clips as clips_mod
from .. import tracks as tracks_mod
from ..errors import BadInputError
from .engine import AceResult, get_engine


def _outdir(project_dir: Path, sub: str) -> Path:
    d = project_dir / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_audio_track(project_dir: Path, track_name: str) -> None:
    if tracks_mod.exists(project_dir, track_name):
        data = tracks_mod.load(project_dir, track_name)
        if data.get("track", {}).get("type") != "audio":
            raise BadInputError(
                f"track '{track_name}' exists but is not an audio track"
            )
    else:
        tracks_mod.add(project_dir, track_name, "audio")


def _maybe_import(project_dir: Path, result: AceResult, *, track: str | None,
                  clip_prefix: str, native_bpm: float | None) -> dict[str, Any]:
    """Optionally import each produced audio file onto ``track`` as a clip."""
    out = result.to_dict()
    if track and result.paths:
        _ensure_audio_track(project_dir, track)
        imported = []
        for i, path in enumerate(result.paths):
            clip_name = clip_prefix if len(result.paths) == 1 else f"{clip_prefix}-{i + 1}"
            clips_mod.import_clip(
                project_dir, track, clip_name,
                sample_path=path, native_bpm=native_bpm or result.bpm,
            )
            imported.append({"clip": clip_name, "track": track})
        out["imported"] = imported
    return out


# -- generative ----------------------------------------------------------

def generate(project_dir: Path, *, caption: str, duration: float | None = None,
             bpm: float | None = None, key: str | None = None,
             time_signature: str | None = None, lyrics: str | None = None,
             ref_audio: str | None = None, batch_size: int = 1,
             track: str | None = None, clip_name: str = "generated") -> dict[str, Any]:
    save_dir = _outdir(project_dir, "generated")
    result = get_engine().generate(
        caption=caption, save_dir=str(save_dir), duration=duration, bpm=bpm,
        key=key, time_signature=time_signature, lyrics=lyrics,
        ref_audio=ref_audio, batch_size=batch_size,
    )
    return _maybe_import(project_dir, result, track=track,
                         clip_prefix=clip_name, native_bpm=bpm)


def cover(project_dir: Path, *, src_audio: str, caption: str, strength: float = 0.8,
          track: str | None = None, clip_name: str = "cover") -> dict[str, Any]:
    save_dir = _outdir(project_dir, "generated")
    result = get_engine().cover(
        src_audio=src_audio, caption=caption, strength=strength, save_dir=str(save_dir)
    )
    return _maybe_import(project_dir, result, track=track,
                         clip_prefix=clip_name, native_bpm=None)


def repaint(project_dir: Path, *, src_audio: str, start: float, end: float,
            caption: str, track: str | None = None,
            clip_name: str = "repaint") -> dict[str, Any]:
    save_dir = _outdir(project_dir, "generated")
    result = get_engine().repaint(
        src_audio=src_audio, start=start, end=end, caption=caption, save_dir=str(save_dir)
    )
    return _maybe_import(project_dir, result, track=track,
                         clip_prefix=clip_name, native_bpm=None)


def separate(project_dir: Path, *, src_audio: str, stems: list[str],
             track: str | None = None) -> dict[str, Any]:
    save_dir = _outdir(project_dir, "stems")
    engine = get_engine()
    produced: list[dict[str, Any]] = []
    all_paths: list[str] = []
    for stem in stems:
        result = engine.separate(src_audio=src_audio, stem=stem, save_dir=str(save_dir))
        all_paths.extend(result.paths)
        out = {"stem": stem, "paths": result.paths}
        if track and result.paths:
            _ensure_audio_track(project_dir, track)
            for i, path in enumerate(result.paths):
                clip_name = f"{stem}" if len(result.paths) == 1 else f"{stem}-{i + 1}"
                clips_mod.import_clip(project_dir, track, clip_name, sample_path=path)
            out["imported_track"] = track
        produced.append(out)
    return {"stems": produced, "paths": all_paths}


def layer(project_dir: Path, *, src_audio: str, instruction: str, strength: float = 0.4,
          track: str | None = None, clip_name: str = "layer") -> dict[str, Any]:
    save_dir = _outdir(project_dir, "generated")
    result = get_engine().layer(
        src_audio=src_audio, instruction=instruction, strength=strength, save_dir=str(save_dir)
    )
    return _maybe_import(project_dir, result, track=track,
                         clip_prefix=clip_name, native_bpm=None)


def vocal2bgm(project_dir: Path, *, src_audio: str, caption: str,
              track: str | None = None, clip_name: str = "bgm") -> dict[str, Any]:
    save_dir = _outdir(project_dir, "generated")
    result = get_engine().vocal2bgm(
        src_audio=src_audio, caption=caption, save_dir=str(save_dir)
    )
    return _maybe_import(project_dir, result, track=track,
                         clip_prefix=clip_name, native_bpm=None)


# -- understanding / LM helpers -----------------------------------------

def understand(project_dir: Path, *, src_audio: str) -> dict[str, Any]:
    return get_engine().understand(src_audio=src_audio).to_dict()


def lrc(project_dir: Path, *, src_audio: str) -> dict[str, Any]:
    result = get_engine().understand(src_audio=src_audio)
    if not result.lrc:
        raise BadInputError(
            "no LRC lyric timestamps were returned — the source may be "
            "instrumental, or this ACE-Step build doesn't emit LRC"
        )
    return {"lrc": result.lrc}


def score(project_dir: Path, *, src_audio: str) -> dict[str, Any]:
    result = get_engine().understand(src_audio=src_audio)
    if result.score is None:
        raise BadInputError("this ACE-Step build returned no quality score")
    return {"score": result.score}


def simple(project_dir: Path, *, query: str, instrumental: bool = False,
           language: str | None = None) -> dict[str, Any]:
    return get_engine().create_sample(
        query=query, instrumental=instrumental, vocal_language=language
    )


def rewrite(project_dir: Path, *, caption: str | None = None,
            lyrics: str | None = None, bpm: float | None = None) -> dict[str, Any]:
    metadata = {"bpm": bpm} if bpm is not None else {}
    return get_engine().rewrite(caption=caption, lyrics=lyrics, metadata=metadata)


def lora_info() -> dict[str, Any]:
    """LoRA fine-tuning is a one-click Gradio workflow upstream, not a single
    CLI call (annotation + training UI). Surface the pointer rather than
    pretending to drive a training loop from here."""
    return {
        "available": True,
        "workflow": "gradio",
        "note": (
            "LoRA training (one-click annotation + training, ~8 songs / ~1h on a "
            "12GB 3090) runs in ACE-Step's Gradio app, not via Mendell. Launch it "
            "with `uv run acestep --train` from your ACE-Step checkout, point it at "
            "your sample folder, and load the resulting LoRA via ACESTEP_* env vars."
        ),
        "docs": "https://github.com/ace-step/ACE-Step-1.5",
    }
