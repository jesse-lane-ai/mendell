"""`mendell library ...` — register and search named external sample folders."""

import click

from .. import library as library_mod
from ._base import command, json_option


def _split_tags(tags: str | None) -> list[str] | None:
    if tags is None:
        return None
    return [t.strip() for t in tags.split(",") if t.strip()]


@click.group("library")
def library():
    """Sample library — register external folders and reference them by name."""


@library.command("add")
@click.argument("name")
@click.argument("path")
@click.option("--tags", type=str, default=None, help="Comma-separated tags, e.g. drums,lofi")
@click.option("--analyze", is_flag=True, default=False,
              help="Also run signal-analysis BPM detection on loop-categorized files lacking a filename BPM (slower).")
@json_option
@command
def add(name, path, tags, analyze):
    """Register (or update) external folder PATH under NAME.

    Indexes every audio file once: guesses a category (kick/snare/hat/loop/...),
    a BPM from the filename, the duration, and a loop/one-shot kind for all of
    them; pass --analyze to additionally run real tempo analysis on loops whose
    filename doesn't carry a BPM hint.
    """
    data = library_mod.add(name, path, tags=_split_tags(tags), analyze=analyze)
    return data, data


@library.command("list")
@json_option
@command
def list_():
    """List every registered library folder."""
    data = library_mod.list_entries()
    return data, data


@library.command("scan")
@click.argument("name", required=False, default=None)
@click.option("--analyze", is_flag=True, default=False,
              help="Also run signal-analysis BPM detection on loop-categorized files lacking a filename BPM (slower).")
@json_option
@command
def scan(name, analyze):
    """Re-scan one (or all) registered folders for added/removed files."""
    data = library_mod.scan(name, analyze=analyze)
    return data, data


@library.command("show")
@click.argument("name")
@json_option
@command
def show(name):
    """List every file in NAME with its category, kind, and BPM, as ready-to-use refs."""
    data = library_mod.show(name)
    return data, data


@library.command("search")
@click.argument("query", required=False, default=None)
@click.option("--library", "library_name", type=str, default=None, help="Scope the search to one registered library")
@click.option("--tag", type=str, default=None, help="Only consider libraries tagged with this")
@click.option("--category", type=str, default=None, help="Only match files guessed as this category (kick, snare, hat, loop, ...)")
@click.option("--kind", type=click.Choice(["loop", "one-shot", "unknown"]), default=None,
              help="Only match files of this kind (loop / one-shot / unknown)")
@click.option("--bpm", type=float, default=None, help=f"Only match files with a detected BPM within ±{library_mod.BPM_TOLERANCE} of this")
@json_option
@command
def search(query, library_name, tag, category, kind, bpm):
    """Search registered folders by filename keyword, tag, category, kind, and/or BPM."""
    data = library_mod.search(query, library=library_name, tag=tag, category=category, kind=kind, bpm=bpm)
    return data, data


@library.command("remove")
@click.argument("name")
@json_option
@command
def remove(name):
    """Unregister NAME (the folder and its files on disk are left untouched)."""
    data = library_mod.remove(name)
    return data, data
