"""`mendell config ...` — inspect and edit the global config.json.

config.json lives in the OS-appropriate config directory alongside the shared
SQLite DB. Today it holds one key, ``projects_folder`` — the default parent
directory new projects are created under when given a bare name.
"""

import click

from .. import config as config_mod
from ..errors import BadInputError
from ._base import command, json_option

# Keys a user may set via `mendell config set`.
SETTABLE_KEYS = {"projects_folder"}


@click.group("config")
def config():
    """Global configuration — projects folder and resolved paths."""


@config.command("show")
@json_option
@command
def show():
    """Show the resolved config and the paths it lives at."""
    data = config_mod.info()
    return data, data


@config.command("path")
@json_option
@command
def path():
    """Print the config.json location."""
    data = {"config_path": str(config_mod.config_path())}
    return data, data["config_path"]


@config.command("get")
@click.argument("key")
@json_option
@command
def get(key):
    """Get a single config KEY's value."""
    value = config_mod.get(key)
    data = {"key": key, "value": value}
    return data, value


@config.command("set")
@click.argument("key")
@click.argument("value")
@json_option
@command
def set_(key, value):
    """Set a config KEY to VALUE (e.g. `config set projects_folder ~/beats`)."""
    if key not in SETTABLE_KEYS:
        raise BadInputError(
            f"unknown config key '{key}' (settable: {sorted(SETTABLE_KEYS)})"
        )
    config_mod.set_value(key, value)
    # Re-resolve so the caller sees the effective (expanded/created) path.
    data = config_mod.info()
    return data, data
