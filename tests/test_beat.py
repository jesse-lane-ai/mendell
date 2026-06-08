"""Unit tests for `mendell beat` scaffolding and the high-level `make` orchestration."""

import random

import pytest

from mendell import arrangement as arrangement_mod
from mendell import beat
from mendell import clips as clips_mod
from mendell import project as project_mod
from mendell import tracks as tracks_mod
from mendell.errors import BadInputError


# --- duration parsing -------------------------------------------------------

def test_parse_duration_seconds_forms():
    assert beat._parse_duration_seconds("60s") == 60.0
    assert beat._parse_duration_seconds("90") == 90.0
    assert beat._parse_duration_seconds(1.5) == 1.5
    assert beat._parse_duration_seconds(30) == 30.0


def test_parse_duration_seconds_invalid():
    with pytest.raises(BadInputError):
        beat._parse_duration_seconds("abc")
    with pytest.raises(BadInputError):
        beat._parse_duration_seconds("0s")
    with pytest.raises(BadInputError):
        beat._parse_duration_seconds(-5)


def test_seconds_per_bar():
    # 4 beats/bar at 120 BPM -> 2.0s per bar
    assert beat._seconds_per_bar(120.0) == pytest.approx(2.0)


# --- variation humanizing ---------------------------------------------------

def _notes(pattern):
    return [n for _, n, _, _ in pattern]


def test_humanize_keeps_kick_and_snare():
    base = beat.STYLES["lofi"]["pattern"]
    rng = random.Random(1234)
    for _ in range(25):
        variant = beat._humanize_pattern(base, rng)
        notes = _notes(variant)
        # backbone is preserved: every kick/snare in base survives
        assert notes.count(beat._GM_KICK) >= _notes(base).count(beat._GM_KICK)
        assert notes.count(beat._GM_SNARE) == _notes(base).count(beat._GM_SNARE)


def test_humanize_velocities_in_range_and_sorted():
    base = beat.STYLES["dark"]["pattern"]
    variant = beat._humanize_pattern(base, random.Random(7))
    offsets = [o for o, _, _, _ in variant]
    assert offsets == sorted(offsets)
    assert all(1 <= vel <= 127 for _, _, vel, _ in variant)


def test_humanize_is_deterministic_per_seed():
    base = beat.STYLES["lofi"]["pattern"]
    a = beat._humanize_pattern(base, random.Random(42))
    b = beat._humanize_pattern(base, random.Random(42))
    assert a == b


# --- make orchestration -----------------------------------------------------

def test_make_rejects_bad_style(tmp_path):
    with pytest.raises(BadInputError):
        beat.make(tmp_path, "x", style="nope")


def test_make_rejects_zero_variations(tmp_path):
    with pytest.raises(BadInputError):
        beat.make(tmp_path, "x", style="lofi", variations=0)


def test_make_applies_overrides_and_sections(tmp_path):
    result = beat.make(
        tmp_path, "song", style="lofi",
        bpm=90.0, key="F", duration="30s", variations=3, seed=1,
    )
    project_dir = tmp_path / "song"

    # bpm/key overrides take effect
    assert result["bpm"] == 90.0
    assert result["key"] == "F"
    info = project_mod.info(project_dir)
    assert info["bpm"] == 90.0
    assert info["key"] == "F"

    # 30s at 90 BPM (2.667s/bar) -> 12 bars -> ceil(12/8) = 2 sections
    assert result["sections"] == 2
    assert result["arrangement_bars"] == 2 * beat.ARRANGEMENT_BARS
    assert result["variations"] == 3

    # an mp3 was exported
    out = result["export"].get("out") or result["export"].get("path")
    assert out is not None
    assert (project_dir / out).exists() if not str(out).startswith("/") else __import__("pathlib").Path(out).exists()


def test_make_places_variants_across_sections(tmp_path):
    beat.make(tmp_path, "tune", style="lofi", duration="60s", variations=4, seed=2)
    project_dir = tmp_path / "tune"

    placements = arrangement_mod.list_placements(project_dir)
    drum_bars = sorted(p["bar"] for p in placements if p["track"] == beat.DRUM_TRACK)

    # 60s at 78 BPM -> ~20 bars -> 3 sections, placed at bars 1, 9, 17
    assert drum_bars == [1, 9, 17]

    # the expected variant clips exist on the drum track
    clip_names = {c["name"] for c in clips_mod.list_clips(project_dir, beat.DRUM_TRACK)}
    assert {"var-1", "var-2", "var-3", "var-4"} <= clip_names


def test_make_minimum_one_section(tmp_path):
    # tiny duration still yields a full 8-bar section
    result = beat.make(tmp_path, "short", style="energetic", duration="2s", variations=1, seed=3)
    assert result["sections"] == 1
    assert result["arrangement_bars"] == beat.ARRANGEMENT_BARS


def test_make_creates_expected_tracks(tmp_path):
    beat.make(tmp_path, "trk", style="dark", duration="16s", variations=2, seed=4)
    project_dir = tmp_path / "trk"
    track_names = {t["name"] for t in tracks_mod.list_tracks(project_dir)}
    assert beat.DRUM_TRACK in track_names
    assert beat.KIT_TRACK in track_names
