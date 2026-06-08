# mendell-beat-maker

Reusable skill for low-token agent orchestration of the mendell music production CLI.

## Core Principle
Never ship the full mendell command reference or long task history in every subagent turn. Instead:
- Use a single high-level `mendell beat make` command for 80% of use cases.
- Keep the agent prompt under ~4k tokens by referencing this skill.
- Reserve expensive models (Opus) only for creative decisions; use Grok for scaffolding.

## New High-Level Command (proposed for mendell)

```bash
mendell beat make <project> \
  --style [genre-or-style] \
  --bpm [value] \
  --key "[key]" \
  --duration [seconds]s \
  --variations [number] \
  --kit ./samples/One\ Shots/ \
  --melody [loop.wav] \
  --bass [loop.wav] \
  --export mp3 \
  --json
```

This single command should internally:
1. `beat new` with starter pattern
2. `kit load` (recursive, **minimum one-shots only** — 5–10 is enough 90% of the time; do not load the entire folder)
3. Generate + sequence N distinct [bars]-bar MIDI clips with variation rules
4. Add warped bass + melody tracks
5. Render and export

## Condensed Agent Prompt (use this instead of long task descriptions)

```
You are a mendell operator. Use the `mendell-beat-maker` skill.
Goal: [one sentence, style-agnostic].
Run `mendell beat make ...` with the flags above. If the high-level command is not yet implemented, fall back to the low-level sequence in the skill file but keep total context <5k tokens.
Output only the final MP3 path and a 1-line summary.
```

## Token Savings
- Before: 40k–88k tokens per full beat (multiple long subagent turns).
- After: ~8k–12k tokens (one focused turn + high-level command).

## Next Steps
1. Implement `mendell beat make` in mendell/src/mendell/beat.py (or equivalent).
2. Add this skill to the agent workspace.
3. Update all future mendell subagent tasks to reference it.
