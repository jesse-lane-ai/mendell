"""``AceEngine`` — a thin, lazy wrapper around the ACE-Step 1.5 model family.

ACE-Step is a heavyweight generative audio model (LM planner + Diffusion
Transformer). Like the ``clap`` / ``gemini`` recognizers, it is an **opt-in
extra**: the base Mendell install never imports it, and selecting any ACE
command without the extra installed (or without a checkpoint available) raises
an actionable ``BadInputError`` instead of a bare ``ImportError``.

The model is too large to run in CI / this dev environment, so this module is
written to the documented ACE-Step Python API (``acestep.inference`` /
``acestep.handler`` — see ``docs/en/INFERENCE.md`` upstream) and exercised
through the wrapper's lazy seam: handlers are constructed on first use and
cached, so merely *constructing* ``AceEngine`` (e.g. for CLI help) costs
nothing.

Configuration is read from the environment so no global config schema change is
needed:

  * ``ACESTEP_CHECKPOINT_DIR`` — directory holding the downloaded checkpoints
                                 (required; the DiT + LM weights live here).
  * ``ACESTEP_DIT_CONFIG``     — DiT config name (default ``acestep-v15-turbo``).
  * ``ACESTEP_LM_MODEL``       — LM model name (default ``acestep-5Hz-lm-0.6B``).
  * ``ACESTEP_DEVICE``         — ``cuda`` | ``mps`` | ``cpu`` | ``xpu``
                                 (default ``cuda``).
  * ``ACESTEP_LM_BACKEND``     — LM inference backend (default ``vllm``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import BadInputError, EngineError

ACE_INSTALL_HINT = (
    "ACE-Step generation needs the ACE-Step package and a model checkpoint. "
    "Install it from source (it isn't on PyPI): pip install "
    "'git+https://github.com/ace-step/ACE-Step-1.5' — then set "
    "ACESTEP_CHECKPOINT_DIR to your downloaded checkpoint directory."
)

DEFAULT_DIT_CONFIG = "acestep-v15-turbo"
DEFAULT_LM_MODEL = "acestep-5Hz-lm-0.6B"
DEFAULT_DEVICE = "cuda"
DEFAULT_LM_BACKEND = "vllm"


@dataclass(frozen=True)
class AceResult:
    """Normalized verdict from any ACE-Step task — flattens the upstream
    ``result.audios[*]`` / ``extra_outputs`` shapes into something the CLI can
    emit as a JSON envelope."""

    paths: list[str] = field(default_factory=list)
    caption: str | None = None
    bpm: float | None = None
    key: str | None = None
    time_signature: str | None = None
    lyrics: str | None = None
    lrc: str | None = None
    score: float | None = None
    seed: int | None = None
    time_cost: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in ("paths", "caption", "bpm", "key", "time_signature",
                  "lyrics", "lrc", "score", "seed", "time_cost"):
            v = getattr(self, k)
            if v not in (None, [], {}):
                out[k] = v
        return out


class AceEngine:
    """Lazy handle on the ACE-Step pipeline. Construct freely; the heavy
    handlers (and the ``acestep`` import) are deferred to first task call."""

    def __init__(self) -> None:
        self._dit = None
        self._llm = None
        self._inference = None

    # -- lazy loading -----------------------------------------------------

    def _config(self) -> dict[str, str]:
        ckpt = os.environ.get("ACESTEP_CHECKPOINT_DIR")
        if not ckpt:
            raise BadInputError(ACE_INSTALL_HINT)
        if not Path(ckpt).is_dir():
            raise BadInputError(
                f"ACESTEP_CHECKPOINT_DIR points at a missing directory: {ckpt}"
            )
        return {
            "checkpoint_dir": ckpt,
            "dit_config": os.environ.get("ACESTEP_DIT_CONFIG", DEFAULT_DIT_CONFIG),
            "lm_model": os.environ.get("ACESTEP_LM_MODEL", DEFAULT_LM_MODEL),
            "device": os.environ.get("ACESTEP_DEVICE", DEFAULT_DEVICE),
            "lm_backend": os.environ.get("ACESTEP_LM_BACKEND", DEFAULT_LM_BACKEND),
        }

    def _load(self):
        """Construct (once) and cache the DiT + LM handlers and the inference
        module. Returns ``(dit_handler, llm_handler, inference_module)``."""
        if self._inference is not None:
            return self._dit, self._llm, self._inference

        cfg = self._config()
        try:
            from acestep.handler import AceStepHandler  # type: ignore
            from acestep.llm_inference import LLMHandler  # type: ignore
            from acestep import inference as inference_mod  # type: ignore
        except ImportError as err:
            raise BadInputError(f"{ACE_INSTALL_HINT} (missing: {err.name})")

        try:
            dit = AceStepHandler()
            dit.initialize_service(
                project_root=cfg["checkpoint_dir"],
                config_path=cfg["dit_config"],
                device=cfg["device"],
            )
            llm = LLMHandler()
            llm.initialize(
                checkpoint_dir=cfg["checkpoint_dir"],
                lm_model_path=cfg["lm_model"],
                backend=cfg["lm_backend"],
                device=cfg["device"],
            )
        except Exception as err:  # model load / device failures
            raise EngineError(f"failed to initialize ACE-Step: {err}")

        self._dit, self._llm, self._inference = dit, llm, inference_mod
        return dit, llm, inference_mod

    # -- result normalization --------------------------------------------

    @staticmethod
    def _normalize(result: Any) -> AceResult:
        if result is None:
            raise EngineError("ACE-Step returned no result")
        if not getattr(result, "success", True):
            raise EngineError(f"ACE-Step task failed: {getattr(result, 'error', 'unknown error')}")

        audios = list(getattr(result, "audios", []) or [])
        paths = [a["path"] for a in audios if isinstance(a, dict) and a.get("path")]
        first = audios[0] if audios else {}
        params = first.get("params", {}) if isinstance(first, dict) else {}
        extra = getattr(result, "extra_outputs", {}) or {}
        time_costs = extra.get("time_costs", {}) if isinstance(extra, dict) else {}

        return AceResult(
            paths=paths,
            caption=first.get("caption") or extra.get("caption"),
            bpm=first.get("bpm") or extra.get("bpm"),
            key=first.get("key"),
            time_signature=first.get("time_signature") or extra.get("time_signature"),
            lyrics=first.get("lyrics") or extra.get("lyrics"),
            lrc=first.get("lrc") or extra.get("lrc"),
            score=first.get("quality_score") or extra.get("quality_score"),
            seed=params.get("seed"),
            time_cost=time_costs.get("pipeline_total_time"),
            raw={"audios": audios, "extra_outputs": extra},
        )

    def _run(self, params_kwargs: dict[str, Any], *, save_dir: str,
             batch_size: int = 1, audio_format: str = "flac") -> AceResult:
        dit, llm, inf = self._load()
        params = inf.GenerationParams(**params_kwargs)
        config = inf.GenerationConfig(batch_size=batch_size, audio_format=audio_format)
        try:
            result = inf.generate_music(dit, llm, params, config, save_dir=save_dir)
        except Exception as err:
            raise EngineError(f"ACE-Step generation error: {err}")
        return self._normalize(result)

    # -- generative tasks -------------------------------------------------

    def generate(self, *, caption: str, save_dir: str, duration: float | None = None,
                 bpm: float | None = None, key: str | None = None,
                 time_signature: str | None = None, lyrics: str | None = None,
                 ref_audio: str | None = None, batch_size: int = 1,
                 audio_format: str = "flac") -> AceResult:
        """Text-to-music with full metadata control and optional reference
        audio to steer style."""
        kw: dict[str, Any] = {"task_type": "text2music", "caption": caption}
        if duration is not None:
            kw["duration"] = duration
        if bpm is not None:
            kw["bpm"] = bpm
        if key is not None:
            kw["key"] = key
        if time_signature is not None:
            kw["time_signature"] = time_signature
        if lyrics is not None:
            kw["lyrics"] = lyrics
        if ref_audio is not None:
            kw["ref_audio"] = ref_audio
        return self._run(kw, save_dir=save_dir, batch_size=batch_size, audio_format=audio_format)

    def cover(self, *, src_audio: str, caption: str, save_dir: str,
              strength: float = 0.8, audio_format: str = "flac") -> AceResult:
        """Re-imagine an existing track in a new style (cover generation)."""
        return self._run(
            {"task_type": "cover", "src_audio": src_audio, "caption": caption,
             "audio_cover_strength": strength},
            save_dir=save_dir, audio_format=audio_format,
        )

    def repaint(self, *, src_audio: str, start: float, end: float, caption: str,
                save_dir: str, audio_format: str = "flac") -> AceResult:
        """Selectively regenerate the ``[start, end)`` window of a track."""
        return self._run(
            {"task_type": "repaint", "src_audio": src_audio,
             "repainting_start": start, "repainting_end": end, "caption": caption},
            save_dir=save_dir, audio_format=audio_format,
        )

    def separate(self, *, src_audio: str, stem: str, save_dir: str,
                 audio_format: str = "flac") -> AceResult:
        """Extract a single stem (``vocals`` / ``drums`` / ``bass`` / ...)."""
        return self._run(
            {"task_type": "extract", "src_audio": src_audio,
             "instruction": f"Extract the {stem} track from the audio:"},
            save_dir=save_dir, audio_format=audio_format,
        )

    def layer(self, *, src_audio: str, instruction: str, save_dir: str,
              strength: float = 0.4, audio_format: str = "flac") -> AceResult:
        """Add a new layer over an existing track (à la Suno "Add Layer") — a
        low-strength cover keyed by an additive instruction."""
        return self._run(
            {"task_type": "cover", "src_audio": src_audio,
             "caption": f"add layer: {instruction}", "audio_cover_strength": strength},
            save_dir=save_dir, audio_format=audio_format,
        )

    def vocal2bgm(self, *, src_audio: str, caption: str, save_dir: str,
                  audio_format: str = "flac") -> AceResult:
        """Auto-generate instrumental accompaniment for a vocal track."""
        return self._run(
            {"task_type": "vocal2bgm", "src_audio": src_audio, "caption": caption},
            save_dir=save_dir, audio_format=audio_format,
        )

    # -- LM-only tasks (understanding / rewriting / simple mode) ----------

    def _encode_audio(self, src_audio: str) -> str:
        """Encode an audio file into ACE-Step audio codes for the LM tasks.

        The DiT handler owns the audio tokenizer; the exact entrypoint varies
        across builds, so probe the documented names and give an actionable
        error if none is present."""
        dit, _, _ = self._load()
        for attr in ("encode_audio", "audio_to_codes", "tokenize_audio"):
            fn = getattr(dit, attr, None)
            if callable(fn):
                return fn(src_audio)
        raise EngineError(
            "this ACE-Step build exposes no audio-encoding entrypoint for "
            "understanding tasks — update ACE-Step or use the REST API"
        )

    def understand(self, *, src_audio: str, temperature: float = 0.85) -> AceResult:
        """Extract BPM, key/scale, time signature, and a caption from audio."""
        _, llm, inf = self._load()
        codes = self._encode_audio(src_audio)
        try:
            result = inf.understand_music(
                llm_handler=llm, audio_codes=codes,
                temperature=temperature, use_constrained_decoding=True,
            )
        except Exception as err:
            raise EngineError(f"ACE-Step understanding error: {err}")
        # understand_music returns a structured analysis, not audio files.
        data = result if isinstance(result, dict) else getattr(result, "__dict__", {})
        return AceResult(
            caption=data.get("caption"),
            bpm=data.get("bpm"),
            key=data.get("key"),
            time_signature=data.get("time_signature"),
            lrc=data.get("lrc"),
            score=data.get("quality_score") or data.get("score"),
            raw={"understanding": data},
        )

    def create_sample(self, *, query: str, instrumental: bool = False,
                       vocal_language: str | None = None,
                       temperature: float = 0.85) -> dict[str, Any]:
        """Simple Mode — expand a one-line description into a full song
        blueprint (caption + metadata + lyrics) without rendering audio."""
        _, llm, inf = self._load()
        try:
            result = inf.create_sample(
                llm_handler=llm, query=query, instrumental=instrumental,
                vocal_language=vocal_language, temperature=temperature,
            )
        except Exception as err:
            raise EngineError(f"ACE-Step simple-mode error: {err}")
        return result if isinstance(result, dict) else getattr(result, "__dict__", {})

    def rewrite(self, *, caption: str | None = None, lyrics: str | None = None,
                metadata: dict[str, Any] | None = None,
                temperature: float = 0.85) -> dict[str, Any]:
        """Query Rewriting — LM expansion/cleanup of tags + lyrics + metadata."""
        _, llm, inf = self._load()
        try:
            result = inf.format_sample(
                llm_handler=llm, caption=caption, lyrics=lyrics,
                user_metadata=metadata or {}, temperature=temperature,
            )
        except Exception as err:
            raise EngineError(f"ACE-Step rewrite error: {err}")
        return result if isinstance(result, dict) else getattr(result, "__dict__", {})


# Module-level singleton so repeated CLI calls in one process reuse the loaded
# model (matters for batch agent workflows that shell into the same process).
_ENGINE: AceEngine | None = None


def get_engine() -> AceEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = AceEngine()
    return _ENGINE
