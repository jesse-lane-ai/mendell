"""``gemini-generative`` recognizer — cloud multimodal model, constrained JSON.

Lazy-imports the ``google-genai`` SDK only when this backend is selected.
For each file, uploads the audio and prompts a Gemini multimodal model with
the project's category/instrument taxonomy plus a constrained JSON response
schema, so the model returns labels FROM the taxonomy rather than inventing
new ones.

``confidence`` is presence/absence here (~1.0 for every label the model
returns) rather than a continuous score — the model either names a label or
doesn't.

API key comes from ``GEMINI_API_KEY``/``GOOGLE_API_KEY`` — never config.json.

Scaffolded — cannot be exercised in this environment (no SDK, no key, no
network). Selecting this backend without the extra installed, or without an
API key set, raises an actionable ``BadInputError``.
"""

from __future__ import annotations

import os

from ..errors import BadInputError
from .types import INSTRUMENT_VOCAB, LOOP_CATEGORIES, ONESHOT_CATEGORIES, FileProbe, Recognition

NAME = "gemini-generative"

GENERATIVE_MODEL = "gemini-2.5-flash"

# Presence/absence confidence for labels the model returns.
PRESENCE_CONFIDENCE = 1.0

GEMINI_INSTALL_HINT = (
    "the 'gemini-generative' recognizer needs the optional Gemini dependency — "
    "install it with: pip install 'mendell[gemini]'"
)
GEMINI_KEY_HINT = (
    "the 'gemini-generative' recognizer needs a Gemini API key — "
    "set the GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable"
)


def _category_prompts(kind: str) -> tuple[str, ...]:
    if kind == "loop":
        return LOOP_CATEGORIES
    return ONESHOT_CATEGORIES


def _response_schema(categories: tuple[str, ...]):
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(categories)},
            "instruments": {
                "type": "array",
                "items": {"type": "string", "enum": list(INSTRUMENT_VOCAB)},
            },
        },
        "required": ["category", "instruments"],
    }


_PROMPT_TEMPLATE = (
    "Listen to this audio sample. It is a {kind} from a music sample library. "
    "Classify it with exactly one `category` from the allowed list, and list "
    "every `instrument` audibly present from the allowed list (can be empty). "
    "Only use labels from the provided enums."
)


class GeminiGenerativeRecognizer:
    """Cloud multimodal recognizer via the Gemini API with constrained JSON output."""

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

    def recognize(self, items: list[FileProbe]) -> list[Recognition | None]:
        results: list[Recognition | None] = []
        for item in items:
            categories = _category_prompts(item.kind)
            uploaded = self._client.files.upload(file=str(item.path))
            response = self._client.models.generate_content(
                model=GENERATIVE_MODEL,
                contents=[uploaded, _PROMPT_TEMPLATE.format(kind=item.kind)],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": _response_schema(categories),
                },
            )

            import json

            try:
                parsed = json.loads(response.text)
                category = parsed["category"]
                instruments = [i for i in parsed.get("instruments", []) if i in INSTRUMENT_VOCAB]
                if category not in categories:
                    results.append(None)
                    continue
            except (ValueError, KeyError, TypeError):
                results.append(None)
                continue

            results.append(
                Recognition(
                    category=category,
                    instruments=instruments,
                    source=NAME,
                    confidence=PRESENCE_CONFIDENCE,
                )
            )
        return results
