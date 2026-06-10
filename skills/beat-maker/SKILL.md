---
name: beat-maker
description: "Token-efficient high-level commands + reusable prompt patterns for AI agents driving mendell CLI (reduces per-turn context by 60-80%)"
---

# mendell-beat-maker

Reusable skill for low-token agent orchestration of the mendell music production CLI.

## Core Principle

Never ship the full mendell command reference or long task history in every subagent turn. Instead:
- Use `mendell beat make` for 80% of use cases — it is fully implemented.
- Keep the agent prompt under ~4k tokens by referencing this skill.
- Reserve expensive models (Opus) only for creative decisions; use faster models for scaffolding.

## Mixing Methods — One-Shots and Loops Are Interchangeable

The two approaches (sampler/one-shot drums vs. loop-based stems) are not exclusive. Mix them freely per stem:

| Stem | One-shot / sampler | Loop (audio track) |
|------|-------------------|--------------------|
| Drums | `beat new` + `kit load` + MIDI clip | `track add --type audio` + `clip import --warp beats` |
| Bass | sampler track triggered by MIDI | `track add --type audio` + `clip import --warp beats` |
| Melody | sampler track triggered by MIDI | `track add --type audio` + `clip import --warp melodic` |

**Common hybrid**: MIDI drums (sampler one-shots) + audio loops for bass/melody/harmony.

```bash
# Drums from one-shots
mendell beat new NAME --style dark
mendell kit load NAME kit ./one-shots/

# Bass and melody from loops — add on top
mendell track add NAME bass    --type audio
mendell track add NAME melody  --type audio
mendell clip import NAME bass   bass-loop  --sample ./loops/bass.wav   --native-bpm BPM --warp beats   --link
mendell clip import NAME melody mel-loop   --sample ./loops/melody.wav --native-bpm BPM --warp melodic --link
mendell clip set NAME bass   bass-loop  --loop
mendell clip set NAME melody mel-loop   --loop
mendell arrange place NAME bass   bass-loop  --bar 1
mendell arrange place NAME melody mel-loop   --bar 1
mendell export NAME --format mp3
```

The reverse (loop drums + sampler melody) works identically — just swap which tracks use which method.

## `mendell beat make` — Fully Implemented

```bash
mendell beat make NAME \
  --style lofi|dark|energetic \   # required
  --bpm FLOAT \                   # overrides style default
  --key TEXT \                    # overrides style default (e.g. "A", "F#")
  --duration TEXT \               # e.g. "60s", "90s" (default: 60s)
  --variations INT \              # number of 8-bar pattern variations (default: 8)
  --kit PATH \                    # folder of one-shot WAV samples (optional)
  --melody PATH \                 # loop WAV to warp + add as melody track (optional)
  --bass PATH \                   # loop WAV to warp + add as bass track (optional)
  --export TEXT \                 # output format: mp3 or wav (default: mp3)
  --json                          # emit JSON envelope
```

### What it does internally

1. `project create` with style-tuned BPM/key
2. `drums` (midi) → `kit` (sampler) track pair with routing
3. `kit load` if `--kit` given (curate the folder — 5–10 one-shots is enough)
4. Generates `--variations` distinct 8-bar MIDI clips (velocity/hat humanization per variation)
5. Tiles variations across the arrangement to fill `--duration`
6. Adds warped melody track (`warp=melodic`) and bass track (`warp=beats`) if given
7. Renders and exports to `<project>/export/<name>.<format>`

### Style presets

| Style     | BPM   | Key | Scale |
|-----------|-------|-----|-------|
| lofi      | 78    | A   | minor |
| dark      | 140   | F   | minor |
| energetic | 128   | C   | major |

### Output envelope (--json)

```json
{
  "project": { ... },
  "style": "lofi",
  "bpm": 78.0,
  "key": "A",
  "tracks": ["drums", "kit"],
  "variations": 8,
  "sections": 4,
  "arrangement_bars": 32.0,
  "kit": { ... } | null,
  "export": { "path": "/path/to/project/export/name.mp3", ... }
}
```

