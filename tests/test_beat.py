"""Unit tests for `mendell beat` scaffolding and the high-level `make` orchestration."""

import random
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from mendell import arrangement as arrangement_mod
from mendell import beat
from mendell import clips as clips_mod
from mendell import project as project_mod
from mendell import tracks as tracks_mod
from mendell.errors import BadInputError


def _write_wav(path: Path, seconds: float, sr: int = 22050) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(seconds * sr), dtype="float32"), sr)


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


# --- beat from-library ------------------------------------------------------

def _match(ref, category="", instruments=None):
    return {"ref": ref, "category": category, "instruments": instruments or []}


def test_bucket_library_oneshots_by_category_instrument_and_filename():
    matches = [
        _match("lib/kick01.wav", category="kick"),
        _match("lib/sn.wav", category="snare"),
        _match("lib/x.wav", instruments=["open hat"]),       # instrument tag
        _match("lib/HHCLOSED.wav"),                           # filename fallback
        _match("lib/melodic-stab.wav", category="melody"),    # not a drum role
    ]
    buckets = beat._bucket_library_oneshots(matches)
    assert "lib/kick01.wav" in buckets[beat._GM_KICK]
    assert "lib/sn.wav" in buckets[beat._GM_SNARE]
    assert "lib/x.wav" in buckets[beat._GM_OPEN_HAT]
    assert "lib/HHCLOSED.wav" in buckets[beat._GM_CLOSED_HAT]  # via filename guess
    assert not any("melodic-stab" in r for refs in buckets.values() for r in refs)


def test_pick_kit_is_seeded_and_falls_back():
    buckets = {note: [] for note in beat._NOTE_ROLES}
    buckets[beat._GM_KICK] = ["k1", "k2"]
    buckets[beat._GM_SNARE] = ["s1"]
    buckets[beat._GM_CLOSED_HAT] = ["h1"]
    notes = {beat._GM_KICK, beat._GM_SNARE, beat._GM_OPEN_HAT, beat._GM_PERC}
    chosen, silent = beat._pick_kit(buckets, notes, random.Random(1))
    assert chosen[beat._GM_KICK] in ("k1", "k2")
    assert chosen[beat._GM_SNARE] == "s1"
    assert chosen[beat._GM_OPEN_HAT] == "h1"   # borrowed closed-hat pool (fallback)
    assert beat._GM_PERC in silent             # nothing anywhere -> silent
    chosen2, _ = beat._pick_kit(buckets, notes, random.Random(1))
    assert chosen2 == chosen                    # same seed -> same pick


def _fake_library(monkeypatch, tmp_path, refs_by_category):
    """Point beat.from_library at fake library data: real tiny WAVs on disk, a
    search() returning them, and resolve_ref() mapping ref -> path."""
    paths, matches = {}, []
    for category, names in refs_by_category.items():
        for n in names:
            p = tmp_path / "samples" / n
            _write_wav(p, 0.1)
            ref = f"lib/{n}"
            paths[ref] = p
            matches.append(_match(ref, category=category))
    monkeypatch.setattr(beat.library_mod, "search", lambda **kw: {"matches": matches})
    monkeypatch.setattr(beat.library_mod, "resolve_ref", lambda ref: paths[ref])


def test_from_library_builds_loop(tmp_path, monkeypatch):
    _fake_library(monkeypatch, tmp_path, {
        "kick": ["kick1.wav", "kick2.wav"],
        "snare": ["snare1.wav"],
        "hat": ["hat1.wav"],
    })
    out = beat.from_library(tmp_path, "loop", library="lib", style="lofi", bars=4, seed=5)

    project_dir = tmp_path / "loop"
    assert out["bars"] == 4
    assert out["silent_notes"] == []  # lofi uses kick/snare/closed-hat, all present
    notes = {m["note"] for m in out["kit"]}
    assert {"C2", "D2", "F#2"} <= notes  # GM 36/38/42
    track_names = {t["name"] for t in tracks_mod.list_tracks(project_dir)}
    assert {beat.DRUM_TRACK, beat.KIT_TRACK} <= track_names
    assert arrangement_mod.load(project_dir)["arrangement"]["length"] == 4.0
    # Reproducible: same seed -> same kit refs.
    out2 = beat.from_library(tmp_path, "loop2", library="lib", style="lofi", bars=4, seed=5)
    assert [m["ref"] for m in out2["kit"]] == [m["ref"] for m in out["kit"]]


def test_from_library_needs_kick_or_snare(tmp_path, monkeypatch):
    _fake_library(monkeypatch, tmp_path, {"hat": ["hat1.wav"], "perc": ["perc1.wav"]})
    with pytest.raises(BadInputError, match="no kick or snare"):
        beat.from_library(tmp_path, "loop", library="lib", style="lofi")


def test_from_library_rejects_bad_style(tmp_path):
    with pytest.raises(BadInputError):
        beat.from_library(tmp_path, "x", library="lib", style="nope")
