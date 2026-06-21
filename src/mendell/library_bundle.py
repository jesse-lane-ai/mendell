"""Library bundle import/export — zip-based transfer of samples, kits, projects, and MIDI clips.

``export_bundle(out_path, include=[...])``
    Bundle selected catalog content into a zip. Includes a top-level
    ``manifest.json`` with schema_version, created_at, counts, and a file list.
    Catalog metadata is stored as JSON so the target machine can rebuild DB rows
    without depending on source-machine absolute paths. Audio and MIDI files are
    stored at relative paths inside the zip.

``import_bundle(zip_path, dest_dir)``
    Extract files into ``dest_dir`` and register everything into the local DB
    idempotently (create-or-update). Returns a dict with
    ``{added, updated, skipped}`` counts per content type.

Supported ``include`` values: ``"samples"``, ``"kits"``, ``"projects"``,
``"midi"`` (case-insensitive; default is all four).

Notes
-----
* Samples are bundled under ``samples/<library_name>/<rel_path>``. On import,
  each library is re-registered pointing to the extracted directory.
* Kit slot ``source_path`` values are rewritten to the extracted location when
  the original sample lived inside a bundled library.
* Projects are bundled under ``projects/<project_name>/`` (full dir tree).
  On import they are extracted but not re-registered (``mendell projects sync``
  can rebuild the registry index from them).
* MIDI clips are bundled under ``midi/<name>.mid``. On import they are
  extracted and registered in the MIDI catalog (when available).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
MANIFEST_NAME = "manifest.json"

VALID_INCLUDE = {"samples", "kits", "projects", "midi"}


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

@contextmanager
def _read_db():
    """Read-only connection to the current library DB."""
    from . import library as library_mod
    path = library_mod.db_path()
    if not path.exists():
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        yield con
        con.close()
        return
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _parse_include(include: list[str] | None) -> set[str]:
    if include is None:
        return set(VALID_INCLUDE)
    result = set()
    for item in include:
        lower = item.strip().lower()
        if lower not in VALID_INCLUDE:
            raise ValueError(
                f"Unknown include value '{item}' (valid: {', '.join(sorted(VALID_INCLUDE))})"
            )
        result.add(lower)
    return result


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _maybe(d: dict, key: str, value: Any) -> None:
    """Add ``key`` to ``d`` only when ``value`` is not None (and not empty str)."""
    if value is not None and value != "":
        d[key] = value


def _has_column(row: sqlite3.Row, col: str) -> bool:
    try:
        _ = row[col]
        return True
    except (IndexError, KeyError):
        return False


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def export_bundle(
    out_path: str | Path,
    *,
    include: list[str] | None = None,
) -> dict[str, Any]:
    """Bundle selected catalog content into a zip at ``out_path``.

    Returns a manifest dict (same as the ``manifest.json`` written into the zip).
    """
    out_path = Path(out_path)
    include_set = _parse_include(include)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "include": sorted(include_set),
        "samples": [],
        "kits": [],
        "projects": [],
        "midi": [],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        with _read_db() as con:

            # ----------------------------------------------------------------
            # samples
            # ----------------------------------------------------------------
            if "samples" in include_set and _table_exists(con, "libraries"):
                lib_rows = con.execute(
                    "SELECT * FROM libraries ORDER BY name"
                ).fetchall()

                for lib_row in lib_rows:
                    lib_name = lib_row["name"]
                    lib_path = Path(lib_row["path"])
                    tags = [t for t in lib_row["tags"].split(",") if t]
                    file_count = lib_row["file_count"]

                    file_rows = (
                        con.execute(
                            "SELECT * FROM files WHERE library_name = ? ORDER BY rel_path",
                            (lib_name,),
                        ).fetchall()
                        if _table_exists(con, "files") else []
                    )

                    lib_meta: dict[str, Any] = {
                        "name": lib_name,
                        "original_path": lib_row["path"],
                        "tags": tags,
                        "file_count": file_count,
                        "files": [],
                    }

                    for file_row in file_rows:
                        rel = file_row["rel_path"]
                        abs_path = lib_path / rel
                        zip_rel = f"samples/{lib_name}/{rel}"

                        if abs_path.is_file():
                            zf.write(abs_path, zip_rel)

                        file_meta: dict[str, Any] = {
                            "rel_path": rel,
                            "zip_path": zip_rel,
                            "category": file_row["category"],
                        }
                        _maybe(file_meta, "bpm", file_row["bpm"])
                        _maybe(file_meta, "bpm_source", file_row["bpm_source"])
                        _maybe(file_meta, "kind", file_row["kind"])
                        _maybe(file_meta, "kind_source", file_row["kind_source"])
                        _maybe(file_meta, "duration", file_row["duration"])
                        _maybe(file_meta, "category_source", file_row["category_source"])
                        _maybe(file_meta, "category_confidence", file_row["category_confidence"])
                        # optional columns added in later migrations
                        try:
                            instruments = file_row["instruments"]
                            if instruments:
                                file_meta["instruments"] = [t for t in instruments.split(",") if t]
                        except (IndexError, KeyError):
                            pass
                        try:
                            caption = file_row["caption"]
                            if caption:
                                file_meta["caption"] = caption
                        except (IndexError, KeyError):
                            pass

                        lib_meta["files"].append(file_meta)

                    manifest["samples"].append(lib_meta)

            # ----------------------------------------------------------------
            # kits  (optional module — may not be present in all deployments)
            # ----------------------------------------------------------------
            if "kits" in include_set and _table_exists(con, "kits"):
                kit_rows = con.execute("SELECT * FROM kits ORDER BY name").fetchall()
                for kit_row in kit_rows:
                    kit_name = kit_row["name"]
                    slot_rows = (
                        con.execute(
                            "SELECT * FROM kit_slots WHERE kit_name = ? ORDER BY gm_note",
                            (kit_name,),
                        ).fetchall()
                        if _table_exists(con, "kit_slots") else []
                    )

                    kit_meta: dict[str, Any] = {
                        "name": kit_name,
                        "description": kit_row["description"],
                        "slots": [],
                    }
                    for slot in slot_rows:
                        kit_meta["slots"].append({
                            "gm_note": slot["gm_note"],
                            "category": slot["category"],
                            "source_path": slot["source_path"],
                            "slot_name": slot["slot_name"],
                        })
                    manifest["kits"].append(kit_meta)

            # ----------------------------------------------------------------
            # projects
            # ----------------------------------------------------------------
            if "projects" in include_set and _table_exists(con, "projects"):
                proj_rows = con.execute("SELECT * FROM projects ORDER BY name").fetchall()
                for proj_row in proj_rows:
                    proj_dir = Path(proj_row["path"])
                    if not proj_dir.is_dir():
                        continue
                    proj_name = proj_row["name"]
                    prefix = f"projects/{proj_name}/"

                    proj_meta: dict[str, Any] = {
                        "name": proj_name,
                        "original_path": proj_row["path"],
                        "zip_prefix": prefix,
                    }
                    _maybe(proj_meta, "genre", proj_row["genre"])
                    _maybe(proj_meta, "key", proj_row["key"])
                    _maybe(proj_meta, "scale", proj_row["scale"])
                    _maybe(proj_meta, "bpm", proj_row["bpm"])
                    _maybe(proj_meta, "time_sig", proj_row["time_sig"])
                    _maybe(proj_meta, "sample_rate", proj_row["sample_rate"])

                    for root, _dirs, files in os.walk(proj_dir):
                        for fname in sorted(files):
                            abs_file = Path(root) / fname
                            rel = abs_file.relative_to(proj_dir)
                            zip_rel = prefix + str(rel).replace(os.sep, "/")
                            zf.write(abs_file, zip_rel)

                    manifest["projects"].append(proj_meta)

            # ----------------------------------------------------------------
            # midi  (optional module — may not be present in all deployments)
            # ----------------------------------------------------------------
            if "midi" in include_set and _table_exists(con, "midi_clips"):
                midi_rows = con.execute("SELECT * FROM midi_clips ORDER BY name").fetchall()
                for midi_row in midi_rows:
                    midi_name = midi_row["name"]
                    midi_path = Path(midi_row["path"])
                    zip_rel = f"midi/{midi_name}.mid"

                    midi_meta: dict[str, Any] = {
                        "name": midi_name,
                        "zip_path": zip_rel,
                        "category": midi_row["category"],
                        "note_count": midi_row["note_count"],
                    }
                    _maybe(midi_meta, "bars", midi_row["bars"])
                    _maybe(midi_meta, "bpm", midi_row["bpm"])

                    if midi_path.is_file():
                        zf.write(midi_path, zip_rel)
                    manifest["midi"].append(midi_meta)

        # Write manifest
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))

    manifest["counts"] = {
        "samples_libraries": len(manifest["samples"]),
        "samples_files": sum(len(lib["files"]) for lib in manifest["samples"]),
        "kits": len(manifest["kits"]),
        "projects": len(manifest["projects"]),
        "midi": len(manifest["midi"]),
    }

    return manifest


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def import_bundle(
    zip_path: str | Path,
    dest_dir: str | Path,
) -> dict[str, Any]:
    """Extract a bundle into ``dest_dir`` and register its contents in the local DB.

    Returns::

        {
          "samples": {"added": N, "updated": N, "skipped": N},
          "kits":    {"added": N, "updated": N, "skipped": N},
          "projects":{"added": N, "skipped": N},
          "midi":    {"added": N, "updated": N, "skipped": N},
        }
    """
    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not zip_path.is_file():
        raise FileNotFoundError(f"bundle not found: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"not a valid zip file: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest_data = zf.read(MANIFEST_NAME)
        except KeyError:
            raise ValueError("bundle is missing manifest.json")
        manifest: dict[str, Any] = json.loads(manifest_data)

        schema = manifest.get("schema_version")
        if schema != SCHEMA_VERSION:
            raise ValueError(f"unsupported bundle schema_version: {schema!r}")

        zf.extractall(dest_dir)

    results: dict[str, Any] = {
        "samples": {"added": 0, "updated": 0, "skipped": 0},
        "kits": {"added": 0, "updated": 0, "skipped": 0},
        "projects": {"added": 0, "skipped": 0},
        "midi": {"added": 0, "updated": 0, "skipped": 0},
    }

    from . import library as library_mod

    # ----------------------------------------------------------------
    # register samples
    # ----------------------------------------------------------------
    for lib_meta in manifest.get("samples", []):
        lib_name = lib_meta["name"]
        tags = lib_meta.get("tags", [])
        lib_dest = dest_dir / "samples" / lib_name
        files_meta = lib_meta.get("files", [])

        if not lib_dest.is_dir():
            results["samples"]["skipped"] += 1
            continue

        try:
            existing = library_mod.list_entries()
            existing_names = {e["name"] for e in existing["libraries"]}
        except Exception:
            existing_names = set()

        was_existing = lib_name in existing_names

        try:
            _register_library_from_meta(lib_name, lib_dest, tags, files_meta)
        except Exception:
            results["samples"]["skipped"] += 1
            continue

        if was_existing:
            results["samples"]["updated"] += 1
        else:
            results["samples"]["added"] += 1

    # ----------------------------------------------------------------
    # register kits  (graceful no-op when kits module unavailable)
    # ----------------------------------------------------------------
    for kit_meta in manifest.get("kits", []):
        kit_name = kit_meta["name"]
        description = kit_meta.get("description", "")
        slots = kit_meta.get("slots", [])

        try:
            from . import kits as kits_mod  # type: ignore[import]
        except ImportError:
            results["kits"]["skipped"] += len(manifest.get("kits", []))
            break

        try:
            existing_kits = kits_mod.list_kits()
            existing_kit_names = {k["name"] for k in existing_kits["kits"]}
        except Exception:
            existing_kit_names = set()

        was_existing = kit_name in existing_kit_names

        try:
            kits_mod.create(kit_name, description=description)
            for slot in slots:
                source_path = slot["source_path"]
                if not Path(source_path).exists():
                    remapped = _remap_slot_path(source_path, dest_dir, manifest)
                    if remapped:
                        source_path = remapped
                if not Path(source_path).exists():
                    continue
                kits_mod.add_slot(
                    kit_name, slot["gm_note"], source_path,
                    slot_name=slot.get("slot_name", ""),
                )
        except Exception:
            results["kits"]["skipped"] += 1
            continue

        if was_existing:
            results["kits"]["updated"] += 1
        else:
            results["kits"]["added"] += 1

    # ----------------------------------------------------------------
    # projects — extract only (registry is a secondary index)
    # ----------------------------------------------------------------
    for proj_meta in manifest.get("projects", []):
        proj_name = proj_meta["name"]
        proj_dest = dest_dir / "projects" / proj_name
        if proj_dest.is_dir():
            results["projects"]["added"] += 1
        else:
            results["projects"]["skipped"] += 1

    # ----------------------------------------------------------------
    # midi  (graceful no-op when midi_catalog module unavailable)
    # ----------------------------------------------------------------
    for midi_meta in manifest.get("midi", []):
        midi_name = midi_meta["name"]
        category = midi_meta.get("category", "other")
        zip_dest = dest_dir / "midi" / f"{midi_name}.mid"

        if not zip_dest.is_file():
            results["midi"]["skipped"] += 1
            continue

        try:
            from . import midi_catalog as midi_mod  # type: ignore[import]
        except ImportError:
            results["midi"]["skipped"] += 1
            continue

        try:
            existing_clips = midi_mod.list_clips()
            existing_midi_names = {c["name"] for c in existing_clips["clips"]}
        except Exception:
            existing_midi_names = set()

        was_existing = midi_name in existing_midi_names

        try:
            midi_mod.import_file(str(zip_dest), name=midi_name, category=category)
        except Exception:
            results["midi"]["skipped"] += 1
            continue

        if was_existing:
            results["midi"]["updated"] += 1
        else:
            results["midi"]["added"] += 1

    return results


# ---------------------------------------------------------------------------
# import helpers
# ---------------------------------------------------------------------------

def _register_library_from_meta(
    lib_name: str,
    lib_dest: Path,
    tags: list[str],
    files_meta: list[dict[str, Any]],
) -> None:
    """Register a library row + file rows directly via DB, reusing bundled metadata.

    Avoids re-running audio analysis — the bundled metadata is inserted as-is.
    The library path is set to ``lib_dest`` (the extracted location).
    """
    from . import library as library_mod

    now = time.time()
    with library_mod._conn() as con:
        tag_str = ",".join(tags)
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
            (lib_name, str(lib_dest), tag_str, len(files_meta), now),
        )
        con.execute("DELETE FROM files WHERE library_name = ?", (lib_name,))
        rows = []
        for f in files_meta:
            instruments = f.get("instruments")
            instruments_str = (
                ",".join(instruments) if isinstance(instruments, list) else (instruments or "")
            )
            rows.append((
                lib_name,
                f["rel_path"],
                f.get("category", "one-shot"),
                f.get("bpm"),
                f.get("bpm_source"),
                f.get("kind"),
                f.get("kind_source"),
                f.get("duration"),
                instruments_str or None,
                f.get("category_source"),
                f.get("category_confidence"),
                f.get("caption"),
            ))
        con.executemany(
            "INSERT OR IGNORE INTO files "
            "(library_name, rel_path, category, bpm, bpm_source, kind, kind_source, duration, "
            "instruments, category_source, category_confidence, caption) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def _remap_slot_path(
    original_path: str,
    dest_dir: Path,
    manifest: dict[str, Any],
) -> str | None:
    """Try to find a kit slot's sample inside the extracted bundle.

    Returns the dest_dir-local path when the original_path matches a bundled
    library file, else None.
    """
    original = Path(original_path)
    for lib_meta in manifest.get("samples", []):
        lib_name = lib_meta["name"]
        original_lib_path = lib_meta.get("original_path", "")
        if not original_lib_path:
            continue
        try:
            rel = original.relative_to(original_lib_path)
            candidate = dest_dir / "samples" / lib_name / rel
            if candidate.exists():
                return str(candidate)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# full-DB backup / restore
# ---------------------------------------------------------------------------
#
# A bundle copies the actual audio files and is portable to a machine that
# doesn't have the originals. A DB backup is the opposite trade-off: a single
# small ``.db`` snapshot of the *entire* shared catalog (libraries, indexed
# files, kits, projects, MIDI, recognition cache) with no audio copied. It's
# the fastest way to back up or move the catalog between machines that share the
# same sample paths. Built on SQLite's online-backup API so the snapshot is
# consistent even with WAL writers in flight.

def export_db(out_path: str | Path) -> dict[str, Any]:
    """Write a consistent snapshot of the whole library DB to ``out_path``."""
    from . import library as library_mod

    out = Path(out_path).expanduser()
    if out.suffix.lower() != ".db":
        out = out.with_suffix(".db")
    out.parent.mkdir(parents=True, exist_ok=True)

    src_path = library_mod.db_path()
    if not src_path.exists():
        raise FileNotFoundError(
            f"no library DB to back up at {src_path} — add a folder first"
        )

    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    return {
        "format": "db",
        "path": str(out),
        "bytes": out.stat().st_size,
    }


def import_db(src_path: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Restore a DB snapshot created by :func:`export_db`, replacing the live
    catalog. Refuses to clobber an existing non-empty DB unless ``overwrite``."""
    from . import library as library_mod

    src = Path(src_path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"DB backup not found: {src}")
    # Validate it's a usable SQLite DB carrying our schema before touching live data.
    probe = sqlite3.connect(src)
    try:
        if not _table_exists(probe, "libraries"):
            raise ValueError(f"'{src}' is not a Mendell library DB (no 'libraries' table)")
    finally:
        probe.close()

    dest = library_mod.db_path()
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        raise ValueError(
            f"a library DB already exists at {dest} — pass overwrite to replace it"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Restore via the backup API into the canonical location, then drop any stale
    # WAL/SHM siblings so the restored snapshot is what subsequent reads see.
    srccon = sqlite3.connect(src)
    try:
        dstcon = sqlite3.connect(dest)
        try:
            srccon.backup(dstcon)
        finally:
            dstcon.close()
    finally:
        srccon.close()
    for suffix in ("-wal", "-shm"):
        sibling = dest.with_name(dest.name + suffix)
        if sibling.exists():
            sibling.unlink()

    with _read_db() as con:
        libs = con.execute("SELECT COUNT(*) AS n FROM libraries").fetchone()["n"]
    return {"format": "db", "path": str(dest), "libraries": libs}
