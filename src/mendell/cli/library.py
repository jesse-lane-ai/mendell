"""`mendell library ...` — register and search named external sample folders."""

import click

from .. import config as config_mod
from .. import library as library_mod
from ..recognize import list_backends
from ._base import command, json_option


def _split_tags(tags: str | None) -> list[str] | None:
    if tags is None:
        return None
    return [t.strip() for t in tags.split(",") if t.strip()]


def _resolve_recognizer(recognize: str | None) -> str | None:
    """``--recognize`` if given, else the ``library.recognizer`` config
    default, else ``None`` (recognition off — filename-only, unchanged)."""
    return recognize or config_mod.library_recognizer_default()


_RECOGNIZE_OPTION = click.option(
    "--recognize", type=click.Choice(list_backends()), default=None,
    help="Content-based recognition backend for category/instruments on the "
         "filename-fallback path (default: the 'library.recognizer' config "
         "setting, or off if unset).",
)


@click.group("library")
def library():
    """Sample library — register external folders and reference them by name."""


@library.command("add")
@click.argument("name")
@click.argument("path")
@click.option("--tags", type=str, default=None, help="Comma-separated tags, e.g. drums,lofi")
@click.option("--analyze", is_flag=True, default=False,
              help="Also run signal-analysis BPM detection on loop-categorized files lacking a filename BPM (slower).")
@_RECOGNIZE_OPTION
@json_option
@command
def add(name, path, tags, analyze, recognize):
    """Register (or update) external folder PATH under NAME.

    Indexes every audio file once: guesses a category (kick/snare/hat/loop/...),
    a BPM from the filename, the duration, and a loop/one-shot kind for all of
    them; pass --analyze to additionally run real tempo analysis on loops whose
    filename doesn't carry a BPM hint.

    --recognize <backend> additionally listens to each file's audio (like
    --analyze) and fills in `instruments` plus a content-based `category` on
    files the filename guess couldn't classify (heuristic/clap/gemini-embedding/
    gemini-generative — see `mendell config set library.recognizer` for a default).
    """
    data = library_mod.add(name, path, tags=_split_tags(tags), analyze=analyze, recognize=_resolve_recognizer(recognize))
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
@_RECOGNIZE_OPTION
@json_option
@command
def scan(name, analyze, recognize):
    """Re-scan one (or all) registered folders for added/removed files.

    --recognize <backend> behaves as in `library add`; unchanged files
    (same path + mtime) reuse their cached recognition instead of re-running
    the backend, so cloud backends aren't re-billed on every scan.
    """
    data = library_mod.scan(name, analyze=analyze, recognize=_resolve_recognizer(recognize))
    return data, data


@library.command("show")
@click.argument("name")
@json_option
@command
def show(name):
    """List every file in NAME with its category, kind, BPM, and (if
    recognized) instruments, as ready-to-use refs."""
    data = library_mod.show(name)
    return data, data


@library.command("search")
@click.argument("query", required=False, default=None)
@click.option("--library", "library_name", type=str, default=None, help="Scope the search to one registered library")
@click.option("--tag", type=str, default=None, help="Only consider libraries tagged with this")
@click.option("--category", type=str, default=None, help="Only match files guessed as this category (kick, snare, hat, loop, ...)")
@click.option("--kind", type=click.Choice(["loop", "one-shot", "unknown"]), default=None,
              help="Only match files of this kind (loop / one-shot / unknown)")
@click.option("--instrument", type=str, default=None, help="Only match files whose recognized instruments include this (e.g. piano, bass)")
@click.option("--bpm", type=float, default=None, help=f"Only match files with a detected BPM within ±{library_mod.BPM_TOLERANCE} of this")
@json_option
@command
def search(query, library_name, tag, category, kind, instrument, bpm):
    """Search registered folders by filename keyword, tag, category, kind, instrument, and/or BPM."""
    data = library_mod.search(query, library=library_name, tag=tag, category=category, kind=kind, instrument=instrument, bpm=bpm)
    return data, data


@library.command("serve")
@click.option("--host", default="127.0.0.1", help="Interface to bind (default: localhost only).")
@click.option("--port", type=int, default=8765, help="Port to listen on (default: 8765).")
@click.option("--no-open", "no_open", is_flag=True, default=False, help="Don't auto-open a browser.")
def serve(host, port, no_open):
    """Launch a local web UI to browse, audition, and manage the library.

    Serves a single ``library.html`` page (stdlib HTTP server, no extra deps)
    that lists every registered library, plays samples in the browser, and lets
    you search, re-scan, add, and unregister folders. Runs until Ctrl-C.
    """
    from .. import library_server
    library_server.serve(host=host, port=port, open_browser=not no_open)


@library.command("remove")
@click.argument("name")
@json_option
@command
def remove(name):
    """Unregister NAME (the folder and its files on disk are left untouched)."""
    data = library_mod.remove(name)
    return data, data


@library.command("export")
@click.argument("out_zip")
@click.option(
    "--include",
    type=str,
    default=None,
    help="Comma-separated list of content types to bundle: samples,kits,projects,midi "
         "(default: all four).",
)
@json_option
@command
def export_bundle_cmd(out_zip, include):
    """Export the library (samples, kits, projects, MIDI) to a zip bundle at OUT_ZIP.

    The bundle contains a manifest.json plus the actual files, all at relative
    paths.  Import on another machine with `mendell library import <bundle.zip>`.
    """
    from .. import library_bundle as bundle_mod
    include_list = [s.strip() for s in include.split(",")] if include else None
    data = bundle_mod.export_bundle(out_zip, include=include_list)
    return data, data


@library.command("import")
@click.argument("bundle_zip")
@click.option(
    "--dest",
    type=str,
    default=None,
    help="Directory to extract bundle files into (default: alongside the zip, "
         "in a folder named after the zip without its extension).",
)
@json_option
@command
def import_bundle_cmd(bundle_zip, dest):
    """Import a zip bundle created by `mendell library export`.

    Extracts sample files, kits, project directories, and MIDI clips into DEST,
    then registers them in the local library DB (create-or-update; idempotent).
    Returns counts of added/updated/skipped items per content type.
    """
    from pathlib import Path

    from .. import library_bundle as bundle_mod
    from ..errors import BadInputError

    zip_path = Path(bundle_zip)
    if not zip_path.is_file():
        raise BadInputError(f"bundle not found: {bundle_zip}")

    dest_dir = Path(dest) if dest else zip_path.parent / zip_path.stem
    data = bundle_mod.import_bundle(zip_path, dest_dir)
    return data, data
