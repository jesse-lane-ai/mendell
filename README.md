# Mendell

Mendell is an agent-first music production CLI. Think Ableton Live's feature
set, but operated entirely through shell commands with structured JSON
output — no TUI, no GUI, no interactive prompts. It's designed to be driven
by AI agents (and humans who like scripting their DAW).

Projects, tracks, clips, samplers, arrangements, mixing, automation, and FX
are all managed via single-shot `mendell <noun> <verb>` commands, and the
final song is rendered offline to WAV/MP3 with `mendell export`.

See [`SPEC.md`](SPEC.md) for the full command reference and [`CLAUDE.md`](CLAUDE.md)
for an architecture overview.

## Requirements

- **Python 3.11+**
- **`rubberband`** — command-line tool used for time-stretching and
  pitch-shifting warped audio clips. Required only if you place audio clips
  with `warp` enabled or use clip-level pitch automation; everything else
  works without it.
  - Debian/Ubuntu: `sudo apt install rubberband-cli`
  - macOS (Homebrew): `brew install rubberband`
  - Arch: `sudo pacman -S rubberband`

  Without it, `mendell export` raises a clear `EngineError` naming the
  missing binary as soon as it encounters a warped clip — everything else
  (MIDI/sampler synthesis, unwarped audio, mixing, FX, automation, export)
  works fine.

## Install

Clone the repo and install in a virtual environment:

```bash
git clone https://github.com/jesse-lane-ai/mendell.git mendell
cd mendell
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `mendell` console script along with its Python
dependencies (`click`, `numpy`, `soundfile`, `mido`, `tomli-w`, `scipy`,
`librosa`, `pyrubberband`).

Verify the install:

```bash
mendell --help
```

## Docker

Docker is the recommended way to run Mendell for agent use, since it bundles
the `rubberband` system dependency and gives you a consistent environment:

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends rubberband-cli \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
ENTRYPOINT ["mendell"]
```

```bash
docker build -t mendell .
docker run --rm -v "$PWD/songs:/songs" mendell new /songs/my-song --json
```

## Quick start

The fastest path to a finished beat — scaffold a project from a style preset,
drop in a folder of one-shot drum samples (auto-mapped by filename), and render:

```bash
mendell beat new my-song --style energetic --json   # project + tracks + routing + starter pattern
mendell kit load my-song kit ./drum-one-shots/ --json  # auto-map kick/snare/hat/... by name
mendell export my-song --json                        # renders to my-song/export/my-song.wav
```

No drum loops handy? Generate a pattern straight onto a track:

```bash
mendell midi generate my-song drums fill --style trap --bars 2 --json
```

And before committing to a long render, check the plan first — duration, which
tracks are active, FX chains, and any missing-file/missing-`rubberband` problems
— without rendering any audio:

```bash
mendell export my-song --dry-run --json
```

### Sample library

Tired of retyping paths to your sample packs? Register a folder once, by name,
and reference it from any project from then on — Mendell remembers it globally
(`~/.config/mendell/library.toml`), and you can register as many folders as you
like:

```bash
mendell library add my-loops ~/Samples/lofi-loops --tags loops,lofi --json
mendell library search --bpm 90 --category loop --json   # find loops by tempo, no path-hunting
mendell kit load my-song kit --library my-drums --json   # load a registered folder straight onto a track
mendell sampler map add my-song kit --note C2 --sample my-drums/Kicks/808.wav --json
```

`library add`/`library scan` index each folder's files once and cache a guessed
category (kick/snare/hat/loop/...) and BPM (from the filename, instantly — pass
`--analyze` to also tempo-detect loops with no filename hint) per file, so
`library search`/`library show` answer immediately — no path-hunting, agent-friendly
by design. See [`SPEC.md`](SPEC.md#sample-library) for the full reference.

Or build one up from the primitives directly:

```bash
# Create a project
mendell new my-song --bpm 120 --json

# Add tracks
mendell track add my-song drums --type midi --json
mendell track add my-song kit --type sampler --json

# Route MIDI -> Sampler
mendell route set my-song --from drums --to kit --json

# Import clips
mendell clip import my-song drums drums-clip --midi drums.mid --json
mendell clip import my-song drums kick-clip --sample kick.wav --json

# Map a sample onto the sampler
mendell sampler map add my-song kit --note C2 --sample kick.wav --json

# Place clips in the arrangement
mendell arrange place my-song drums drums-clip --bar 1 --json

# Mix — set vol/pan, or drop in a curated FX chain in one shot
mendell mix set my-song drums --vol 90 --pan -10 --json
mendell mix fx apply my-song drums lofi-vinyl --json

# Render to audio — writes to my-song/export/my-song.wav by default;
# pass --out to choose an explicit path, or --format mp3 to change the default's extension
mendell export my-song --json
```

Every command supports `--json` for structured `{ "ok": true, "data": {...} }`
output, making the whole tool easy to drive programmatically.

## Development

```bash
source .venv/bin/activate
pip install -e .
mendell --help
```

## Tests

37 unit tests across three files, runnable with pytest (or `uv run pytest` if you use uv):

```bash
pytest tests/
# or
uv run pytest tests/
```

| File | Coverage |
|------|----------|
| `tests/test_beat.py` | `beat.new` / `beat.make` scaffolding, duration parsing, pattern generation, variation tiling |
| `tests/test_durations.py` | `parse_duration_ms` / `format_duration_ms` edge cases |
| `tests/test_library.py` | Sample library — add/scan/search/remove, BPM caching, category inference, env-var config isolation |

All tests are pure unit tests (no audio rendering, no system dependencies) and run in under 20 seconds.
