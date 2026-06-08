"""`mendell kit load` — auto-map a folder of one-shot drum samples onto a sampler."""

import click

from .. import kit as kit_mod
from .. import library as library_mod
from ..errors import BadInputError
from ..paths import resolve_project
from ._base import command, json_option


@click.group("kit")
def kit():
    """Drum-kit loading helpers."""


@kit.command("load")
@click.argument("project")
@click.argument("track")
@click.argument("folder", required=False, default=None)
@click.option("--library", "library_name", type=str, default=None,
              help="Load a registered library folder by name instead of FOLDER (see `mendell library`).")
@click.option("--start-note", "start_note", type=str, default=kit_mod.DEFAULT_START_NOTE,
              help="Note to start sequential mapping for one-shots that don't match a recognized drum name.")
@json_option
@command
def load(project, track, folder, library_name, start_note):
    """Create (or reuse) sampler TRACK and auto-map one-shots from FOLDER onto it.

    Filenames are matched against common drum names (kick, snare, clap, hat,
    tom, crash, ride, perc, ...) and assigned to their General MIDI percussion
    notes; anything unrecognized is mapped sequentially starting at --start-note.

    FOLDER may be a plain filesystem path, a registered library name, or a
    "<library-name>/<sub-path>" reference — or pass --library NAME to load a
    registered folder by name without spelling out a path at all.
    """
    if (folder is None) == (library_name is None):
        raise BadInputError("pass either FOLDER or --library, not both")
    if library_name is not None:
        resolved = library_mod.resolve_ref(library_name)
    else:
        resolved = library_mod.resolve_path_arg(folder)
    project_dir = resolve_project(project)
    data = kit_mod.load_kit(project_dir, track, str(resolved), start_note=start_note)
    return data, data
