"""Tests for project resolution (`mendell.paths.resolve_project`)."""

import shutil

import pytest

from mendell import paths
from mendell import project as project_mod
from mendell.errors import BadInputError, NotFoundError


# --- filesystem resolution still wins, unchanged ----------------------------

def test_resolve_by_path(tmp_path):
    project_dir = project_mod.create(tmp_path, "song-a")
    assert paths.resolve_project(str(project_dir)) == project_dir.resolve()


def test_resolve_by_name_relative_to_cwd(tmp_path, monkeypatch):
    project_mod.create(tmp_path, "song-b")
    monkeypatch.chdir(tmp_path)
    assert paths.resolve_project("song-b") == (tmp_path / "song-b").resolve()


def test_resolve_from_inside_project_dir(tmp_path, monkeypatch):
    project_dir = project_mod.create(tmp_path, "song-c")
    monkeypatch.chdir(project_dir)
    assert paths.resolve_project("song-c") == project_dir.resolve()


# --- registry fallback -------------------------------------------------------

def test_resolve_bare_name_from_unrelated_cwd(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    project_dir = project_mod.create(projects_root, "song-d")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert paths.resolve_project("song-d") == project_dir.resolve()


def test_ambiguous_name_raises_bad_input(tmp_path, monkeypatch):
    project_mod.create(tmp_path / "a", "dupe")
    project_mod.create(tmp_path / "b", "dupe")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(BadInputError):
        paths.resolve_project("dupe")


def test_stale_registry_entry_raises_not_found_with_sync_hint(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    project_dir = project_mod.create(projects_root, "song-e")

    # Remove the project directory on disk but leave the registry row behind.
    shutil.rmtree(project_dir)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(NotFoundError) as exc_info:
        paths.resolve_project("song-e")

    message = str(exc_info.value)
    assert "song-e" in message
    assert "mendell projects remove" in message


def test_unknown_name_raises_not_found(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(NotFoundError):
        paths.resolve_project("nope")
