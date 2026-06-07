"""Shared CLI plumbing: --json flag, JSON envelope output, exit codes."""

import functools
import sys

import click

from ..errors import EXIT_ENGINE_ERROR, EXIT_OK, MendellError
from ..output import emit_error, emit_ok

json_option = click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")


def command(f):
    """Wrap a command body so it returns ``(data, human_text)`` and let this
    decorator handle JSON-envelope emission, human-readable fallback, and
    exit-code mapping uniformly across the whole CLI."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        as_json = kwargs.pop("as_json", False)
        try:
            result = f(*args, **kwargs)
        except MendellError as err:
            emit_error(err, as_json)
            sys.exit(err.code)
        except Exception as err:  # unexpected failure -> engine error envelope
            wrapped = MendellError(str(err), code=EXIT_ENGINE_ERROR)
            emit_error(wrapped, as_json)
            sys.exit(EXIT_ENGINE_ERROR)

        if isinstance(result, tuple) and len(result) == 2:
            data, human = result
        else:
            data, human = result, result
        emit_ok(data, as_json, human)
        sys.exit(EXIT_OK)

    return wrapper
