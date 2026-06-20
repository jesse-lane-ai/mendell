"""Tests for the on-track instrument model and legacy-project migration.

A MIDI track hosts a sampler instrument directly (no separate sampler track /
routing). A sampler runs in "kit" or "instrument" mode. Old projects that used
a separate sampler track + routes are migrated by `migrate.migrate_project`.
"""

import wave

import numpy as np
import pytest

from mendell import migrate as migrate_mod
from mendell import paths
from mendell import project as project_mod
from mendell import sampler as sampler_mod
from mendell import tracks as tracks_mod
from mendell.errors import BadInputError
from mendell.toml_io import read_toml, write_toml


def _write_wav(path, seconds=0.1, sr=22050):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sr)
    data = (np.random.rand(frames) * 0.2 - 0.1).astype(np.float32)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((data * 32767).astype("<i2").tobytes())


# --- new model --------------------------------------------------------------

def test_sampler_type_no_longer_a_track_type():
    assert tracks_mod.VALID_TYPES == {"midi", "audio"}
    assert "sampler" not in tracks_mod.VALID_TYPES


def test_create_sampler_hosts_on_midi_track(tmp_path):
    proj = project_mod.create(tmp_path, "p", bpm=120)
    tracks_mod.add(proj, "drums", "midi")
    sampler_mod.create(proj, "drums")
    assert tracks_mod.get_instrument(proj, "drums") == {"type": "sampler"}
    assert sampler_mod.load(proj, "drums")["sampler"]["mode"] == "kit"


def test_create_sampler_rejects_non_midi_track(tmp_path):
    proj = project_mod.create(tmp_path, "p", bpm=120)
    tracks_mod.add(proj, "loop", "audio")
    with pytest.raises(BadInputError):
        sampler_mod.create(proj, "loop")


def test_instrument_mode_single_sample_across_range(tmp_path):
    proj = project_mod.create(tmp_path, "p", bpm=120)
    tracks_mod.add(proj, "lead", "midi")
    samp = tmp_path / "lead.wav"
    _write_wav(samp)
    sampler_mod.load_instrument(proj, "lead", str(samp), root="C3")
    data = sampler_mod.load(proj, "lead")
    assert data["sampler"]["mode"] == "instrument"
    slots = data["slots"]
    assert len(slots) == 1
    assert slots[0]["note_low"] == 0 and slots[0]["note_high"] == 127


def test_set_mode_round_trip(tmp_path):
    proj = project_mod.create(tmp_path, "p", bpm=120)
    tracks_mod.add(proj, "drums", "midi")
    sampler_mod.create(proj, "drums")
    sampler_mod.set_mode(proj, "drums", "instrument")
    assert sampler_mod.load(proj, "drums")["sampler"]["mode"] == "instrument"
    with pytest.raises(BadInputError):
        sampler_mod.set_mode(proj, "drums", "bogus")


# --- migration --------------------------------------------------------------

def _legacy_project(tmp_path):
    proj = project_mod.create(tmp_path, "legacy", bpm=120)
    write_toml(paths.track_toml(proj, "drums"), {
        "track": {"name": "drums", "type": "midi"},
        "mixer": {"vol": 100, "pan": 0, "mute": False, "solo": False},
        "fx": [], "clips": ["pat"], "routes": ["kit"],
    })
    write_toml(paths.track_toml(proj, "kit"), {
        "track": {"name": "kit", "type": "sampler"},
        "mixer": {"vol": 80, "pan": 0, "mute": False, "solo": False},
        "fx": [{"id": 0, "type": "reverb"}], "clips": [],
    })
    write_toml(paths.sampler_toml(proj, "kit"), {
        "sampler": {"polyphony": 8, "tune": 0},
        "slots": [{"note_low": 36, "note_high": 36, "root": 36,
                   "sample": "/x/kick.wav", "linked": False}],
    })
    return proj


def test_migration_folds_sampler_into_midi_host(tmp_path):
    proj = _legacy_project(tmp_path)
    assert migrate_mod.needs_migration(proj)

    res = migrate_mod.migrate_project(proj)
    assert res["migrated"] is True
    assert res["folded"] == [{"host": "drums", "from": "kit"}]

    drums = read_toml(paths.track_toml(proj, "drums"))
    assert drums["track"]["instrument"] == {"type": "sampler"}
    assert "routes" not in drums
    assert drums["fx"] == [{"id": 0, "type": "reverb"}]  # inherited from sampler track

    # sampler config moved onto the MIDI track's name, with an inferred mode
    assert paths.sampler_toml(proj, "drums").is_file()
    assert read_toml(paths.sampler_toml(proj, "drums"))["sampler"]["mode"] == "kit"

    # the separate sampler track is gone
    assert not paths.track_toml(proj, "kit").is_file()
    assert not paths.sampler_toml(proj, "kit").is_file()


def test_migration_is_idempotent(tmp_path):
    proj = _legacy_project(tmp_path)
    migrate_mod.migrate_project(proj)
    assert migrate_mod.needs_migration(proj) is False
    again = migrate_mod.migrate_project(proj)
    assert again["migrated"] is False


def test_migration_infers_instrument_mode_for_full_range_slot(tmp_path):
    proj = project_mod.create(tmp_path, "legacy2", bpm=120)
    write_toml(paths.track_toml(proj, "lead"), {
        "track": {"name": "lead", "type": "midi"},
        "mixer": {"vol": 100, "pan": 0, "mute": False, "solo": False},
        "fx": [], "clips": [], "routes": ["synthsamp"],
    })
    write_toml(paths.track_toml(proj, "synthsamp"), {
        "track": {"name": "synthsamp", "type": "sampler"},
        "mixer": {"vol": 100, "pan": 0, "mute": False, "solo": False},
        "fx": [], "clips": [],
    })
    write_toml(paths.sampler_toml(proj, "synthsamp"), {
        "sampler": {"polyphony": 8, "tune": 0},
        "slots": [{"note_low": 0, "note_high": 127, "root": 60,
                   "sample": "/x/pad.wav", "linked": False}],
    })
    migrate_mod.migrate_project(proj)
    assert read_toml(paths.sampler_toml(proj, "lead"))["sampler"]["mode"] == "instrument"


def test_migration_orphan_sampler_becomes_midi_host(tmp_path):
    proj = project_mod.create(tmp_path, "orphan", bpm=120)
    write_toml(paths.track_toml(proj, "kit"), {
        "track": {"name": "kit", "type": "sampler"},
        "mixer": {"vol": 100, "pan": 0, "mute": False, "solo": False},
        "fx": [], "clips": [],
    })
    write_toml(paths.sampler_toml(proj, "kit"), {
        "sampler": {"polyphony": 8, "tune": 0}, "slots": [],
    })
    migrate_mod.migrate_project(proj)
    kit = read_toml(paths.track_toml(proj, "kit"))
    assert kit["track"]["type"] == "midi"
    assert kit["track"]["instrument"] == {"type": "sampler"}
