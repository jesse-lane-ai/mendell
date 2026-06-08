"""Mendell CLI entrypoint — wires every subcommand into one `click` group."""

import click

from . import arrangement as arrangement_cli
from . import automation as automation_cli
from . import beat as beat_cli
from . import clips as clips_cli
from . import engine as engine_cli
from . import kit as kit_cli
from . import midi as midi_cli
from . import mixer as mixer_cli
from . import project as project_cli
from . import sampler as sampler_cli
from . import timing as timing_cli
from . import tracks as tracks_cli


@click.group()
@click.version_option(package_name="mendell")
def cli():
    """Mendell — agent-first music production CLI."""


cli.add_command(project_cli.new)
cli.add_command(project_cli.info)
cli.add_command(project_cli.set_)
cli.add_command(timing_cli.timing)
cli.add_command(tracks_cli.track)
cli.add_command(clips_cli.clip)
cli.add_command(sampler_cli.sampler)
cli.add_command(sampler_cli.route)
cli.add_command(arrangement_cli.arrange)
cli.add_command(mixer_cli.mix)
cli.add_command(automation_cli.auto)
cli.add_command(engine_cli.export)
cli.add_command(kit_cli.kit)
cli.add_command(beat_cli.beat)
cli.add_command(midi_cli.midi)


if __name__ == "__main__":
    cli()
