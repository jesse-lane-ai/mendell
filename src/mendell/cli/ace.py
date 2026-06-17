"""`mendell ace ...` — ACE-Step 1.5 generation, editing, separation, and
audio understanding.

Opt-in: every subcommand routes through the lazy ``ace`` engine, which raises
an actionable error if the optional ``ace`` extra or a model checkpoint
(``ACESTEP_CHECKPOINT_DIR``) isn't available — see `ace.engine.ACE_INSTALL_HINT`.
"""

import click

from .. import ace as ace_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("ace")
def ace():
    """ACE-Step generation, editing, separation, and understanding."""


# -- generation ----------------------------------------------------------

@ace.command("generate")
@click.argument("project")
@click.option("--caption", "--prompt", "caption", required=True,
              help="Text description / tag string to generate from.")
@click.option("--duration", type=float, default=None, help="Length in seconds (10-600).")
@click.option("--bpm", type=float, default=None, help="Target tempo.")
@click.option("--key", type=str, default=None, help="Target key/scale, e.g. 'C minor'.")
@click.option("--time-sig", "time_signature", type=str, default=None, help="Time signature, e.g. '4/4'.")
@click.option("--lyrics", type=str, default=None, help="Lyrics (with [Verse]/[Chorus] tags).")
@click.option("--ref", "ref_audio", type=str, default=None, help="Reference audio file to steer style.")
@click.option("--batch", "batch_size", type=int, default=1, help="Number of variations to generate.")
@click.option("--track", type=str, default=None, help="Auto-import the result as an audio clip on this track.")
@click.option("--clip-name", type=str, default="generated", help="Clip name when --track is given.")
@json_option
@command
def generate(project, caption, duration, bpm, key, time_signature, lyrics,
             ref_audio, batch_size, track, clip_name):
    """Text-to-music generation with full metadata control."""
    project_dir = resolve_project(project)
    data = ace_mod.generate(
        project_dir, caption=caption, duration=duration, bpm=bpm, key=key,
        time_signature=time_signature, lyrics=lyrics, ref_audio=ref_audio,
        batch_size=batch_size, track=track, clip_name=clip_name,
    )
    return data, data


@ace.command("cover")
@click.argument("project")
@click.argument("source")
@click.option("--caption", "--prompt", "caption", required=True, help="Style to cover in.")
@click.option("--strength", type=float, default=0.8, help="Cover strength 0-1 (higher = further from source).")
@click.option("--track", type=str, default=None, help="Auto-import the cover onto this track.")
@click.option("--clip-name", type=str, default="cover")
@json_option
@command
def cover(project, source, caption, strength, track, clip_name):
    """Generate a stylistic cover of SOURCE audio."""
    project_dir = resolve_project(project)
    data = ace_mod.cover(project_dir, src_audio=source, caption=caption,
                         strength=strength, track=track, clip_name=clip_name)
    return data, data


@ace.command("repaint")
@click.argument("project")
@click.argument("source")
@click.option("--start", type=float, required=True, help="Repaint window start (seconds).")
@click.option("--end", type=float, required=True, help="Repaint window end (seconds).")
@click.option("--caption", "--prompt", "caption", required=True, help="What to regenerate the window into.")
@click.option("--track", type=str, default=None)
@click.option("--clip-name", type=str, default="repaint")
@json_option
@command
def repaint(project, source, start, end, caption, track, clip_name):
    """Selectively regenerate the [START, END) window of SOURCE."""
    project_dir = resolve_project(project)
    data = ace_mod.repaint(project_dir, src_audio=source, start=start, end=end,
                          caption=caption, track=track, clip_name=clip_name)
    return data, data


@ace.command("layer")
@click.argument("project")
@click.argument("source")
@click.option("--instruction", "--prompt", "instruction", required=True,
              help="What layer to add, e.g. 'a vinyl-crackle pad'.")
