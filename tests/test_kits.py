"""Unit tests for the named drum-kit registry (`mendell.kits`).

Focused on ``quick_kit`` filling all 16 visual pads (GM notes 36–51).
"""

from pathlib import Path

import pytest

from mendell import kits
from mendell import library as library_mod


@pytest.fixture
def kit_db(tmp_path, monkeypatch):
    """Isolate the shared library.db (kits live in it) per test."""
    monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(tmp_path / "library.db"))
    return tmp_path


def _make_library(tmp_path, monkeypatch, by_category):
    """Monkeypatch library_mod.search / resolve_ref to serve fake one-shots.

    ``by_category`` maps category -> list of filenames. Each ref resolves to a
    real (empty) file under ``tmp_path`` so add_slot's existence check passes.
    """
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir(exist_ok=True)
    ref_to_path: dict[str, Path] = {}
    cat_to_matches: dict[str, list[dict]] = {}
    for category, names in by_category.items():
        matches = []
        for fn in names:
            p = samples_dir / fn
            p.write_bytes(b"RIFF")  # existence only
            ref = f"ref::{fn}"
            ref_to_path[ref] = p
            matches.append({"ref": ref, "kind": "one-shot"})
        cat_to_matches[category] = matches

    all_matches = [m for ms in cat_to_matches.values() for m in ms]

    def fake_search(query=None, *, library=None, category=None, kind=None, **kw):
        if category is None:
            return {"matches": list(all_matches)}
        return {"matches": list(cat_to_matches.get(category, []))}

    def fake_resolve_ref(ref):
        return ref_to_path.get(ref)

    monkeypatch.setattr(library_mod, "search", fake_search)
    monkeypatch.setattr(library_mod, "resolve_ref", fake_resolve_ref)
    return ref_to_path


def test_quick_kit_fills_all_16_pads(kit_db, monkeypatch):
    """A reasonably populated library fills every one of the 16 pads."""
    _make_library(kit_db, monkeypatch, {
        "kick": ["kick1.wav", "kick2.wav"],
        "snare": ["snare1.wav"],
        "clap": ["clap1.wav"],
        "hat": ["hat1.wav", "hat2.wav"],
        "openhat": ["ohat1.wav"],
        "tom": ["tom1.wav", "tom2.wav"],
        "crash": ["crash1.wav"],
        "ride": ["ride1.wav"],
        "rim": ["rim1.wav"],
        "perc": ["perc1.wav", "perc2.wav", "perc3.wav"],
    })

    result = kits.quick_kit("full", seed=1)

    assert result["slot_count"] == 16
    notes = {s["gm_note"] for s in result["slots"]}
    assert notes == set(range(36, 52))
    assert len(result["assigned_categories"]) == 16
    assert "missing_categories" not in result


def test_quick_kit_fallback_to_any_when_category_empty(kit_db, monkeypatch):
    """Pads whose category has no matches fall back to any one-shot — still 16."""
    _make_library(kit_db, monkeypatch, {
        "kick": ["kick1.wav"],
        "snare": ["snare1.wav"],
        # no hat/openhat/tom/crash/ride/rim/clap/perc — must fall back
    })

    result = kits.quick_kit("fallback", seed=7)

    assert result["slot_count"] == 16
    assert {s["gm_note"] for s in result["slots"]} == set(range(36, 52))
    assert "missing_categories" not in result
    # every slot points at one of the two available real files
    for s in result["slots"]:
        assert Path(s["source_path"]).exists()


def test_quick_kit_reuse_allowed_small_pool(kit_db, monkeypatch):
    """A single available sample still fills all 16 pads via reuse."""
    _make_library(kit_db, monkeypatch, {"kick": ["only.wav"]})

    result = kits.quick_kit("one", seed=3)

    assert result["slot_count"] == 16
    paths = {s["source_path"] for s in result["slots"]}
    assert len(paths) == 1  # reused everywhere


def test_quick_kit_empty_library_reports_missing(kit_db, monkeypatch):
    """An empty library fills nothing and reports the unfillable pads."""
    _make_library(kit_db, monkeypatch, {})

    result = kits.quick_kit("empty", seed=0)

    assert result["slot_count"] == 0
    assert result["assigned_categories"] == []
    assert len(result["missing_categories"]) == 16


def test_quick_kit_prefers_variety(kit_db, monkeypatch):
    """With enough distinct samples, distinct pads get distinct files."""
    perc_files = [f"p{i}.wav" for i in range(16)]
    _make_library(kit_db, monkeypatch, {
        "kick": ["k.wav"], "snare": ["s.wav"], "clap": ["c.wav"],
        "rim": ["r.wav"], "hat": ["h.wav"], "openhat": ["oh.wav"],
        "tom": ["t.wav"], "crash": ["cr.wav"], "ride": ["rd.wav"],
        "perc": perc_files,
    })

    result = kits.quick_kit("variety", seed=11)
    paths = [s["source_path"] for s in result["slots"]]
    # 7 perc pads should each pick a different perc file (variety preferred)
    assert len(set(paths)) == 16
