"""`mendell projects ...` — the global registry of every project Mendell has
created (name, genre, key, bpm, created/last-updated), stored in the shared
SQLite DB. A row is recorded automatically on project creation; these commands
read it back, refresh it, or drop entries."""

import click

from .. import registry as registry_mod
from ..paths import resolve_project
from ._base import command, json_option


@click.group("projects")
def projects():
    """Project registry — list and inspect every project Mendell has created."""


@projects.command("list")
@json_option
@command
def list_():
    """List every registered project, most recently updated first."""
    data = registry_mod.list_projects()
    return data, data


@projects.command("show")
@click.argument("project")
@json_option
@command
def show(project):
    """Show one registry entry by project name or directory path."""
    data = registry_mod.show(project)
    return data, data


@projects.command("sync")
@click.argument("project")
@click.option("--genre", type=str, default=None, help="Set/refresh the project's genre.")
@json_option
@command
def sync(project, genre):
    """Refresh PROJECT's registry row from its project.toml (and optional genre).

    Bumps last_updated and re-reads name/key/bpm/etc. Also the way to register a
    project created before the registry existed.
    """
    project_dir = resolve_project(project)
    data = registry_mod.record(project_dir, genre=genre)
    return data, data


@projects.command("remove")
@click.argument("project")
@json_option
@command
def remove(project):
    """Drop PROJECT from the registry (files on disk are left untouched)."""
    data = registry_mod.remove(project)
    return data, data
