"""Project registry — a global table of every Mendell project and its metadata.

Like the sample library, this lives in the shared user-level SQLite DB
(``~/.config/mendell/library.db``, overridable via ``MENDELL_LIBRARY_CONFIG``)
so it spans projects and can be queried from anywhere. It is a *secondary
index*: each project's ``project.toml`` on disk remains the source of truth.

A row is recorded automatically whenever a project is created (every creation
path funnels through ``project.create``), and can be refreshed at any time with
``mendell projects sync``. Rows are keyed by the project's absolute directory
path, so two projects that happen to share a ``name`` never collide.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import library, paths
from .errors import BadInputError, NotFoundError
from .toml_io import read_toml


@contextmanager
def _conn():
    path = library.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    _migrate(con)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            path         TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            genre        TEXT,
            key          TEXT,
            scale        TEXT,
            bpm          REAL,
            time_sig     TEXT,
            sample_rate  INTEGER,
            created      REAL NOT NULL,
            last_updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name);
    """)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _meta_from_toml(project_dir: Path) -> dict[str, Any]:
    """Pull the registry's metadata columns straight from project.toml.

    Reads the TOML directly (rather than via the ``project`` module) to avoid
    an import cycle — ``project.create`` is what calls into here.
    """
    data = read_toml(paths.project_toml(project_dir))
    proj = data.get("project", {})
    return {
        "name": proj.get("name") or project_dir.name,
        "key": proj.get("key"),
        "scale": proj.get("scale"),
        "bpm": proj.get("bpm"),
        "time_sig": proj.get("time_sig"),
        "sample_rate": proj.get("sample_rate"),
    }


def _row_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "path": row["path"],
        "genre": row["genre"],
        "key": row["key"],
        "scale": row["scale"],
        "bpm": row["bpm"],
        "time_sig": row["time_sig"],
        "sample_rate": row["sample_rate"],
        "created": row["created"],
        "last_updated": row["last_updated"],
    }


def _find_rows(con: sqlite3.Connection, identifier: str) -> list[sqlite3.Row]:
    """Match a project by exact directory path first, then by name."""
    resolved = str(Path(identifier).expanduser().resolve())
    rows = con.execute("SELECT * FROM projects WHERE path = ?", (resolved,)).fetchall()
    if rows:
        return rows
    return con.execute(
        "SELECT * FROM projects WHERE name = ? ORDER BY path", (identifier,)
    ).fetchall()


def _require_one(con: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    rows = _find_rows(con, identifier)
    if not rows:
        raise NotFoundError(f"project '{identifier}' not in registry")
    if len(rows) > 1:
        paths_list = ", ".join(r["path"] for r in rows)
        raise BadInputError(
            f"'{identifier}' matches multiple projects ({paths_list}); "
            "use the full path to disambiguate"
        )
    return rows[0]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def record(project_dir: Path, *, genre: str | None = None) -> dict[str, Any]:
    """Insert or refresh the registry row for ``project_dir`` (idempotent).

    On first record, ``created`` and ``last_updated`` are set to now. On a
    refresh, ``created`` is preserved, ``last_updated`` is bumped, metadata is
    re-read from project.toml, and ``genre`` is only overwritten when a new
    non-empty value is supplied (so a plain re-sync never wipes it).
    """
    project_dir = Path(project_dir).expanduser().resolve()
    meta = _meta_from_toml(project_dir)
    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO projects
                (path, name, genre, key, scale, bpm, time_sig, sample_rate,
                 created, last_updated)
            VALUES
                (:path, :name, :genre, :key, :scale, :bpm, :time_sig,
                 :sample_rate, :now, :now)
            ON CONFLICT(path) DO UPDATE SET
                name         = excluded.name,
                genre        = COALESCE(excluded.genre, genre),
                key          = excluded.key,
                scale        = excluded.scale,
                bpm          = excluded.bpm,
                time_sig     = excluded.time_sig,
                sample_rate  = excluded.sample_rate,
                last_updated = excluded.last_updated
            """,
            {"path": str(project_dir), "genre": genre, "now": now, **meta},
        )
        row = con.execute(
            "SELECT * FROM projects WHERE path = ?", (str(project_dir),)
        ).fetchone()
        return _row_to_summary(row)


def record_safe(project_dir: Path, *, genre: str | None = None) -> dict[str, Any] | None:
    """Best-effort :func:`record` for the project-creation hook.

    The registry is a convenience index; a failure to write it must never break
    project creation (project.toml on disk is already the source of truth), so
    any error here is swallowed.
    """
    try:
        return record(project_dir, genre=genre)
    except Exception:
        return None


def list_projects() -> dict[str, Any]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM projects ORDER BY last_updated DESC, name"
        ).fetchall()
        return {"projects": [_row_to_summary(r) for r in rows]}


def show(identifier: str) -> dict[str, Any]:
    with _conn() as con:
        return _row_to_summary(_require_one(con, identifier))


def remove(identifier: str) -> dict[str, Any]:
    with _conn() as con:
        row = _require_one(con, identifier)
        con.execute("DELETE FROM projects WHERE path = ?", (row["path"],))
        return {"removed": row["name"], "path": row["path"]}
