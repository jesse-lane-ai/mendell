"""Unit tests for the sample library registry (`mendell.library`)."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from mendell import library
from mendell.clips import audio_analysis
from mendell.errors import BadInputError, NotFoundError


def _write_wav(path: Path, seconds: float, sr: int = 22050) -> None:
    """Write a real (silent) WAV of a given length — enough for the library's
    header-only duration probe and bar-alignment math (content is irrelevant)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(seconds * sr), dtype="float32"), sr)


@pytest.fixture
def lib_config(tmp_path, monkeypatch):
    """Point the library at an isolated database file for each test."""
    config_path = tmp_path / "library.db"
    monkeypatch.setenv(library.CONFIG_ENV_VAR, str(config_path))
    return config_path


@pytest.fixture
def sample_folder(tmp_path):
    folder = tmp_path / "drum-pack"
    (folder / "Kicks").mkdir(parents=True)
    (folder / "Loops").mkdir()
    (folder / "Kicks" / "kick-808.wav").write_bytes(b"fake")
    (folder / "Kicks" / "snare_tight.wav").write_bytes(b"fake")
    (folder / "Loops" / "dark-loop.wav").write_bytes(b"fake")
    (folder / "Loops" / "notes.txt").write_bytes(b"not audio")  # ignored — wrong extension
    return folder


# --- registration -----------------------------------------------------------

def test_add_registers_and_counts_files(lib_config, sample_folder):
    entry = library.add("drum-pack", str(sample_folder), tags=["drums", "lofi"])
    assert entry["name"] == "drum-pack"
    assert entry["path"] == str(sample_folder.resolve())
    assert entry["tags"] == ["drums", "lofi"]
    assert entry["file_count"] == 3  # .txt is excluded


def test_search_and_show_use_cached_index_not_a_live_walk(lib_config, sample_folder):
    """search/show read the cache populated at add/scan time — files dropped in
    afterwards shouldn't appear until an explicit rescan."""
    library.add("drum-pack", str(sample_folder))

    (sample_folder / "Kicks" / "kick-new.wav").write_bytes(b"fake")
    assert library.search("kick-new")["matches"] == []
    assert all(f["ref"] != "drum-pack/Kicks/kick-new.wav" for f in library.show("drum-pack")["files"])

    library.scan("drum-pack")
    assert [m["ref"] for m in library.search("kick-new")["matches"]] == ["drum-pack/Kicks/kick-new.wav"]


def test_add_missing_folder_raises(lib_config, tmp_path):
    with pytest.raises(BadInputError):
        library.add("nope", str(tmp_path / "does-not-exist"))


def test_add_is_idempotent_and_updates_in_place(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder), tags=["drums"])
    updated = library.add("drum-pack", str(sample_folder), tags=["drums", "new-tag"])
    assert updated["tags"] == ["drums", "new-tag"]
    assert library.list_entries()["libraries"] == [updated]


def test_remove(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder))
    assert library.remove("drum-pack") == {"removed": "drum-pack"}
    assert library.list_entries()["libraries"] == []


def test_remove_unknown_raises(lib_config):
    with pytest.raises(NotFoundError):
        library.remove("nope")


# --- multiple folders --------------------------------------------------------

def test_supports_multiple_registered_folders(lib_config, tmp_path, sample_folder):
    other = tmp_path / "other-pack"
    (other / "Hats").mkdir(parents=True)
    (other / "Hats" / "hat-closed.wav").write_bytes(b"fake")

    library.add("drum-pack", str(sample_folder))
    library.add("other-pack", str(other))

    names = sorted(e["name"] for e in library.list_entries()["libraries"])
    assert names == ["drum-pack", "other-pack"]


# --- categorization ----------------------------------------------------------

def test_guess_category_from_filename():
    assert library.guess_category(Path("kick-808.wav")) == "kick"
    assert library.guess_category(Path("snare_tight.wav")) == "snare"
    assert library.guess_category(Path("hat-closed.wav")) == "hat"


def test_guess_category_falls_back_to_parent_folder():
    assert library.guess_category(Path("Loops/weird-name-123.wav")) == "loop"
    assert library.guess_category(Path("Misc/totally-unrecognized.wav")) == "one-shot"


# --- loop / one-shot kind ----------------------------------------------------

def test_detect_kind_from_filename():
    assert audio_analysis.detect_kind_from_filename("dark-loop.wav") == "loop"
    assert audio_analysis.detect_kind_from_filename("drumloop_90.wav") == "loop"
    assert audio_analysis.detect_kind_from_filename("snare-oneshot.wav") == "one-shot"
    assert audio_analysis.detect_kind_from_filename("vocal-stab.wav") == "one-shot"
    assert audio_analysis.detect_kind_from_filename("808-hit.wav") == "one-shot"
    # bounded matching: "hit" inside "white", "shot" inside "gunshot" don't count
    assert audio_analysis.detect_kind_from_filename("white-noise.wav") is None
    assert audio_analysis.detect_kind_from_filename("gunshot-fx.wav") is None
    assert audio_analysis.detect_kind_from_filename("piano-c3.wav") is None


