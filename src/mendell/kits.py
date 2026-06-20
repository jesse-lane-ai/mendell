"""Named, reusable drum kit registry — stored in the shared ``library.db``.

A "kit" is a collection of drum one-shots mapped to General MIDI percussion
notes.  Unlike ``mendell kit load`` (which maps a folder directly into a
project), kits here are *saved, named objects* in the global library that can
be assembled once and applied across any number of projects.

Tables
------
``kits``       — one row per named kit (PK on name)
``kit_slots``  — one row per (kit, GM note) pair, holding the source path

Public API
----------
create(name)                          — create-or-reuse a kit
add_slot(kit, gm_note_or_category, path)  — add/replace a slot
list_kits()                           — all kits
show(name)                            — one kit with its slots
remove(name)                          — delete a kit and its slots
apply_to_project(kit, project, track) — create sampler track + map slots
quick_kit(name, *, library=None, seed=None) — auto-assemble from library
"""

from __future__ import annotations

import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import library as library_mod
from . import sampler as sampler_mod
from .errors import BadInputError, NotFoundError
from .kit import _GM_DRUM_KEYWORDS, _ensure_sampler_track, _guess_note
from .notes import midi_to_note_name, note_name_to_midi


# ---------------------------------------------------------------------------
# DB connection + migration
# ---------------------------------------------------------------------------

@contextmanager
def _conn():
    path = library_mod.db_path()
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
        CREATE TABLE IF NOT EXISTS kits (
            name         TEXT PRIMARY KEY,
            description  TEXT NOT NULL DEFAULT '',
            created      REAL NOT NULL,
            last_updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kit_slots (
            kit_name    TEXT NOT NULL REFERENCES kits(name) ON DELETE CASCADE,
            gm_note     INTEGER NOT NULL,
            category    TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL,
            slot_name   TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (kit_name, gm_note)
        );
        CREATE INDEX IF NOT EXISTS idx_kit_slots_kit ON kit_slots(kit_name);
    """)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

# Core drum categories picked for quick_kit — one file per category
_QUICK_KIT_CATEGORIES = ["kick", "snare", "hat", "clap", "tom", "crash", "rim"]


def _row_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "description": row["description"],
        "created": row["created"],
        "last_updated": row["last_updated"],
    }


def _slot_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "gm_note": row["gm_note"],
        "note_name": midi_to_note_name(row["gm_note"]),
        "category": row["category"],
        "source_path": row["source_path"],
        "name": row["slot_name"],
    }


def _require_kit(con: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM kits WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFoundError(f"kit '{name}' not found")
    return row


def _category_for_note(gm_note: int) -> str:
    """Reverse-map a GM note to a rough category name for display."""
    for keywords, note in _GM_DRUM_KEYWORDS:
        if note == gm_note:
            return keywords[0]
    return "perc"


def _resolve_note_arg(gm_note_or_category: str | int) -> int:
    """Accept a GM MIDI note number (int or numeric string) OR a category/note
    name like 'kick', 'C3', 'snare', etc. — return the integer MIDI note."""
    # int passed directly
    if isinstance(gm_note_or_category, int):
        return gm_note_or_category

    s = str(gm_note_or_category).strip()

    # numeric string
    if s.isdigit():
        return int(s)

    # category keyword (e.g. "kick", "snare", "hat")
    note = _guess_note(s)
    if note is not None:
        return note

    # note name (e.g. "C3", "A#4")
    try:
        return note_name_to_midi(s)
    except Exception:
        pass

    raise BadInputError(
        f"cannot resolve '{s}' to a GM note — use a MIDI number (36), "
        "a drum category (kick/snare/hat/clap/tom/crash/rim/perc), or a note name (C3)"
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def create(name: str, *, description: str = "") -> dict[str, Any]:
    """Create a named kit, or return the existing one (idempotent)."""
    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO kits (name, description, created, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description  = CASE WHEN excluded.description != '' THEN excluded.description ELSE description END,
                last_updated = excluded.last_updated
            """,
            (name, description, now, now),
        )
        row = con.execute("SELECT * FROM kits WHERE name = ?", (name,)).fetchone()
        return _row_to_summary(row)