The `export.path` field is guaranteed to point at a real, non-empty file.

## `mendell beat random32` — Full Beat Project From the Sample Library

One command builds a complete, **editable project** (drums + bass + melody
tracks, clips, a 32-bar arrangement) from the registered `library.db`, then
renders and exports it. Use this when the goal is "give me a beat from my packs"
that you can still tweak afterwards — unlike `beat make`, no loops/paths to pass.

```bash
mendell beat random32 NAME \   # project created under ./NAME/
  --pattern ARCHETYPE \  # arrangement archetype (default: mutation-loop)
  --bpm FLOAT \          # tempo (default: random 70-160)
  --key A-G \            # key (default: random A-G)
  --db PATH \            # library.db (default: ~/.config/mendell/library.db)
  --seed INT \           # deterministic sample picks
  --export mp3|wav \     # export format (default: mp3)
  --warp / --no-warp \   # force warp engine (default: auto-detect rubberband)
  --json
```

The project is a normal Mendell project: re-mix, re-arrange, swap clips, or add
automation, then `mendell export NAME` again.

### Archetypes (`--pattern`)

The library supplies the *sound*; the archetype supplies the *structure*. Each is
a YAML file in `patterns/` with four 8-bar `sections`, each declaring `layers`
(any of drums/bass/melody) and a melody `treatment`. Shipped:

| Pattern | Shape |
|---------|-------|
| `mutation-loop` *(default)* | same melody mutated: clean → octave-up → lowpass → reverse |
| `layer-builder` | melody → +bass → +drums → full |
| `layer-stripper` | full → drop melody → drop bass → full |
| `octave-journey` | melody original → +12 → −12 → original |
| `dj-intro` | drums → +bass → +melody → full |
| `drop-machine` | full → full → drums-only breakdown → drop |
| `verse-chorus` | verse (fewer layers) ↔ chorus (all layers) |
| `beat-tape` | sample → +bass → +drums → full |

Add an archetype by dropping `patterns/<name>.yaml` — no code changes.
Treatments: `clean`, `octave-up`, `octave-down`, `lowpass`, `reverse`, `transpose:<±n>`.

### What it does internally

1. Picks kick/snare/hat/clap (one-shots) + bass + melody from `library.db`,
   preferring loops near the target tempo; random pick (deterministic under `--seed`).
2. Random tempo (70–160) and key (A–G) unless pinned; project scale = minor.
3. Creates the project: `drums`(midi) → `kit`(sampler, one-shots mapped to GM
   notes) + routing, `bass`(audio), `melody`(audio).
4. Writes a constant drum MIDI loop; places the bass loop (warped + transposed)
   across all 32 bars; places one melody clip per section with its treatment.
5. Realizes per-section **layers** as track-volume step automation (renders
   sample-accurately, so builds/drops are real).
6. Sets the arrangement to 32 bars and exports via the real engine.

### Warp engine

Defaults to **rubberband** (`rubberband` CLI + `pyrubberband`) for independent
tempo/pitch on warped clips — octave-up is a clean +12 semitones, key transpose
doesn't skew timing. When rubberband is missing, clips play unwarped (native
tempo). `--warp` / `--no-warp` force it. Reports `"engine": "rubberband" | "none"`.
Warp export is slower (~15–20s) than the unwarped path.

### Output envelope (--json)

```json
{
  "project": { ... },
  "pattern": "mutation-loop",
  "engine": "rubberband",
  "tempo": 96.0, "key": "F", "bars": 32, "sections": 4,
  "tracks": ["drums", "kit", "bass", "melody"],
  "bass": "95_Gbm_SlapSquare_01_SP.wav",
  "melody": "FAITHONTEN 90 BPM.wav",
  "kit": { "kick": "...", "snare": "...", "hat": "...", "clap": "..." },
  "section_layers": [["bass","drums","melody"], ...],
  "melody_treatments": ["clean", "octave-up", "lowpass", "reverse"],
  "export": { "out": "/path/to/NAME/export/NAME.mp3", ... }
}
```

