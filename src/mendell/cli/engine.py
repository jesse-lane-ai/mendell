"""`mendell export` — render the arrangement to WAV/MP3 with NDJSON progress."""

import click

from .. import engine as engine_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.command("export")
@click.argument("project")
@click.option("--out", type=str, required=True, help="Output file path (.wav or .mp3).")
@click.option("--stems", is_flag=True, help="Also write one file per track to <out>_stems/.")
@json_option
@command
def export(project, out, stems):
    """Render the full arrangement to a stereo audio file."""
    project_dir = resolve_project(project)
    data = engine_mod.export(project_dir, out=out, stems=stems, progress=True)
    return data, data
