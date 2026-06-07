"""`mendell timing` — convenience time-position calculator."""

import click

from .. import project as project_mod
from ..paths import resolve_project
from ..timing import resolve_timing
from ._base import command, json_option


@click.command("timing")
@click.argument("project")
@click.option("--bar", type=int, default=None)
@click.option("--beat", type=float, default=None)
@click.option("--seconds", type=float, default=None)
@click.option("--frames", type=int, default=None)
@json_option
@command
def timing(project, bar, beat, seconds, frames):
    project_dir = resolve_project(project)
    tp = project_mod.timing_params(project_dir)
    result = resolve_timing(
        bpm=tp["bpm"],
        sample_rate=tp["sample_rate"],
        beats_per_bar=tp["beats_per_bar"],
        bar=bar,
        beat=beat,
        seconds=seconds,
        frames=frames,
    )
    data = result.to_dict()
    return data, data
