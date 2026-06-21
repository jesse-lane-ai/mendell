# Mendell

Mendell is an agent-first music production CLI. Think Ableton Live's feature
set, but operated primarily through shell commands with structured JSON
output — single-shot, non-interactive commands that compose and script
cleanly, with optional web UIs (e.g. `mendell library serve`) layered over
the same API. It's designed to be driven by AI agents (and humans who like
scripting their DAW).

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
is built in and needs nothing extra. The higher-accuracy backends are optional installs.

Run these from the cloned repo directory with your virtual environment activated
(same place you ran `pip install -e .` above) — the `.[extra]` syntax installs the
optional extras against the local checkout:

```bash
# Local CLAP backend (--recognize clap) — pulls in torch + laion-clap (large download):
pip install -e '.[clap]'
```

Each backend is lazy-loaded, so a missing package or API key only errors when you
actually select that backend — with a message naming the exact `pip install` / env-var
fix. Once installed, set a default so you don't repeat the flag:
`mendell config set library.recognizer clap`.

### Optional: ACE-Step generative audio

[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) is an open generative-audio
model family. Mendell wires it in two places (both opt-in and GPU-oriented):

- **`mendell ace ...`** — generation, cover, repaint, layer, vocal2bgm, source
  separation, audio understanding, LRC, scoring, simple mode, query rewriting.
- **`--recognize ace-step`** — content recognition for `library add`/`scan`, using
  ACE-Step's captioner model. See [SPEC.md](SPEC.md#ace-step-generative-audio) for
  the full command reference and model zoo.

ACE-Step isn't published on PyPI, so install it from source into the same
environment:

```bash
pip install 'git+https://github.com/ace-step/ACE-Step-1.5'
```

**Model downloads differ between the two paths:**

- **Captioner (`--recognize ace-step`) auto-downloads.** On first use it pulls
  [`ACE-Step/acestep-captioner`](https://huggingface.co/ACE-Step/acestep-captioner)
  (~22 GB) from the Hugging Face Hub into the HF cache (`~/.cache/huggingface`, or
  `HF_HOME`) and reuses it thereafter. It needs only `transformers` + `torch` (no
  generation checkpoint). Override the model with `ACESTEP_CAPTIONER_MODEL`.
  - The captioner is ~11B params (~22 GB in bf16). On a 24 GB card the default
    `ACESTEP_CAPTIONER_LOAD=full` fits and is the **fastest** path. On smaller
    cards set `ACESTEP_CAPTIONER_LOAD=4bit` (or `8bit`) for in-flight
    bitsandbytes quantization (CUDA-only; `pip install bitsandbytes accelerate`)
    — ~6–7 GB / ~11 GB VRAM instead of ~22 GB. Quantization shrinks VRAM but is
    *slower* per token than `full`, so prefer `full` whenever it fits.

  **Speed.** Captioning is the slow part of a scan, and three things drive it:

  - **Batching is the main lever.** The captioner has a large fixed per-call
    cost (a multimodal-prefill + decode loop that's CPU-launch-bound, so it's
    roughly the same whether the batch holds 1 file or 16). `ACESTEP_CAPTIONER_BATCH`
    (default 8) sets files per call; per-file time falls almost linearly with it.
    On a 24 GB card `full` + batch 16 captions one-shots at **~0.3–0.7 s/file**
    (vs ~7 s/file unbatched in 4bit). Lower the batch if you OOM on long loops.
    Batches are **grouped by clip length** automatically, so one long loop only
    inflates the padding of its own batch instead of a batch full of short hits.
  - **Audio length is capped** to the longest clip actually in the batch (or
    `ACESTEP_CAPTIONER_AUDIO_SECONDS`, default 30) instead of the model's fixed
    ~300 s window — so a half-second drum hit no longer pays 300 s of audio-encoder
    compute. Captions are unchanged (the model already masks the padding); raise
    the cap only if you caption long loops and want the encoder to hear all of them.
  - One-time costs (model load, first-call CUDA warmup) amortize over the library,
    so a folder of a handful of files looks slower per-file than a big scan.

  **Resumable.** A scan checkpoints each file's verdict to the recognition cache
  as it lands (committed per file), so a crash, OOM, or Ctrl-C partway through a
  thousands-of-files drop loses only the in-flight batch. Re-running
  `library add` (or `library scan`) skips everything already captioned and
  resumes from where it stopped — unchanged files are matched by path + mtime.

  **VRAM after a scan.** The CLI frees the model on process exit. The web UI
  (`mendell library serve`) frees it after **each** import — VRAM returns to
  baseline once a scan finishes. Set `MENDELL_CAPTIONER_KEEP_WARM=1` before
  launching the server to keep the model resident instead (faster back-to-back
  imports, at the cost of pinning ~6–22 GB until the server stops).

- **Generation (`mendell ace ...`) is manual download.** It loads DiT + LM
  checkpoints from a directory you point at — it never auto-fetches. Download the
  checkpoints you want from the
  [model zoo](https://github.com/ace-step/ACE-Step-1.5#-model-zoo) and set:

  ```bash
  export ACESTEP_CHECKPOINT_DIR=/path/to/checkpoints   # required
  # optional: ACESTEP_DIT_CONFIG (default acestep-v15-turbo),
  #           ACESTEP_LM_MODEL   (default acestep-5Hz-lm-1.7B),
  #           ACESTEP_DEVICE     (cuda|mps|cpu|xpu), ACESTEP_LM_BACKEND
  ```

  Recommended all-round config: `acestep-v15-turbo` + `acestep-5Hz-lm-1.7B`. Note
  that source separation (`mendell ace separate`) needs a `*-base` checkpoint —
  `turbo`/`sft` are generation-only; the command says so if your checkpoint lacks it.

Like every other backend these are lazy-loaded: a missing package, checkpoint, or
env var only errors when you actually run the command, with the exact fix in the
message.

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

The fastest path to a finished beat — scaffold a project from a style preset
with a kit auto-filled from your sample library, and render:

```bash
mendell beat new my-song --style energetic --export --json   # project + routing + 8-bar pattern + library kit + WAV
# kit is pulled from the library by default; for an ad-hoc folder instead:
mendell kit load my-song kit ./drum-one-shots/ --json         # auto-map kick/snare/hat/... by name
mendell export my-song --json                                 # renders to my-song/export/my-song.wav
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
on. The backends trade accuracy for weight — `heuristic` (local, zero-dep, coarse category
only), `clap` (local, opt-in), and `ace-step` (local, opt-in);
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
