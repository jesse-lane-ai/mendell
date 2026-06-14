"""Tests for the project registry (`mendell.registry` + `mendell projects`)."""

from pathlib import Path

import pytest

from mendell import beat
from mendell import project as project_mod
from mendell import registry
from mendell.errors import BadInputError, NotFoundError


# --- automatic recording on creation ---------------------------------------

def test_create_records_project(tmp_path):
    project_mod.create(tmp_path, "song-a", bpm=90.0, key="A", scale="minor")

    projects = registry.list_projects()["projects"]
    assert len(projects) == 1
    row = projects[0]
    assert row["name"] == "song-a"
    assert row["path"] == str((tmp_path / "song-a").resolve())
    assert row["bpm"] == 90.0
    assert row["key"] == "A"
    assert row["scale"] == "minor"
    assert row["genre"] is None
    assert row["created"] is not None
    assert row["last_updated"] == row["created"]


def test_create_records_genre_when_supplied(tmp_path):
    project_mod.create(tmp_path, "song-g", genre="lofi")
    assert registry.show("song-g")["genre"] == "lofi"


def test_beat_new_records_style_as_genre(tmp_path):
    beat.new(tmp_path, "my-beat", style="dark")
    assert registry.show("my-beat")["genre"] == "dark"


# --- idempotent refresh -----------------------------------------------------

def test_record_is_idempotent_and_preserves_created(tmp_path):
    project_mod.create(tmp_path, "song-b", bpm=120.0)
    first = registry.show("song-b")

    # Change a project value, then re-sync: created stays, last_updated bumps,
    # metadata is re-read, and an existing genre is not wiped by a bare re-sync.
    registry.record((tmp_path / "song-b"), genre="trap")
    second = registry.record((tmp_path / "song-b"))

    assert second["created"] == first["created"]
    assert second["last_updated"] >= first["last_updated"]
    assert second["genre"] == "trap"  # preserved across a genreless re-sync
    assert len(registry.list_projects()["projects"]) == 1  # still one row


# --- lookups ----------------------------------------------------------------

def test_show_by_name_and_by_path(tmp_path):
    project_mod.create(tmp_path, "song-c")
    by_name = registry.show("song-c")
    by_path = registry.show(str(tmp_path / "song-c"))
    assert by_name == by_path


def test_show_missing_raises(tmp_path):
    with pytest.raises(NotFoundError):
        registry.show("nope")


def test_duplicate_names_require_path(tmp_path):
    project_mod.create(tmp_path / "a", "dupe")
    project_mod.create(tmp_path / "b", "dupe")

    with pytest.raises(BadInputError):
        registry.show("dupe")
    # ...but the full path disambiguates.
    assert registry.show(str(tmp_path / "a" / "dupe"))["path"] == str(
        (tmp_path / "a" / "dupe").resolve()
    )


def test_remove(tmp_path):
    project_mod.create(tmp_path, "song-d")
    assert registry.remove("song-d") == {
        "removed": "song-d",
        "path": str((tmp_path / "song-d").resolve()),
    }
    assert registry.list_projects()["projects"] == []


# --- robustness: a registry failure must not break creation -----------------

def test_create_survives_registry_failure(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(registry, "record", boom)

    project_dir = project_mod.create(tmp_path, "resilient")
    assert (project_dir / "project.toml").is_file()
    assert registry.list_projects()["projects"] == []  # nothing recorded
