#!/usr/bin/env python
"""Smoke-test for arrange_view module.

Creates a temporary project, builds minimal tracks + a MIDI clip placement
using existing Mendell APIs, calls view(), runs random_clip(), then calls
view() again to confirm the placement landed.

Run with:  .venv/bin/python tests/smoke_arrange_view.py
"""

import json
import sys
import tempfile
from pathlib import Path

# Ensure the src tree is on the path when run directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mendell import arrangement as arrangement_mod
from mendell import clips as clips_mod
from mendell import midi_gen as midi_gen_mod
from mendell import project as project_mod
from mendell import tracks as tracks_mod
from mendell import arrange_view as av_mod


def pp(label: str, obj: object) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print('=' * 60)
    print(json.dumps(obj, indent=2, default=str))


def main():
    with tempfile.TemporaryDirectory(prefix="mendell_smoke_") as tmpdir:
        parent = Path(tmpdir)
        name = "smoke-arrview"

        # 1. Create project
        project_dir = project_mod.create(parent, name, bpm=90.0, key="A", scale="minor")
        print(f"Created project: {project_dir}")

        # 2. Add a MIDI drums track
        tracks_mod.add(project_dir, "drums", "midi")

        # 3. Generate a MIDI clip and place it at bar 1
        midi_gen_mod.generate(project_dir, "drums", "drum-loop", style="lofi", bars=4)
        clips_mod.set_params(project_dir, "drums", "drum-loop", loop=True)
        arrangement_mod.place(project_dir, "drums", "drum-loop", bar=1)

        # 4. Initial view snapshot
        snap1 = av_mod.view(project_dir)
        pp("view() BEFORE random_clip", snap1)
        assert snap1["bpm"] == 90.0, f"Expected bpm=90, got {snap1['bpm']}"
        assert len(snap1["tracks"]) >= 1, "Expected at least one track"

        drums_track = next(t for t in snap1["tracks"] if t["name"] == "drums")
        assert len(drums_track["placements"]) == 1, "Expected 1 placement on drums"
        assert drums_track["placements"][0]["start_bar"] == 1

        # 5. random_clip — generate + place a second clip at bar 5
        clip_result = av_mod.random_clip(
            project_dir, "drums",
            style="boom-bap", bars=4, start_bar=5, seed=42,
        )
        pp("random_clip() result", clip_result)

        # 6. View again — should show 2 placements on drums
        snap2 = av_mod.view(project_dir)
        pp("view() AFTER random_clip", snap2)

        drums_after = next(t for t in snap2["tracks"] if t["name"] == "drums")
        assert len(drums_after["placements"]) == 2, (
            f"Expected 2 placements, got {len(drums_after['placements'])}"
        )
        bars = [p["start_bar"] for p in drums_after["placements"]]
        assert 1 in bars and 5 in bars, f"Expected placements at bars 1 and 5, got {bars}"

        print("\n\n✓ All assertions passed.")


if __name__ == "__main__":
    main()
