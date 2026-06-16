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
  - Windows: download the prebuilt command-line binary from
    [breakfastquay.com/rubberband](https://breakfastquay.com/rubberband/)
    and add the folder containing `rubberband.exe` to your `PATH`
    (see the [Windows](#windows) section below)

  Without it, `mendell export` raises a clear `EngineError` naming the
  missing binary as soon as it encounters a warped clip — everything else
  (MIDI/sampler synthesis, unwarped audio, mixing, FX, automation, export)
  works fine.
- **`ffmpeg`** — only needed for `beat random32`, which decodes audio from
  your sample library. Everything else works without it.
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - macOS (Homebrew): `brew install ffmpeg`
  - Arch: `sudo pacman -S ffmpeg`
  - Windows: see the [Windows](#windows) section below

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

### Optional: sound-recognition backends

`library add` / `library scan --recognize <backend>` can categorize samples by their
audio content (see [Sample library](#sample-library)). The default `heuristic` backend
is built in and needs nothing extra. The higher-accuracy backends are optional installs:

```bash
# Local CLAP backend (--recognize clap) — pulls in torch + laion-clap (large download):
pip install -e '.[clap]'

# Cloud Gemini backends (--recognize gemini-embedding | gemini-generative):
pip install -e '.[gemini]'
export GEMINI_API_KEY=...        # or GOOGLE_API_KEY — read at runtime

# ...or both at once:
pip install -e '.[clap,gemini]'
```

Each backend is lazy-loaded, so a missing package or API key only errors when you
actually select that backend — with a message naming the exact `pip install` / env-var
fix. Once installed, set a default so you don't repeat the flag:
`mendell config set library.recognizer clap`.

## Windows

Mendell runs on Windows — the codebase is pure cross-platform Python and every
dependency ships a Windows wheel. There are three ways to install, easiest
first.

### WSL2 (recommended)

If you have the Windows Subsystem for Linux, just follow the Linux
instructions above inside your WSL distro — install the system binaries with
`apt` and `pip install -e .` as normal. This is the smoothest path and gives
you the same environment the project is developed against:

```bash
sudo apt update && sudo apt install -y rubberband-cli ffmpeg
git clone https://github.com/jesse-lane-ai/mendell.git mendell
cd mendell
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Native Windows (PowerShell)

Install [Python 3.11+](https://www.python.org/downloads/windows/) (tick "Add
python.exe to PATH" in the installer), then:

```powershell
git clone https://github.com/jesse-lane-ai/mendell.git mendell
cd mendell
python -m venv .venv
.venv\Scripts\Activate.ps1   # or .venv\Scripts\activate.bat in cmd.exe
pip install -e .
mendell --help
```

This installs the `mendell` console script and all Python dependencies. Two
optional command-line binaries are only needed for specific features — install
them and add each `.exe` to your `PATH`:

- **`rubberband`** — only for warped audio clips and clip-level pitch
  automation. Download the prebuilt binary from
  [breakfastquay.com/rubberband](https://breakfastquay.com/rubberband/),
  unzip it, and add the folder containing `rubberband.exe` to `PATH`. Without
  it, `mendell export` raises a clear `EngineError` the moment it hits a warped
  clip; everything else works.
- **`ffmpeg`** — only for `beat random32`, which decodes audio from your
  sample library. Grab a build from
  [ffmpeg.org/download.html](https://ffmpeg.org/download.html) (or
  `winget install Gyan.FFmpeg`) and add `ffmpeg.exe` to `PATH`.

To check that `PATH` is set up correctly, open a new terminal and run
`rubberband --help` and `ffmpeg -version`.

### Docker Desktop

The [Docker](#docker) image below also runs on Windows via Docker Desktop and
bundles the `rubberband` system dependency — the most reproducible option for
agent use. Add `ffmpeg` to the image's `apt-get install` line if you need
`beat random32`.

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

A bare project name like `my-song` is created in your **projects folder**
(`~/Documents/mendell` by default, made on first run); pass a path with
separators (`mendell new sub/my-song` or an absolute path) to place it
elsewhere. Manage it with `mendell config show` / `mendell config set
projects_folder ~/beats`.

No drum loops handy? Generate a pattern straight onto a track:

```bash
mendell midi generate my-song drums fill --style trap --bars 2 --json
```

Want a whole beat straight out of your sample library? `beat random32` builds a
complete, **editable project** — drums + bass + melody tracks, clips, and a
32-bar arrangement — then renders and exports it. It pulls
kick/snare/hat/clap/bass/melody from the registered `library.db`, picks a random
tempo (70–160) and key (A–G), warps every loop to tempo, and transposes bass +
melody to key. The *arrangement* itself comes from a declarative **archetype**
(`--pattern`): the library does the sound, the archetype does the structure.

```bash
mendell beat random32 my-beat                       # mutation-loop (default) -> project + export
mendell beat random32 my-beat --pattern drop-machine --bpm 120 --key A
mendell beat random32 my-beat --pattern layer-builder --seed 909 --export wav --json
```

Archetypes live as YAML in [`patterns/`](patterns/) — each is a list of 8-bar
`sections` declaring which layers (drums/bass/melody) play and how the melody is
treated (clean / octave-up / octave-down / lowpass / reverse). Per-section layer
on/off is real track-volume automation, so builds and drops render accurately.
Shipped archetypes:

| Pattern | Shape |
|---------|-------|
| `mutation-loop` *(default)* | same melody mutated each section: clean → octave-up → lowpass → reverse |
| `layer-builder` | melody → +bass → +drums → full |
| `layer-stripper` | full → drop melody → drop bass → full |
| `octave-journey` | melody original → +12 → −12 → original |
| `dj-intro` | drums → +bass → +melody → full |
| `drop-machine` | full → full → drums-only breakdown → drop |
| `verse-chorus` | verse (fewer layers) ↔ chorus (all layers) |
| `beat-tape` | sample → +bass → +drums → full (J Dilla feel) |

Add your own by dropping a new `patterns/<name>.yaml` — no code changes needed.

Because it's a real project, you can keep working on it afterwards — re-mix,
move sections, swap a loop, add automation — then `mendell export` again. When
the `rubberband` CLI (+ `pyrubberband`) is installed it's used by default for
clean, independent tempo/pitch warping; otherwise clips play unwarped. Force
with `--warp` / `--no-warp`; the result reports `"engine": "rubberband" | "none"`.

And before committing to a long render, check the plan first — duration, which
tracks are active, FX chains, and any missing-file/missing-`rubberband` problems
— without rendering any audio:

```bash
mendell export my-song --dry-run --json
```

### Sample library

Tired of retyping paths to your sample packs? Register a folder once, by name,
and reference it from any project from then on — Mendell remembers it globally
(in the shared `library.db`, in your OS config directory), and you can register
as many folders as you like:

```bash
mendell library add my-loops ~/Samples/lofi-loops --tags loops,lofi --json
mendell library add my-drums ~/Samples/drums --recognize heuristic --json   # categorize by sound, not just filename
mendell library search --bpm 90 --kind loop --json       # real loops at ~90 BPM, no path-hunting
mendell library search --category bass --kind one-shot --json   # bass *hits* for a sampler, not phrases
mendell library search --instrument piano --kind loop --json    # loops that actually contain piano
mendell kit load my-song kit --library my-drums --json   # load a registered folder straight onto a track
mendell sampler map add my-song kit --note C2 --sample my-drums/Kicks/808.wav --json
```

`library add`/`library scan` index each folder's files once and cache, per file, a
guessed category (kick/snare/hat/loop/...), a BPM (from the filename, instantly —
pass `--analyze` to also tempo-detect loops with no filename hint), and a **kind**
— `loop` / `one-shot` / `unknown`, classified from the filename, the file's
duration, and bar-alignment to its BPM. Category says *what* a sample is; kind says
whether it's a phrase or a single hit (the two are independent, so you can ask for a
`bass` `one-shot`). `library search`/`library show` answer immediately from the cache
— no path-hunting, agent-friendly by design.

By default the category comes from filename/folder keywords (instant). Pass `--recognize
<backend>` (or set a default with `mendell config set library.recognizer <backend>`) to
additionally *listen* to each file: a specific filename keyword still wins, but recognition
fills in the rest and adds a multi-valued **instruments** list (a melodic loop →
`[piano, strings]`; a full loop → `[drums, bass, keys]`) that `search --instrument` filters
on. Four backends trade accuracy for weight — `heuristic` (local, zero-dep, coarse category
only), `clap` (local, opt-in), and `gemini-embedding` / `gemini-generative` (cloud, opt-in);
results are cached per file, so re-scans only re-analyze what changed. See
[`SPEC.md`](SPEC.md#sample-library) for the full reference.

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

Unit tests across eight files, runnable with pytest (or `uv run pytest` if you use uv):

```bash
pytest tests/
# or
uv run pytest tests/
```

| File | Coverage |
|------|----------|
| `tests/test_beat.py` | `beat.new` / `beat.make` scaffolding, duration parsing, pattern generation, variation tiling |
| `tests/test_beat_random32.py` | `beat random32` — archetype pattern engine, library-sourced sample selection, full editable project build |
| `tests/test_durations.py` | `parse_duration_ms` / `format_duration_ms` edge cases |
| `tests/test_library.py` | Sample library — add/scan/search/remove, BPM caching, category inference, loop/one-shot kind detection, recognition fusion + caching, `--instrument` search, env-var config isolation |
| `tests/test_recognize.py` | Content recognition — heuristic spectral classification, backend registry + missing-dependency errors, fusion precedence, per-mtime caching |
| `tests/test_registry.py` | Project registry — auto-recording on create, genre, idempotent refresh, lookups, removal |
| `tests/test_paths.py` | Project resolution — filesystem-first, bare-name via registry from any cwd, ambiguous-name and stale-entry handling |
| `tests/test_config.py` | Global config — config.json materialization, OS-aware paths, projects-folder resolution, legacy-DB migration |
