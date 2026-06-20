"""`mendell kits ...` — named, reusable drum kit registry.

Commands
--------
  create  — create (or re-open) a named kit
  add     — add a sample slot to a kit
  list    — show all saved kits
  show    — show one kit with its slot table
  remove  — delete a kit
  apply   — apply a kit to a sampler track inside a project
  quick   — assemble a kit automatically from the sample library
"""

import click

from .. import kits as kits_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("kits")
def kits():
    """Named, reusable drum kit registry."""


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

@kits.command("create")
@click.argument("name")
@click.option("--description", "-d", default="", help="Short description for the kit.")
@json_option
@command
def create(name, description):
    """Create (or reuse) a named kit called NAME."""
    data = kits_mod.create(name, description=description)
    return data, data


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

@kits.command("add")
@click.argument("kit")
@click.argument("note_or_category")
@click.argument("path")
@click.option("--name", "slot_name", default="", help="Optional display name for this slot.")
@json_option
@command
def add(kit, note_or_category, path, slot_name):
    """Add a sample slot to KIT.

    NOTE_OR_CATEGORY can be a GM MIDI note number (36), a drum category
    keyword (kick/snare/hat/clap/tom/crash/rim/perc), or a note name (C3).
    PATH is the audio file path (or a library reference such as
    mylib/drums/kick.wav).
    """
    data = kits_mod.add_slot(kit, note_or_category, path, slot_name=slot_name)
    return data, data


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@kits.command("list")
@json_option
@command
def list_():
    """List all saved kits."""
    data = kits_mod.list_kits()
    return data, data


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

@kits.command("show")
@click.argument("name")
@json_option
@command
def show(name):
    """Show kit NAME with its full slot table."""
    data = kits_mod.show(name)
    return data, data


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

@kits.command("remove")
@click.argument("name")
@json_option
@command
def remove(name):
    """Delete kit NAME and all its slots."""
    data = kits_mod.remove(name)
    return data, data


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

@kits.command("apply")
@click.argument("kit")
@click.argument("project")
@click.argument("track")
@json_option
@command
def apply(kit, project, track):
    """Apply KIT to sampler TRACK inside PROJECT.

    Creates the sampler track if it does not exist, then maps every kit slot
    onto it.  Safe to run multiple times (idempotent).
    """
    project_dir = resolve_project(project)
    data = kits_mod.apply_to_project(kit, project_dir, track)
    return data, data


# ---------------------------------------------------------------------------
# quick
# ---------------------------------------------------------------------------

@kits.command("quick")
@click.argument("name")
@click.option(
    "--library", "library_name", default=None,
    help="Restrict sample search to this registered library name."
)
@click.option("--seed", type=int, default=None, help="Random seed for reproducible selection.")
@json_option
@command
def quick(name, library_name, seed):
    """Auto-assemble kit NAME from the sample library.

    Picks one one-shot per core drum category (kick/snare/hat/clap/tom/crash/rim)
    from whatever is registered in your library.  Pass --library to restrict to
    one folder, --seed for reproducible results.
    """
    data = kits_mod.quick_kit(name, library=library_name, seed=seed)
    return data, data
