"""`mendell clip import/show/list/remove/set` and `mendell clip warp-marker ...`"""

import click

from .. import clips as clips_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("clip")
def clip():
    """Clip management (MIDI + audio)."""


@clip.command("import")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@click.option("--midi", "midi_path", type=str, default=None, help="Import a .mid file as a MIDI clip")
@click.option("--midi-track", "midi_track_index", type=int, default=None)
@click.option("--sample", "sample_path", type=str, default=None, help="Import an audio file as an audio clip")
@click.option("--link", is_flag=True, default=False, help="Reference the file in place instead of copying")
@click.option("--native-bpm", "native_bpm", type=float, default=None)
@click.option("--warp", type=str, default=None)
@json_option
@command
def import_(project, track, clip_name, midi_path, midi_track_index, sample_path, link, native_bpm, warp):
    project_dir = resolve_project(project)
    data = clips_mod.import_clip(
        project_dir, track, clip_name,
        midi_path=midi_path, midi_track_index=midi_track_index,
        sample_path=sample_path, link=link,
        native_bpm=native_bpm, warp=warp,
    )
    return data, data


@clip.command("show")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@json_option
@command
def show(project, track, clip_name):
    project_dir = resolve_project(project)
    data = clips_mod.show(project_dir, track, clip_name)
    return data, data


@clip.command("list")
@click.argument("project")
@click.argument("track")
@json_option
@command
def list_(project, track):
    project_dir = resolve_project(project)
    data = clips_mod.list_clips(project_dir, track)
    return data, data


@clip.command("remove")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@json_option
@command
def remove(project, track, clip_name):
    project_dir = resolve_project(project)
    data = clips_mod.remove(project_dir, track, clip_name)
    return data, f"removed clip '{clip_name}' from '{track}'"


@clip.command("set")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@click.option("--gain", type=float, default=None)
@click.option("--native-bpm", "native_bpm", type=float, default=None)
@click.option("--warp", type=str, default=None)
@click.option("--pitch", type=float, default=None)
@click.option("--loop/--no-loop", "loop", default=None)
@click.option("--loop-start", "loop_start", type=float, default=None)
@click.option("--loop-end", "loop_end", type=float, default=None)
@click.option("--transpose", type=int, default=None)
@click.option("--velocity-scale", "velocity_scale", type=float, default=None)
@json_option
@command
def set_(project, track, clip_name, gain, native_bpm, warp, pitch, loop, loop_start, loop_end,
         transpose, velocity_scale):
    project_dir = resolve_project(project)
    data = clips_mod.set_params(
        project_dir, track, clip_name,
        gain=gain, native_bpm=native_bpm, warp=warp, pitch=pitch, loop=loop,
        loop_start=loop_start, loop_end=loop_end,
        transpose=transpose, velocity_scale=velocity_scale,
    )
    return data, data


@click.group("warp-marker")
def warp_marker():
    """Warp marker management (Beats-mode audio clips only)."""


@warp_marker.command("add")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@click.option("--beat", type=float, required=True)
@click.option("--offset", type=str, required=True)
@json_option
@command
def warp_marker_add(project, track, clip_name, beat, offset):
    project_dir = resolve_project(project)
    data = clips_mod.warp_marker_add(project_dir, track, clip_name, beat=beat, offset=offset)
    return data, data


@warp_marker.command("list")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@json_option
@command
def warp_marker_list(project, track, clip_name):
    project_dir = resolve_project(project)
    data = clips_mod.warp_marker_list(project_dir, track, clip_name)
    return data, data


@warp_marker.command("remove")
@click.argument("project")
@click.argument("track")
@click.argument("clip_name")
@click.option("--beat", type=float, required=True)
@json_option
@command
def warp_marker_remove(project, track, clip_name, beat):
    project_dir = resolve_project(project)
    data = clips_mod.warp_marker_remove(project_dir, track, clip_name, beat=beat)
    return data, data


clip.add_command(warp_marker)
