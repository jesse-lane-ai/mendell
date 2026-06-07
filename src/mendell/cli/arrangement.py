"""`mendell arrange place/remove/list/set-loop/set`"""

import click

from .. import arrangement as arrangement_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("arrange")
def arrange():
    """Arrangement — clip placements and loop points."""


@arrange.command("place")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@click.option("--bar", type=int, required=True)
@json_option
@command
def place(project, track, clip_name, bar):
    project_dir = resolve_project(project)
    data = arrangement_mod.place(project_dir, track, clip_name, bar)
    return data, data


@arrange.command("remove")
@click.argument("project")
@click.argument("track")
@click.option("--bar", type=int, required=True)
@json_option
@command
def remove(project, track, bar):
    project_dir = resolve_project(project)
    data = arrangement_mod.remove(project_dir, track, bar)
    return data, data


@arrange.command("list")
@click.argument("project")
@json_option
@command
def list_(project):
    project_dir = resolve_project(project)
    data = arrangement_mod.list_placements(project_dir)
    return data, data


@arrange.command("set-loop")
@click.argument("project")
@click.option("--in", "loop_in", type=str, required=True)
@click.option("--out", "loop_out", type=str, required=True)
@json_option
@command
def set_loop(project, loop_in, loop_out):
    project_dir = resolve_project(project)
    data = arrangement_mod.set_loop(project_dir, loop_in=loop_in, loop_out=loop_out)
    return data, data


@arrange.command("set")
@click.argument("project")
@click.option("--loop/--no-loop", "loop", default=None)
@click.option("--loop-in", "loop_in", type=str, default=None)
@click.option("--loop-out", "loop_out", type=str, default=None)
@click.option("--length", type=float, default=None)
@json_option
@command
def set_(project, loop, loop_in, loop_out, length):
    project_dir = resolve_project(project)
    data = arrangement_mod.set_params(project_dir, loop=loop, loop_in=loop_in, loop_out=loop_out, length=length)
    return data, data
