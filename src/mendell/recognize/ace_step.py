"""``ace-step`` recognizer — content recognition via ACE-Step's audio
understanding (caption + BPM/key) mapped onto Mendell's taxonomy.

Reuses the generative ``ace.engine`` wrapper's ``understand`` path: ACE-Step
returns a free-text caption per file, which we keyword-map onto the same
``category`` / ``instruments`` vocabularies the other recognizers emit, so the
library fusion logic (``library._fuse_category``) treats it identically.

Like ``clap``, this is an opt-in extra and cannot be exercised in this
environment (no model checkpoint). Selecting it without the ``ace`` extra (or
without ``ACESTEP_CHECKPOINT_DIR``) raises an actionable ``BadInputError`` via
the engine constructor.
"""

from __future__ import annotations

from .types import INSTRUMENT_VOCAB, LOOP_CATEGORIES, ONESHOT_CATEGORIES, FileProbe, Recognition

NAME = "ace-step"

# Confidence floor for a caption-derived verdict — ACE-Step's caption is a
# strong signal but it's a text match, so keep it modest.
CAPTION_CONFIDENCE = 0.7
INSTRUMENT_CAP = 4


def _categories(kind: str) -> tuple[str, ...]:
    return LOOP_CATEGORIES if kind == "loop" else ONESHOT_CATEGORIES


def _match_vocab(caption: str, vocab: tuple[str, ...]) -> list[str]:
    low = caption.lower()
    return [term for term in vocab if term in low]


class AceStepRecognizer:
    """Caption-based recognition via the ACE-Step understanding model."""

    name = NAME

    def __init__(self) -> None:
        # Construct the engine eagerly so a missing extra / checkpoint fails
        # fast at backend-selection time (matches ClapRecognizer's contract).
        from ..ace.engine import get_engine

        self._engine = get_engine()
        # Force the config check (raises BadInputError if unconfigured) without
        # loading the model weights yet.
        self._engine._config()

    def recognize(self, items: list[FileProbe]) -> list[Recognition | None]:
        results: list[Recognition | None] = []
        for item in items:
            try:
                verdict = self._engine.understand(src_audio=str(item.path))
            except Exception:
                results.append(None)  # defer to filename guess
                continue
            caption = verdict.caption or ""
            if not caption:
                results.append(None)
                continue

            cats = _categories(item.kind)
            matched_cats = _match_vocab(caption, cats)
            category = matched_cats[0] if matched_cats else cats[0]
            instruments = _match_vocab(caption, INSTRUMENT_VOCAB)[:INSTRUMENT_CAP]

            results.append(
                Recognition(
                    category=category,
                    instruments=instruments,
                    source=NAME,
                    confidence=CAPTION_CONFIDENCE if matched_cats else 0.3,
                )
            )
        return results
