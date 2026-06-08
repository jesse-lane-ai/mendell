"""Sample library — a small global registry of named external sample folders.

Unlike everything else in Mendell, the library lives outside any project (in
a user-level config file) so it persists across projects and can be reused
from any of them. It only ever stores *references* (paths + lightweight
filename-derived metadata) — sample audio is still copied into a project's
``samples/`` directory at the point of use, exactly as it always has been.

This module is the single seam other commands (``kit load``, ``sampler map
add``, ``clip import``, ...) go through to turn a ``<library-name>/<relative
path>`` reference — or a bare registered name — into a real filesystem path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from . import sampler as sampler_mod
from .errors import BadInputError, NotFoundError
from .toml_io import read_toml, write_toml

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


def _config_path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".config" / "mendell" / "library.toml"


def _load() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file():
        return {"library": []}
    data = read_toml(path)
    data.setdefault("library", [])
    return data


def _save(data: dict[str, Any]) -> None:
    write_toml(_config_path(), data)


def _entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    return data["library"]


def _find(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for entry in _entries(data):
        if entry["name"] == name:
            return entry
    return None


def _require(data: dict[str, Any], name: str) -> dict[str, Any]:
    entry = _find(data, name)
    if entry is None:
        raise NotFoundError(f"library '{name}' not registered")
    return entry


def _scan_folder(path: Path) -> list[Path]:
    return sorted(
        p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )


def _index_folder(folder: Path) -> list[dict[str, Any]]:
    """Walk `folder` once and cache each file's relative path + guessed
    category — the result `add`/`scan` persist so `search`/`show` can read
    straight from the registry instead of re-walking and re-guessing."""
    return [
        {"path": str(file_path.relative_to(folder)), "category": guess_category(file_path)}
        for file_path in _scan_folder(folder)
    ]


def _cached_files(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Cached file index for `entry`, building it on the fly for entries
    registered before caching existed (so old registries keep working)."""
    files = entry.get("files")
    if files is None:
        folder = Path(entry["path"])
        files = _index_folder(folder) if folder.is_dir() else []
    return files


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

def add(name: str, path: str, *, tags: list[str] | None = None) -> dict[str, Any]:
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise BadInputError(f"folder not found: {path}")
    folder = folder.resolve()

    data = _load()
    entry = _find(data, name)
    indexed = _index_folder(folder)
    if entry is None:
        entry = {"name": name, "path": str(folder), "tags": list(tags or [])}
        _entries(data).append(entry)
    else:
        entry["path"] = str(folder)
        if tags is not None:
            entry["tags"] = list(tags)
    entry["files"] = indexed
    entry["file_count"] = len(indexed)
    entry["last_scanned"] = time.time()

    _entries(data).sort(key=lambda e: e["name"])
    _save(data)
    return _summary(entry)


def remove(name: str) -> dict[str, Any]:
    data = _load()
    entry = _require(data, name)
    _entries(data).remove(entry)
    _save(data)
    return {"removed": name}


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "path": entry["path"],
        "tags": list(entry.get("tags", [])),
        "file_count": entry.get("file_count", 0),
        "last_scanned": entry.get("last_scanned"),
    }


def list_entries() -> dict[str, Any]:
    data = _load()
    return {"libraries": [_summary(e) for e in _entries(data)]}


def scan(name: str | None = None) -> dict[str, Any]:
    data = _load()
    targets = [_require(data, name)] if name is not None else _entries(data)
    if not targets:
        raise BadInputError("no libraries registered")

    scanned = []
    for entry in targets:
        folder = Path(entry["path"])
        if not folder.is_dir():
            raise BadInputError(f"library '{entry['name']}' folder no longer exists: {entry['path']}")
        indexed = _index_folder(folder)
        entry["files"] = indexed
        entry["file_count"] = len(indexed)
        entry["last_scanned"] = time.time()
        scanned.append(_summary(entry))

    _save(data)
    return {"scanned": scanned}


def show(name: str) -> dict[str, Any]:
    data = _load()
    entry = _require(data, name)

    files = [
        {"ref": f"{name}/{f['path']}", "category": f["category"]}
        for f in _cached_files(entry)
    ]
    return {**_summary(entry), "files": files}


def search(
    query: str | None = None, *, library: str | None = None,
    tag: str | None = None, category: str | None = None,
) -> dict[str, Any]:
    data = _load()
    entries = [_require(data, library)] if library is not None else _entries(data)

    needle = query.lower() if query else None
    matches = []
    for entry in entries:
        if tag is not None and tag not in entry.get("tags", []):
            continue
        for f in _cached_files(entry):
            if category is not None and f["category"] != category:
                continue
            if needle is not None and needle not in Path(f["path"]).name.lower():
                continue
            matches.append({
                "ref": f"{entry['name']}/{f['path']}",
                "category": f["category"],
                "tags": list(entry.get("tags", [])),
            })

    return {"query": query, "matches": matches, "count": len(matches)}


# ---------------------------------------------------------------------------
# reference resolution — the seam other commands hook into
# ---------------------------------------------------------------------------

def resolve_ref(ref: str) -> Path | None:
    """Resolve a ``<library-name>[/<relative-path>]`` reference to a real path.

    Returns ``None`` (rather than raising) when ``ref`` doesn't name a
    registered library, so callers can fall back to treating it as a literal
    filesystem path — library refs and plain paths share the same option/arg.
    """
    name, _, rest = ref.partition("/")
    data = _load()
    entry = _find(data, name)
    if entry is None:
        return None

    folder = Path(entry["path"])
    target = (folder / rest).resolve() if rest else folder
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        raise BadInputError(f"'{ref}' escapes library '{name}'")
    if not target.exists():
        raise NotFoundError(f"'{ref}' not found in library '{name}' ({entry['path']})")
    return target


def resolve_path_arg(value: str | None) -> str | None:
    """Resolve ``value`` as a library reference if it names one; otherwise
    return it unchanged so plain filesystem paths keep working untouched."""
    if value is None:
        return None
    resolved = resolve_ref(value)
    return str(resolved) if resolved is not None else value
