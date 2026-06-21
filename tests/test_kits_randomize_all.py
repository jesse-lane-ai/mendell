"""Tests for ``kits.randomize_all`` — fills all 16 GM drum pads in one action.

The real library search/resolve is monkeypatched so the test needs no actual
audio files or registered library.
"""

from pathlib import Path

from mendell import kits
from mendell import library as library_mod


def _fake_library(monkeypatch, tmp_path):
    """Make library.search always return one resolvable candidate and
    resolve_ref return a real (touched) file path."""
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"RIFF")  # content irrelevant — add_slot just stores the path

    def fake_search(*, library=None, **kw):
        return {"matches": [{"ref": "fake:1", "kind": "one-shot"}]}

    monkeypatch.setattr(library_mod, "search", fake_search)
    monkeypatch.setattr(library_mod, "resolve_ref", lambda ref: sample)
    return sample


def test_randomize_all_fills_sixteen_pads(monkeypatch, tmp_path):
    _fake_library(monkeypatch, tmp_path)

    result = kits.randomize_all("rk")

    assert result["filled_count"] == 16
    assert len(result["failures"]) == 0
    notes = sorted(p["gm_note"] for p in result["filled"])
    assert notes == list(range(36, 52))
    # All 16 slots are present on the kit itself.
    assert {s["gm_note"] for s in result["slots"]} == set(range(36, 52))


def test_randomize_all_is_deterministic_with_seed(monkeypatch, tmp_path):
    _fake_library(monkeypatch, tmp_path)

    a = kits.randomize_all("ka", seed=42)
    b = kits.randomize_all("kb", seed=42)

    pa = {s["gm_note"]: s["source_path"] for s in a["slots"]}
    pb = {s["gm_note"]: s["source_path"] for s in b["slots"]}
    assert pa == pb


def test_randomize_all_collects_failures(monkeypatch, tmp_path):
    # No candidates -> every pad fails, but the call still returns gracefully.
    monkeypatch.setattr(library_mod, "search", lambda **kw: {"matches": []})

    result = kits.randomize_all("kf")

    assert result["filled_count"] == 0
    assert len(result["failures"]) == 16
