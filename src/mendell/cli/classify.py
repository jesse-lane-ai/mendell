"""`mendell classify probe <path>` — inspect name-derived metadata for a sample file.

A diagnostic tool: show what ``classify_from_names`` extracts from a file path,
and optionally show what the AceStep recognizer would add on top of it.

Usage::

    mendell classify probe /path/to/sample.wav
    mendell classify probe /path/to/sample.wav --with-acestep

This command is intentionally standalone — it does NOT touch the library DB or
any project. It's a "what would Mendell infer?" inspector for library builders.
"""

from __future__ import annotations

import click

from ._base import command, json_option


@click.group("classify")
def classify():
    """Name-based sample classifier — inspect what Mendell infers from file paths."""


@classify.command("probe")
@click.argument("path")
@click.option(
    "--with-acestep",
    is_flag=True,
    default=False,
    help="Also run the AceStep recognizer on the file (loads model, slow).",
)
@json_option
@command
def probe(path: str, with_acestep: bool):
    """Show metadata derived from PATH's filename and parent-folder names.

    Reports kind/category/key/scale/bpm/instruments/confidence from pure path
    parsing — no audio is loaded.  Use --with-acestep to also show the
    ACE-Step captioner's verdict for the same file.
    """
    from pathlib import Path

    from ..clips.name_classify import classify_from_names

    file_path = Path(path).expanduser().resolve()
    name_result = classify_from_names(file_path)

    data: dict = {"path": str(file_path), "name_classify": name_result}

    if with_acestep:
        from ..recognize import get_recognizer
        from ..recognize.types import FileProbe
        from ..clips.audio_analysis import probe_duration_seconds

        duration = probe_duration_seconds(str(file_path))
        # Use name_classify's kind as the FileProbe kind (best available).
        kind = name_result.get("kind") or "unknown"
        probe_obj = FileProbe(
            path=file_path,
            filename=file_path.name,
            duration=duration,
            kind=kind,
        )
        recognizer = get_recognizer("ace-step")
        results = recognizer.recognize([probe_obj])
        rec = results[0]
        if rec is not None:
            data["ace_step"] = {
                "category": rec.category,
                "instruments": rec.instruments,
                "confidence": rec.confidence,
                "caption": rec.caption,
                "source": rec.source,
            }
        else:
            data["ace_step"] = None

    # Human-readable summary
    nc = name_result
    lines = [
        f"path:        {file_path}",
        f"kind:        {nc['kind'] or '—'}",
        f"category:    {nc['category'] or '—'}",
        f"key:         {nc['key'] or '—'}  scale: {nc['scale'] or '—'}",
        f"bpm:         {nc['bpm'] or '—'}",
        f"instruments: {', '.join(nc['instruments']) or '—'}",
        f"confidence:  {nc['confidence']}",
    ]
    if with_acestep and data.get("ace_step"):
        ace = data["ace_step"]
        lines += [
            "",
            "--- ACE-Step ---",
            f"category:    {ace['category']}",
            f"instruments: {', '.join(ace['instruments'])}",
            f"confidence:  {ace['confidence']}",
            f"caption:     {ace['caption'] or '—'}",
        ]

    return data, "\n".join(lines)
