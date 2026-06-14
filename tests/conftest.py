"""Shared test fixtures."""

import pytest

from mendell import library


@pytest.fixture(autouse=True)
def isolated_mendell_db(tmp_path, monkeypatch):
    """Point the shared Mendell SQLite DB (sample library + project registry)
    at an isolated per-test file so tests never read or write the real
    ``~/.config/mendell/library.db``.

    Creating a project now records a registry row, so this isolation applies to
    every test that touches ``project.create`` — not just the library tests.
    """
    monkeypatch.setenv(library.CONFIG_ENV_VAR, str(tmp_path / "mendell.db"))
