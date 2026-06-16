"""Project directory resolution and layout.

A project is a directory containing project.toml. Commands address it by name
(resolved relative to the current directory) or by path (relative/absolute).
"""

from pathlib import Path

from .errors import NotFoundError

PROJECT_FILE = "project.toml"


def resolve_project(name_or_path: str) -> Path:
    """Resolve a project name/path to its directory. Raises NotFoundError if absent."""
    candidates = [
        Path(name_or_path),
        Path.cwd() / name_or_path,
    ]
    for candidate in candidates:
        if (candidate / PROJECT_FILE).is_file():
            return candidate.resolve()

    # Allow running commands from inside the project directory itself.
    cwd = Path.cwd()
    if (cwd / PROJECT_FILE).is_file() and cwd.name == name_or_path:
        return cwd.resolve()

    # Fall back to the project registry, a secondary index that lets a bare
    # name resolve from any working directory. Lazy-imported: registry
    # imports this module, so a module-level import would cycle.
    from . import registry

    registered = registry.lookup_path(name_or_path)
    if registered is not None:
        if (registered / PROJECT_FILE).is_file():
            return registered.resolve()
        raise NotFoundError(
            f"project '{name_or_path}' is registered at {registered} but "
            f"{PROJECT_FILE} is missing there; run "
            f"'mendell projects remove {name_or_path}' to clear the stale entry"
        )

    raise NotFoundError(f"project '{name_or_path}' not found")


def project_toml(project: Path) -> Path:
    return project / PROJECT_FILE


def tracks_dir(project: Path) -> Path:
    return project / "tracks"


def track_toml(project: Path, track: str) -> Path:
    return tracks_dir(project) / f"{track}.toml"


def clips_dir(project: Path) -> Path:
    return project / "clips"


def clip_toml(project: Path, clip: str) -> Path:
    return clips_dir(project) / f"{clip}.toml"


def samplers_dir(project: Path) -> Path:
    return project / "samplers"


def sampler_toml(project: Path, track: str) -> Path:
    return samplers_dir(project) / f"{track}.toml"


def arrangement_toml(project: Path) -> Path:
    return project / "arrangement.toml"


def samples_dir(project: Path) -> Path:
    return project / "samples"
