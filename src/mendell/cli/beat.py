"""`mendell beat new` — scaffold a ready-to-go beat project from a style preset."""

from pathlib import Path

import click

from .. import beat as beat_mod
from ._base import command, json_option


@click.group("beat")
def beat():
    """Quick-start beat scaffolding."""


@beat.command("new")
@click.argument("name")
@click.option("--style", type=click.Choice(sorted(beat_mod.STYLES)), required=True,
              help="Style preset — sets tempo/key defaults and a starter drum pattern.")
@json_option
@command
def new(name, style):
    """Scaffold NAME: a project seeded with --style's tempo/key, a MIDI 'drums'
    track routed to a sampler 'kit' track, and a looping starter drum pattern
    placed across the arrangement — ready for `mendell kit load` and `export`.
    """
    data = beat_mod.new(Path.cwd(), name, style=style)
    return data, f"created beat '{name}' ({style}) at {data['project']['path']}"
