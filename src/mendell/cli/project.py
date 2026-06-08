"""`mendell new` / `mendell info` / `mendell set`"""

from pathlib import Path

import click

from .. import project as project_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.command("new")
@click.argument("name")
@click.option("--bpm", type=float, default=None)
@click.option("--key", type=str, default=None)
@click.option("--scale", type=str, default=None)
@click.option("--sample-rate", "sample_rate", type=int, default=None)
@click.option("--time-sig", "time_sig", type=str, default=None)
@json_option
@command
def new(name, bpm, key, scale, sample_rate, time_sig):
    kwargs = {}
    for k, v in (
        ("bpm", bpm),
        ("key", key),
        ("scale", scale),
        ("sample_rate", sample_rate),
        ("time_sig", time_sig),
    ):
        if v is not None:
            kwargs[k] = v

    # `name` may be a path (e.g. `mendell new /songs/my-song` or `mendell new
    # sub/my-song`) — split it into parent dir + bare project name so the
    # stored `project.name` is always just the directory's basename, never a
    # path string (which would otherwise corrupt default export-path resolution).
    target = Path.cwd() / name
    project_dir = project_mod.create(target.parent, target.name, **kwargs)
    data = project_mod.info(project_dir)
    return data, f"created project '{target.name}' at {project_dir}"


@click.command("info")
@click.argument("project")
@json_option
@command
def info(project):
    project_dir = resolve_project(project)
    data = project_mod.info(project_dir)
    return data, data


@click.command("set")
@click.argument("project")
@click.option("--bpm", type=float, default=None)
@click.option("--key", type=str, default=None)
@click.option("--scale", type=str, default=None)
@click.option("--time-sig", "time_sig", type=str, default=None)
@click.option("--master-vol", "master_vol", type=int, default=None)
@click.option("--limiter-ceiling", "limiter_ceiling", type=float, default=None)
@json_option
@command
def set_(project, bpm, key, scale, time_sig, master_vol, limiter_ceiling):
    project_dir = resolve_project(project)
    data = project_mod.set_params(
        project_dir,
        bpm=bpm,
        key=key,
        scale=scale,
        time_sig=time_sig,
        master_vol=master_vol,
        limiter_ceiling=limiter_ceiling,
    )
    return data, data
