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
from typing import TYPE_CHECKING, Any

from . import sampler as sampler_mod
from .clips import audio_analysis
from .errors import BadInputError, NotFoundError

if TYPE_CHECKING:
    from .recognize import Recognition

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

# Categories that are inherently single hits — used as a weak `kind` fallback
# when neither the filename nor the duration/bar-alignment is conclusive.
_DRUM_CATEGORIES = {"kick", "snare", "clap", "hat", "tom", "crash", "ride", "rim", "perc"}

# Loop/one-shot ("kind") detection tuning.
ONESHOT_MAX_SEC = 1.2            # at/under this, treat as a one-shot hit
BAR_ALIGN_TOL = 0.06            # ±6% of a bar counts as "a whole number of bars"
_LOOP_BAR_COUNTS = (1, 2, 4, 8, 16)


DB_FILENAME = "library.db"


def _legacy_db_path() -> Path:
    """The pre-OS-dirs hard-coded location (always ``~/.config/mendell``)."""
    return Path.home() / ".config" / "mendell" / DB_FILENAME


def db_path() -> Path:
    """Path to the shared user-level Mendell SQLite DB.

    Holds the sample library *and* the project registry (see ``registry.py``).
    Lives in the OS-appropriate config directory (see ``config.config_dir``);
    overridable via the ``MENDELL_LIBRARY_CONFIG`` env var, primarily for tests.

    The first time the resolved location differs from the old hard-coded
    ``~/.config/mendell/library.db`` (i.e. on macOS/Windows, or Linux with a
    custom ``XDG_CONFIG_HOME``), an existing legacy DB is migrated across so
    users keep their library + registry.
    """
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)

    from . import config

    path = config.config_dir() / DB_FILENAME
    legacy = _legacy_db_path()
    if path != legacy and legacy.is_file() and not path.is_file():
        _migrate_legacy_db(legacy, path)
    return path


