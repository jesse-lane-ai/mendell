"""`mendell arrview ...` — arrangement-view commands.

Exposes the arrange_view module as a click group with sub-commands:
    show          — print the track×bar grid snapshot as JSON
    random-loop   — pick a random loop from the library and place it
    random-kit    — load a random drum kit onto a sampler track
    random-clip   — generate + place a random MIDI drum clip
"""

from __future__ import annotations

import click

from .. import arrange_view as av_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("arrview")
def arrview():
    """Arrangement view — inspect and fill a project's arrangement timeline."""


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

@arrview.command("show")
@click.argument("project")
@json_option
@command
def show(project):
    """Print a grid snapshot of PROJECT's arrangement (tracks × bars)."""
    project_dir = resolve_project(project)
    data = av_mod.view(project_dir)
    return data, data


# ---------------------------------------------------------------------------
# random-loop
# ---------------------------------------------------------------------------

@arrview.command("random-loop")
@click.argument("project")
@click.argument("track")
@click.option("--bars", type=int, default=8, show_default=True,
              help="Loop length hint (bars).")
@click.option("--start-bar", type=int, default=1, show_default=True,
              help="Bar position to place the clip.")
@click.option("--library", type=str, default=None,
              help="Restrict to a specific library name.")
@click.option("--seed", type=int, default=None,
              help="RNG seed for deterministic selection.")
@json_option
@command
def random_loop(project, track, bars, start_bar, library, seed):
    """Pick a random audio loop and place it on TRACK in PROJECT."""
    project_dir = resolve_project(project)
    data = av_mod.random_loop(
        project_dir, track,
        bars=bars, library=library, seed=seed, start_bar=start_bar,
    )
    return data, data


# ---------------------------------------------------------------------------
# random-kit
# ---------------------------------------------------------------------------

@arrview.command("random-kit")
@click.argument("project")
@click.option("--name", type=str, default=None,
              help="Sampler track name (default: 'kit').")
@click.option("--seed", type=int, default=None,
              help="RNG seed for deterministic selection.")
@json_option
@command
def random_kit(project, name, seed):
    """Load a randomly chosen drum kit onto PROJECT's sampler track."""
    project_dir = resolve_project(project)
    data = av_mod.random_kit(project_dir, name=name, seed=seed)
    return data, data


# ---------------------------------------------------------------------------
# random-clip
# ---------------------------------------------------------------------------

@arrview.command("random-clip")
@click.argument("project")
@click.argument("track")
@click.option("--style",
              type=click.Choice(["boom-bap", "lofi", "trap"], case_sensitive=False),
              default="lofi", show_default=True,
              help="Drum pattern style.")
@click.option("--bars", type=int, default=4, show_default=True,
              help="Pattern length in bars.")
@click.option("--start-bar", type=int, default=1, show_default=True,
              help="Bar position to place the clip.")
@click.option("--seed", type=int, default=None,
              help="RNG seed for deterministic selection.")
@json_option
@command
def random_clip(project, track, style, bars, start_bar, seed):
    """Generate and place a random MIDI drum clip on TRACK in PROJECT."""
    project_dir = resolve_project(project)
    data = av_mod.random_clip(
        project_dir, track,
        style=style, bars=bars, seed=seed, start_bar=start_bar,
    )
    return data, data
