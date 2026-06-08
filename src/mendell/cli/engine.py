"""`mendell export` — render the arrangement to WAV/MP3 with NDJSON progress."""

import click

from .. import engine as engine_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.command("export")
@click.argument("project")
@click.option("--out", type=str, default=None,
              help="Output file path (.wav or .mp3). Defaults to <project>/export/<project-name>.<format>.")
@click.option("--format", "fmt", type=click.Choice(["wav", "mp3"]), default=None,
              help="Output format when --out is omitted (default: wav).")
@click.option("--stems", is_flag=True, help="Also write one file per track to <out>_stems/.")
@json_option
@command
def export(project, out, fmt, stems):
    """Render the full arrangement to a stereo audio file."""
    project_dir = resolve_project(project)
    data = engine_mod.export(project_dir, out=out, format=fmt, stems=stems, progress=True)
    return data, data
