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
mendell new <name> [--bpm 120] [--key C] [--scale minor] [--sample-rate 44100] [--time-sig 4/4]
mendell info <project> [--json]
mendell set <project> --bpm 140
mendell set <project> --key A --scale major
```

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

After `mendell new`, every other command operates on the project by name (resolved from the current directory or an absolute path).

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
4. Stores native BPM, warp mode, and file path in the clip's TOML

The clip is then ready to be placed in the arrangement and will be time-stretched to the project BPM at export.

```bash
# Import an audio file — native BPM and warp mode auto-detected
mendell clip import <project> <track> <clip-name> --sample /path/to/loop.wav

# Import shows what was detected:
# → {"ok": true, "data": {"clip": "loop", "native_bpm": 135.0, "warp": "beats", "source": "tempo_analysis"}}

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
mendell export <project> --out ./render.wav            # render full arrangement
mendell export <project> --out ./render.wav --stems    # one file per track
mendell export <project> --out ./render.mp3
```

Export emits NDJSON progress events to stdout:

```json
{"event": "export_progress", "pct": 42}
{"event": "export_complete", "path": "./render.wav", "duration_s": 124.5}
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
