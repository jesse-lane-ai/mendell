"""Shared error types and exit codes.

Exit codes (per SPEC.md): 0 success · 1 bad input · 2 project not found · 3 engine error
"""

EXIT_OK = 0
EXIT_BAD_INPUT = 1
EXIT_NOT_FOUND = 2
EXIT_ENGINE_ERROR = 3


class MendellError(Exception):
    """Base error carrying a JSON-envelope message and a process exit code."""

    code = EXIT_BAD_INPUT

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class BadInputError(MendellError):
    code = EXIT_BAD_INPUT


class NotFoundError(MendellError):
    code = EXIT_NOT_FOUND


class EngineError(MendellError):
    code = EXIT_ENGINE_ERROR
