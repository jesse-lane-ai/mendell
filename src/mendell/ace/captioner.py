"""``AceCaptioner`` — ACE-Step's standalone music-captioner model.

``ACE-Step/acestep-captioner`` is a Qwen2.5-Omni-7B multimodal model that emits
a free-text description of a piece of audio. Unlike the generative ACE-Step
stack (DiT + planner LM), it's a plain ``transformers`` model, so it needs only
``torch`` + ``transformers`` and no ACE-Step checkpoint — a much lighter
dependency surface, which makes it the right backend for *recognition* (the
``ace-step`` library recognizer) rather than reusing the generation engine's
``understand_music`` path.

Lazy and opt-in like every other model backend: constructing this class is free;
the model is built on first ``caption()`` call, and a missing ``transformers``
raises an actionable ``BadInputError``.

Config (env):
  * ``ACESTEP_CAPTIONER_MODEL`` — HF repo id or local path
                                  (default ``ACE-Step/acestep-captioner``).
  * ``ACESTEP_DEVICE``          — shared with the generation engine
                                  (``cuda`` | ``mps`` | ``cpu`` | ``xpu``).
  * ``ACESTEP_CAPTIONER_LOAD``  — in-flight quantization: ``full`` (default),
                                  ``8bit``, or ``4bit``. The quantized modes use
                                  bitsandbytes (CUDA-only) to shrink the ~22 GB
                                  model to ~11 GB / ~6–7 GB, quantizing only the
                                  LLM tower so the audio encoder stays accurate.
  * ``ACESTEP_CAPTIONER_BATCH`` — files per ``generate()`` call (default ``1``).
                                  Higher values amortize per-call overhead and
                                  cut wall-clock on large scans, at the cost of
                                  more VRAM (longest clip in the batch sets the
                                  padded length). Try 4–8 on a 24 GB card.
"""

from __future__ import annotations

import os

from ..errors import BadInputError, EngineError

DEFAULT_CAPTIONER_MODEL = "ACE-Step/acestep-captioner"
# The model card's documented prompt format for the captioner.
CAPTION_PROMPT = "Describe this audio in detail"

CAPTIONER_INSTALL_HINT = (
    "the ACE-Step captioner needs 'transformers' + 'torch'. Install them with: "
    "pip install transformers torch  (the model "
    "'ACE-Step/acestep-captioner' downloads on first use; override with "
    "ACESTEP_CAPTIONER_MODEL)."
)


