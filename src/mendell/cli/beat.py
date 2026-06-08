"""`mendell beat new` — scaffold a ready-to-go beat project from a style preset."""

from pathlib import Path

import click

from .. import beat as beat_mod
from ._base import command, json_option


@click.group("beat")
def beat():
    """Quick-start beat scaffolding."""


@beat.command("new")
@click.argument("name")
@click.option("--style", type=click.Choice(sorted(beat_mod.STYLES)), required=True,
              help="Style preset — sets tempo/key defaults and a starter drum pattern.")
@json_option
@command
def new(name, style):
    """Scaffold NAME: a project seeded with --style's tempo/key, a MIDI 'drums'
    track routed to a sampler 'kit' track, and a looping starter drum pattern
    placed across the arrangement — ready for `mendell kit load` and `export`.
    """
    data = beat_mod.new(Path.cwd(), name, style=style)
    return data, f"created beat '{name}' ({style}) at {data['project']['path']}"


@beat.command("make")
@click.argument("name")
@click.option("--style", type=click.Choice(sorted(beat_mod.STYLES)), required=True, help="Style preset.")
@click.option("--bpm", type=float, help="Override BPM.")
@click.option("--key", help="Override key.")
@click.option("--duration", default="60s", help="Target duration (e.g. 60s).")
@click.option("--variations", default=8, type=int, help="Number of 8-bar variation sections.")
@click.option("--kit", type=click.Path(exists=True), help="Path to one-shot folder (uses minimum 5-10 shots).")
@click.option("--melody", type=click.Path(exists=True), help="Melody loop to warp and add.")
@click.option("--bass", type=click.Path(exists=True), help="Bass loop to warp and add.")
@click.option("--export", default="mp3", help="Export format.")
@json_option
@command
def make(name, style, bpm, key, duration, variations, kit, melody, bass, export):
    """High-level command: create project, minimal kit load, generate variations, add loops, export."""
    data = beat_mod.make(
        Path.cwd(), name,
        style=style, bpm=bpm, key=key, duration=duration,
        variations=variations, kit=kit, melody=melody, bass=bass,
        export_format=export,
    )
    out = data["export"].get("out") or data["export"].get("path")
    return data, (
        f"made '{name}' ({style}) — {data['sections']} sections / "
        f"{data['variations']} variations @ {data['bpm']:g} BPM -> {out}"
    )
