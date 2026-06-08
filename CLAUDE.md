# Mendell — Claude Code Context

## What Is This

Mendell is an agent-first music production CLI tool. Think Ableton Live's feature set, but operated entirely through shell commands with structured JSON output. No TUI, no GUI, no interactive prompts. Designed to be driven by AI agents.

## Current State

**Implementation complete.** Every module in the architecture below is implemented and wired into the CLI (`src/mendell/`), including the export engine (`mendell export`). End-to-end smoke-tested: project creation → tracks → MIDI/audio clip import → sampler mapping → MIDI→sampler routing → arrangement placement → mixer/FX/automation → WAV/MP3 export with NDJSON progress and `--stems`.

The `rubberband` CLI binary (required by `pyrubberband` for time-stretch/pitch-shift on warped audio clips) is now installed in this environment — verified end-to-end by importing a warped loop, placing it, and exporting (rendered correctly at a tempo different from the clip's native BPM).

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

### Implementation Watch-Items

- **Negative-number flags in `click`**: many commands take signed values (`--pan -10`, `--pitch -2`/`+2`, `--tune -10`, `--transpose -12`). `click` usually parses these fine with `type=int`/`type=float`, but test each signed-value command during implementation — fix only the ones that actually misparse (e.g. switch to `--flag=value` syntax in docs, or a custom `ParamType`) rather than pre-solving this everywhere.

## Field Notes — Friction Points From Real Usage (Resolved)

Surfaced while building actual beats end-to-end with the agent, and since addressed:

1. **Subagents reported export paths that were never written.** Fixed: `engine.export()` now verifies the output file exists and is non-empty immediately after writing (`engine/__init__.py:_write_audio`), raising `EngineError` if not — a returned `path` is now guaranteed to point at real audio.
2. **Sampler + one-shot drum-kit workflow was too manual.** Fixed: `mendell kit load <project> <track> <folder>` (`src/mendell/kit.py`) creates the sampler track + instrument if needed and auto-maps one-shots to General MIDI percussion notes by filename keyword (kick/snare/clap/hat/tom/crash/ride/perc/...), with sequential fallback for anything unrecognized.
3. **Output paths were inconsistent across runs.** Fixed: `mendell export` now defaults `--out` to `<project>/export/<project-name>.<format>` (format defaults to `wav`, override with `--format mp3`) — predictable, idempotent-overwrite, no more scattered paths. Explicit `--out` still works as an override.
4. **No quick-start templates.** Fixed: `mendell beat new <name> --style lofi|dark|energetic` (`src/mendell/beat.py`) scaffolds a project with style-tuned tempo/key, a `drums`(midi)→`kit`(sampler) routing pair, and a looping starter MIDI drum pattern (generated via `mido`, written to `<project>/midi/`) placed across an 8-bar arrangement. Patterns use the same GM percussion notes that `kit load` maps one-shots onto, so the two compose seamlessly: `beat new` → `kit load` → `export`.
5. **Error messages for missing `rubberband` / missing sample files were vague.** Fixed: `engine/render.py` now proactively checks `shutil.which("rubberband")` before any stretch/pitch-shift and raises an actionable message with install commands (apt/brew/pacman); missing sample/clip source files are detected before `sf.read` and raise a message naming the exact path and the fix (`sampler map add` / `clip import`).

The pure-CLI + structured-JSON design remains the right foundation for agent-driven use — these were "batteries included" gaps, now closed. See `SPEC.md` → "Quick-Start Helpers" and "Export" for the new command docs.

## Full Spec

See `SPEC.md` for the complete command reference, parameter tables, warp mode details, timing engine formulas, and project format.
