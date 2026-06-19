# Mendell — Music Production CLI

> Ableton Live's feature set, designed for AI agents.

---

## Vision

Mendell is a headless music production tool operated entirely through the CLI. No TUI, no GUI. Every feature is a single-shot command with structured JSON output — designed to be composed, scripted, and driven by AI agents.

---

## Core Concepts

| Concept | Description |
|---|---|
| **Project** | A directory containing all tracks, clips, samples, and config for a song |
| **Track** | A named lane — MIDI, Audio, or Sampler |
| **MIDI Track** | Holds MIDI clips imported from .mid files; routes note output to one or more Sampler Tracks |
| **Audio Track** | Holds audio clips |
| **Sampler Track** | Hosts a Sampler instrument; receives note triggers from MIDI Tracks |
| **Clip** | A loopable pattern (MIDI) or audio file (Audio) assigned to a track; audio clips own their own warp mode and time-stretch settings |
| **Sampler** | Maps audio samples across a note range; plays them on MIDI note-on |
| **Sample Map** | The per-note mappings of audio files within a Sampler |
| **Arrangement** | The complete song definition — tracks, clips, sampler mappings, mixer settings, and all automation |
| **Mixer** | Per-track volume, pan, mute, solo, and FX chain |
| **Automation** | Time-indexed points that change any numeric parameter on a track, clip, or FX slot during export |

---

## Timing Engine

All timing in Mendell flows through a single unified system. Every command that involves a time position — clip placement, automation points, warp markers — uses the same calculations, ensuring audio tracks, MIDI tracks, and the sampler stay sample-accurately in sync at export time.

### Time Representations

| Representation | Used For | Example |
|---|---|---|
| **bar.beat** | All user-facing CLI input | `5.3` = bar 5, beat 3 |
| **seconds** | Internal intermediate | `8.0s` |
| **sample frames** | Audio rendering (canonical internal unit) | `352800` @ 44100 Hz |
| **MIDI ticks** | Reading .mid files (converted on import) | `480 PPQ` |

The canonical internal unit is **sample frames**. Everything is converted to frames before being stored or rendered.

### Project Sample Rate

Fixed at project creation and immutable thereafter — defaults to 44100 Hz:

```bash
mendell new my-song --sample-rate 48000
```

There is no `mendell set --sample-rate`. Every imported sample is resampled to the project's
sample rate on import (if it differs), and every render uses that same rate, so the whole
project — and every file inside it — stays on a single, consistent sample rate for its lifetime.

### Core Conversions

```
seconds_per_beat  = 60.0 / bpm
seconds_per_bar   = seconds_per_beat × beats_per_bar
frames_per_beat   = seconds_per_beat × sample_rate
frames_per_bar    = seconds_per_bar  × sample_rate

bar.beat → frames:
  frames = ((bar - 1) × beats_per_bar + (beat - 1)) × frames_per_beat

MIDI ticks → frames (on .mid import):
  seconds = ticks / (ppq × (bpm / 60))
  frames  = seconds × sample_rate
```

Time signature defaults to 4/4. Configurable per project:

```bash
mendell set my-song --time-sig 3/4
```

### How This Affects Commands

Every CLI argument that takes a time position (`--at`, `--bar`, `--beat`, `--offset`) is resolved through the timing engine at command execution time and stored as sample frames internally. This means:

- Changing project BPM after placing clips rescales all musical positions correctly
- Audio clips, MIDI clips, and automation stay locked to the same musical grid
- MIDI files imported at any PPQ resolution are converted to the project's frame grid on import
- Warp markers specified in beats are stored as both beat position and sample offset

### `mendell timing` Utility

Convenience command for agents to calculate positions without doing the math externally:

```bash
mendell timing <project> --bar 5 --beat 3
# → {"bar": 5, "beat": 3, "seconds": 8.0, "frames": 352800, "ms": 8000.0}

mendell timing <project> --seconds 8.0
# → {"bar": 5, "beat": 3, "seconds": 8.0, "frames": 352800, "ms": 8000.0}

mendell timing <project> --frames 352800
# → {"bar": 5, "beat": 3, "seconds": 8.0, "frames": 352800, "ms": 8000.0}
```

---

## CLI Design Principles

- **No interactive prompts** — every operation is a single command with flags
- **Structured output** — all commands support `--json`; output is always a JSON envelope
- **Idempotent writes** — create-or-update semantics; never errors on duplicates
- **Meaningful exit codes** — `0` success · `1` bad input · `2` project not found · `3` engine error
- **Pipe-friendly** — reads from stdin, writes to stdout, events to stdout as NDJSON during playback

### JSON Envelope

```json
{ "ok": true, "data": { ... } }
{ "ok": false, "error": "track 'drums' not found", "code": 2 }
```

---

## Full Command Reference

### Project

```bash
mendell new <name> [--bpm 120] [--key C] [--scale minor] [--sample-rate 44100] [--time-sig 4/4] [--genre <genre>]
mendell info <project> [--json]
mendell set <project> --bpm 140
mendell set <project> --key A --scale major
```

Creating a project also records it in the global **Project Registry** (see that section);
`--genre` tags it there at creation time (genre is registry-only, not stored in `project.toml`).

`mendell new` creates a fully scaffolded project directory ready for use:

```
<name>/
├── project.toml       # BPM, key, scale, master vol/limiter defaults
├── tracks/            # empty — populated by `mendell track add`
├── clips/             # empty — populated by `mendell clip import`
├── samplers/          # empty — populated by `mendell sampler create`
├── arrangement.toml   # empty timeline
└── samples/           # empty — populated when samples are copied in
```

`project.toml` is initialized with the provided flags (or defaults):

```toml
[project]
name = "my-song"
bpm = 120
key = "C"
scale = "minor"
sample_rate = 44100
time_sig = "4/4"

[master]
vol = 100
limiter_ceiling = -0.3
```

**Where projects are created.** A bare `<name>` (no path separators) is created
under the configured **projects folder** (see the *Configuration* section) —
e.g. `mendell new my-song` → `~/Documents/mendell/my-song`. A `<name>` containing
path separators or an absolute path is created literally, relative to the current
directory (`mendell new sub/my-song`, `mendell new /tmp/my-song`) — the escape
hatch for placing a project somewhere specific. The same rule applies to
`beat new`, `beat make`, and `beat random32`.

