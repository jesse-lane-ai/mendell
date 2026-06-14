"""Sample library — a small global registry of named external sample folders.

Unlike everything else in Mendell, the library lives outside any project (in
a user-level SQLite database) so it persists across projects and can be reused
from any of them. It only ever stores *references* (paths + lightweight
filename-derived metadata) — sample audio is still copied into a project's
``samples/`` directory at the point of use, exactly as it always has been.

This module is the single seam other commands (``kit load``, ``sampler map
add``, ``clip import``, ...) go through to turn a ``<library-name>/<relative
path>`` reference — or a bare registered name — into a real filesystem path.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import sampler as sampler_mod
from .clips import audio_analysis
from .errors import BadInputError, NotFoundError

AUDIO_EXTS = sampler_mod.AUDIO_EXTS

CONFIG_ENV_VAR = "MENDELL_LIBRARY_CONFIG"

# Filename-keyword -> category guess, reusing the same drum-name vocabulary
# `kit load` recognizes (first match wins; specific names before generic ones).
_CATEGORY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("kick", "bd", "bassdrum", "bass_drum", "bass-drum", "kik", "808"), "kick"),
    (("rim", "rimshot", "sidestick", "side_stick", "side-stick"), "rim"),
    (("snare", "sd", "snr"), "snare"),
    (("clap", "clp"), "clap"),
    (("closedhat", "closed_hat", "closed-hat", "hatclosed", "hat_closed", "chh"), "hat"),
    (("openhat", "open_hat", "open-hat", "hatopen", "hat_open", "ohh"), "hat"),
    (("hat", "hh", "hihat", "hi-hat", "hi_hat"), "hat"),
    (("tom",), "tom"),
    (("crash",), "crash"),
    (("ride",), "ride"),
    (("perc", "shaker", "tamb", "cowbell", "conga", "bongo", "clave"), "perc"),
    (("loop",), "loop"),
    (("bass",), "bass"),
    (("melody", "melodic", "lead", "synth", "pad", "chord", "keys"), "melody"),
]


def db_path() -> Path:
    """Path to the shared user-level Mendell SQLite DB.

    Holds the sample library *and* the project registry (see ``registry.py``);
    overridable via the ``MENDELL_LIBRARY_CONFIG`` env var, primarily for tests.
    """
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".config" / "mendell" / "library.db"


def _db_path() -> Path:
    return db_path()


@contextmanager
def _conn():
    path = _db_path()
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
        CREATE TABLE IF NOT EXISTS libraries (
            name         TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            tags         TEXT NOT NULL DEFAULT '',
            file_count   INTEGER NOT NULL DEFAULT 0,
            last_scanned REAL
        );
        CREATE TABLE IF NOT EXISTS files (
            id           INTEGER PRIMARY KEY,
            library_name TEXT NOT NULL REFERENCES libraries(name) ON DELETE CASCADE,
            rel_path     TEXT NOT NULL,
            category     TEXT NOT NULL,
            bpm          REAL,
            bpm_source   TEXT,
            UNIQUE(library_name, rel_path)
        );
        CREATE INDEX IF NOT EXISTS idx_files_library  ON files(library_name);
        CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
        CREATE INDEX IF NOT EXISTS idx_files_bpm      ON files(bpm);
    """)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _scan_folder(path: Path) -> list[Path]:
    return sorted(
        p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )


def _detect_bpm(file_path: Path, category: str, *, analyze: bool) -> tuple[float | None, str | None]:
    """BPM guess for a cached file: filename check always runs; full signal
    analysis (slow) is reserved for loop-categorized files when analyze=True."""
    bpm = audio_analysis.detect_bpm_from_filename(file_path.name)
    if bpm is not None:
        return bpm, "filename"
    if analyze and category == "loop":
        return audio_analysis.detect_bpm_via_analysis(str(file_path)), "tempo_analysis"
    return None, None


def _index_folder(folder: Path, *, analyze: bool = False) -> list[dict[str, Any]]:
    indexed = []
    for file_path in _scan_folder(folder):
        category = guess_category(file_path)
        bpm, bpm_source = _detect_bpm(file_path, category, analyze=analyze)
        entry: dict[str, Any] = {"path": str(file_path.relative_to(folder)), "category": category}
        if bpm is not None:
            entry["bpm"] = bpm
            entry["bpm_source"] = bpm_source
        indexed.append(entry)
    return indexed


def _require_library(con: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM libraries WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFoundError(f"library '{name}' not registered")
    return row


def _write_files(con: sqlite3.Connection, name: str, indexed: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM files WHERE library_name = ?", (name,))
    con.executemany(
        "INSERT INTO files (library_name, rel_path, category, bpm, bpm_source) VALUES (?,?,?,?,?)",
        [
            (name, f["path"], f["category"], f.get("bpm"), f.get("bpm_source"))
            for f in indexed
        ],
    )


def _row_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "path": row["path"],
        "tags": [t for t in row["tags"].split(",") if t],
        "file_count": row["file_count"],
        "last_scanned": row["last_scanned"],
    }


def _file_row_to_summary(library_name: str, row: sqlite3.Row) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ref": f"{library_name}/{row['rel_path']}",
        "category": row["category"],
    }
    if row["bpm"] is not None:
        summary["bpm"] = row["bpm"]
        summary["bpm_source"] = row["bpm_source"]
    return summary


def guess_category(file_path: Path) -> str:
    """Best-effort category guess from filename (then parent-folder) keywords."""
    for haystack in (file_path.stem.lower(), file_path.parent.name.lower()):
        for keywords, category in _CATEGORY_KEYWORDS:
            if any(kw in haystack for kw in keywords):
                return category
    return "one-shot"


