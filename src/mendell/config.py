"""Global Mendell configuration (``config.json``) and OS-appropriate paths.

Mendell keeps a little user-level state outside any project: the shared SQLite
DB (sample library + project registry) and this JSON config file. Both live in
an OS-appropriate config directory:

    Linux / other : ``$XDG_CONFIG_HOME/mendell``      (default ``~/.config/mendell``)
    macOS         : ``~/Library/Application Support/mendell``
    Windows       : ``%APPDATA%\\mendell``

The config file currently holds one key, ``projects_folder`` — the default
parent directory that ``mendell new <name>`` (and ``beat new``/``make``/
``random32``) create projects under when given a bare name. On first use it is
materialized with an OS-appropriate default (``~/Documents/mendell``) and the
folder is created on disk.

Overrides (primarily for tests/CI/agents):
  * ``MENDELL_CONFIG_DIR``      — relocate the whole config dir (config.json + db)
  * ``MENDELL_PROJECTS_FOLDER`` — force the projects folder for one invocation
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

CONFIG_DIR_ENV_VAR = "MENDELL_CONFIG_DIR"
PROJECTS_FOLDER_ENV_VAR = "MENDELL_PROJECTS_FOLDER"
CONFIG_FILENAME = "config.json"

# Schema for config.json. An empty ``projects_folder`` means "resolve to the
# OS default on first use" (see :func:`projects_folder`).
DEFAULTS: dict[str, Any] = {
    "projects_folder": "",
}


# ---------------------------------------------------------------------------
# directory resolution
# ---------------------------------------------------------------------------

def config_dir() -> Path:
    """The OS-appropriate Mendell config directory (config.json + library.db)."""
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return _os_config_dir()


def _os_config_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else home / "AppData" / "Roaming"
        return root / "mendell"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "mendell"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else home / ".config"
    return root / "mendell"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def default_projects_folder() -> Path:
    """OS-appropriate default for a fresh ``projects_folder`` (``~/Documents/mendell``)."""
    return Path.home() / "Documents" / "mendell"


# ---------------------------------------------------------------------------
# config.json read / write
# ---------------------------------------------------------------------------

def _read() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt config never wedges the tool — fall back to defaults.
        return {}


def _write(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)  # atomic swap


def load() -> dict[str, Any]:
    """Return the full config, materializing config.json with defaults on first
    use. Missing keys are backfilled and persisted so the on-disk file always
    reflects the current schema."""
    data = _read()
    existed = config_path().is_file()
    changed = False
    for key, default in DEFAULTS.items():
        if key not in data:
            data[key] = default
            changed = True
    if changed or not existed:
        _write(data)
    return data


def get(key: str) -> Any:
    return load().get(key)


def set_value(key: str, value: Any) -> dict[str, Any]:
    """Set a config key and persist. Returns the full updated config."""
    data = load()
    data[key] = value
    _write(data)
    return data


# ---------------------------------------------------------------------------
# projects folder
# ---------------------------------------------------------------------------

def projects_folder(*, create: bool = True) -> Path:
    """Resolve the default parent directory for new projects.

    Resolution order:
      1. ``MENDELL_PROJECTS_FOLDER`` env override
      2. the ``projects_folder`` key in config.json (if non-empty)
      3. the OS default (``~/Documents/mendell``), which is written back into
         config.json so it is explicit thereafter.

    With ``create=True`` (the default) the directory is created on disk — this
    is the "first start makes the projects folder" behavior.
    """
    override = os.environ.get(PROJECTS_FOLDER_ENV_VAR)
    if override:
        folder = Path(override).expanduser()
    else:
        configured = str(get("projects_folder") or "").strip()
        if configured:
            folder = Path(configured).expanduser()
        else:
            folder = default_projects_folder()
            set_value("projects_folder", str(folder))
    folder = folder.resolve()
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def resolve_project_parent(name: str) -> tuple[Path, str]:
    """Split a ``new``-style argument into ``(parent_dir, bare_name)``.

    A bare name (no path separators) is created under :func:`projects_folder`;
    a name containing separators or an absolute path is resolved literally,
    relative to the current directory — the escape hatch for placing a project
    somewhere specific.
    """
    has_path = (
        os.path.isabs(name)
        or os.sep in name
        or bool(os.altsep and os.altsep in name)
    )
    if has_path:
        target = Path(name) if os.path.isabs(name) else (Path.cwd() / name)
        return target.parent, target.name
    return projects_folder(), name


# ---------------------------------------------------------------------------
# first-run initialization
# ---------------------------------------------------------------------------

def ensure_initialized() -> None:
    """Best-effort first-run setup: materialize config.json and the projects
    folder. Wired into the CLI's top-level group so the projects folder exists
    the first time any ``mendell`` command runs. Never raises — a config-write
    failure must not break a command (project.toml stays the source of truth)."""
    try:
        projects_folder(create=True)
    except OSError:
        pass


def info() -> dict[str, Any]:
    """A JSON-friendly snapshot of the resolved config and paths.

    ``projects_folder`` is the *effective* folder (after env override / default
    resolution), which may differ from the raw value stored in config.json.
    """
    data = load()
    return {
        "config_path": str(config_path()),
        "config_dir": str(config_dir()),
        **data,
        "projects_folder": str(projects_folder(create=False)),
    }