def add_slot(
    kit_name: str,
    gm_note_or_category: str | int,
    path: str,
    *,
    slot_name: str = "",
) -> dict[str, Any]:
    """Add (or replace) a slot in the kit.

    ``gm_note_or_category`` accepts a MIDI note number, a drum category keyword
    (kick/snare/hat/clap/tom/crash/rim/perc), or a note name (C3).
    ``path`` is the source audio file path (may be a library ref that gets
    resolved, or a plain filesystem path).
    """
    gm_note = _resolve_note_arg(gm_note_or_category)
    if not (0 <= gm_note <= 127):
        raise BadInputError(f"GM note {gm_note} out of range 0–127")

    # Resolve library refs, but accept plain paths too
    resolved = library_mod.resolve_path_arg(path)
    if resolved is None:
        resolved = path
    source_path = str(Path(resolved).expanduser().resolve())
    if not Path(source_path).exists():
        raise BadInputError(f"audio file not found: {source_path}")

    category = _category_for_note(gm_note)
    if not slot_name:
        slot_name = Path(source_path).stem

    now = time.time()
    with _conn() as con:
        _require_kit(con, kit_name)
        con.execute(
            """
            INSERT INTO kit_slots (kit_name, gm_note, category, source_path, slot_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(kit_name, gm_note) DO UPDATE SET
                category    = excluded.category,
                source_path = excluded.source_path,
                slot_name   = excluded.slot_name
            """,
            (kit_name, gm_note, category, source_path, slot_name),
        )
        con.execute(
            "UPDATE kits SET last_updated = ? WHERE name = ?", (now, kit_name)
        )
    return {
        "kit": kit_name,
        "gm_note": gm_note,
        "note_name": midi_to_note_name(gm_note),
        "category": category,
        "source_path": source_path,
        "name": slot_name,
    }


def list_kits() -> dict[str, Any]:
    """Return all kits (without slots)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM kits ORDER BY name"
        ).fetchall()
        return {"kits": [_row_to_summary(r) for r in rows]}


def show(name: str) -> dict[str, Any]:
    """Return a kit with all its slots."""
    with _conn() as con:
        kit_row = _require_kit(con, name)
        slot_rows = con.execute(
            "SELECT * FROM kit_slots WHERE kit_name = ? ORDER BY gm_note",
            (name,),
        ).fetchall()
        return {
            **_row_to_summary(kit_row),
            "slots": [_slot_row_to_dict(r) for r in slot_rows],
            "slot_count": len(slot_rows),
        }


def remove(name: str) -> dict[str, Any]:
    """Delete a kit and all its slots."""
    with _conn() as con:
        _require_kit(con, name)
        con.execute("DELETE FROM kits WHERE name = ?", (name,))
    return {"removed": name}


def remove_slot(kit_name: str, gm_note_or_category: str | int) -> dict[str, Any]:
    """Clear a single slot (one GM note) from a kit. Idempotent."""
    gm_note = _resolve_note_arg(gm_note_or_category)
    now = time.time()
    with _conn() as con:
        _require_kit(con, kit_name)
        con.execute(
            "DELETE FROM kit_slots WHERE kit_name = ? AND gm_note = ?",
            (kit_name, gm_note),
        )
        con.execute("UPDATE kits SET last_updated = ? WHERE name = ?", (now, kit_name))
    return {"kit": kit_name, "gm_note": gm_note, "removed": True}


def slot_source_path(kit_name: str, gm_note_or_category: str | int) -> Path | None:
    """Return the filesystem path backing a kit slot, or ``None`` if unset."""
    gm_note = _resolve_note_arg(gm_note_or_category)
    with _conn() as con:
        _require_kit(con, kit_name)
        row = con.execute(
            "SELECT source_path FROM kit_slots WHERE kit_name = ? AND gm_note = ?",
            (kit_name, gm_note),
        ).fetchone()
    if row is None:
        return None
    p = Path(row["source_path"])
    return p if p.exists() else None


def random_slot(
    kit_name: str,
    gm_note_or_category: str | int,
    *,
    library: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Assign a random library one-shot to a single kit slot.

    Prefers a sample whose category matches the pad's GM note (kick/snare/…),
    falling back to any one-shot if the category has no matches. Idempotent on
    the kit row itself (creates it if missing).
    """
    rng = random.Random(seed)
    create(kit_name)  # ensure kit exists
    gm_note = _resolve_note_arg(gm_note_or_category)
    category = _category_for_note(gm_note)

    def _matches(**kw) -> list[dict]:
        return library_mod.search(library=library, **kw).get("matches", [])

    candidates = _matches(category=category, kind="one-shot")
    if not candidates:
        candidates = [
            m for m in _matches(category=category)
            if m.get("kind") in ("one-shot", None, "unknown")
        ]
    if not candidates:
        candidates = _matches(kind="one-shot")  # any one-shot
    if not candidates:
        raise BadInputError(
            f"no samples available to fill note {gm_note} ({category}) — "
            "register a sample library first with `mendell library add`"
        )

    # Try a few picks in case a ref fails to resolve.
    rng.shuffle(candidates)
    for chosen in candidates:
        path = library_mod.resolve_ref(chosen["ref"])
        if path is not None and path.exists():
            return add_slot(kit_name, gm_note, str(path), slot_name=Path(str(path)).stem)

    raise BadInputError(
        f"found {len(candidates)} candidate samples for note {gm_note} "
        f"({category}) but none resolved to a readable file"
    )