@click.option("--strength", type=float, default=0.4)
@click.option("--track", type=str, default=None)
@click.option("--clip-name", type=str, default="layer")
@json_option
@command
def layer(project, source, instruction, strength, track, clip_name):
    """Add a new layer over SOURCE (multi-track generation)."""
    project_dir = resolve_project(project)
    data = ace_mod.layer(project_dir, src_audio=source, instruction=instruction,
                        strength=strength, track=track, clip_name=clip_name)
    return data, data


@ace.command("vocal2bgm")
@click.argument("project")
@click.argument("source")
@click.option("--caption", "--prompt", "caption", required=True,
              help="Style of accompaniment to generate under the vocal.")
@click.option("--track", type=str, default=None)
@click.option("--clip-name", type=str, default="bgm")
@json_option
@command
def vocal2bgm(project, source, caption, track, clip_name):
    """Auto-generate instrumental accompaniment for vocal SOURCE."""
    project_dir = resolve_project(project)
    data = ace_mod.vocal2bgm(project_dir, src_audio=source, caption=caption,
                           track=track, clip_name=clip_name)
    return data, data


# -- separation ----------------------------------------------------------

@ace.command("separate")
@click.argument("project")
@click.argument("source")
@click.option("--stems", type=str, default="vocals,drums,bass,other",
              help="Comma-separated stems to extract.")
@click.option("--track", type=str, default=None, help="Auto-import each stem onto this track.")
@json_option
@command
def separate(project, source, stems, track):
    """Separate SOURCE into individual stems."""
    project_dir = resolve_project(project)
    stem_list = [s.strip() for s in stems.split(",") if s.strip()]
    data = ace_mod.separate(project_dir, src_audio=source, stems=stem_list, track=track)
    return data, data


# -- understanding / LM --------------------------------------------------

@ace.command("understand")
@click.argument("project")
@click.argument("source")
@json_option
@command
def understand(project, source):
    """Extract BPM, key/scale, time signature, and a caption from SOURCE."""
    project_dir = resolve_project(project)
    data = ace_mod.understand(project_dir, src_audio=source)
    return data, data


@ace.command("lrc")
@click.argument("project")
@click.argument("source")
@json_option
@command
def lrc(project, source):
    """Generate LRC lyric timestamps for SOURCE."""
    project_dir = resolve_project(project)
    data = ace_mod.lrc(project_dir, src_audio=source)
    return data, data


@ace.command("score")
@click.argument("project")
@click.argument("source")
@json_option
@command
def score(project, source):
    """Quality-score SOURCE audio."""
    project_dir = resolve_project(project)
    data = ace_mod.score(project_dir, src_audio=source)
    return data, data


@ace.command("simple")
@click.argument("project")
@click.option("--query", "--prompt", "query", required=True, help="One-line song description.")
@click.option("--instrumental", is_flag=True, help="No vocals.")
@click.option("--language", type=str, default=None, help="Vocal language code, e.g. 'en', 'bn'.")
@json_option
@command
def simple(project, query, instrumental, language):
    """Simple Mode — expand a one-line description into a full song blueprint."""
    project_dir = resolve_project(project)
    data = ace_mod.simple(project_dir, query=query, instrumental=instrumental, language=language)
    return data, data


@ace.command("rewrite")
@click.argument("project")
@click.option("--caption", type=str, default=None, help="Tags/caption to expand and clean up.")
@click.option("--lyrics", type=str, default=None, help="Lyrics to format.")
@click.option("--bpm", type=float, default=None, help="BPM hint.")
@json_option
@command
def rewrite(project, caption, lyrics, bpm):
    """Query Rewriting — LM expansion of tags and lyrics."""
    project_dir = resolve_project(project)
    data = ace_mod.rewrite(project_dir, caption=caption, lyrics=lyrics, bpm=bpm)
    return data, data


@ace.command("lora")
@json_option
@command
def lora():
    """Show how to run ACE-Step LoRA training (one-click Gradio workflow)."""
    data = ace_mod.lora_info()
    return data, data