class AceCaptioner:
    """Lazy handle on the ACE-Step (Qwen2.5-Omni) captioner."""

    def __init__(self) -> None:
        self._model = None
        self._processor = None

    def _model_id(self) -> str:
        return os.environ.get("ACESTEP_CAPTIONER_MODEL", DEFAULT_CAPTIONER_MODEL)

    def _device(self) -> str:
        return os.environ.get("ACESTEP_DEVICE", "cuda")

    def _load_mode(self) -> str:
        """In-flight quantization mode: ``full`` (default, fp16/bf16),
        ``8bit``, or ``4bit`` — the last two shrink the ~22 GB captioner to
        roughly ~11 GB / ~6–7 GB of VRAM via bitsandbytes, quantizing only the
        LLM tower (the audio encoder stays full precision)."""
        mode = os.environ.get("ACESTEP_CAPTIONER_LOAD", "full").lower()
        if mode not in ("full", "8bit", "4bit"):
            raise BadInputError(
                f"ACESTEP_CAPTIONER_LOAD must be 'full', '8bit', or '4bit' (got '{mode}')"
            )
        return mode

    def _quant_config(self, mode: str):
        """Build a bitsandbytes ``BitsAndBytesConfig`` for the quantized modes,
        or ``None`` for ``full``. bitsandbytes is CUDA-only, so this is the GPU
        path; raise an actionable error if the dep is missing."""
        if mode == "full":
            return None
        try:
            import accelerate  # noqa: F401  (device_map placement for quantized load)
            import bitsandbytes  # noqa: F401
            from transformers import BitsAndBytesConfig
        except ImportError as err:
            raise BadInputError(
                f"ACESTEP_CAPTIONER_LOAD={mode} needs bitsandbytes + accelerate "
                f"(CUDA-only) — install them with: pip install bitsandbytes accelerate "
                f"(missing: {err.name})"
            )
        if mode == "8bit":
            return BitsAndBytesConfig(load_in_8bit=True)
        return BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype="float16",
        )

    def check_available(self) -> None:
        """Verify the import-time dependencies are present *without* loading or
        downloading the model — used at backend-selection time to fail fast."""
        try:
            import torch  # noqa: F401
            from transformers import (  # noqa: F401
                AutoModelForMultimodalLM,
                AutoProcessor,
            )
        except ImportError as err:
            raise BadInputError(f"{CAPTIONER_INSTALL_HINT} (missing: {err.name})")

    def _load(self):
        if self._model is not None:
            return self._model, self._processor

        try:
            import torch  # noqa: F401
            from transformers import AutoModelForMultimodalLM, AutoProcessor
        except ImportError as err:
            raise BadInputError(f"{CAPTIONER_INSTALL_HINT} (missing: {err.name})")

        model_id = self._model_id()
        mode = self._load_mode()
        quant_config = self._quant_config(mode)
        try:
            processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
            kwargs = {"trust_remote_code": True}
            if quant_config is not None:
                # bitsandbytes places weights on the GPU itself and forbids a
                # later .to(); let device_map handle placement.
                kwargs["quantization_config"] = quant_config
                kwargs["device_map"] = self._device()
            model = AutoModelForMultimodalLM.from_pretrained(model_id, **kwargs)
            if quant_config is None:
                model.to(self._device())
            model.eval()
        except Exception as err:
            raise EngineError(f"failed to load ACE-Step captioner '{model_id}': {err}")

        self._model, self._processor = model, processor
        return model, processor

    def _load_audio(self, path: str):
        """Load audio at the processor's expected sampling rate (Qwen2.5-Omni
        uses 16 kHz)."""
        import librosa

        sr = getattr(getattr(self._processor, "feature_extractor", None),
                     "sampling_rate", 16000)
        waveform, _ = librosa.load(path, sr=sr, mono=True)
        return waveform

    def _conversation(self, audio):
        """Qwen2.5-Omni-style multimodal chat: one audio turn + the captioning
        instruction. Built via the chat template so it works across processor
        versions; the documented "<audio>" placeholder is supplied by the
        template's audio content part."""
        return [
            {"role": "user", "content": [
                {"type": "audio", "audio": audio},
                {"type": "text", "text": CAPTION_PROMPT},
            ]}
        ]

    def caption(self, path: str) -> str:
        """Return a free-text caption for the audio file at ``path``."""
        return self.caption_batch([path])[0]

    def caption_batch(self, paths: list[str]) -> list[str]:
        """Caption several files in one ``generate()`` call.

        Batching amortizes the per-call Python/kernel-launch overhead across
        files, which is the main throughput lever for the captioner (a
        multi-hour library scan is dominated by thousands of single-file
        round-trips). Decoder generation is done with **left padding** so every
        row's prompt is the same length and the prompt-trim below is uniform.

        Returns one caption per input path, in order. An empty list in, empty
        list out.
        """
        if not paths:
            return []
        import torch

        model, processor = self._load()
        audios = [self._load_audio(p) for p in paths]
        texts = [
            processor.apply_chat_template(
                self._conversation(a), add_generation_prompt=True, tokenize=False
            )
            for a in audios
        ]

        # Left-pad so the (right-aligned) prompts share a common length; without
        # it batched generation would mis-trim and emit pad tokens mid-caption.
        tok = getattr(processor, "tokenizer", None)
        prev_side = getattr(tok, "padding_side", None) if tok is not None else None
        if tok is not None:
            tok.padding_side = "left"
        try:
            inputs = processor(
                text=texts, audio=audios, return_tensors="pt", padding=True
            ).to(self._device())
            with torch.no_grad():
                # Qwen2.5-Omni can also synthesize speech, in which case
                # generate() returns (text_ids, audio_waveform). Ask for
                # text-only; fall back if this build doesn't accept the kwarg.
                try:
                    generated = model.generate(
                        **inputs, max_new_tokens=256, return_audio=False
                    )
                except TypeError:
                    generated = model.generate(**inputs, max_new_tokens=256)
            # Unwrap a (text_ids, audio) tuple if the talker still fired.
            if isinstance(generated, (tuple, list)):
                generated = generated[0]
            # Drop the (uniform, left-padded) prompt tokens before decoding.
            trimmed = generated[:, inputs["input_ids"].shape[1]:]
            out = processor.batch_decode(
                trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )
        except Exception as err:
            raise EngineError(f"ACE-Step captioner inference error: {err}")
        finally:
            if tok is not None and prev_side is not None:
                tok.padding_side = prev_side
        return [(c or "").strip() for c in out]


_CAPTIONER: AceCaptioner | None = None


def get_captioner() -> AceCaptioner:
    global _CAPTIONER
    if _CAPTIONER is None:
        _CAPTIONER = AceCaptioner()
    return _CAPTIONER