def apply_to_project(
    kit_name: str,
    project_dir: Path,
    track_name: str,
) -> dict[str, Any]:
    """Create (or reuse) a sampler track in *project_dir* and map all kit
    slots onto it.  Idempotent — safe to call multiple times.

    Returns a summary identical in shape to ``kit load``'s output.
    """
    with _conn() as con:
        _require_kit(con, kit_name)
        slot_rows = con.execute(
            "SELECT * FROM kit_slots WHERE kit_name = ? ORDER BY gm_note",
            (kit_name,),
        ).fetchall()

    if not slot_rows:
        raise BadInputError(f"kit '{kit_name}' has no slots — add some first with `mendell kits add`")

    _ensure_sampler_track(project_dir, track_name)

    mapped = []
    for slot in slot_rows:
        note_name = midi_to_note_name(slot["gm_note"])
        sampler_mod.map_add(
            project_dir, track_name,
            note=note_name,
            sample=slot["source_path"],
        )
        mapped.append({"note": note_name, "sample": Path(slot["source_path"]).name})

    return {
        "kit": kit_name,
        "track": track_name,
        "project": str(project_dir),
        "mapped": mapped,
        "count": len(mapped),
    }


def quick_kit(
    name: str,
    *,
    library: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Assemble a kit by randomly picking one one-shot per drum category from
    the sample library.

    ``library`` restricts the search to one registered library (default: all).
    ``seed`` makes selection deterministic (useful for tests / reproducibility).

    Returns the result of :func:`show` after building the kit.
    """
    rng = random.Random(seed)

    # Ensure the kit row exists first
    create(name)

    assigned_categories: list[str] = []
    missing_categories: list[str] = []

    for category in _QUICK_KIT_CATEGORIES:
        result = library_mod.search(
            category=category,
            kind="one-shot",
            library=library,
        )
        candidates = result.get("matches", [])

        if not candidates:
            # Retry without the kind filter — some DBs may not have kind indexed
            result2 = library_mod.search(category=category, library=library)
            candidates = [
                m for m in result2.get("matches", [])
                if m.get("kind") in ("one-shot", None, "unknown")
            ]

        if not candidates:
            missing_categories.append(category)
            continue

        chosen = rng.choice(candidates)
        ref = chosen["ref"]

        # Resolve to a real path
        path = library_mod.resolve_ref(ref)
        if path is None or not path.exists():
            missing_categories.append(category)
            continue

        gm_note = _guess_note(category) or _guess_note(Path(str(path)).stem)
        if gm_note is None:
            # Fall back to the first GM keyword match for this category
            for keywords, note in _GM_DRUM_KEYWORDS:
                if category in keywords or any(category == kw for kw in keywords):
                    gm_note = note
                    break
        if gm_note is None:
            missing_categories.append(category)
            continue

        add_slot(name, gm_note, str(path), slot_name=Path(str(path)).stem)
        assigned_categories.append(category)

    kit_data = show(name)
    kit_data["assigned_categories"] = assigned_categories
    if missing_categories:
        kit_data["missing_categories"] = missing_categories
    return kit_data
