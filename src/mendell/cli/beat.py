"""`mendell beat new` — scaffold a ready-to-go beat project from a style preset."""

import click

from .. import beat as beat_mod
from .. import beat_random32
from .. import config as config_mod
from .. import library as library_mod
from ._base import command, json_option


@click.group("beat")
def beat():
    """Quick-start beat scaffolding."""


@beat.command("new")
@click.argument("name")
@click.option("--style", type=click.Choice(sorted(beat_mod.STYLES)), default="lofi",
              help="Style preset — tempo/key defaults + base drum pattern (default: lofi).")
@click.option("--library", "library", default=None,
              help="Pull kit one-shots from this library only (default: all registered libraries).")
@click.option("--bars", default=8, type=int, help="Loop length in bars (default: 8).")
@click.option("--seed", type=int, help="Random seed for a reproducible kit + variations.")
@click.option("--export", "export", is_flag=True, default=False, help="Also render the loop to a WAV.")
@json_option
@command
def new(name, style, library, bars, seed, export):
    """Scaffold NAME: a project seeded with --style's tempo/key, a MIDI 'drums'
    track routed to a sampler 'kit' track, and a --bars-bar drum pattern.

    The kit is filled with random one-shots from the sample library (matched by
    recognized category/instrument, or filename) — across all libraries, or just
    --library. Falls back to an empty kit (with a `kit load` hint) if the library
    has no usable one-shots. --seed reproduces the pick; --export renders a WAV.
    """
    parent, base = config_mod.resolve_project_parent(name)
    data = beat_mod.new(parent, base, style=style, library=library, bars=bars, seed=seed, export=export)
    kit_n = len(data["kit"])
    if kit_n:
        silent = f", silent: {','.join(data['silent_notes'])}" if data["silent_notes"] else ""
        kit_msg = f"{kit_n} kit samples from library{silent}"
    else:
        kit_msg = "empty kit (no library one-shots — run `kit load`)"
    tail = ""
    if export:
        out = data["export"].get("out") or data["export"].get("path")
        tail = f" -> {out}"
    return data, f"created beat '{base}' ({style}, {bars} bars) — {kit_msg}{tail}"


@beat.command("make")
@click.argument("name")
@click.option("--style", type=click.Choice(sorted(beat_mod.STYLES)), required=True, help="Style preset.")
@click.option("--bpm", type=float, help="Override BPM.")
@click.option("--key", help="Override key.")
@click.option("--duration", default="60s", help="Target duration (e.g. 60s).")
@click.option("--variations", default=8, type=int, help="Number of 8-bar variation sections.")
@click.option("--kit", type=click.Path(exists=True), help="One-shot folder override (default: pull kit from the library).")
@click.option("--library", "library", default=None, help="Pull kit one-shots from this library only (default: all).")
@click.option("--melody", type=click.Path(exists=True), help="Melody loop to warp and add.")
@click.option("--bass", type=click.Path(exists=True), help="Bass loop to warp and add.")
@click.option("--export", default="mp3", help="Export format.")
@click.option("--seed", type=int, help="Random seed for reproducible kit + variations.")
@json_option
@command
def make(name, style, bpm, key, duration, variations, kit, library, melody, bass, export, seed):
    """High-level command: create project, fill kit from the library (or --kit
    folder), generate variations, add loops, export."""
    parent, base = config_mod.resolve_project_parent(name)
    data = beat_mod.make(
        parent, base,
        style=style, bpm=bpm, key=key, duration=duration,
        variations=variations, kit=kit, library=library, melody=melody, bass=bass,
        export_format=export, seed=seed,
    )
    out = data["export"].get("out") or data["export"].get("path")
    return data, (
        f"made '{base}' ({style}) — {data['sections']} sections / "
        f"{data['variations']} variations @ {data['bpm']:g} BPM -> {out}"
    )


@beat.command("random32")
@click.argument("name")
@click.option("--bpm", type=float, help="Tempo (default: random 70-160).")
@click.option("--key", type=click.Choice(beat_random32.KEYS), help="Key (default: random A-G).")
@click.option("--db", "db_path", default=None, type=click.Path(),
              help="Sample library.db path (default: the shared Mendell DB).")
@click.option("--seed", type=int, help="Random seed for reproducible picks.")
@click.option("--export", "export_format", default="mp3", help="Export format (mp3/wav).")
@click.option("--warp/--no-warp", "warp", default=None,
              help="Force rubberband warp on/off (default: auto-detect rubberband).")
@click.option("--pattern", default="mutation-loop",
              type=click.Choice(beat_random32.available_patterns()),
              help="Beat archetype (default: mutation-loop).")
@json_option
@command
def random32(name, bpm, key, db_path, seed, export_format, warp, pattern):
    """Build a full 32-bar beat PROJECT from the sample db using a declarative
    arrangement archetype. --pattern picks the archetype (mutation-loop, layer-builder,
    drop-machine, verse-chorus, octave-journey, dj-intro, beat-tape, layer-stripper).
    Builds drums + bass + melody tracks, clips, a real arrangement (per-section layer
    on/off + melody treatments), then renders + exports. Warp defaults to rubberband."""
    parent, base = config_mod.resolve_project_parent(name)
    db_path = db_path or str(library_mod.db_path())
    data = beat_random32.render(parent, base, db_path=db_path, tempo=bpm,
                                key=key, seed=seed, export_format=export_format, warp=warp,
                                pattern=pattern)
    out = data["export"].get("out") or data["export"].get("path")
    return data, (
        f"random32 '{base}' [{data['pattern']}] -> {out} | {data['tempo']:g} BPM, "
        f"key {data['key']}, 32 bars | {data['engine']} | "
        f"bass {data['bass']} | mel {data['melody']}"
    )
