"""`mendell kit load` — auto-map a folder of one-shot drum samples onto a sampler."""

import click

from .. import kit as kit_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("kit")
def kit():
    """Drum-kit loading helpers."""


@kit.command("load")
@click.argument("project")
@click.argument("track")
@click.argument("folder")
@click.option("--start-note", "start_note", type=str, default=kit_mod.DEFAULT_START_NOTE,
              help="Note to start sequential mapping for one-shots that don't match a recognized drum name.")
@json_option
@command
def load(project, track, folder, start_note):
    """Create (or reuse) sampler TRACK and auto-map one-shots from FOLDER onto it.

    Filenames are matched against common drum names (kick, snare, clap, hat,
    tom, crash, ride, perc, ...) and assigned to their General MIDI percussion
    notes; anything unrecognized is mapped sequentially starting at --start-note.
    """
    project_dir = resolve_project(project)
    data = kit_mod.load_kit(project_dir, track, folder, start_note=start_note)
    return data, data