def _migrate_legacy_db(legacy: Path, dest: Path) -> None:
    """Move a legacy DB (and its WAL/SHM siblings) to ``dest``. Best-effort: if
    anything fails, a fresh DB is simply created at ``dest`` instead."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            src = legacy.with_name(legacy.name + suffix)
            if src.is_file():
                src.replace(dest.with_name(dest.name + suffix))
    except OSError:
        pass


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


def _add_column_if_missing(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotent ``ALTER TABLE ... ADD COLUMN`` for evolving an existing DB."""
    existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            kind         TEXT,
            kind_source  TEXT,
            duration     REAL,
            UNIQUE(library_name, rel_path)
        );
        CREATE INDEX IF NOT EXISTS idx_files_library  ON files(library_name);
        CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
        CREATE INDEX IF NOT EXISTS idx_files_bpm      ON files(bpm);
    """)
    # Added after the initial release — backfill onto pre-existing DBs (the
    # columns stay NULL on old rows until the folder is re-scanned).
    _add_column_if_missing(con, "files", "kind", "TEXT")
    _add_column_if_missing(con, "files", "kind_source", "TEXT")
    _add_column_if_missing(con, "files", "duration", "REAL")
    con.execute("CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind)")

    # Content-recognition columns (pluggable recognizer backends — see
    # recognize/). `instruments` is comma-joined, like `tags`.
    _add_column_if_missing(con, "files", "instruments", "TEXT")
    _add_column_if_missing(con, "files", "category_source", "TEXT")
    _add_column_if_missing(con, "files", "category_confidence", "REAL")
    # Free-text caption from a generative recognizer (e.g. ace-step); NULL for
    # backends that don't produce one, and for rows indexed before this column.
    _add_column_if_missing(con, "files", "caption", "TEXT")

    # Recognition cache, keyed by (library, rel_path, mtime) — lets re-scans
    # skip re-recognizing unchanged files (content backends can be slow/billed).
    con.executescript("""
        CREATE TABLE IF NOT EXISTS recognition_cache (
            library_name TEXT NOT NULL,
            rel_path     TEXT NOT NULL,
            mtime         REAL NOT NULL,
            backend       TEXT NOT NULL,
            category      TEXT NOT NULL,
            instruments   TEXT NOT NULL DEFAULT '',
            confidence    REAL NOT NULL,
            PRIMARY KEY (library_name, rel_path, backend)
        );
    """)
    _add_column_if_missing(con, "recognition_cache", "caption", "TEXT")


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


def _detect_kind(
    file_path: Path, category: str, bpm: float | None, duration: float | None
) -> tuple[str, str | None]:
    """Classify a file as ``loop`` / ``one-shot`` / ``unknown``, orthogonal to
    its instrument ``category``. Cheap-first, most-precise-wins:

      1. an explicit filename keyword (``...loop...`` / ``...oneshot/hit/...``);
      2. duration — at/under ``ONESHOT_MAX_SEC`` it's a hit;
      3. bar-alignment — a mid-length file whose duration is (within tolerance)
         a whole number of bars at its known tempo is a loop;
      4. the instrument category as a weak prior (drums → one-shot, ``loop`` →
         loop), else ``unknown`` — an honest "don't know" beats a wrong guess.
    """
    explicit = audio_analysis.detect_kind_from_filename(file_path.name)
    if explicit is not None:
        return explicit, "filename"

    if duration is not None:
        if duration <= ONESHOT_MAX_SEC:
            return "one-shot", "duration"
        if bpm:
            bar_seconds = 4.0 * 60.0 / bpm
            if bar_seconds > 0:
                bars = duration / bar_seconds
                nearest = min(_LOOP_BAR_COUNTS, key=lambda n: abs(n - bars))
                if abs(bars - nearest) <= BAR_ALIGN_TOL * nearest:
                    return "loop", "bar-align"

    if category in _DRUM_CATEGORIES:
        return "one-shot", "category"
    if category == "loop":
        return "loop", "category"
    return "unknown", None


# A recognizer's coarse category is only trusted on the filename-fallback
# path, and only at or above this confidence — below it we keep the filename
# default ("one-shot") rather than substitute a low-confidence guess.
RECOGNITION_CONFIDENCE_THRESHOLD = 0.5


def _fuse_category(
    filename_category: str, filename_source: str, recognition: Recognition | None
) -> tuple[str, str, float | None]:
    """Combine the filename keyword guess with a recognizer's verdict.

    Filename wins outright when it matched a real keyword (cheap + usually
    right). On the fallback path (filename guessed the default), use the
    recognizer's category if its confidence clears
    ``RECOGNITION_CONFIDENCE_THRESHOLD``; otherwise keep the filename default.

    Returns ``(category, category_source, category_confidence)`` —
    ``category_confidence`` is ``None`` when no recognizer ran or it deferred.
    """
    if filename_source == "filename":
        return filename_category, "filename", None
    if recognition is not None and recognition.confidence >= RECOGNITION_CONFIDENCE_THRESHOLD:
        return recognition.category, recognition.source, recognition.confidence
    return filename_category, "fallback", None


def _load_recognition_cache(con: sqlite3.Connection, library_name: str, backend: str) -> dict[str, sqlite3.Row]:
    """``rel_path -> cached row`` for ``backend``, keyed for an O(1) per-file lookup."""
    rows = con.execute(
        "SELECT * FROM recognition_cache WHERE library_name = ? AND backend = ?",
        (library_name, backend),
    ).fetchall()
    return {row["rel_path"]: row for row in rows}


def _write_recognition_cache(con: sqlite3.Connection, library_name: str, backend: str, entries: list[dict[str, Any]]) -> None:
    """Replace the cached recognition rows for ``backend`` with ``entries``
    (each carrying ``rel_path``, ``mtime``, ``category``, ``instruments``,
    ``confidence``). Other backends' cache rows are left untouched."""
    con.execute("DELETE FROM recognition_cache WHERE library_name = ? AND backend = ?", (library_name, backend))
    con.executemany(
        "INSERT INTO recognition_cache (library_name, rel_path, mtime, backend, category, instruments, confidence, caption) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (library_name, e["rel_path"], e["mtime"], backend, e["category"], ",".join(e["instruments"]), e["confidence"], e.get("caption"))
            for e in entries
        ],
    )