# ---------------------------------------------------------------------------
# registry management
# ---------------------------------------------------------------------------

def add(name: str, path: str, *, tags: list[str] | None = None, analyze: bool = False) -> dict[str, Any]:
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise BadInputError(f"folder not found: {path}")

    indexed = _index_folder(folder, analyze=analyze)
    tag_str = ",".join(tags or [])
    now = time.time()

    with _conn() as con:
        con.execute(
            """
            INSERT INTO libraries (name, path, tags, file_count, last_scanned)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path         = excluded.path,
                tags         = CASE WHEN excluded.tags != '' THEN excluded.tags ELSE tags END,
                file_count   = excluded.file_count,
                last_scanned = excluded.last_scanned
            """,
            (name, str(folder), tag_str, len(indexed), now),
        )
        _write_files(con, name, indexed)
        row = con.execute("SELECT * FROM libraries WHERE name = ?", (name,)).fetchone()
        return _row_to_summary(row)


def remove(name: str) -> dict[str, Any]:
    with _conn() as con:
        _require_library(con, name)
        con.execute("DELETE FROM libraries WHERE name = ?", (name,))
    return {"removed": name}


def list_entries() -> dict[str, Any]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM libraries ORDER BY name").fetchall()
        return {"libraries": [_row_to_summary(r) for r in rows]}


def scan(name: str | None = None, *, analyze: bool = False) -> dict[str, Any]:
    with _conn() as con:
        if name is not None:
            targets = [_require_library(con, name)]
        else:
            targets = con.execute("SELECT * FROM libraries ORDER BY name").fetchall()
            if not targets:
                raise BadInputError("no libraries registered")

        scanned = []
        now = time.time()
        for row in targets:
            folder = Path(row["path"])
            if not folder.is_dir():
                raise BadInputError(
                    f"library '{row['name']}' folder no longer exists: {row['path']}"
                )
            indexed = _index_folder(folder, analyze=analyze)
            _write_files(con, row["name"], indexed)
            con.execute(
                "UPDATE libraries SET file_count = ?, last_scanned = ? WHERE name = ?",
                (len(indexed), now, row["name"]),
            )
            scanned.append({**_row_to_summary(row), "file_count": len(indexed), "last_scanned": now})

        return {"scanned": scanned}


def show(name: str) -> dict[str, Any]:
    with _conn() as con:
        row = _require_library(con, name)
        file_rows = con.execute(
            "SELECT * FROM files WHERE library_name = ? ORDER BY rel_path", (name,)
        ).fetchall()
        files = [_file_row_to_summary(name, r) for r in file_rows]
        return {**_row_to_summary(row), "files": files}


BPM_TOLERANCE = 2.0


def search(
    query: str | None = None, *,
    library: str | None = None,
    tag: str | None = None,
    category: str | None = None,
    bpm: float | None = None,
) -> dict[str, Any]:
    with _conn() as con:
        if library is not None:
            _require_library(con, library)

        clauses: list[str] = []
        params: list[Any] = []

        if library is not None:
            clauses.append("f.library_name = ?")
            params.append(library)

        if tag is not None:
            # tags stored as comma-separated string — match whole tag token
            clauses.append(
                "((',' || l.tags || ',') LIKE ?)"
            )
            params.append(f"%,{tag},%")

        if category is not None:
            clauses.append("f.category = ?")
            params.append(category)

        if bpm is not None:
            clauses.append("f.bpm IS NOT NULL AND ABS(f.bpm - ?) <= ?")
            params.extend([bpm, BPM_TOLERANCE])

        if query is not None:
            clauses.append("LOWER(f.rel_path) LIKE ?")
            params.append(f"%{query.lower()}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT f.library_name, f.rel_path, f.category, f.bpm, f.bpm_source,
                   l.tags
            FROM files f
            JOIN libraries l ON l.name = f.library_name
            {where}
            ORDER BY f.library_name, f.rel_path
        """
        rows = con.execute(sql, params).fetchall()

        matches = []
        for r in rows:
            m: dict[str, Any] = {
                "ref": f"{r['library_name']}/{r['rel_path']}",
                "category": r["category"],
                "tags": [t for t in r["tags"].split(",") if t],
            }
            if r["bpm"] is not None:
                m["bpm"] = r["bpm"]
                m["bpm_source"] = r["bpm_source"]
            matches.append(m)

        return {"query": query, "matches": matches, "count": len(matches)}


# ---------------------------------------------------------------------------
# reference resolution — the seam other commands hook into
# ---------------------------------------------------------------------------

def resolve_ref(ref: str) -> Path | None:
    """Resolve a ``<library-name>[/<relative-path>]`` reference to a real path.

    Returns ``None`` when ``ref`` doesn't name a registered library, so callers
    can fall back to treating it as a literal filesystem path.
    """
    name, _, rest = ref.partition("/")
    with _conn() as con:
        row = con.execute("SELECT path FROM libraries WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None

    folder = Path(row["path"])
    target = (folder / rest).resolve() if rest else folder
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        raise BadInputError(f"'{ref}' escapes library '{name}'")
    if not target.exists():
        raise NotFoundError(f"'{ref}' not found in library '{name}' ({row['path']})")
    return target


def resolve_path_arg(value: str | None) -> str | None:
    """Resolve ``value`` as a library reference if it names one; otherwise
    return it unchanged so plain filesystem paths keep working untouched."""
    if value is None:
        return None
    resolved = resolve_ref(value)
    return str(resolved) if resolved is not None else value
