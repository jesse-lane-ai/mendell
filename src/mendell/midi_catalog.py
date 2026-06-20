"""Global MIDI clip catalog — lives in library.db, spans projects.

Stores reusable MIDI patterns/clips: generated drum patterns, imported .mid
files, and hand-authored note grids from the visual editor.  The catalog is a
*library* (not project-scoped) resource, so a pattern recorded once is
reusable in any project via `mendell clip import --midi <stored-path>`.

Table: midi_clips
  name         TEXT PRIMARY KEY
  path         TEXT NOT NULL   — absolute path to the stored .mid file
  bars         INTEGER         — length in 4/4 bars (derived from MIDI)
  bpm          REAL            — reference BPM (generation BPM or user-supplied)
  category     TEXT            — drums / bass / melody / chords / perc / other
  note_count   INTEGER         — total note events in the file
  created      REAL
  last_updated REAL
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import mido

from . import library
from .errors import BadInputError, NotFoundError
from .midi_gen import PATTERN_PPQ, BEATS_PER_BAR, DRUM_PATTERNS, write_pattern_midi

_VALID_CATEGORIES = {"drums", "bass", "melody", "chords", "perc", "other"}

# Directory inside the config folder where catalog .mid files are stored.
_MIDI_DIR_NAME = "midi_catalog"


def _midi_dir() -> Path:
    path = library.db_path().parent / _MIDI_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        CREATE TABLE IF NOT EXISTS midi_clips (
            name         TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            bars         INTEGER,
            bpm          REAL,
            category     TEXT NOT NULL DEFAULT 'other',
            note_count   INTEGER NOT NULL DEFAULT 0,
            created      REAL NOT NULL,
            last_updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_midi_clips_category ON midi_clips(category);
    """)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _count_notes(mid: mido.MidiFile) -> int:
    """Total note_on (velocity > 0) events across all tracks."""
    count = 0
    for track in mid.tracks:
        for msg in track:
            if msg.type == "note_on" and msg.velocity > 0:
                count += 1
    return count


def _count_bars(mid: mido.MidiFile) -> int:
    """Approximate bar count from total tick length (assumes 4/4)."""
    ppq = mid.ticks_per_beat or PATTERN_PPQ
    total_ticks = 0
    for track in mid.tracks:
        t = sum(msg.time for msg in track)
        total_ticks = max(total_ticks, t)
    bar_ticks = ppq * BEATS_PER_BAR
    bars = max(1, round(total_ticks / bar_ticks))
    return bars


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "path": row["path"],
        "bars": row["bars"],
        "bpm": row["bpm"],
        "category": row["category"],
        "note_count": row["note_count"],
        "created": row["created"],
        "last_updated": row["last_updated"],
    }


