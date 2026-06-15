"""``gemini-embedding`` recognizer — cloud audio embeddings (opt-in extra + key).

Lazy-imports the ``google-genai`` SDK only when this backend is selected.
Same thresholded multi-label mechanic as the ``clap`` backend, but the
embedding comes from a Gemini embedding model over the audio file; category
and instrument labels are scored against the same taxonomy text prompts via
cosine similarity.

API key comes from the ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` environment
variable — never from config.json (these are cloud credentials, not
project config).

Scaffolded — cannot be exercised in this environment (no SDK, no key, no
network). Selecting this backend without the extra installed, or without an
API key set, raises an actionable ``BadInputError``.
"""

from __future__ import annotations

import os

from ..errors import BadInputError
from .types import INSTRUMENT_VOCAB, LOOP_CATEGORIES, ONESHOT_CATEGORIES, FileProbe, Recognition

NAME = "gemini-embedding"

INSTRUMENT_THRESHOLD_RATIO = 0.5
INSTRUMENT_CAP = 4

EMBEDDING_MODEL = "gemini-embedding-001"

GEMINI_INSTALL_HINT = (
    "the 'gemini-embedding' recognizer needs the optional Gemini dependency — "
    "install it with: pip install 'mendell[gemini]'"
)
GEMINI_KEY_HINT = (
    "the 'gemini-embedding' recognizer needs a Gemini API key — "
    "set the GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable"
)


def _category_prompts(kind: str) -> tuple[str, ...]:
    if kind == "loop":
        return LOOP_CATEGORIES
    return ONESHOT_CATEGORIES


def _cosine(a, b) -> float:
    import numpy as np

    a = np.asarray(a)
    b = np.asarray(b)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / denom if denom else 0.0


class GeminiEmbeddingRecognizer:
    """Cloud audio-embedding recognizer via the Gemini API."""

    name = NAME

    def __init__(self) -> None:
        try:
            from google import genai  # noqa: F401
        except ImportError as err:
            raise BadInputError(f"{GEMINI_INSTALL_HINT} (missing: {err.name})")

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise BadInputError(GEMINI_KEY_HINT)

        from google import genai

        self._client = genai.Client(api_key=api_key)

    def _embed_text(self, texts: list[str]):
        resp = self._client.models.embed_content(model=EMBEDDING_MODEL, contents=texts)
        return [e.values for e in resp.embeddings]

    def _embed_audio(self, item: FileProbe):
        uploaded = self._client.files.upload(file=str(item.path))
        resp = self._client.models.embed_content(model=EMBEDDING_MODEL, contents=[uploaded])
        return resp.embeddings[0].values

    def recognize(self, items: list[FileProbe]) -> list[Recognition | None]:
        results: list[Recognition | None] = []
        instrument_embeds = self._embed_text(list(INSTRUMENT_VOCAB))

        for item in items:
            categories = _category_prompts(item.kind)
            category_embeds = self._embed_text(list(categories))
            audio_embed = self._embed_audio(item)

            category_scores = [_cosine(audio_embed, e) for e in category_embeds]
            best_idx = max(range(len(categories)), key=lambda i: category_scores[i])
            category = categories[best_idx]
            category_confidence = category_scores[best_idx]

            instrument_scores = [_cosine(audio_embed, e) for e in instrument_embeds]
            top_score = max(instrument_scores)
            threshold = top_score * INSTRUMENT_THRESHOLD_RATIO
            order = sorted(range(len(INSTRUMENT_VOCAB)), key=lambda i: instrument_scores[i], reverse=True)
            instruments = []
            for idx in order:
                if len(instruments) >= INSTRUMENT_CAP:
                    break
                if len(instruments) == 0 or instrument_scores[idx] >= threshold:
                    instruments.append(INSTRUMENT_VOCAB[idx])

            results.append(
                Recognition(
                    category=category,
                    instruments=instruments,
                    source=NAME,
                    confidence=max(0.0, min(1.0, category_confidence)),
                )
            )
        return results