After `mendell new`, every other command operates on the project by name. Resolution
checks the filesystem first — a relative or absolute path, or a bare name in (or matching)
the current directory — then falls back to the **Project Registry** (see that section), so
a bare `<name>` resolves from *any* working directory, not just the project's parent. A
name shared by two registered projects is ambiguous and must be addressed by full path; a
registry entry whose `project.toml` has since moved is reported as a stale entry (with a
hint to clear it) rather than silently resolving to a dead path.

### Configuration

Mendell keeps a little user-level state outside any project, in an
OS-appropriate config directory:

| OS | Config directory |
|---|---|
| Linux / other | `$XDG_CONFIG_HOME/mendell` (default `~/.config/mendell`) |
| macOS | `~/Library/Application Support/mendell` |
| Windows | `%APPDATA%\mendell` |

That directory holds `config.json` and the shared `library.db` (sample library +
project registry). On first run Mendell materializes `config.json` and creates
the **projects folder** — the default parent directory new projects are created
under. When the `projects_folder` key is empty it resolves to an OS default
(`~/Documents/mendell`) and is written back into `config.json`.

```bash
mendell config show              # resolved config + the paths it lives at
mendell config path              # just the config.json location
mendell config get projects_folder
mendell config set projects_folder ~/beats
```

Env overrides (mainly for tests/CI/agents): `MENDELL_CONFIG_DIR` relocates the
whole config directory; `MENDELL_PROJECTS_FOLDER` forces the projects folder for
one invocation; `MENDELL_LIBRARY_CONFIG` points `library.db` at a specific file.
A `library.db` found at the old hard-coded `~/.config/mendell/library.db` is
migrated to the resolved location automatically the first time they differ.

### Quick-Start Helpers

The primitives above (`new`, `track add`, `sampler create`, `route set`, `sampler map add`, ...)
cover every case, but the 80% case — "stand up a beat skeleton" / "load a drum kit" — used to
take a dozen calls. Two higher-level commands collapse that:

```bash
# Scaffold a ready-to-go beat: project + tempo/key preset + drums(midi)->kit(sampler)
# routing + a looping starter MIDI pattern placed across the arrangement.
mendell beat new <name> --style lofi|dark|energetic [--json]

# Auto-map a folder of one-shots onto a sampler track by filename — creates the
# sampler track if needed. Recognized drum names (kick/snare/clap/hat/tom/crash/
# ride/perc/...) are mapped to their General MIDI percussion notes; anything
# else is mapped sequentially starting at --start-note (default C5).
mendell kit load <project> <track> <folder> [--start-note C5] [--json]

# Generate a starter drum pattern from a style preset, write it to
# <project>/midi/<clip-name>.mid, and import it onto an existing 'midi' track
# in one shot — uses the same General MIDI percussion notes as `beat new` /
# `kit load`, so it composes with both. Loops by default (--no-loop to disable).
mendell midi generate <project> <track> <clip-name> --style boom-bap|lofi|trap [--bars 1] [--no-loop] [--json]

# Build a complete, editable 32-bar beat PROJECT from the sample library and
# export it. Creates drums(midi)->kit(sampler) + bass(audio) + melody(audio)
# tracks and a 4 x 8-bar arrangement whose STRUCTURE comes from a declarative
# archetype (--pattern). Picks kick/snare/hat/clap/bass/melody from the
# registered library.db, with a random tempo (70-160) and key (A-G) unless
# pinned. Loops are warped to tempo and bass + melody transposed to key.
mendell beat random32 <name> [--pattern <archetype>] [--bpm N] [--key A-G]
                      [--db PATH] [--seed N] [--export mp3|wav] [--warp/--no-warp] [--json]
```

`beat random32` produces a real project (under `<name>/`) you can keep editing —
re-mix, re-arrange, swap loops, automate — not a one-off WAV.

### ACE-Step (generative audio)

`mendell ace ...` wraps the [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)
open model family — text-to-music generation, editing, source separation, and
audio understanding. Like the `clap`/`gemini` recognizers it is **opt-in and
heavyweight (GPU-oriented)**: ACE-Step isn't on PyPI, so install it from source
and point Mendell at a downloaded checkpoint via environment variables. Any `ace`
command without those raises an actionable error instead of a stack trace.

```bash
pip install 'git+https://github.com/ace-step/ACE-Step-1.5'
export ACESTEP_CHECKPOINT_DIR=/path/to/checkpoints   # required
# optional overrides: ACESTEP_DIT_CONFIG (default acestep-v15-turbo),
#                     ACESTEP_LM_MODEL (default acestep-5Hz-lm-1.7B),
#                     ACESTEP_DEVICE (cuda|mps|cpu|xpu), ACESTEP_LM_BACKEND
```

