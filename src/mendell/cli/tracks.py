"""`mendell track add/remove/list/show`"""

import click

from .. import tracks as tracks_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("track")
def track():
    """Track management."""


@track.command("add")
@click.argument("project")
@click.argument("name")
@click.option("--type", "track_type", type=click.Choice(["midi", "audio", "sampler"]), required=True)
@json_option
@command
def add(project, name, track_type):
    project_dir = resolve_project(project)
    data = tracks_mod.add(project_dir, name, track_type)
    return data, f"track '{name}' ({track_type}) ready"


@track.command("remove")
@click.argument("project")
@click.argument("name")
@json_option
@command
def remove(project, name):
    project_dir = resolve_project(project)
    data = tracks_mod.remove(project_dir, name)
    return data, f"removed track '{name}'"


@track.command("list")
@click.argument("project")
@json_option
@command
def list_(project):
    project_dir = resolve_project(project)
    data = tracks_mod.list_tracks(project_dir)
    return data, data


@track.command("show")
@click.argument("project")
@click.argument("name")
@json_option
@command
def show(project, name):
    project_dir = resolve_project(project)
    data = tracks_mod.show(project_dir, name)
    return data, data


@track.command("set")
@click.argument("project")
@click.argument("name")
@click.option("--name", "new_name", type=str, default=None)
@click.option("--type", "new_type", type=click.Choice(["midi", "audio", "sampler"]), default=None)
@json_option
@command
def set_(project, name, new_name, new_type):
    project_dir = resolve_project(project)
    data = tracks_mod.set_params(project_dir, name, new_name=new_name, new_type=new_type)
    return data, data
