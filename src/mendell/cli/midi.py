"""`mendell midi generate` — write a starter drum pattern straight onto a track."""

import click

from .. import midi_gen as midi_gen_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("midi")
def midi():
    """MIDI pattern generation."""


@midi.command("generate")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@click.option("--style", type=click.Choice(midi_gen_mod.DRUM_STYLES), required=True,
              help="Drum pattern style.")
@click.option("--bars", type=int, default=1, show_default=True, help="Bars to tile the pattern across.")
@click.option("--no-loop", is_flag=True, help="Don't set the clip to loop (defaults to looping).")
@json_option
@command
def generate(project, track, clip_name, style, bars, no_loop):
    """Generate a --style drum pattern, write it to <project>/midi/CLIP_NAME.mid,
    and import it as CLIP_NAME on TRACK (must be a 'midi' track) — ready to
    route to a sampler/kit and place in the arrangement.
    """
    project_dir = resolve_project(project)
    data = midi_gen_mod.generate(project_dir, track, clip_name, style=style, bars=bars, loop=not no_loop)
    return data, f"generated '{style}' pattern '{clip_name}' on track '{track}' ({bars} bar(s))"
