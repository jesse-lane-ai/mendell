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

import json
import os
import subprocess
import sys
import time

from .types import INSTRUMENT_VOCAB, LOOP_CATEGORIES, ONESHOT_CATEGORIES, FileProbe, Recognition

NAME = "ace-step"

# A caption hit on the taxonomy is a strong but text-derived signal.
CAPTION_CONFIDENCE = 0.7
INSTRUMENT_CAP = 4


def _gpu_mem_mib() -> int | None:
    """Best-effort current GPU memory use (MiB) via nvidia-smi; None if no GPU
    / nvidia-smi. Cheap enough to sample per file (4 samples = 4 calls)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except Exception:
        return None


class _Analytics:
    """Append-only JSONL sink for per-file recognition analytics. A no-op when
    no path is configured, so the hot path stays free unless asked for."""

    def __init__(self, path: str | None) -> None:
        self._fh = None
        if path:
            try:
                self._fh = open(path, "a", encoding="utf-8")
            except OSError:
                self._fh = None

    def emit(self, record: dict) -> None:
        if self._fh is None:
            return
        record = {"ts": round(time.time(), 3), **record}
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


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
        #
        # Set MENDELL_ACE_ANALYTICS=<path> to also append a JSONL analytics
        # record per file (timing, caption, derived tags, VRAM) plus load/summary
        # events — readable after the fact without touching the server's stdout.
        total = len(items)
        analytics = _Analytics(os.environ.get("MENDELL_ACE_ANALYTICS"))

        # Load the model up front (if not already) so its cost is measured
        # separately from per-file inference rather than hiding in file #1.
        already = self._captioner._model is not None
        t_load = time.time()
        try:
            self._captioner._load()
        except Exception as err:
            analytics.emit({"event": "load_error", "error": f"{type(err).__name__}: {err}"})
            analytics.close()
            raise
        analytics.emit({
            "event": "load", "total_files": total, "already_loaded": already,
            "model_load_seconds": round(time.time() - t_load, 2) if not already else 0.0,
            "load_mode": self._captioner._load_mode(), "vram_mib": _gpu_mem_mib(),
        })

        results: list[Recognition | None] = []
        captioned = deferred = 0
        run_start = time.time()
        for i, item in enumerate(items, start=1):
            t0 = time.time()
            try:
                caption = self._captioner.caption(str(item.path))
            except Exception as err:
                deferred += 1
                self._progress(i, total, item.filename, f"deferred ({type(err).__name__})")
                analytics.emit({"event": "file", "i": i, "total": total, "file": item.filename,
                                "seconds": round(time.time() - t0, 2), "deferred": f"{type(err).__name__}: {err}"})
                results.append(None)  # defer to filename guess
                continue
            seconds = round(time.time() - t0, 2)
            if not caption:
                deferred += 1
                self._progress(i, total, item.filename, "deferred (empty caption)")
                analytics.emit({"event": "file", "i": i, "total": total, "file": item.filename,
                                "seconds": seconds, "deferred": "empty caption"})
                results.append(None)
                continue

            cats = _categories(item.kind)
            matched_cats = _match_vocab(caption, cats)
            category = matched_cats[0] if matched_cats else cats[0]
            instruments = _match_vocab(caption, INSTRUMENT_VOCAB)[:INSTRUMENT_CAP]
            confidence = CAPTION_CONFIDENCE if matched_cats else 0.3
            captioned += 1

            self._progress(i, total, item.filename, f"{seconds}s -> {category}")
            analytics.emit({"event": "file", "i": i, "total": total, "file": item.filename,
                            "kind": item.kind, "seconds": seconds, "category": category,
                            "category_matched": bool(matched_cats), "instruments": instruments,
                            "confidence": confidence, "vram_mib": _gpu_mem_mib(),
                            "caption": caption})
            results.append(
                Recognition(
                    category=category,
                    instruments=instruments,
                    source=NAME,
                    confidence=confidence,
                    caption=caption,
                )
            )

        analytics.emit({"event": "summary", "total_files": total, "captioned": captioned,
                        "deferred": deferred, "inference_seconds": round(time.time() - run_start, 2),
                        "avg_seconds_per_file": round((time.time() - run_start) / max(total, 1), 2),
                        "vram_mib": _gpu_mem_mib()})
        analytics.close()
        return results

    @staticmethod
    def _progress(done: int, total: int, filename: str, note: str) -> None:
        if os.environ.get("MENDELL_QUIET"):
            return
        sys.stderr.write(f"[ace-step] {done}/{total} {filename} {note}\n")
        sys.stderr.flush()
