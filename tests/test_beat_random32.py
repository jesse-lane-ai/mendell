"""Tests for `mendell beat random32` — the library-driven 32-bar beat.

Hermetic: builds a tiny sqlite library.db pointing at synthetic WAVs, then renders
with --no-warp so the test needs neither rubberband nor the real sample packs.
"""

import sqlite3
import wave

import numpy as np
import pytest

from mendell import beat_random32 as br


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


def test_render_produces_32_bar_wav(tmp_path, library_db):
    out = tmp_path / "beat.wav"
    data = br.render(str(out), db_path=library_db, tempo=120, key="A",
                     seed=1, warp=False)

    assert out.exists() and out.stat().st_size > 0
    assert data["engine"] == "resample"
    assert data["tempo"] == 120.0
    assert data["key"] == "A"
    assert data["bars"] == 32
    assert data["sections"] == 4
    assert data["mutations"] == ["clean", "octave-up", "lowpass", "reverse"]

    # 32 bars of 4/4 at 120 BPM = 64.0s; the WAV should match within a frame.
    with wave.open(str(out)) as w:
        assert w.getframerate() == br.SR
        assert w.getnchannels() == 2
        assert abs(w.getnframes() / br.SR - 64.0) < 0.05


def test_seed_is_deterministic(tmp_path, library_db):
    a = br.render(str(tmp_path / "a.wav"), db_path=library_db, seed=7, warp=False)
    b = br.render(str(tmp_path / "b.wav"), db_path=library_db, seed=7, warp=False)
    assert (a["tempo"], a["key"], a["bass"], a["melody"]) == \
           (b["tempo"], b["key"], b["bass"], b["melody"])


def test_invalid_key_rejected(tmp_path, library_db):
    with pytest.raises(ValueError):
        br.render(str(tmp_path / "x.wav"), db_path=library_db, key="H", warp=False)


def test_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        br.render(str(tmp_path / "x.wav"), db_path=str(tmp_path / "nope.db"), warp=False)
