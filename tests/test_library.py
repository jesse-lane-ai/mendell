"""Unit tests for the sample library registry (`mendell.library`)."""

from pathlib import Path

import pytest

from mendell import library
from mendell.errors import BadInputError, NotFoundError


@pytest.fixture
def lib_config(tmp_path, monkeypatch):
    """Point the library at an isolated config file for each test."""
    config_path = tmp_path / "library.toml"
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