def _index_folder(
    folder: Path, *, analyze: bool = False, recognize: str | None = None,
    con: sqlite3.Connection | None = None, library_name: str | None = None,
) -> list[dict[str, Any]]:
    """Walk ``folder`` and build the per-file index.

    ``recognize``, when given, names a recognizer backend (see
    ``recognize.registry``) run on every file in a single batch — its
    ``category``/``instruments`` are fused with the filename guess (see
    ``_fuse_category``). Like ``analyze``, recognition loads audio; the two
    share one ``_AnalysisCache`` per file when ``recognize == "heuristic"``.

    ``con``/``library_name``, when both given, enable the recognition cache
    (``recognition_cache`` table) keyed by ``(library, rel_path, mtime)`` —
    unchanged files reuse their prior verdict instead of being re-recognized,
    so re-scans with a cloud backend don't re-bill for stable files.
    """
    files = _scan_folder(folder)

    # Filename-only pass first — cheap, always runs, and gives the recognizer
    # the `kind` it needs to pick the right label vocabulary.
    base: list[dict[str, Any]] = []
    caches: dict[str, audio_analysis._AnalysisCache] = {}
    for file_path in files:
        category, category_source = _guess_category_with_source(file_path)
        bpm, bpm_source = _detect_bpm(file_path, category, analyze=analyze)
        duration = audio_analysis.probe_duration_seconds(str(file_path))
        kind, kind_source = _detect_kind(file_path, category, bpm, duration)
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            mtime = None
        base.append({
            "file_path": file_path,
            "category": category,
            "category_source": category_source,
            "bpm": bpm,
            "bpm_source": bpm_source,
            "duration": duration,
            "kind": kind,
            "kind_source": kind_source,
            "mtime": mtime,
        })

    recognitions: list[Recognition | None] = [None] * len(files)
    if recognize is not None:
        from .recognize import FileProbe, Recognition as _Recognition, get_recognizer
        from .recognize.heuristic import HeuristicRecognizer

        cached_by_rel: dict[str, sqlite3.Row] = {}
        if con is not None and library_name is not None:
            cached_by_rel = _load_recognition_cache(con, library_name, recognize)

        # Only files whose cache entry is missing or stale (mtime changed)
        # actually need (re-)recognition.
        to_recognize_idx: list[int] = []
        for i, item in enumerate(base):
            rel = str(item["file_path"].relative_to(folder))
            cached = cached_by_rel.get(rel)
            if cached is not None and item["mtime"] is not None and cached["mtime"] == item["mtime"]:
                instruments = [t for t in cached["instruments"].split(",") if t]
                recognitions[i] = _Recognition(
                    category=cached["category"], instruments=instruments,
                    source=recognize, confidence=cached["confidence"],
                    caption=cached["caption"] if "caption" in cached.keys() else None,
                )
            else:
                to_recognize_idx.append(i)

        if to_recognize_idx:
            if recognize == "heuristic":
                recognizer = HeuristicRecognizer(cache_provider=lambda p: caches.setdefault(p, audio_analysis._AnalysisCache(p)))
            else:
                recognizer = get_recognizer(recognize)

            probes = [
                FileProbe(path=base[i]["file_path"], filename=base[i]["file_path"].name, duration=base[i]["duration"], kind=base[i]["kind"])
                for i in to_recognize_idx
            ]
            fresh = recognizer.recognize(probes)
            for i, recognition in zip(to_recognize_idx, fresh):
                recognitions[i] = recognition

        if con is not None and library_name is not None:
            cache_entries = []
            for item, recognition in zip(base, recognitions):
                if recognition is None or item["mtime"] is None:
                    continue
                rel = str(item["file_path"].relative_to(folder))
                cache_entries.append({
                    "rel_path": rel, "mtime": item["mtime"], "category": recognition.category,
                    "instruments": recognition.instruments, "confidence": recognition.confidence,
                    "caption": recognition.caption,
                })
            _write_recognition_cache(con, library_name, recognize, cache_entries)

    indexed = []
    for item, recognition in zip(base, recognitions):
        file_path = item["file_path"]
        if recognize is not None:
            category, category_source, category_confidence = _fuse_category(
                item["category"], item["category_source"], recognition
            )
        else:
            category, category_source, category_confidence = item["category"], None, None
        entry: dict[str, Any] = {
            "path": str(file_path.relative_to(folder)),
            "category": category,
            "kind": item["kind"],
        }
        if item["bpm"] is not None:
            entry["bpm"] = item["bpm"]
            entry["bpm_source"] = item["bpm_source"]
        if item["kind_source"] is not None:
            entry["kind_source"] = item["kind_source"]
        if item["duration"] is not None:
            entry["duration"] = item["duration"]
        if category_source is not None:
            entry["category_source"] = category_source
        if category_confidence is not None:
            entry["category_confidence"] = category_confidence
        if recognition is not None:
            entry["instruments"] = recognition.instruments
            if recognition.caption:
                entry["caption"] = recognition.caption
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
        "INSERT INTO files "
        "(library_name, rel_path, category, bpm, bpm_source, kind, kind_source, duration, "
        "instruments, category_source, category_confidence, caption) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                name, f["path"], f["category"], f.get("bpm"), f.get("bpm_source"),
                f.get("kind"), f.get("kind_source"), f.get("duration"),
                ",".join(f["instruments"]) if "instruments" in f else None,
                f.get("category_source"), f.get("category_confidence"), f.get("caption"),
            )
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


