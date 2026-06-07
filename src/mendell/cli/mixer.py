"""`mendell mix set/mute/solo/show` and `mendell mix fx add/set/remove/list`"""

import click

from .. import mixer as mixer_mod
from ..fx import schema as fx_schema
from ..paths import resolve_project
from ._base import command, json_option


@click.group("mix")
def mix():
    """Mixer — vol/pan/mute/solo and FX chains."""


@mix.command("set")
@click.argument("project")
@click.argument("track")
@click.option("--vol", type=int, default=None)
@click.option("--pan", type=int, default=None)
@json_option
@command
def set_(project, track, vol, pan):
    project_dir = resolve_project(project)
    data = mixer_mod.set_mixer(project_dir, track, vol=vol, pan=pan)
    return data, data


@mix.command("mute")
@click.argument("project")
@click.argument("track")
@click.option("--off", is_flag=True, default=False)
@json_option
@command
def mute(project, track, off):
    project_dir = resolve_project(project)
    data = mixer_mod.set_mute(project_dir, track, on=not off)
    return data, data


@mix.command("solo")
@click.argument("project")
@click.argument("track")
@click.option("--off", is_flag=True, default=False)
@json_option
@command
def solo(project, track, off):
    project_dir = resolve_project(project)
    data = mixer_mod.set_solo(project_dir, track, on=not off)
    return data, data


@mix.command("show")
@click.argument("project")
@json_option
@command
def show(project):
    project_dir = resolve_project(project)
    data = mixer_mod.show(project_dir)
    return data, data


@click.group("fx")
def fx():
    """FX chain management (stable per-track ids)."""


def _parse_fx_params(fx_type, kv_pairs):
    """Turn ('--room', '0.6') style extra args into a coerced params dict."""
    params = {}
    it = iter(kv_pairs)
    for key in it:
        if not key.startswith("--"):
            raise click.BadParameter(f"unexpected argument '{key}'")
        name = key[2:].replace("-", "_")
        try:
            value = next(it)
        except StopIteration:
            raise click.BadParameter(f"missing value for '{key}'")
        params[name] = value
    return fx_schema.coerce_params(fx_type, params) if fx_type else params


@fx.command("add", context_settings={"ignore_unknown_options": True})
@click.argument("project")
@click.argument("track")
@click.argument("fx_type")
@click.argument("params", nargs=-1, type=click.UNPROCESSED)
@json_option
@command
def fx_add(project, track, fx_type, params):
    project_dir = resolve_project(project)
    raw = _parse_fx_params(fx_type, params)
    data = mixer_mod.fx_add(project_dir, track, fx_type, **raw)
    return data, data


@fx.command("set", context_settings={"ignore_unknown_options": True})
@click.argument("project")
@click.argument("track")
@click.argument("fx_id", type=int)
@click.argument("params", nargs=-1, type=click.UNPROCESSED)
@json_option
@command
def fx_set(project, track, fx_id, params):
    project_dir = resolve_project(project)
    raw = _parse_fx_params(None, params)
    data = mixer_mod.fx_set(project_dir, track, fx_id, **raw)
    return data, data


@fx.command("remove")
@click.argument("project")
@click.argument("track")
@click.argument("fx_id", type=int)
@json_option
@command
def fx_remove(project, track, fx_id):
    project_dir = resolve_project(project)
    data = mixer_mod.fx_remove(project_dir, track, fx_id)
    return data, data


@fx.command("list")
@click.argument("project")
@click.argument("track")
@json_option
@command
def fx_list(project, track):
    project_dir = resolve_project(project)
    data = mixer_mod.fx_list(project_dir, track)
    return data, data


mix.add_command(fx)