def test_kind_one_shot_via_duration(lib_config, tmp_path):
    folder = tmp_path / "pack"
    _write_wav(folder / "blip.wav", 0.3)  # short, no keyword, no bpm

    f = {x["ref"]: x for x in library.show(library.add("pack", str(folder))["name"])["files"]}
    assert f["pack/blip.wav"]["kind"] == "one-shot"
    assert f["pack/blip.wav"]["kind_source"] == "duration"
    assert f["pack/blip.wav"]["duration"] == 0.3


def test_kind_loop_via_bar_alignment(lib_config, tmp_path):
    folder = tmp_path / "pack"
    # 4.0s == 2 bars at 120 BPM; filename carries the tempo but not "loop".
    _write_wav(folder / "melody-phrase-120bpm.wav", 4.0)

    f = {x["ref"]: x for x in library.show(library.add("pack", str(folder))["name"])["files"]}
    entry = f["pack/melody-phrase-120bpm.wav"]
    assert entry["category"] == "melody"   # orthogonal to kind
    assert entry["kind"] == "loop"
    assert entry["kind_source"] == "bar-align"


def test_kind_unknown_when_inconclusive(lib_config, tmp_path):
    folder = tmp_path / "pack"
    # mid-length, no filename keyword, no tempo, ambiguous category -> honest unknown
    _write_wav(folder / "atmosphere.wav", 3.7)

    f = {x["ref"]: x for x in library.show(library.add("pack", str(folder))["name"])["files"]}
    assert f["pack/atmosphere.wav"]["kind"] == "unknown"
    assert "kind_source" not in f["pack/atmosphere.wav"]


def test_kind_filename_keyword_beats_a_short_duration(lib_config, tmp_path):
    folder = tmp_path / "pack"
    # explicitly a loop by name, even though it's shorter than ONESHOT_MAX_SEC
    _write_wav(folder / "micro-loop.wav", 0.5)

    f = {x["ref"]: x for x in library.show(library.add("pack", str(folder))["name"])["files"]}
    assert f["pack/micro-loop.wav"]["kind"] == "loop"
    assert f["pack/micro-loop.wav"]["kind_source"] == "filename"


def test_search_by_kind(lib_config, tmp_path):
    folder = tmp_path / "pack"
    _write_wav(folder / "drum-loop.wav", 4.0)  # loop via filename
    _write_wav(folder / "clap.wav", 0.2)       # one-shot via duration
    library.add("pack", str(folder))

    assert [m["ref"] for m in library.search(kind="loop")["matches"]] == ["pack/drum-loop.wav"]
    assert [m["ref"] for m in library.search(kind="one-shot")["matches"]] == ["pack/clap.wav"]


def test_unreadable_file_has_no_duration_and_falls_back_to_category(lib_config, sample_folder):
    """Placeholder/corrupt files (no readable header) still index — kind falls
    back to the category prior, with no duration recorded."""
    f = {x["ref"]: x for x in library.show(library.add("drum-pack", str(sample_folder))["name"])["files"]}
    kick = f["drum-pack/Kicks/kick-808.wav"]
    assert "duration" not in kick               # b"fake" bytes -> header unreadable
    assert kick["kind"] == "one-shot"           # drum category prior
    assert kick["kind_source"] == "category"
    assert f["drum-pack/Loops/dark-loop.wav"]["kind"] == "loop"  # filename keyword


# --- BPM detection -----------------------------------------------------------

def test_add_caches_bpm_from_filename_for_any_category(lib_config, tmp_path):
    folder = tmp_path / "pack"
    (folder / "Kicks").mkdir(parents=True)
    (folder / "Loops").mkdir()
    (folder / "Kicks" / "kick-808-90bpm.wav").write_bytes(b"fake")
    (folder / "Loops" / "dark-loop-135bpm.wav").write_bytes(b"fake")
    (folder / "Loops" / "no-tempo-hint.wav").write_bytes(b"fake")

    entry = library.show(library.add("pack", str(folder))["name"])
    by_ref = {f["ref"]: f for f in entry["files"]}

    assert by_ref["pack/Kicks/kick-808-90bpm.wav"]["bpm"] == 90.0
    assert by_ref["pack/Kicks/kick-808-90bpm.wav"]["bpm_source"] == "filename"
    assert by_ref["pack/Loops/dark-loop-135bpm.wav"]["bpm"] == 135.0
    assert "bpm" not in by_ref["pack/Loops/no-tempo-hint.wav"]


