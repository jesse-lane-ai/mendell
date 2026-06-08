"""JSON envelope output and human-readable fallback rendering for the CLI."""

import json
import sys
from typing import Any

from .errors import EXIT_OK, MendellError


def emit_ok(data: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        click_echo({"ok": True, "data": data})
    else:
        click_echo_human(human if human is not None else data)


def emit_error(err: MendellError, as_json: bool) -> None:
    if as_json:
        sys.stderr.write(json.dumps({"ok": False, "error": err.message, "code": err.code}, indent=2, sort_keys=False) + "\n")
    else:
        sys.stderr.write(f"error: {err.message}\n")


def click_echo(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=False) + "\n")


def click_echo_human(obj: Any) -> None:
    if isinstance(obj, str):
        sys.stdout.write(obj + "\n")
    else:
        sys.stdout.write(json.dumps(obj, indent=2, sort_keys=False) + "\n")


def emit_event(event: dict[str, Any]) -> None:
    """Write a single NDJSON event line to stdout (used for export progress)."""
    sys.stdout.write(json.dumps(event, sort_keys=False) + "\n")
    sys.stdout.flush()
