"""`mendell auto add/list/remove/clear`"""

import click

from .. import automation as automation_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("auto")
def auto():
    """Automation — time-indexed parameter changes."""


@auto.command("add")
@click.argument("project")
@click.argument("track")
@click.argument("param")
@click.option("--at", type=str, required=True)
@click.option("--value", type=float, required=True)
@click.option("--curve", type=click.Choice(list(automation_mod.CURVES)), default=automation_mod.DEFAULT_CURVE)
@json_option
@command
def add(project, track, param, at, value, curve):
    project_dir = resolve_project(project)
    data = automation_mod.add(project_dir, track, param, at=at, value=value, curve=curve)
    return data, data


@auto.command("list")
@click.argument("project")
@click.argument("track")
@click.option("--param", type=str, default=None)
@json_option
@command
def list_(project, track, param):
    project_dir = resolve_project(project)
    data = automation_mod.list_points(project_dir, track, param)
    return data, data


@auto.command("remove")
@click.argument("project")
@click.argument("track")
@click.argument("param")
@click.option("--at", type=str, required=True)
@json_option
@command
def remove(project, track, param, at):
    project_dir = resolve_project(project)
    data = automation_mod.remove(project_dir, track, param, at=at)
    return data, data


@auto.command("clear")
@click.argument("project")
@click.argument("track")
@click.option("--param", type=str, default=None)
@json_option
@command
def clear(project, track, param):
    project_dir = resolve_project(project)
    data = automation_mod.clear(project_dir, track, param)
    return data, data
