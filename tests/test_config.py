"""Tests for global config (`mendell.config`) and the OS-aware paths it drives."""

import json
from pathlib import Path

import pytest

from mendell import config, library
from mendell import project as project_mod


# --- config dir / config.json materialization ------------------------------

def test_config_dir_honors_override(tmp_path):
    # conftest points MENDELL_CONFIG_DIR at tmp_path/config.
    assert config.config_dir() == tmp_path / "config"
    assert config.config_path() == tmp_path / "config" / "config.json"


def test_load_materializes_config_with_defaults():
    assert not config.config_path().exists()
    data = config.load()
    assert config.config_path().is_file()
    assert "projects_folder" in data
    # Round-trips as valid JSON on disk.
    on_disk = json.loads(config.config_path().read_text())
    assert on_disk == data


def test_set_and_get_round_trip():
    config.set_value("projects_folder", "/tmp/some-beats")
    assert config.get("projects_folder") == "/tmp/some-beats"
    assert json.loads(config.config_path().read_text())["projects_folder"] == "/tmp/some-beats"


# --- projects folder resolution --------------------------------------------

def test_projects_folder_env_override_creates_dir(tmp_path):
    # conftest sets MENDELL_PROJECTS_FOLDER to tmp_path/projects.
    folder = config.projects_folder()
    assert folder == (tmp_path / "projects").resolve()
    assert folder.is_dir()


def test_projects_folder_falls_back_to_os_default(tmp_path, monkeypatch):
    monkeypatch.delenv(config.PROJECTS_FOLDER_ENV_VAR, raising=False)
    fake_default = tmp_path / "Documents" / "mendell"
    monkeypatch.setattr(config, "default_projects_folder", lambda: fake_default)

    folder = config.projects_folder()
    assert folder == fake_default.resolve()
    assert folder.is_dir()
    # The resolved default is written back into config.json.
    assert config.get("projects_folder") == str(fake_default)


def test_projects_folder_uses_config_value_when_set(tmp_path, monkeypatch):
    monkeypatch.delenv(config.PROJECTS_FOLDER_ENV_VAR, raising=False)
    target = tmp_path / "my-beats"
    config.set_value("projects_folder", str(target))

    assert config.projects_folder() == target.resolve()
    assert target.is_dir()


# --- new-argument parent resolution ----------------------------------------

def test_resolve_bare_name_goes_to_projects_folder(tmp_path):
    parent, base = config.resolve_project_parent("song-x")
    assert parent == (tmp_path / "projects").resolve()
    assert base == "song-x"


def test_resolve_relative_path_is_literal():
    parent, base = config.resolve_project_parent("sub/song-y")
    assert parent == Path.cwd() / "sub"
    assert base == "song-y"


def test_resolve_absolute_path_is_literal(tmp_path):
    abs_target = tmp_path / "elsewhere" / "song-z"
    parent, base = config.resolve_project_parent(str(abs_target))
    assert parent == abs_target.parent
    assert base == "song-z"


# --- DB path now lives in the config dir -----------------------------------

def test_db_path_lives_in_config_dir(tmp_path, monkeypatch):
    # Drop the direct DB override so resolution flows through config_dir.
    monkeypatch.delenv(library.CONFIG_ENV_VAR, raising=False)
    assert library.db_path() == tmp_path / "config" / "library.db"


def test_legacy_db_is_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv(library.CONFIG_ENV_VAR, raising=False)
    # A pre-existing DB at the old hard-coded location...
    legacy = tmp_path / "legacy" / "library.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old-db")
    monkeypatch.setattr(library, "_legacy_db_path", lambda: legacy)

    resolved = library.db_path()  # config dir != legacy -> migrate

    assert resolved == tmp_path / "config" / "library.db"
    assert resolved.read_bytes() == b"old-db"
    assert not legacy.exists()


# --- end-to-end: creating a project still records cleanly ------------------

def test_create_under_resolved_parent_records(tmp_path):
    parent, base = config.resolve_project_parent("e2e-song")
    project_dir = project_mod.create(parent, base, bpm=100.0)
    assert (project_dir / "project.toml").is_file()
    assert project_dir.parent == (tmp_path / "projects").resolve()