def _require(con: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM midi_clips WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFoundError(f"MIDI clip '{name}' not in catalog")
    return row


def _validate_category(category: str) -> str:
    cat = category.lower().strip()
    if cat not in _VALID_CATEGORIES:
        raise BadInputError(
            f"unknown category '{category}' (valid: {', '.join(sorted(_VALID_CATEGORIES))})"
        )
    return cat


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def import_file(path: str, *, name: str, category: str = "other") -> dict[str, Any]:
    """Copy a .mid file into the catalog store and register it.

    Idempotent: if a clip with this name already exists, it is overwritten.
    """
    category = _validate_category(category)
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise BadInputError(f"MIDI file not found: {path}")
    if src.suffix.lower() not in (".mid", ".midi"):
        raise BadInputError(f"not a .mid file: {path}")

    try:
        mid = mido.MidiFile(str(src))
    except Exception as exc:
        raise BadInputError(f"could not parse MIDI file '{path}': {exc}")

    note_count = _count_notes(mid)
    bars = _count_bars(mid)

    dest = _midi_dir() / f"{name}.mid"
    shutil.copy2(src, dest)

    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO midi_clips (name, path, bars, bpm, category, note_count, created, last_updated)
            VALUES (:name, :path, :bars, :bpm, :category, :note_count, :now, :now)
            ON CONFLICT(name) DO UPDATE SET
                path         = excluded.path,
                bars         = excluded.bars,
                category     = excluded.category,
                note_count   = excluded.note_count,
                last_updated = excluded.last_updated
            """,
            {"name": name, "path": str(dest), "bars": bars, "bpm": None,
             "category": category, "note_count": note_count, "now": now},
        )
        row = con.execute("SELECT * FROM midi_clips WHERE name = ?", (name,)).fetchone()
        return _row_to_dict(row)


def generate(
    name: str,
    *,
    style: str,
    bars: int = 1,
    category: str = "drums",
    bpm: float | None = None,
) -> dict[str, Any]:
    """Generate a drum pattern and register it in the catalog.

    Wraps ``midi_gen.write_pattern_midi`` — uses the same style presets as
    ``mendell midi generate`` and ``beat new``.
    """
    category = _validate_category(category)
    style = style.lower()
    if style not in DRUM_PATTERNS:
        styles = ", ".join(sorted(DRUM_PATTERNS))
        raise BadInputError(f"unknown style '{style}' (valid: {styles})")
    bars = max(1, int(bars))

    dest = _midi_dir() / f"{name}.mid"
    write_pattern_midi(dest, DRUM_PATTERNS[style], bars=bars)

    mid = mido.MidiFile(str(dest))
    note_count = _count_notes(mid)

    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO midi_clips (name, path, bars, bpm, category, note_count, created, last_updated)
            VALUES (:name, :path, :bars, :bpm, :category, :note_count, :now, :now)
            ON CONFLICT(name) DO UPDATE SET
                path         = excluded.path,
                bars         = excluded.bars,
                bpm          = excluded.bpm,
                category     = excluded.category,
                note_count   = excluded.note_count,
                last_updated = excluded.last_updated
            """,
            {"name": name, "path": str(dest), "bars": bars, "bpm": bpm,
             "category": category, "note_count": note_count, "now": now},
        )
        row = con.execute("SELECT * FROM midi_clips WHERE name = ?", (name,)).fetchone()
        return {**_row_to_dict(row), "style": style}