**Model zoo** — download checkpoints into `ACESTEP_CHECKPOINT_DIR` and select
them with `ACESTEP_DIT_CONFIG` / `ACESTEP_LM_MODEL`. Links + index:
[awesome-ace-step](https://github.com/ace-step/awesome-ace-step),
[model zoo](https://github.com/ace-step/ACE-Step-1.5#-model-zoo).

| DiT model | Size | VRAM | Tasks |
|---|---|---|---|
| `acestep-v15-turbo` *(default)* | 2B | ~4.7 GB | text2music, cover, repaint (8-step, fast) |
| `acestep-v15-sft` | 2B | ~4.7 GB | text2music, cover, repaint (50-step) |
| `acestep-v15-base` | 2B | ~4.7 GB | **+ extract (separation), lego, complete** |
| `acestep-v15-xl-{turbo,sft,base}` | 4B | ≥12 GB | as above, higher fidelity |

| LM model | Size | Notes |
|---|---|---|
| `acestep-5Hz-lm-0.6B` | 0.6B | lightweight |
| `acestep-5Hz-lm-1.7B` *(default)* | 1.7B | recommended, full feature coverage |
| `acestep-5Hz-lm-4B` | 4B | strongest audio understanding |

> **Capability gate:** `ace separate` (and the underlying `extract`/`lego`/
> `complete` tasks) require a **`*-base`** DiT checkpoint — `turbo`/`sft` are
> generation-only. Point `ACESTEP_DIT_CONFIG` at `acestep-v15-base` (or
> `-xl-base`) for separation. The recommended all-round config is
> `acestep-v15-turbo` + `acestep-5Hz-lm-1.7B`.

```bash
# Text-to-music with full metadata control + optional reference audio. With
# --track, the rendered file is auto-imported as an audio clip in one shot.
mendell ace generate <project> --prompt "dusty lofi boom-bap, vinyl crackle"
                      [--duration 30] [--bpm 90] [--key "C minor"] [--time-sig 4/4]
                      [--lyrics "..."] [--ref ref.wav] [--batch N]
                      [--track <name>] [--clip-name <name>] [--json]

# Cover an existing track in a new style; --strength 0..1 (higher = further off)
mendell ace cover <project> <source> --prompt "jazz piano version" [--strength 0.8] [--track ..]

# Selectively regenerate a [start,end) window (seconds)
mendell ace repaint <project> <source> --start 10 --end 20 --prompt "piano solo" [--track ..]

# Add a layer over a track (multi-track / "Add Layer")
mendell ace layer <project> <source> --prompt "a warm sub bass" [--strength 0.4] [--track ..]

# Auto-generate instrumental accompaniment for a vocal
mendell ace vocal2bgm <project> <vocal> --prompt "boom-bap drums and rhodes" [--track ..]

# Source separation — one clip per stem, optionally imported onto a track
mendell ace separate <project> <source> [--stems vocals,drums,bass,other] [--track ..]

# Audio understanding: BPM, key/scale, time signature, caption
mendell ace understand <project> <source>
mendell ace lrc <project> <source>      # lyric timestamps
mendell ace score <project> <source>    # quality score

# LM helpers (no audio render): Simple Mode + Query Rewriting
mendell ace simple <project> --prompt "a soft love song for a quiet evening" [--instrumental] [--language bn]
mendell ace rewrite <project> [--caption "latin pop, reggaeton"] [--lyrics "..."] [--bpm 95]

# LoRA fine-tuning is ACE-Step's one-click Gradio workflow, not a CLI call —
# `ace lora` prints how to launch it.
mendell ace lora
```

Generated audio lands in `<project>/generated/` and stems in `<project>/stems/`;
passing `--track` imports the output as a placeable audio clip, so an agent can go
from prompt → clip → arrangement without leaving the CLI.

**Content recognition (`--recognize ace-step`)** — ACE-Step's purpose-built
captioner, [`ACE-Step/acestep-captioner`](https://huggingface.co/ACE-Step/acestep-captioner)
(a Qwen2.5-Omni-7B multimodal model), is wired in as a `library` recognition
backend alongside `clap`/`gemini`. It needs only `transformers` + `torch` (no
generation checkpoint — far lighter than the DiT stack); the model downloads on
first use and is overridable via `ACESTEP_CAPTIONER_MODEL`. Its free-text caption
is keyword-mapped onto the standard `category`/`instruments` taxonomy, so it
fuses with filename guesses exactly like the other backends.

```bash
pip install transformers torch                 # captioner deps (no ACE-Step checkpoint needed)
mendell library add <name> <folder> --recognize ace-step
```

The captioner is an ~11B model (~22 GB in fp16). To run it on a normal GPU, set
`ACESTEP_CAPTIONER_LOAD=4bit` (or `8bit`) for in-flight bitsandbytes
quantization (CUDA-only; `pip install bitsandbytes accelerate`) — this shrinks it to
~6–7 GB / ~11 GB by quantizing only the LLM tower, leaving the audio encoder at
full precision. Default is `full`.

**Archetypes (`--pattern`, default `mutation-loop`).** Each archetype is a YAML
file in `patterns/` describing four 8-bar `sections`, each with `layers` (any of
`drums`/`bass`/`melody`) and a melody `treatment` (`clean`, `octave-up`,
`octave-down`, `lowpass`, `reverse`, or `transpose:<±n>`). The driver builds the
project and realizes the arrangement two ways:

- **Layers** — per-section on/off is written as **track-volume step automation**
  (vol → 0/100 at each 8-bar boundary). Volume automation renders sample-accurately,
  so builds/drops/breakdowns are real, not approximated.
- **Melody treatments** — one melody clip per section (bars 1/9/17/25); the engine
  loops each until the next placement. Pitch offsets (octave/transpose) use the
  clip `pitch` param; `lowpass`/`reverse` are pre-rendered sample variants in the
  project's `samples/` (the unprocessed original stays referenced, so they're undoable).

Shipped archetypes: `mutation-loop`, `layer-builder`, `layer-stripper`,
`octave-journey`, `dj-intro`, `drop-machine`, `verse-chorus`, `beat-tape`. Add
more by dropping a `patterns/<name>.yaml` — no code changes. `--pattern` is
validated against the files present.

It uses the **rubberband** warp engine (`rubberband` CLI + `pyrubberband`) by
default when available, for independent time-stretch and pitch-shift — so the
octave-up treatment is a clean +12 semitones and key transposition doesn't skew
timing. When rubberband is absent, clips play unwarped (native tempo).
`--warp`/`--no-warp` force the choice; the result reports
`"engine": "rubberband" | "none"`. Sample picks are deterministic under `--seed`.

`beat new` writes its starter pattern using the same General MIDI percussion notes
(kick=C2, snare=D2, clap=D#2, closed hat=F#2, open hat=A#2, ...) that `kit load`
assigns one-shots to — so loading a kit onto the scaffolded `kit` track lines up
with the pattern immediately, no manual note-mapping required:

```bash
mendell beat new my-song --style energetic
mendell kit load my-song kit ./drum-one-shots/
mendell export my-song
```

### Sample Library

`kit load` and `clip import --sample` are great once you know the path to a folder of
samples — but agents (and humans) tend to keep their sample collections scattered across
several external folders (a one-shots pack here, a loop pack there, a client's stems
somewhere else), and re-typing/re-discovering those paths on every project is friction
that compounds. The **library** is a small global registry — independent of any project —
that lets you register named external folders once and reference them by name from then on,
from any project, forever.

Registrations are stored outside any project, in the shared user-level SQLite DB
(`library.db` in the OS config directory — see *Configuration*), so the library
persists across projects and survives
project deletion. Multiple folders can be registered side by side under distinct names —
nothing about the design assumes a single sample root.

```bash
# Register an external folder under a short name. Recurses by default; doesn't copy
# anything — the library only ever stores paths + metadata, never sample audio itself.
# Indexes every file once: guesses category (kick/snare/hat/loop/...) and BPM from
# its filename, and caches both — see "Indexing & BPM detection" below.
mendell library add <name> <path> [--tags drums,lofi,kicks] [--analyze] [--recognize heuristic|clap|gemini-embedding|gemini-generative] [--json]

# List every registered folder (name, path, tags, file count, last-scanned time)
mendell library list [--json]

# Re-scan a folder (or all of them) — picks up files added/removed since registration
# and rebuilds the cached category/BPM index
mendell library scan [<name>] [--analyze] [--recognize heuristic|clap|gemini-embedding|gemini-generative] [--json]

# Inspect one registered folder — full file listing with detected category and
# (where known) BPM per file, as ready-to-use refs
mendell library show <name> [--json]

# Search across all registered folders (or scope to one with --library) by filename
# keyword, tag, category, kind, and/or BPM (±2 BPM tolerance) — the building block
# agents use to find material without knowing any paths up front
mendell library search <query> [--library <name>] [--tag <tag>] [--category kick|snare|loop|...] [--kind loop|one-shot|unknown] [--instrument <name>] [--bpm <n>] [--json]

# Unregister (does not touch the folder or its files on disk)
mendell library remove <name>
```

### Indexing, BPM, Kind & Recognition

`library add`/`library scan` walk the registered folder once and cache, per file,
the same things `kit load` and `clip import` already derive — a **category** guess
(kick/snare/hat/loop/one-shot/... from filename and parent-folder keywords) and a
**BPM** guess — plus the file's **duration** and a **kind** classification.

BPM detection is two-tier, mirroring the `clip import` pipeline (see "Audio Clips"):

- **Filename pass (always runs, instant):** the same `<number>bpm` / bare-number
  pattern matching `clip import` uses — e.g. `loop-135bpm.wav` → `135.0`,
  `source: "filename"`.
- **Signal-analysis pass (opt-in via `--analyze`, slower):** real tempo detection
  via `librosa` — reserved for files categorized as `loop` that have no filename
  BPM hint, since one-shots (kicks, claps, hats, ...) don't have a meaningful
  tempo and running full analysis across a large pack of them would make
  indexing sluggish for no payoff.

Both checks are cheap to skip — files with no detectable BPM simply omit the field.

**Kind — `loop` vs `one-shot` (vs `unknown`).** This is a separate axis from
`category`: `category` says *what* a file is (kick, bass, melody…), `kind` says
whether it's a sustained phrase or a single hit — so a `bass` file can be either.
It's resolved cheapest-first, most-precise-wins (`kind_source` records which rule
fired):

- **`filename`** — an explicit keyword (`...loop...`, or a bounded
  `oneshot`/`hit`/`stab`/`shot`) wins outright.
- **`duration`** — a file at/under ~1.2s with no keyword is a one-shot hit. Duration
  comes from a header-only read (no decode), so it's computed for every file.
- **`bar-align`** — a longer file whose duration is (within ±6%) a whole number of
  bars at its known BPM is a loop. This is what catches an unlabeled `bass`/`melody`
  loop.
- **`category`** *(weak fallback)* — drum categories → one-shot, `loop` → loop.
- Otherwise **`unknown`** — an honest "don't know" rather than a wrong guess; filter
  for these to triage a messy pack.

Files with an unreadable header (corrupt/placeholder) simply omit `duration` and
fall back to the keyword/category rules.

**Content recognition — `category` + `instruments` from the audio.** By default
`category` is guessed from filename/folder keywords (instant). Pass `--recognize <backend>`
(or set a default with `mendell config set library.recognizer <backend>`) to additionally
*listen* to each file. Recognition is fused with the filename guess, most-confident-wins: a
specific filename keyword still wins (`category_source: "filename"`); otherwise the backend
supplies the coarse `category` (`category_source` = the backend name, plus a
`category_confidence`), and it always adds a multi-valued **`instruments`** list — the
second taxonomy tier, orthogonal to `category`/`kind`. A one-shot usually has 0–1
instruments; a loop can have several (a melodic loop → `["piano", "strings"]`; a
full/construction loop → `["drums", "bass", "keys"]`). `search --instrument <name>` matches
any file whose list contains that token (whole-token match, like `--tag`).

Four backends trade accuracy for weight, all behind the one `--recognize` flag:

- **`heuristic`** *(local, zero new deps)* — a spectral-feature classifier reusing the same
  signal analysis as warp detection. Fills the coarse `category` only; does not enumerate
  instruments.
- **`clap`** *(local, opt-in: `pip install 'mendell[clap]'`)* — CLAP audio↔text embeddings,
  zero-shot against the category/instrument vocabulary; coarse `category` **and** multi-label
  `instruments`.
- **`gemini-embedding`** *(cloud, opt-in: `pip install 'mendell[gemini]'` + a `GEMINI_API_KEY`
  / `GOOGLE_API_KEY` env var)* — the same embedding mechanic via Gemini Embedding.
- **`gemini-generative`** *(cloud, same dep + key)* — prompts the Gemini multimodal model for
  an instrument list directly; strongest on dense mixes (`category_confidence` is
  presence/absence, ~1.0).

Recognition results are **cached per file**, keyed by path + modification time, so a re-scan
only re-runs a backend on files that were added or changed — unchanged files are never
re-analyzed (and a cloud backend is never re-billed for them). A missing optional dependency
or API key surfaces as an actionable error naming the exact `pip install` / env-var fix.

```bash
mendell library search "808" --bpm 90 --kind loop --instrument 808 --json
```
```json
{ "ok": true, "data": { "matches": [
  { "ref": "my-drum-pack/Loops/dark-808-90bpm.wav", "category": "bass", "category_source": "gemini-generative", "category_confidence": 1.0, "instruments": ["808"], "kind": "loop", "kind_source": "filename", "bpm": 90.0, "bpm_source": "filename", "duration": 3.556, "tags": ["drums", "lofi"] }
] } }
```

Once registered, library entries plug directly into the commands that already accept a
folder or sample path — so the rest of the workflow doesn't change, it just stops requiring
absolute paths:

```bash
# kit load accepts a registered name in place of a folder path
mendell kit load my-song kit --library my-drum-pack [--json]

# sampler import / map add / clip import resolve a "<library-name>/<relative-path>"
# reference anywhere they take a folder or sample path — same as a real path today
mendell sampler import my-song kit my-drum-pack/Kicks --start-note C1
mendell sampler map add my-song kit --note C1 --sample my-drum-pack/Kicks/808.wav
mendell clip import my-song drums loop-a --sample my-drum-pack/Loops/dark-loop.wav
```

`library search` is the piece purpose-built for agents: instead of an agent needing to
`ls` around the filesystem (which it may not have access to, or may waste tokens
exploring), it can ask Mendell directly — `mendell library search "808" --category kick`
— and get back a ranked list of `library-name/relative/path` references it can hand
straight to `kit load` / `sampler map add` / `clip import`.

```json
{ "ok": true, "data": { "matches": [
  { "ref": "my-drum-pack/Kicks/808-deep.wav", "category": "kick", "tags": ["drums", "lofi"] },
  { "ref": "my-drum-pack/Kicks/808-punchy.wav", "category": "kick", "tags": ["drums", "lofi"] }
] } }
```

Out of scope for v1: copying/syncing library contents into projects (samples are still
copied into `<project>/samples/` on use, exactly as they are today — the library only
stores *references*), audio analysis/tagging beyond filename heuristics, and remote/cloud
folders (local paths only).

### Project Registry

A global table of every project Mendell has created, with its metadata (name, genre,
key, scale, bpm, time signature, sample rate, and `created` / `last_updated`
timestamps). Like the sample library it lives in the shared user-level SQLite DB
(`library.db` in the OS config directory — see *Configuration* — overridable via
`MENDELL_LIBRARY_CONFIG`), so it spans projects and is queryable from anywhere.
Each project's `project.toml` remains the source of truth — the registry is a
secondary index.

Rows are recorded **automatically** whenever a project is created — any creation path
(`mendell new`, `mendell beat new`, `mendell beat random32`) funnels through the same
seam. `beat new` records its `--style` as the genre; `mendell new` accepts an optional
`--genre`. Entries are keyed by absolute project directory, so two projects that share a
`name` never collide (address those by full path).

This index is also what lets every command resolve a project by a bare `<name>` from any
working directory: project resolution tries the filesystem first, then falls back to this
registry — a single match wins, a shared name is ambiguous (use the full path), and a
project that has moved away from its recorded path reports a stale-entry error.

```bash
# List every recorded project, most-recently-updated first
mendell projects list [--json]

# Show one entry by project name or directory path
mendell projects show <project> [--json]

# Re-read a project's metadata into the registry, bump last_updated, and
# optionally set/refresh its genre. Also how you register a project that
# predates the registry. A bare re-sync never wipes an existing genre.
mendell projects sync <project> [--genre <genre>] [--json]

# Drop an entry (the project files on disk are left untouched)
mendell projects remove <project> [--json]
```

`mendell new` also takes `--genre <genre>` to tag a project at creation time. Genre is
registry-only metadata; it is **not** written into `project.toml`.

Recording is best-effort: if the registry write ever fails, project creation still
succeeds (project.toml is already on disk) — the registry never blocks core work.

### Tracks

```bash
mendell track add <project> <name> --type midi|audio|sampler
mendell track remove <project> <name>
mendell track list <project> [--json]
mendell track show <project> <name> [--json]
```

### MIDI Clips

```bash
# Import a .mid file as a clip
mendell clip import <project> <track> <clip-name> --midi /path/to/file.mid
# If the .mid has multiple tracks, pick one by index
mendell clip import <project> <track> <clip-name> --midi /path/to/file.mid --midi-track 0

# Inspect
mendell clip show <project> <track> <clip> [--json]   # notes as array
mendell clip list <project> <track> [--json]
mendell clip remove <project> <track> <clip>
```

### Audio Clips

Importing an audio file creates an audio clip. On import, Mendell automatically:
1. Copies the file into `samples/` (unless `--link` is passed)
2. Detects the native BPM — first from filename keywords, then via tempo analysis if no keyword matches
3. Detects the warp mode — same two-stage pipeline (filename → signal analysis)
4. Detects the musical key — chroma analysis + Krumhansl-Schmuckler key-profile correlation
   (no reliable filename heuristic exists for key, so this always runs via signal analysis)
5. Stores native BPM, warp mode, detected key/scale, and file path in the clip's TOML

The clip is then ready to be placed in the arrangement and will be time-stretched to the project BPM at export.
Detected key/scale are informational (`clip show` → `detected_key`/`detected_scale`) — they don't
affect rendering, but help an agent pick/transpose material that fits the project's `key`/`scale`.

```bash
# Import an audio file — native BPM, warp mode, and key auto-detected
mendell clip import <project> <track> <clip-name> --sample /path/to/loop.wav

# Import shows what was detected:
# → {"ok": true, "data": {"clip": "loop", "native_bpm": 135.0, "warp": "beats", "source": "tempo_analysis"}}
# `clip show` additionally reports detected_key / detected_scale, e.g. "A" / "minor"

# Link in place instead of copying
mendell clip import <project> <track> <clip-name> --sample /path/to/loop.wav --link

# Override detected values at import time
mendell clip import <project> <track> <clip-name> --sample /path/to/loop.wav --native-bpm 128 --warp melodic

# Override after import
mendell clip set <project> <track> <clip-name> --warp beats
mendell clip set <project> <track> <clip-name> --native-bpm 135.0
mendell clip set <project> <track> <clip-name> --pitch +2   # semitones, independent of stretch
mendell clip set <project> <track> <clip-name> --warp off   # play at original speed, no stretching

# Warp marker (Beats mode only) — locks a transient to a beat position
mendell clip warp-marker add <project> <track> <clip> --beat 2.0 --offset -10ms
mendell clip warp-marker list <project> <track> <clip> [--json]
mendell clip warp-marker remove <project> <track> <clip> --beat 2.0
```

### Arrangement

```bash
mendell arrange place <project> <track> <clip> --bar 1
mendell arrange remove <project> <track> --bar 1
mendell arrange list <project> [--json]
mendell arrange set-loop <project> --in 1 --out 16
```

### Sampler

Note names use scientific pitch notation, where **middle C is C4** (MIDI note 60). Octaves run
`C<n>`–`B<n>`; e.g. `C1` = MIDI 24, `C4` = MIDI 60, `B5` = MIDI 83.

```bash
# Create a sampler track and load samples
mendell sampler create <project> <track-name>

# Map a sample to a note or range
mendell sampler map add <project> <track> --note C1 --sample /path/to/kick.wav --root C1
mendell sampler map add <project> <track> --range C4-B5 --sample /path/to/pad.wav --root C4 --loop
mendell sampler map add <project> <track> --note C1 --sample /path/to/kick.wav --link  # reference in place, no copy

# Bulk-import a folder — auto-maps each file to successive notes
mendell sampler import <project> <track> /path/to/drum-kit/ --start-note C1

# Per-slot parameters
mendell sampler map set <project> <track> --note C1 --vol 90 --pan -5 --tune -10
mendell sampler map set <project> <track> --note C1 --attack 5ms --decay 100ms --sustain 80 --release 200ms

# Sampler-level settings
mendell sampler set <project> <track> --polyphony 8 --tune -5

# Inspect
mendell sampler map list <project> <track> [--json]
mendell sampler map remove <project> <track> --note C1
```

### Sample Parameters (per slot)

| Parameter | Description |
|---|---|
| **Root note** | Pitch at which the sample plays unmodified |
| **Note range** | Low–high note bounds that trigger this slot |
| **Loop** | Off / Forward / Ping-pong |
| **Loop start/end** | Sample points for the loop region |
| **Tune** | Fine-tune in cents (±100) |
| **Vol / Pan** | Per-slot gain and stereo position |
| **Envelope** | ADSR applied to each triggered note |
| **Pitch follow** | Whether notes outside root pitch-shift the sample |

### MIDI → Sampler Routing

```bash
mendell route set <project> --from <midi-track> --to <sampler-track>
mendell route remove <project> --from <midi-track> --to <sampler-track>
mendell route list <project> [--json]
```

- One MIDI track can route to multiple Sampler Tracks (layering)
- One Sampler Track can receive from multiple MIDI Tracks

### Mixer

```bash
mendell mix set <project> <track> --vol 80 --pan -10
mendell mix mute <project> <track> [--off]
mendell mix solo <project> <track> [--off]
mendell mix show <project> [--json]

# FX chain — each slot gets a stable id when added (a per-track counter that
# never repeats and is never reused); processing order follows insertion order.
# Removing a slot does not renumber the others, so automation referencing
# fx.<id>.<param> stays valid for the lifetime of that slot.
mendell mix fx add <project> <track> reverb --room 0.6 --wet 0.3
# → {"ok": true, "data": {"id": 0, "type": "reverb"}}
mendell mix fx set <project> <track> 0 --room 0.8     # update by id
mendell mix fx remove <project> <track> 0
mendell mix fx list <project> <track> [--json]

# Apply a curated, named FX chain in one shot — appends each preset slot via
# the normal `fx add` path (stable ids, validated params), so re-running it
# stacks another copy rather than replacing the existing chain.
mendell mix fx apply <project> <track> lofi-vinyl|tape-warmth|radio|telephone|spacious|punch [--json]
```

### Automation

Automation defines how a parameter changes over time during export. It is stored as a list of (bar.beat, value) points; the engine interpolates between them linearly (or with a curve).

Any numeric parameter on a track, clip, or FX slot can be automated.

**Automatable parameters:**

| Target | Parameters |
|---|---|
| Track (mixer) | `vol`, `pan`, `mute` (0/1), `send.<fx-name>` (send level) |
| Audio clip | `gain`, `pitch`, `warp` (0/1) |
| FX slot | any numeric parameter by name (e.g. `room`, `wet`, `cutoff`) |

```bash
# Add an automation point: parameter reaches `value` at bar.beat
mendell auto add <project> <track> vol --at 1.1 --value 0
mendell auto add <project> <track> vol --at 5.1 --value 80
mendell auto add <project> <track> vol --at 9.1 --value 0

# Automate a clip parameter (clip must be on the track)
mendell auto add <project> <track> clip.<clip-name>.gain --at 3.1 --value -6

# Automate an FX parameter (fx slot by id)
mendell auto add <project> <track> fx.0.wet --at 1.1 --value 0
mendell auto add <project> <track> fx.0.wet --at 8.1 --value 0.8

# Set interpolation curve between two points (default: linear)
mendell auto add <project> <track> vol --at 5.1 --value 80 --curve ease-in

# Inspect / remove
mendell auto list <project> <track> [--param vol] [--json]
mendell auto remove <project> <track> vol --at 5.1
mendell auto clear <project> <track> [--param vol]   # clear all points for a param
```

**Curves:** `linear` (default) · `ease-in` · `ease-out` · `ease-in-out` · `step` (jump at the point, no interpolation)

Each entity owns the automation for its own parameters and stores it in its own TOML file: track/mixer/FX automation lives in the track's TOML alongside mixer settings, and clip automation (`clip.<clip-name>.gain`, `pitch`, `warp`) lives in that clip's TOML alongside its warp settings. `mendell auto` commands route writes to the correct file transparently — the agent always addresses automation through the track (e.g. `mendell auto add <project> <track> clip.<clip-name>.gain ...`), regardless of where it's persisted.

### Parameter Reference

Every mutable parameter across all entities is settable via the appropriate `set` command (the
exception is `sample_rate`, which is fixed at `mendell new` and immutable — see
[Project Sample Rate](#project-sample-rate)). This is the complete reference.

#### Project (`mendell set <project> --<param> <value>`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `bpm` | float | 120.0 | Tempo in beats per minute |
| `key` | string | `C` | Root key (C, C#, D, D#, E, F, F#, G, G#, A, A#, B) |
| `scale` | string | `minor` | Scale type (`major`, `minor`) |
| `time_sig` | string | `4/4` | Time signature |
| `master_vol` | int | 100 | Master output volume (0–100) |
| `limiter_ceiling` | float | -0.3 | Master limiter ceiling in dBFS |

#### Track (`mendell track set <project> <track> --<param> <value>`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | string | — | Rename the track |
| `type` | string | — | `midi` / `audio` / `sampler` |

#### Mixer (`mendell mix set <project> <track> --<param> <value>`)

| Parameter | Type | Range | Description |
|---|---|---|---|
| `vol` | int | 0–100 | Track volume |
| `pan` | int | -100–100 | Stereo pan (negative = left, 0 = center) |
| `mute` | bool | on/off | Mute track |
| `solo` | bool | on/off | Solo track |

#### Audio Clip (`mendell clip set <project> <track> <clip> --<param> <value>`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `gain` | float | 0.0 | Clip gain in dB |
| `native_bpm` | float | auto-detected | The file's original tempo |
| `warp` | string | auto-detected | `beats` / `melodic` / `harmonic` / `vocal` / `complex` / `off` |
| `pitch` | float | 0.0 | Pitch shift in semitones (independent of tempo) |
| `loop` | bool | off | Loop the clip |
| `loop_start` | float | 0.0 | Loop start in seconds |
| `loop_end` | float | clip length | Loop end in seconds |

#### MIDI Clip (`mendell clip set <project> <track> <clip> --<param> <value>`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `transpose` | int | 0 | Shift all notes by N semitones |
| `velocity_scale` | float | 1.0 | Multiply all velocities by this factor |
| `loop` | bool | off | Loop the clip |

#### Sampler (`mendell sampler set <project> <track> --<param> <value>`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `polyphony` | int | 8 | Max simultaneous voices |
| `tune` | int | 0 | Global fine-tune in cents (±100) |

#### Sampler Slot (`mendell sampler map set <project> <track> --note <note> --<param> <value>`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `root` | note | same as mapped note | Pitch at which sample plays unmodified |
| `vol` | int | 100 | Slot volume (0–100) |
| `pan` | int | 0 | Slot pan (-100–100) |
| `tune` | int | 0 | Fine-tune in cents (±100) |
| `pitch_follow` | bool | on | Pitch-shift sample for notes outside root |
| `loop` | string | `off` | `off` / `forward` / `pingpong` |
| `loop_start` | float | 0.0 | Loop start in seconds |
| `loop_end` | float | sample length | Loop end in seconds |
| `attack` | string | `1ms` | Envelope attack time |
| `decay` | string | `10ms` | Envelope decay time |
| `sustain` | int | 100 | Envelope sustain level (0–100) |
| `release` | string | `50ms` | Envelope release time |

#### FX Slot (`mendell mix fx set <project> <track> <id> --<param> <value>`)

| Effect | Parameters |
|---|---|
| `reverb` | `room` (0.0–1.0), `damping` (0.0–1.0), `wet` (0.0–1.0) |
| `delay` | `time` (beats, e.g. `0.5`), `feedback` (0.0–1.0), `wet` (0.0–1.0) |
| `compressor` | `threshold` (dBFS), `ratio` (1–20), `attack` (ms), `release` (ms) |
| `eq` | `low_shelf` (dB), `mid_freq` (Hz), `mid_gain` (dB), `high_shelf` (dB) |
| `chorus` | `rate` (Hz), `depth` (0.0–1.0), `wet` (0.0–1.0) |
| `bitcrusher` | `bits` (1–16), `rate_reduction` (1–16) |
| `filter` | `type` (`lp`/`hp`/`bp`), `cutoff` (Hz), `resonance` (0.0–1.0) |
| `limiter` | `ceiling` (dBFS), `lookahead` (ms) |

#### Arrangement (`mendell arrange set <project> --<param> <value>`)

| Parameter | Type | Description |
|---|---|---|
| `loop` | bool | Enable arrangement loop |
| `loop_in` | float | Loop start in bar.beat |
| `loop_out` | float | Loop end in bar.beat |
| `length` | float | Total arrangement length in bars |

### Export

```bash
mendell export <project>                               # render to <project>/export/<name>.wav
mendell export <project> --format mp3                  # render to <project>/export/<name>.mp3
mendell export <project> --out ./render.wav            # explicit path overrides the default
mendell export <project> --out ./render.wav --stems    # one file per track
mendell export <project> --out ./render.mp3
mendell export <project> --dry-run                     # plan only — no audio rendered, nothing written
```

`--out` is optional. When omitted, export writes to a consistent, predictable
location — `<project>/export/<project-name>.<format>` (format defaults to `wav`,
or pick `--format mp3`) — so repeated exports land in the same place with the
same name (idempotent overwrite) instead of scattering output paths across runs.
After writing, export verifies the file actually exists and is non-empty before
reporting success, so a returned `path` is guaranteed to point at real audio.

Export emits NDJSON progress events to stdout:

```json
{"event": "export_progress", "pct": 42}
{"event": "export_complete", "path": "./render.wav", "duration_s": 124.5}
```

#### Dry run (`--dry-run`)

Builds the exact same render plan export would execute — duration, the resolved
output path, every track's type/active/mute/solo/placement-count/FX-chain, and a
list of warnings (missing sample/clip files, a warped clip needing `rubberband`
when it isn't installed, an empty arrangement, no active audio-producing track) —
without rendering a single sample or touching disk. Useful for an agent to sanity
check a long render, or diagnose "why did export produce silence/fail" up front:

```bash
mendell export <project> --dry-run --json
# → {"ok": true, "data": {
#     "project": "my-song", "bpm": 128.0, "sample_rate": 44100,
#     "duration_s": 32.0, "out_path": ".../export/my-song.wav", "would_write_stems": false,
#     "tracks": [{"name": "drums", "type": "midi", "active": null, "muted": false,
#                 "soloed": false, "placements": 1, "fx_chain": []}, ...],
#     "warnings": []
# }}
```

---

## Time-Stretching

Each audio clip owns its own warp settings — mode, native BPM, pitch shift, and warp markers. Two clips on the same track can have different warp modes. The clip stores the file's native BPM (auto-detected on import, overridable) and the engine stretches to the project BPM at export time.

### Warp Modes

| Mode | Best For | Algorithm |
|---|---|---|
| **Beats** | Drum loops, percussion | Transient detection + beat-slice warping |
| **Melodic** | Bass lines, leads, monophonic | Phase vocoder, pitch-coherent |
| **Harmonic** | Pads, chords, polyphonic | Granular phase vocoder, smooth texture |
| **Vocal** | Sung/spoken vocals | Formant-preserving phase vocoder |
| **Complex** | Full mixes, unknown material | High-quality phase vocoder, safe default |

### Auto-Detection Pipeline

**Stage 1 — Filename keywords (instant):**

| Keywords | Mode |
|---|---|
| `drum`, `loop`, `beat`, `perc`, `hat`, `kick`, `snare` | Beats |
| `bass`, `lead`, `melody`, `arp`, `mono` | Melodic |
| `pad`, `chord`, `keys`, `synth`, `harm`, `atmo` | Harmonic |
| `vox`, `vocal`, `voice`, `acap`, `sing` | Vocal |
| no match | → Stage 2 |

**Stage 2 — Signal analysis via `librosa` + `aubio` (runs once, cached):**

- High transient density + percussive energy → **Beats**
- Stable single-pitch track + low polyphony → **Melodic**
- Multiple concurrent pitches + low transients → **Harmonic**
- Formant structure (F1/F2) + voiced speech → **Vocal**
- None of the above → **Complex**

Manual override via `--warp <mode>` always takes precedence and is stored per clip.

### Tech Note

Implemented via **Rubber Band Library** (`pyrubberband`):

| Mode | Options |
|---|---|
| Beats | `TRANSIENTS_CRISP`, `DETECTOR_PERCUSSIVE` |
| Melodic | `ENGINE_FINER`, `TRANSIENTS_MIXED`, `PHASE_INDEPENDENT` |
| Harmonic | `ENGINE_FINER`, `TRANSIENTS_SMOOTH`, `PHASE_LAMINAR` |
| Vocal | `ENGINE_FINER`, `FORMANT_PRESERVED`, `PHASE_INDEPENDENT` |
| Complex | `ENGINE_FINER`, `TRANSIENTS_MIXED`, `FORMANT_SHIFTED` |

---

## Effects

All effects are parameter-driven, no external plugins required:

| Effect | Parameters |
|---|---|
| **Reverb** | Room size, Damping, Wet/Dry |
| **Delay** | Time (BPM-synced), Feedback, Wet/Dry |
| **Compressor** | Threshold, Ratio, Attack, Release |
| **EQ** | Low shelf, Mid peak (freq + gain), High shelf |
| **Chorus** | Rate, Depth, Wet/Dry |
| **Bitcrusher** | Bit depth, Sample rate reduction |
| **Filter** | Type (LP/HP/BP), Cutoff, Resonance |
| **Limiter** | Ceiling, Lookahead |

---

## Audio Engine

- **numpy** + **soundfile** — sample-accurate offline rendering; no live audio output
- Internal BPM clock with swing for step-accurate rendering
- Master limiter on output
- Export renders the full arrangement to a buffer, then writes to WAV/MP3

---

## Project Format

```
my-project/
├── project.toml          # BPM, key, scale, master settings
├── tracks/
│   └── drums.toml        # Track type, mixer settings (vol/pan/mute), FX chain, automation
├── clips/
│   └── kick-1.toml       # MIDI ref or audio ref + warp settings + clip automation
├── samplers/
│   └── drums.toml        # Sample map, per-slot parameters
├── arrangement.toml      # Clip placements at bar positions
└── samples/              # Copied audio files (linked files stay at original path)
```

The arrangement is the union of all of these — tracks, clips, sampler mappings, mixer state, and automation all serialize into this directory. Plain TOML throughout — diffable, git-friendly, reproducible.

---

## Installation

### Native

**macOS**
```bash
brew install rubberband
pipx install mendell
```

**Linux (Ubuntu/Debian)**
```bash
sudo apt install librubberband-dev
pipx install mendell
```

`rubberband` is the only system dependency. Everything else installs automatically via pip.

### Docker

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y librubberband-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install mendell
ENTRYPOINT ["mendell"]
```

Published to Docker Hub as `mendell/mendell`. Usage:

```bash
# Mount your working directory so projects and exports persist
docker run -v $(pwd):/projects mendell new my-song --bpm 120
docker run -v $(pwd):/projects mendell export my-song --out /projects/render.wav
```

Zero local dependencies — the recommended option for agents running in containers.

---

## Tech Stack

| Layer | Library |
|---|---|
| CLI framework | `click` (Python) |
| Rendering | `numpy` + `soundfile` |
| Time-stretching | `pyrubberband` (Rubber Band Library) |
| BPM / warp detection | `aubio` + `librosa` |
| Audio file I/O | `soundfile` |
| MIDI file I/O | `mido` |
| Config format | `tomllib` / `tomli-w` |
| DSP effects | `scipy.signal` + custom |

---

## Out of Scope (v1)

- Any TUI or GUI
- Live audio playback (output is exported files only)
- VST/AU plugin hosting
- Audio recording from microphone/interface
- Video sync
- Collaboration / cloud sync

---

## Success Criteria

An AI agent should be able to:
1. Create a project, add tracks, import MIDI and audio files, and export a stereo WAV — entirely via CLI commands, no human interaction
2. Load a folder of drum samples, auto-map them to a sampler, import a MIDI file to drive it, and render the result
3. Load an audio loop, have the correct warp mode auto-detected, and export it time-stretched to a new BPM
4. Inspect full project state at any point via `--json` output
5. Commit the project directory to git and reproduce the exact same render on another machine
