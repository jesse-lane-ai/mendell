"""Mendell CLI entrypoint — wires every subcommand into one `click` group."""

import click

from .. import config as config_mod
from . import ace as ace_cli
from . import arrange_view as arrange_view_cli
from . import arrangement as arrangement_cli
from . import automation as automation_cli
from . import beat as beat_cli
from . import classify as classify_cli
from . import clips as clips_cli
from . import config as config_cli
from . import engine as engine_cli
from . import kit as kit_cli
from . import kits as kits_cli
from . import library as library_cli
from . import midi as midi_cli
from . import midi_catalog as midi_catalog_cli
from . import mixer as mixer_cli
from . import project as project_cli
from . import registry as registry_cli
from . import sampler as sampler_cli
from . import timing as timing_cli
from . import tracks as tracks_cli


@click.group()
@click.version_option(package_name="mendell")
def cli():
    """Mendell — agent-first music production CLI."""
    # First-run setup: materialize config.json and create the projects folder.
    # Best-effort — never blocks a command if the config dir isn't writable.
    config_mod.ensure_initialized()


cli.add_command(project_cli.new)
cli.add_command(project_cli.info)
cli.add_command(project_cli.set_)
cli.add_command(config_cli.config)
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
cli.add_command(kits_cli.kits)
cli.add_command(library_cli.library)
cli.add_command(registry_cli.projects)
cli.add_command(beat_cli.beat)
cli.add_command(midi_cli.midi)
cli.add_command(midi_catalog_cli.midilib)
cli.add_command(classify_cli.classify)
cli.add_command(arrange_view_cli.arrview)
cli.add_command(ace_cli.ace)


if __name__ == "__main__":
    cli()