## Condensed Agent Prompt (use this instead of long task descriptions)

```
You are a mendell operator. Use the `mendell-beat-maker` skill.
Goal: [one sentence, style-agnostic].
Run `mendell beat make <name> --style <style> [flags]`.
Output only the final export path and a 1-line summary.
```

## Token Savings

- Before: 40k–88k tokens per full beat (multiple long subagent turns).
- After: ~8k–12k tokens (one focused turn + `beat make`).

## Building a beat from loop samples (no one-shots)

Use this when your sample library has loops (drums, melody, bass, harmony, fx) rather than one-shots. `beat make` won't help here — build it track by track.

```bash
# 1. Create project
mendell new NAME --bpm BPM --key KEY --scale minor

# 2. Add one audio track per stem
mendell track add NAME drm-top --type audio
mendell track add NAME drm-808 --type audio
mendell track add NAME melody  --type audio
mendell track add NAME bass    --type audio
mendell track add NAME harmony --type audio
mendell track add NAME seq     --type audio
# (add as many as needed)

# 3. Import each loop — set native-bpm and warp mode
#    warp=beats   → rhythmic/drum loops
#    warp=melodic → pitched/tonal loops
mendell clip import NAME drm-top  top-loop  --sample /path/to/drums.wav  --native-bpm BPM --warp beats   --link
mendell clip import NAME drm-808  808-loop  --sample /path/to/808.wav    --native-bpm BPM --warp beats   --link
mendell clip import NAME melody   mel-loop  --sample /path/to/melody.wav --native-bpm BPM --warp melodic --link
mendell clip import NAME bass     bass-loop --sample /path/to/bass.wav   --native-bpm BPM --warp beats   --link
mendell clip import NAME harmony  har-loop  --sample /path/to/harmony.wav --native-bpm BPM --warp melodic --link
mendell clip import NAME seq      seq-loop  --sample /path/to/seq.wav    --native-bpm BPM --warp beats   --link

# 4. Loop every clip and place at bar 1
mendell clip set NAME drm-top  top-loop  --loop
mendell clip set NAME drm-808  808-loop  --loop
mendell clip set NAME melody   mel-loop  --loop
mendell clip set NAME bass     bass-loop --loop
mendell clip set NAME harmony  har-loop  --loop
mendell clip set NAME seq      seq-loop  --loop

mendell arrange place NAME drm-top  top-loop  --bar 1
mendell arrange place NAME drm-808  808-loop  --bar 1
mendell arrange place NAME melody   mel-loop  --bar 1
mendell arrange place NAME bass     bass-loop --bar 1
mendell arrange place NAME harmony  har-loop  --bar 1
mendell arrange place NAME seq      seq-loop  --bar 1

# 5. Set arrangement length (bars = desired_seconds / (240 / BPM))
mendell arrange set NAME --length 32   # e.g. 32 bars ≈ 54s at 140 BPM

# 6. Mix levels (0–100; 100 = unity)
mendell mix set NAME drm-top  --vol 90
mendell mix set NAME drm-808  --vol 95
mendell mix set NAME melody   --vol 75
mendell mix set NAME bass     --vol 88
mendell mix set NAME harmony  --vol 65
mendell mix set NAME seq      --vol 55

# 7. Export
mendell export NAME --format mp3 --json
```

### Typical level guidelines

| Role    | Vol range | Notes |
|---------|-----------|-------|
| 808/kick | 90–100   | Anchor of the mix |
| Top drums | 85–95  | |
| Bass     | 85–92    | |
| Melody   | 70–80    | Sits under the main hook |
| Harmony/pads | 60–70 | Background bed |
| Seq/FX   | 50–60    | Subtle texture |

### Bars-to-seconds formula

