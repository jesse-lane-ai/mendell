# Mendell — Claude Code Context

## What Is This

Mendell is an agent-first music production CLI tool. Think Ableton Live's feature set, but operated entirely through shell commands with structured JSON output. No TUI, no GUI, no interactive prompts. Designed to be driven by AI agents.

## Current State

**Spec phase only.** `SPEC.md` is complete and committed. No code has been written yet. The next session should begin implementation.

## Key Design Decisions (Do Not Revisit)

- **No TUI or GUI** — pure CLI, every operation is a single command
- **No live audio playback** — output is exported files only (WAV/MP3)
- **No step sequencer** — MIDI clips come exclusively from imported `.mid` files
- **No scenes** — clips place directly into the arrangement at bar positions
- **Clip owns warp settings** — not the track; each audio clip carries its own warp mode, native BPM, pitch shift
- **Sample frames are the canonical time unit** — all positions convert to frames internally; user-facing input is `bar.beat` notation
- **Python** is the implementation language
- **Docker** is the recommended distribution for agent use; native install requires only `rubberband` as a system dep

## Architecture Overview

```
mendell/
├── project/        # project.toml read/write, scaffolding
├── tracks/         # track TOML management
├── clips/          # MIDI clip import (mido), audio clip import + warp detection
├── sampler/        # sample map, ADSR, routing
├── mixer/          # vol/pan/mute/solo/FX chain
├── arrangement/    # bar-position placements, loop points
├── automation/     # (bar.beat, value) point lists, interpolation
├── timing/         # unified time conversion (bar.beat ↔ seconds ↔ frames ↔ MIDI ticks)
├── engine/         # export renderer — reads arrangement, renders to numpy buffer, writes WAV/MP3
├── fx/             # built-in effects (reverb, delay, compressor, EQ, chorus, bitcrusher, filter, limiter)
└── cli/            # click command definitions wiring everything together
```

## Tech Stack

| Layer | Library |
|---|---|
| CLI | `click` |
| Rendering | `numpy` + `soundfile` |
| Time-stretching | `pyrubberband` (needs `librubberband-dev` system dep) |
| BPM / warp detection | `aubio` + `librosa` |
| MIDI file I/O | `mido` |
| DSP effects | `scipy.signal` + custom |
| Config | `tomllib` (read) + `tomli-w` (write) |

## Core Concepts (Quick Reference)

- **Project** — directory with `project.toml`, `tracks/`, `clips/`, `samplers/`, `arrangement.toml`, `samples/`
- **Track** — `midi`, `audio`, or `sampler` type
- **MIDI Track** — imports `.mid` files as clips, routes note output to Sampler Tracks
- **Audio Track** — holds audio clips with per-clip warp/stretch settings
- **Sampler Track** — hosts a Sampler; receives MIDI note triggers; maps notes to sample files
- **Arrangement** — clips placed at bar positions; also contains mixer state and automation
- **Automation** — `(bar.beat, value)` points for any numeric parameter on any track/clip/FX slot
- **Timing Engine** — converts between `bar.beat`, seconds, sample frames, and MIDI ticks; single source of truth

## CLI Conventions

- All commands: `mendell <noun> <verb> <project> [args] [--json]`
- `--json` flag on every command returns `{ "ok": true, "data": {...} }` or `{ "ok": false, "error": "...", "code": N }`
- Exit codes: `0` success · `1` bad input · `2` project not found · `3` engine error
- Idempotent writes everywhere — create-or-update, never error on duplicates
- Time positions expressed as `bar.beat` (e.g. `5.3` = bar 5, beat 3)

## What To Build First

Suggested implementation order:

1. **`timing/`** — get the unified time math right first; everything else depends on it
2. **`project/`** — `mendell new`, `mendell info`, `mendell set`; scaffolds directory + `project.toml`
3. **`tracks/`** — `mendell track add/remove/list/show`
4. **`clips/`** — audio import with warp detection, MIDI import via `mido`
5. **`sampler/`** — sample map, ADSR, `mendell sampler` commands
6. **`arrangement/`** — `mendell arrange place/remove/list`
7. **`mixer/`** — `mendell mix set/mute/solo/fx`
8. **`automation/`** — `mendell auto add/list/remove`
9. **`fx/`** — built-in DSP effects
10. **`engine/`** — export renderer; reads everything, renders to buffer, writes file
11. **`cli/`** — wire all commands together, `--json` output, exit codes

## Full Spec

See `SPEC.md` for the complete command reference, parameter tables, warp mode details, timing engine formulas, and project format.
