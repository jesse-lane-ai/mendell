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
@json_option
@command
def add(name, path, tags):
    """Register (or update) external folder PATH under NAME."""
    data = library_mod.add(name, path, tags=_split_tags(tags))
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
@json_option
@command
def scan(name):
    """Re-scan one (or all) registered folders for added/removed files."""
    data = library_mod.scan(name)
    return data, data


@library.command("show")
@click.argument("name")
@json_option
@command
def show(name):
    """List every file in NAME with its guessed category, as a ready-to-use ref."""
    data = library_mod.show(name)
    return data, data


@library.command("search")
@click.argument("query", required=False, default=None)
@click.option("--library", "library_name", type=str, default=None, help="Scope the search to one registered library")
@click.option("--tag", type=str, default=None, help="Only consider libraries tagged with this")
@click.option("--category", type=str, default=None, help="Only match files guessed as this category (kick, snare, hat, loop, ...)")
@json_option
@command
def search(query, library_name, tag, category):
    """Search registered folders by filename keyword, tag, and/or category."""
    data = library_mod.search(query, library=library_name, tag=tag, category=category)
    return data, data


@library.command("remove")
@click.argument("name")
@json_option
@command
def remove(name):
    """Unregister NAME (the folder and its files on disk are left untouched)."""
    data = library_mod.remove(name)
    return data, data