```
bars = ceil(target_seconds / (240 / BPM))
# e.g. 60s at 140 BPM → ceil(60 / 1.714) = 35 bars
```

## Fallback: MIDI-based beat from one-shots

If your library has one-shot samples (kick, snare, hat) instead of loops:

```bash
mendell beat new NAME --style STYLE           # scaffold + starter MIDI pattern
mendell kit load NAME kit ./samples/          # auto-map one-shots to GM notes (curate to 5-10)
mendell midi generate NAME drums var-2 --style STYLE --bars 8
mendell midi generate NAME drums var-3 --style STYLE --bars 8
mendell arrange place NAME drums var-2 --bar 9
mendell arrange place NAME drums var-3 --bar 17
mendell export NAME --format mp3
```

## Arrangement Variation — Change Something Every 4 Bars

A static loop that never changes sounds flat. Every 4 bars, mutate at least one element. Use `mendell auto add` for gradual changes and `mendell clip` + `mendell arrange place` for hard swaps.

### Vol automation — mute/drop a track for a section

```bash
# Drop hi-hats (drm-top) from bar 5 to 8, bring back at bar 9
mendell auto add NAME drm-top vol --at 5.1 --value 0   --curve step
mendell auto add NAME drm-top vol --at 9.1 --value 90  --curve step

# Drop kick+bass together at bar 13
mendell auto add NAME drm-808 vol --at 13.1 --value 0  --curve step
mendell auto add NAME bass    vol --at 13.1 --value 0  --curve step
mendell auto add NAME drm-808 vol --at 17.1 --value 95 --curve step
mendell auto add NAME bass    vol --at 17.1 --value 88 --curve step

# Drop drums entirely for a bar (breakdown)
mendell auto add NAME drm-top    vol --at 21.1 --value 0  --curve step
mendell auto add NAME drm-808    vol --at 21.1 --value 0  --curve step
mendell auto add NAME drm-stutter vol --at 21.1 --value 0 --curve step
mendell auto add NAME drm-top    vol --at 25.1 --value 90 --curve step
mendell auto add NAME drm-808    vol --at 25.1 --value 95 --curve step
mendell auto add NAME drm-stutter vol --at 25.1 --value 70 --curve step
```

### Pitch-shift a loop at a section boundary

```bash
# Transpose melody up 2 semitones at bar 17 (requires a second clip import)
mendell clip import NAME melody mel-shifted --sample ./loops/melody.wav --native-bpm BPM --warp melodic --link
mendell clip set NAME melody mel-shifted --pitch 2.0 --loop
mendell arrange place NAME melody mel-shifted --bar 17
```

### Swap a MIDI variation at a bar boundary (MIDI drums only)

```bash
# Generate a stripped variation (no hats) and place it at bar 9
mendell midi generate NAME drums no-hats --style dark --bars 8
mendell arrange place NAME drums no-hats --bar 9
```

### Suggested 32-bar template (4-bar moves)

| Bars  | Move |
|-------|------|
| 1–4   | Full arrangement — establish the groove |
| 5–8   | Drop hi-hats (vol → 0 on top drum track) |
| 9–12  | Full arrangement back |
| 13–16 | Drop kick + bass (stripped-down section) |
| 17–20 | Full arrangement, pitch-shift melody +2st |
| 21–24 | Drums out entirely (breakdown — melody + harmony only) |
| 25–28 | Everything back in |
| 29–32 | Add seq/FX layer, push to end |

Automate `vol` with `--curve step` for hard cuts, `--curve linear` or `ease-in-out` for gradual fades.

## Notes

- `--kit` path should contain a curated subset of one-shots (5–10). Do not pass a huge library folder.
- GM percussion note mapping (kick=36, snare=38, clap=39, closed-hat=42, open-hat=46) is consistent across `beat new`, `beat make`, `kit load`, and `midi generate`.
- `--dry-run` on `mendell export` previews the render plan without writing audio — use it to validate before a real export.
