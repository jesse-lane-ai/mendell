"""``ace-step`` recognizer — content recognition via ACE-Step's purpose-built
captioner model (``ACE-Step/acestep-captioner``, a Qwen2.5-Omni multimodal
model), mapped onto Mendell's taxonomy.

The captioner emits a free-text description per file; we keyword-map that
caption onto the same ``category`` / ``instruments`` vocabularies the other
recognizers emit, so the library fusion logic (``library._fuse_category``)
treats it identically.

This path needs only ``transformers`` + ``torch`` (no ACE-Step generation
checkpoint), so it's far lighter than reusing the generative engine. Like
``clap`` it's opt-in and can't be exercised in this environment (no model
download). Selecting it without ``transformers`` raises an actionable
``BadInputError`` via the captioner constructor's first use.
"""

from __future__ import annotations

import os
import sys

from .types import INSTRUMENT_VOCAB, LOOP_CATEGORIES, ONESHOT_CATEGORIES, FileProbe, Recognition

NAME = "ace-step"

# A caption hit on the taxonomy is a strong but text-derived signal.
CAPTION_CONFIDENCE = 0.7
INSTRUMENT_CAP = 4


def _categories(kind: str) -> tuple[str, ...]:
    return LOOP_CATEGORIES if kind == "loop" else ONESHOT_CATEGORIES


def _match_vocab(caption: str, vocab: tuple[str, ...]) -> list[str]:
    low = caption.lower()
    return [term for term in vocab if term in low]


class AceStepRecognizer:
    """Caption-based recognition via the ACE-Step captioner."""

    name = NAME

    def __init__(self) -> None:
        # Construct the captioner eagerly so a missing dependency fails fast at
        # backend-selection time (matches ClapRecognizer's contract); the model
        # itself is loaded lazily on first caption.
        from ..ace.captioner import get_captioner

        self._captioner = get_captioner()
        # Surface a missing `transformers`/`torch` now rather than mid-batch
        # (cheap import check — does not download the model).
        self._captioner.check_available()

    def recognize(self, items: list[FileProbe]) -> list[Recognition | None]:
        # Captioning is the slow part (seconds per file), and the whole add
        # commits in one transaction, so emit per-file progress to stderr — it
        # never touches the JSON envelope on stdout and shows up in both the CLI
        # and the `library serve` terminal. Silence with MENDELL_QUIET=1.
        total = len(items)
        results: list[Recognition | None] = []
        for i, item in enumerate(items, start=1):
            try:
                caption = self._captioner.caption(str(item.path))
            except Exception as err:
                self._progress(i, total, item.filename, f"deferred ({type(err).__name__})")
                results.append(None)  # defer to filename guess
                continue
            if not caption:
                self._progress(i, total, item.filename, "deferred (empty caption)")
                results.append(None)
                continue

            cats = _categories(item.kind)
            matched_cats = _match_vocab(caption, cats)
            category = matched_cats[0] if matched_cats else cats[0]
            instruments = _match_vocab(caption, INSTRUMENT_VOCAB)[:INSTRUMENT_CAP]

            self._progress(i, total, item.filename, f"-> {category}")
            results.append(
                Recognition(
                    category=category,
                    instruments=instruments,
                    source=NAME,
                    confidence=CAPTION_CONFIDENCE if matched_cats else 0.3,
                )
            )
        return results

    @staticmethod
    def _progress(done: int, total: int, filename: str, note: str) -> None:
        if os.environ.get("MENDELL_QUIET"):
            return
        sys.stderr.write(f"[ace-step] {done}/{total} {filename} {note}\n")
        sys.stderr.flush()
