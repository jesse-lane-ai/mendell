"""`mendell midilib` — global MIDI clip catalog commands.

Manages a library of reusable MIDI patterns stored in the shared library.db
(same DB as the sample library and project registry).  Patterns are independent
of any project and can be imported into a project via
``mendell clip import --midi <path>``.

Sub-commands
------------
import      Register an external .mid file in the catalog.
generate    Generate a drum-pattern clip from a style preset and catalog it.
create      Build a clip from a JSON note-grid (backs the visual step editor).
list        List all cataloged MIDI clips (optionally filtered by category).
show        Show metadata for one clip.
remove      Delete a clip from the catalog.
summary     Parse any .mid file and return its note-grid JSON.
"""

import json
import sys

import click

from .. import midi_catalog as cat_mod
from ._base import command, json_option


@click.group("midilib")
def midilib():
    """Global MIDI clip catalog (cross-project)."""


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@midilib.command("import")
@click.argument("path")
@click.option("--name", required=True, help="Catalog name for this clip.")
@click.option("--category", default="other", show_default=True,
              type=click.Choice(["drums", "bass", "melody", "chords", "perc", "other"]),
              help="Clip category.")
@json_option
@command
def import_(path, name, category):
    """Register an external .mid file in the MIDI catalog.

    The file is copied into the catalog store; the original is not modified.
    Re-running with the same NAME overwrites the existing entry (idempotent).
    """
    data = cat_mod.import_file(path, name=name, category=category)
    return data, f"imported '{name}' ({data['note_count']} notes, {data['bars']} bar(s))"


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

@midilib.command("generate")
@click.argument("name")
@click.option("--style", required=True,
              type=click.Choice(["boom-bap", "lofi", "trap"]),
              help="Drum pattern style.")
@click.option("--bars", type=int, default=1, show_default=True,
              help="Bars to tile the pattern across.")
@click.option("--bpm", type=float, default=None,
              help="Reference BPM to store (does not affect the .mid content).")
@click.option("--category", default="drums", show_default=True,
              type=click.Choice(["drums", "bass", "melody", "chords", "perc", "other"]),
              help="Clip category.")
@json_option
@command
def generate(name, style, bars, bpm, category):
    """Generate a drum-pattern MIDI clip and add it to the catalog."""
    data = cat_mod.generate(name, style=style, bars=bars, bpm=bpm, category=category)
    return data, f"generated '{name}' ({style}, {bars} bar(s))"


# ---------------------------------------------------------------------------
# create (from notes JSON)
# ---------------------------------------------------------------------------

@midilib.command("create")
@click.argument("name")
@click.option("--notes-file", "notes_file", default=None,
              help="Path to a JSON file containing the notes array.")
@click.option("--notes-json", "notes_json", default=None,
              help="Inline JSON string for the notes array (alternative to --notes-file).")
@click.option("--bpm", type=float, default=120.0, show_default=True,
              help="Reference BPM.")
@click.option("--bars", type=int, default=1, show_default=True,
              help="Clip length in bars.")
@click.option("--category", default="other", show_default=True,
              type=click.Choice(["drums", "bass", "melody", "chords", "perc", "other"]),
              help="Clip category.")
@json_option
@command
def create(name, notes_file, notes_json, bpm, bars, category):
    """Build a MIDI clip from a note-grid JSON and catalog it.

    Notes format: a JSON array of objects, each with keys:
      pitch        MIDI note number (0-127)
      start_beat   Beat offset from clip start (float; 4 beats = 1 bar)
      length_beats Note duration in beats (float; default 0.25)
      velocity     MIDI velocity 1-127 (default 100)

    Example: '[{"pitch":36,"start_beat":0,"length_beats":0.5,"velocity":110}]'

    Provide notes via --notes-file <path.json> or --notes-json '<json>'.
    """
    if notes_file and notes_json:
        click.echo("Provide either --notes-file or --notes-json, not both.", err=True)
        sys.exit(1)
    if notes_file:
        try:
            raw = open(notes_file).read()
        except OSError as exc:
            click.echo(f"error reading notes file: {exc}", err=True)
            sys.exit(1)
    elif notes_json:
        raw = notes_json
    else:
        click.echo("Provide --notes-file or --notes-json.", err=True)
        sys.exit(1)

    try:
        notes = json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"invalid JSON: {exc}", err=True)
        sys.exit(1)

    data = cat_mod.create_from_notes(name, notes, bpm=bpm, bars=bars, category=category)
    return data, f"created '{name}' ({data['note_count']} notes, {bars} bar(s))"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@midilib.command("list")
@click.option("--category", default=None,
              type=click.Choice(["drums", "bass", "melody", "chords", "perc", "other"]),
              help="Filter by category.")
@json_option
@command
def list_(category):
    """List MIDI clips in the catalog."""
    data = cat_mod.list_clips(category=category)
    return data, f"{data['count']} clip(s)"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

@midilib.command("show")
@click.argument("name")
@json_option
@command
def show(name):
    """Show metadata for a cataloged MIDI clip."""
    data = cat_mod.show(name)
    return data, data


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

@midilib.command("remove")
@click.argument("name")
@json_option
@command
def remove(name):
    """Remove a MIDI clip from the catalog (also deletes the stored .mid file)."""
    data = cat_mod.remove(name)
    return data, f"removed '{name}'"


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

@midilib.command("summary")
@click.argument("path")
@json_option
@command
def summary(path):
    """Parse a .mid file and return its note-grid as JSON.

    PATH can be a filesystem path or the path stored for a cataloged clip
    (use `mendell midilib show <name>` to get it).  The output is suitable
    for feeding back into `mendell midilib create` after editing.
    """
    data = cat_mod.summary(path)
    return data, f"{len(data['notes'])} note(s), {data['bars']} bar(s)"
