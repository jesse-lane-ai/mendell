"""Tests for `mendell beat random32` — the library-driven 32-bar beat project.

Hermetic: builds a tiny sqlite library.db pointing at synthetic WAVs, then renders
with --no-warp so the test needs neither rubberband nor the real sample packs.
"""

import sqlite3
import wave

import numpy as np
import pytest

from mendell import beat_random32 as br
from mendell import project as project_mod


def _write_wav(path, seconds=1.0, freq=220.0, sr=br.SR):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    tone = 0.2 * np.sin(2 * np.pi * freq * t).astype(np.float32)
    stereo = np.column_stack([tone, tone])
    w = wave.open(str(path), "w")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes((stereo * 32767).astype(np.int16).tobytes())
    w.close()


@pytest.fixture
def library_db(tmp_path):
    """A registered library with one sample per category random32 needs."""
    samples = tmp_path / "samples"
    samples.mkdir()
    cats = {"kick": 110, "snare": 300, "hat": 800, "clap": 500,
            "bass": 90, "melody": 440, "loop": 660}
    for cat, freq in cats.items():
        _write_wav(samples / f"{cat}.wav", freq=freq)

    db = tmp_path / "library.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE libraries (name TEXT, path TEXT)")
    con.execute("CREATE TABLE files (library_name TEXT, rel_path TEXT, category TEXT, bpm REAL)")
    con.execute("INSERT INTO libraries VALUES (?, ?)", ("test", str(samples)))
    for cat in cats:
        con.execute("INSERT INTO files VALUES (?, ?, ?, ?)",
                    ("test", f"{cat}.wav", cat, 90.0))
    con.commit()
    con.close()
    return str(db)


def test_render_builds_full_project(tmp_path, library_db):
    data = br.render(tmp_path, "beat", db_path=library_db, tempo=120, key="A",
                     seed=1, export_format="wav", warp=False)

    proj = tmp_path / "beat"
    assert (proj / "project.toml").exists()
    assert data["pattern"] == "mutation-loop"
    assert data["engine"] == "none"          # warp disabled in test
    assert data["tempo"] == 120.0
    assert data["key"] == "A"
    assert data["bars"] == 32
    assert data["sections"] == 4
    assert data["tracks"] == ["drums", "kit", "bass", "melody"]
    assert data["melody_treatments"] == ["clean", "octave-up", "lowpass", "reverse"]

    # one melody clip per section + bass + drum clips exist on disk
    for clip in ("mel-1-clean", "mel-2-octave-up", "mel-3-lowpass", "mel-4-reverse",
                 "bass-loop", "drum-loop"):
        assert (proj / "clips" / f"{clip}.toml").exists()

    # exported a real, non-empty file
    out = data["export"].get("out") or data["export"].get("path")
    assert out and __import__("os").path.getsize(out) > 0


def test_pattern_drives_layers(tmp_path, library_db):
    """A layer pattern toggles tracks off via vol automation."""
    import tomllib
    data = br.render(tmp_path, "lb", db_path=library_db, seed=2,
                     export_format="wav", warp=False, pattern="layer-builder")
    assert data["pattern"] == "layer-builder"
    # layer-builder starts melody-only -> drums (kit) silent in section 1
    assert data["section_layers"][0] == ["melody"]
    kit = tomllib.loads((tmp_path / "lb" / "tracks" / "kit.toml").read_text())
    vol = next(a for a in kit["automation"] if a["param"] == "vol")
    assert vol["points"][0]["value"] == 0.0      # drums start muted


def test_unknown_pattern_raises(tmp_path, library_db):
    with pytest.raises(FileNotFoundError):
        br.render(tmp_path, "x", db_path=library_db, warp=False, pattern="nope")


def test_arrangement_is_32_bars(tmp_path, library_db):
    br.render(tmp_path, "beat", db_path=library_db, seed=3,
              export_format="wav", warp=False)
    info = project_mod.info(tmp_path / "beat")
    # project tempo lands in the random range
    assert 70.0 <= info["bpm"] <= 160.0


def test_seed_is_deterministic(tmp_path, library_db):
    a = br.render(tmp_path / "a", "beat", db_path=library_db, seed=7,
                  export_format="wav", warp=False)
    b = br.render(tmp_path / "b", "beat", db_path=library_db, seed=7,
                  export_format="wav", warp=False)
    assert (a["tempo"], a["key"], a["bass"], a["melody"]) == \
           (b["tempo"], b["key"], b["bass"], b["melody"])


def test_invalid_key_rejected(tmp_path, library_db):
    with pytest.raises(ValueError):
        br.render(tmp_path, "beat", db_path=library_db, key="H", warp=False)


def test_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        br.render(tmp_path, "beat", db_path=str(tmp_path / "nope.db"), warp=False)
