"""Shared test fixtures."""

import pytest

from mendell import config, library


@pytest.fixture(autouse=True)
def isolated_mendell_db(tmp_path, monkeypatch):
    """Isolate all user-level Mendell state per test so nothing touches the real
    ``~/.config/mendell`` or ``~/Documents/mendell``:

    * the shared SQLite DB (sample library + project registry),
    * config.json and its config directory,
    * the default projects folder.

    Creating a project records a registry row and config.json is read on every
    CLI run, so this isolation applies broadly — not just to the library tests.
    """
    monkeypatch.setenv(library.CONFIG_ENV_VAR, str(tmp_path / "mendell.db"))
    monkeypatch.setenv(config.CONFIG_DIR_ENV_VAR, str(tmp_path / "config"))
    monkeypatch.setenv(config.PROJECTS_FOLDER_ENV_VAR, str(tmp_path / "projects"))