def create_from_notes(
    name: str,
    notes: list[dict[str, Any]],
    *,
    bpm: float = 120.0,
    bars: int = 1,
    category: str = "other",
) -> dict[str, Any]:
    """Build a .mid from a note grid and register it.

    ``notes`` is a list of dicts: ``{pitch, start_beat, length_beats, velocity}``.
    ``start_beat`` and ``length_beats`` are floats in beat units (0-indexed,
    where 4 beats = 1 bar in 4/4).  Pitch is a MIDI note number (0-127).
    velocity is 1-127.

    This is the backing API for the visual step-grid editor.
    """
    category = _validate_category(category)
    bpm = max(20.0, float(bpm))
    bars = max(1, int(bars))
    ppq = PATTERN_PPQ

    if not isinstance(notes, list):
        raise BadInputError("'notes' must be a list")
    if not notes:
        raise BadInputError("notes list is empty — provide at least one note")

    # Build mido events
    events: list[tuple[int, int, int, int]] = []  # (abs_tick, kind, note, velocity)
    for i, n in enumerate(notes):
        try:
            pitch = int(n["pitch"])
            start_beat = float(n["start_beat"])
            length_beats = float(n.get("length_beats", 0.25))
            velocity = int(n.get("velocity", 100))
        except (KeyError, TypeError, ValueError) as exc:
            raise BadInputError(f"note[{i}] is malformed: {exc}")

        if not (0 <= pitch <= 127):
            raise BadInputError(f"note[{i}] pitch {pitch} out of range 0-127")
        velocity = max(1, min(127, velocity))
        length_beats = max(1 / ppq, length_beats)

        start_tick = round(start_beat * ppq)
        end_tick = max(start_tick + 1, round((start_beat + length_beats) * ppq))
        events.append((start_tick, 1, pitch, velocity))
        events.append((end_tick, 0, pitch, 0))

    events.sort(key=lambda e: (e[0], e[1]))

    mid = mido.MidiFile(ticks_per_beat=ppq)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    last_tick = 0
    for abs_tick, kind, note, velocity in events:
        msg_type = "note_on" if kind == 1 else "note_off"
        track.append(mido.Message(msg_type, note=note, velocity=velocity,
                                  time=abs_tick - last_tick))
        last_tick = abs_tick

    dest = _midi_dir() / f"{name}.mid"
    dest.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(dest))

    note_count = sum(1 for _, kind, _, v in events if kind == 1)
    now = time.time()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO midi_clips (name, path, bars, bpm, category, note_count, created, last_updated)
            VALUES (:name, :path, :bars, :bpm, :category, :note_count, :now, :now)
            ON CONFLICT(name) DO UPDATE SET
                path         = excluded.path,
                bars         = excluded.bars,
                bpm          = excluded.bpm,
                category     = excluded.category,
                note_count   = excluded.note_count,
                last_updated = excluded.last_updated
            """,
            {"name": name, "path": str(dest), "bars": bars, "bpm": bpm,
             "category": category, "note_count": note_count, "now": now},
        )
        row = con.execute("SELECT * FROM midi_clips WHERE name = ?", (name,)).fetchone()
        return _row_to_dict(row)


def list_clips(*, category: str | None = None) -> dict[str, Any]:
    with _conn() as con:
        if category:
            rows = con.execute(
                "SELECT * FROM midi_clips WHERE category = ? ORDER BY name", (category,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM midi_clips ORDER BY name"
            ).fetchall()
        return {"clips": [_row_to_dict(r) for r in rows], "count": len(rows)}


def show(name: str) -> dict[str, Any]:
    with _conn() as con:
        return _row_to_dict(_require(con, name))


def remove(name: str) -> dict[str, Any]:
    with _conn() as con:
        row = _require(con, name)
        path = Path(row["path"])
        con.execute("DELETE FROM midi_clips WHERE name = ?", (name,))
    # Remove the stored file only if it lives inside the catalog dir (don't
    # touch externally-linked files that were imported by path).
    try:
        midi_dir = _midi_dir()
        path.relative_to(midi_dir)  # raises ValueError if outside
        if path.is_file():
            path.unlink()
    except ValueError:
        pass
    return {"removed": name}


def summary(path: str) -> dict[str, Any]:
    """Parse a .mid file and return a note-grid representation.

    Returns::

        {
          "bars": int,
          "ppq": int,
          "bpm": float | None,
          "notes": [{"pitch": int, "start_beat": float,
                      "length_beats": float, "velocity": int}, ...]
        }

    ``start_beat`` is 0-indexed beats from the start of the file (e.g. beat 4
    = bar 2, beat 0 in 4/4).  This is what ``create_from_notes`` accepts back,
    so summary → edit → create_from_notes round-trips cleanly.
    """
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise BadInputError(f"file not found: {path}")

    try:
        mid = mido.MidiFile(str(src))
    except Exception as exc:
        raise BadInputError(f"could not parse '{path}': {exc}")

    ppq = mid.ticks_per_beat or PATTERN_PPQ

    # Detect BPM from tempo message if present
    bpm: float | None = None
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                bpm = round(60_000_000 / msg.tempo, 2)
                break
        if bpm is not None:
            break

    # Parse all note_on/off pairs from all tracks combined
    open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    note_events: list[dict[str, Any]] = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                key = (msg.channel, msg.note)
                open_notes.setdefault(key, []).append((abs_tick, msg.velocity))
            elif msg.type in ("note_off", "note_on"):
                key = (msg.channel, msg.note)
                stack = open_notes.get(key)
                if stack:
                    start_tick, velocity = stack.pop(0)
                    dur_tick = max(abs_tick - start_tick, 1)
                    note_events.append({
                        "pitch": msg.note,
                        "start_beat": round(start_tick / ppq, 4),
                        "length_beats": round(dur_tick / ppq, 4),
                        "velocity": velocity,
                    })

    note_events.sort(key=lambda n: (n["start_beat"], n["pitch"]))
    bars = _count_bars(mid)

    return {
        "bars": bars,
        "ppq": ppq,
        "bpm": bpm,
        "notes": note_events,
    }