def _add_file_metadata(summary: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    """Attach the optional per-file metadata (bpm/kind/duration/instruments/...)
    to ``summary``, omitting whatever a row doesn't carry — shared by ``show``
    and ``search`` so a file looks the same however you reach it. Several
    fields may be absent on rows indexed before the corresponding feature,
    until the folder is re-scanned."""
    if row["bpm"] is not None:
        summary["bpm"] = row["bpm"]
        summary["bpm_source"] = row["bpm_source"]
    if row["kind"] is not None:
        summary["kind"] = row["kind"]
    if row["kind_source"] is not None:
        summary["kind_source"] = row["kind_source"]
    if row["duration"] is not None:
        summary["duration"] = round(row["duration"], 3)
    if row["instruments"] is not None and row["instruments"] != "":
        summary["instruments"] = [t for t in row["instruments"].split(",") if t]
    if row["category_source"] is not None:
        summary["category_source"] = row["category_source"]
    if row["category_confidence"] is not None:
        summary["category_confidence"] = row["category_confidence"]
    if "caption" in row.keys() and row["caption"]:
        summary["caption"] = row["caption"]
    return summary


def _file_row_to_summary(library_name: str, row: sqlite3.Row) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ref": f"{library_name}/{row['rel_path']}",
        "category": row["category"],
    }
    return _add_file_metadata(summary, row)


def _guess_category_with_source(file_path: Path) -> tuple[str, str]:
    """Like ``guess_category``, but also reports whether a real keyword
    matched (``"filename"``) or the default fell through (``"fallback"``) —
    drives the filename-vs-recognizer fusion in ``_index_folder``."""
    for haystack in (file_path.stem.lower(), file_path.parent.name.lower()):
        for keywords, category in _CATEGORY_KEYWORDS:
            if any(kw in haystack for kw in keywords):
                return category, "filename"
    return "one-shot", "fallback"


def guess_category(file_path: Path) -> str:
    """Best-effort category guess from filename (then parent-folder) keywords."""
    category, _source = _guess_category_with_source(file_path)
    return category


# ---------------------------------------------------------------------------
# registry management
# ---------------------------------------------------------------------------

def add(
    name: str, path: str, *, tags: list[str] | None = None, analyze: bool = False,
    recognize: str | None = None,
) -> dict[str, Any]:
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise BadInputError(f"folder not found: {path}")

    tag_str = ",".join(tags or [])
    now = time.time()

    with _conn() as con:
        indexed = _index_folder(folder, analyze=analyze, recognize=recognize, con=con, library_name=name)
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


def scan(name: str | None = None, *, analyze: bool = False, recognize: str | None = None) -> dict[str, Any]:
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
            indexed = _index_folder(folder, analyze=analyze, recognize=recognize, con=con, library_name=row["name"])
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
    kind: str | None = None,
    instrument: str | None = None,
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

        if kind is not None:
            clauses.append("f.kind = ?")
            params.append(kind)

        if instrument is not None:
            # instruments stored as comma-separated string — match whole token
            clauses.append(
                "((',' || f.instruments || ',') LIKE ?)"
            )
            params.append(f"%,{instrument},%")

        if bpm is not None:
            clauses.append("f.bpm IS NOT NULL AND ABS(f.bpm - ?) <= ?")
            params.extend([bpm, BPM_TOLERANCE])

        if query is not None:
            clauses.append("LOWER(f.rel_path) LIKE ?")
            params.append(f"%{query.lower()}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT f.library_name, f.rel_path, f.category, f.bpm, f.bpm_source,
                   f.kind, f.kind_source, f.duration, f.instruments,
                   f.category_source, f.category_confidence, l.tags
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
            _add_file_metadata(m, r)
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
