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
        try:
            processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
            model = AutoModelForMultimodalLM.from_pretrained(
                model_id, trust_remote_code=True
            )
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

    def caption(self, path: str) -> str:
        """Return a free-text caption for the audio file at ``path``."""
        import torch

        model, processor = self._load()
        audio = self._load_audio(path)

        # Qwen2.5-Omni-style multimodal chat: one audio turn + the captioning
        # instruction. Built via the chat template so it works across processor
        # versions; the documented "<audio>" placeholder is supplied by the
        # template's audio content part.
        conversation = [
            {"role": "user", "content": [
                {"type": "audio", "audio": audio},
                {"type": "text", "text": CAPTION_PROMPT},
            ]}
        ]
        try:
            text = processor.apply_chat_template(
                conversation, add_generation_prompt=True, tokenize=False
            )
            inputs = processor(
                text=text, audio=audio, return_tensors="pt", padding=True
            ).to(self._device())
            with torch.no_grad():
                generated = model.generate(**inputs, max_new_tokens=256)
            # Drop the prompt tokens before decoding so we keep only the caption.
            trimmed = generated[:, inputs["input_ids"].shape[1]:]
            out = processor.batch_decode(
                trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )
        except Exception as err:
            raise EngineError(f"ACE-Step captioner inference error: {err}")
        return (out[0] if out else "").strip()


_CAPTIONER: AceCaptioner | None = None


def get_captioner() -> AceCaptioner:
    global _CAPTIONER
    if _CAPTIONER is None:
        _CAPTIONER = AceCaptioner()
    return _CAPTIONER