def test_analyze_flag_runs_signal_analysis_only_for_loops_without_filename_bpm(lib_config, tmp_path, monkeypatch):
    folder = tmp_path / "pack"
    (folder / "Kicks").mkdir(parents=True)
    (folder / "Loops").mkdir()
    (folder / "Kicks" / "kick-no-hint.wav").write_bytes(b"fake")       # one-shot, no filename bpm
    (folder / "Loops" / "loop-no-hint.wav").write_bytes(b"fake")       # loop, no filename bpm
    (folder / "Loops" / "loop-128bpm.wav").write_bytes(b"fake")        # loop, has filename bpm

    analyzed = []
    monkeypatch.setattr(
        library.audio_analysis, "detect_bpm_via_analysis",
        lambda path, cache=None: analyzed.append(path) or 99.0,
    )

    entry = library.show(library.add("pack", str(folder), analyze=True)["name"])
    by_ref = {f["ref"]: f for f in entry["files"]}

    # Only the loop lacking a filename hint should trigger real analysis.
    assert len(analyzed) == 1
    assert analyzed[0].endswith("loop-no-hint.wav")
    assert by_ref["pack/Loops/loop-no-hint.wav"] == {
        "ref": "pack/Loops/loop-no-hint.wav", "category": "loop", "bpm": 99.0, "bpm_source": "tempo_analysis",
        "kind": "loop", "kind_source": "filename",  # "loop" in the filename
    }
    assert "bpm" not in by_ref["pack/Kicks/kick-no-hint.wav"]
    assert by_ref["pack/Loops/loop-128bpm.wav"]["bpm_source"] == "filename"


def test_analyze_defaults_to_off(lib_config, tmp_path, monkeypatch):
    folder = tmp_path / "pack"
    (folder / "Loops").mkdir(parents=True)
    (folder / "Loops" / "loop-no-hint.wav").write_bytes(b"fake")

    monkeypatch.setattr(
        library.audio_analysis, "detect_bpm_via_analysis",
        lambda path, cache=None: pytest.fail("signal analysis should not run without --analyze"),
    )
    library.add("pack", str(folder))  # analyze=False by default


# --- search by BPM ------------------------------------------------------------

def test_search_by_bpm_within_tolerance(lib_config, tmp_path):
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "loop-128bpm.wav").write_bytes(b"fake")
    (folder / "loop-129bpm.wav").write_bytes(b"fake")
    (folder / "loop-140bpm.wav").write_bytes(b"fake")
    library.add("pack", str(folder))

    refs = sorted(m["ref"] for m in library.search(bpm=128.0)["matches"])
    assert refs == ["pack/loop-128bpm.wav", "pack/loop-129bpm.wav"]


# --- search ------------------------------------------------------------------

def test_search_by_query_and_category(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder), tags=["drums"])

    by_query = library.search("808")
    assert [m["ref"] for m in by_query["matches"]] == ["drum-pack/Kicks/kick-808.wav"]

    by_category = library.search(category="loop")
    assert [m["ref"] for m in by_category["matches"]] == ["drum-pack/Loops/dark-loop.wav"]


def test_search_scoped_to_one_library(lib_config, tmp_path, sample_folder):
    other = tmp_path / "other-pack"
    (other / "Kicks").mkdir(parents=True)
    (other / "Kicks" / "kick-deep.wav").write_bytes(b"fake")
    library.add("drum-pack", str(sample_folder))
    library.add("other-pack", str(other))

    scoped = library.search(category="kick", library="other-pack")
    assert [m["ref"] for m in scoped["matches"]] == ["other-pack/Kicks/kick-deep.wav"]


def test_search_filters_by_tag(lib_config, tmp_path, sample_folder):
    other = tmp_path / "other-pack"
    other.mkdir()
    (other / "perc.wav").write_bytes(b"fake")
    library.add("drum-pack", str(sample_folder), tags=["lofi"])
    library.add("other-pack", str(other), tags=["trap"])

    matches = library.search(tag="lofi")["matches"]
    assert all(m["ref"].startswith("drum-pack/") for m in matches)


# --- ref resolution ----------------------------------------------------------

def test_resolve_ref_returns_path_for_known_library(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder))
    resolved = library.resolve_ref("drum-pack/Kicks/kick-808.wav")
    assert resolved == (sample_folder / "Kicks" / "kick-808.wav").resolve()

    assert library.resolve_ref("drum-pack") == sample_folder.resolve()


def test_resolve_ref_returns_none_for_unknown_name(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder))
    assert library.resolve_ref("not-a-library/whatever.wav") is None


def test_resolve_ref_missing_file_raises(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder))
    with pytest.raises(NotFoundError):
        library.resolve_ref("drum-pack/Kicks/does-not-exist.wav")


def test_resolve_ref_rejects_escape(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder))
    with pytest.raises(BadInputError):
        library.resolve_ref("drum-pack/../../etc/passwd")


def test_resolve_path_arg_passes_through_plain_paths(lib_config, sample_folder):
    library.add("drum-pack", str(sample_folder))
    assert library.resolve_path_arg("/some/literal/path.wav") == "/some/literal/path.wav"
    assert library.resolve_path_arg(None) is None
