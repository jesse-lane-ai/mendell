"""`mendell sampler ...` and `mendell route ...`"""

import click

from .. import library as library_mod
from .. import routing as routing_mod
from .. import sampler as sampler_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("sampler")
def sampler():
    """Sampler instrument management."""


@sampler.command("create")
@click.argument("project")
@click.argument("track")
@json_option
@command
def create(project, track):
    project_dir = resolve_project(project)
    data = sampler_mod.create(project_dir, track)
    return data, f"sampler ready on track '{track}'"


@sampler.command("set")
@click.argument("project")
@click.argument("track")
@click.option("--polyphony", type=int, default=None)
@click.option("--tune", type=int, default=None)
@json_option
@command
def set_(project, track, polyphony, tune):
    project_dir = resolve_project(project)
    data = sampler_mod.set_params(project_dir, track, polyphony=polyphony, tune=tune)
    return data, data


@sampler.command("show")
@click.argument("project")
@click.argument("track")
@json_option
@command
def show(project, track):
    project_dir = resolve_project(project)
    data = sampler_mod.show(project_dir, track)
    return data, data


@sampler.command("import")
@click.argument("project")
@click.argument("track")
@click.argument("folder")
@click.option("--start-note", "start_note", type=str, required=True)
@json_option
@command
def import_(project, track, folder, start_note):
    project_dir = resolve_project(project)
    data = sampler_mod.bulk_import(
        project_dir, track, library_mod.resolve_path_arg(folder), start_note=start_note,
    )
    return data, data


@click.group("map")
def map_group():
    """Sample-map slot management."""


@map_group.command("add")
@click.argument("project")
@click.argument("track")
@click.option("--note", type=str, default=None)
@click.option("--range", "note_range", type=str, default=None)
@click.option("--sample", type=str, required=True)
@click.option("--root", type=str, default=None)
@click.option("--link", is_flag=True, default=False)
@click.option("--loop", is_flag=True, default=False)
@json_option
@command
def map_add(project, track, note, note_range, sample, root, link, loop):
    project_dir = resolve_project(project)
    data = sampler_mod.map_add(
        project_dir, track, note=note, note_range=note_range,
        sample=library_mod.resolve_path_arg(sample),
        root=root, link=link, loop=loop,
    )
    return data, data


@map_group.command("set")
@click.argument("project")
@click.argument("track")
@click.option("--note", type=str, required=True)
@click.option("--root", type=str, default=None)
@click.option("--vol", type=int, default=None)
@click.option("--pan", type=int, default=None)
@click.option("--tune", type=int, default=None)
@click.option("--pitch-follow/--no-pitch-follow", "pitch_follow", default=None)
@click.option("--loop", type=click.Choice(["off", "forward", "pingpong"]), default=None)
@click.option("--loop-start", "loop_start", type=float, default=None)
@click.option("--loop-end", "loop_end", type=float, default=None)
@click.option("--attack", type=str, default=None)
@click.option("--decay", type=str, default=None)
@click.option("--sustain", type=int, default=None)
@click.option("--release", type=str, default=None)
@json_option
@command
def map_set(project, track, note, root, vol, pan, tune, pitch_follow, loop, loop_start,
            loop_end, attack, decay, sustain, release):
    project_dir = resolve_project(project)
    data = sampler_mod.map_set(
        project_dir, track, note,
        root=root, vol=vol, pan=pan, tune=tune, pitch_follow=pitch_follow, loop=loop,
        loop_start=loop_start, loop_end=loop_end,
        attack=attack, decay=decay, sustain=sustain, release=release,
    )
    return data, data


@map_group.command("list")
@click.argument("project")
@click.argument("track")
@json_option
@command
def map_list(project, track):
    project_dir = resolve_project(project)
    data = sampler_mod.map_list(project_dir, track)
    return data, data


@map_group.command("remove")
@click.argument("project")
@click.argument("track")
@click.option("--note", type=str, required=True)
@json_option
@command
def map_remove(project, track, note):
    project_dir = resolve_project(project)
    data = sampler_mod.map_remove(project_dir, track, note)
    return data, data


sampler.add_command(map_group)


@click.group("route")
def route():
    """MIDI -> Sampler routing."""


@route.command("set")
@click.argument("project")
@click.option("--from", "from_track", type=str, required=True)
@click.option("--to", "to_track", type=str, required=True)
@json_option
@command
def route_set(project, from_track, to_track):
    project_dir = resolve_project(project)
    data = routing_mod.set_route(project_dir, from_track, to_track)
    return data, data


@route.command("remove")
@click.argument("project")
@click.option("--from", "from_track", type=str, required=True)
@click.option("--to", "to_track", type=str, required=True)
@json_option
@command
def route_remove(project, from_track, to_track):
    project_dir = resolve_project(project)
    data = routing_mod.remove_route(project_dir, from_track, to_track)
    return data, data


@route.command("list")
@click.argument("project")
@json_option
@command
def route_list(project):
    project_dir = resolve_project(project)
    data = routing_mod.list_routes(project_dir)
    return data, data
