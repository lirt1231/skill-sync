"""Stable machine-readable protocol primitives for skill-sync.

This module deliberately has no CLI or workflow dependencies.  Commands can
adopt the envelope incrementally without changing the protocol contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA_VERSION = 1

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_SAFETY = 4


def success_envelope(
    command: str,
    result: Any,
    *,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a successful response using the shared JSON envelope."""

    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": True,
        "result": result,
        "warnings": list(warnings),
        "errors": [],
    }


def error_envelope(
    command: str,
    errors: Any | Iterable[Any],
    *,
    result: Any = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a failed response from one or more structured exceptions.

    Error objects are intentionally normalized by attribute instead of by
    concrete exception type.  This keeps the protocol module independent from
    the exception module and allows callers to provide equivalent structured
    errors at integration boundaries.
    """

    if isinstance(errors, BaseException) or isinstance(errors, (str, bytes)):
        error_items = [errors]
    else:
        try:
            error_items = list(errors)
        except TypeError:
            error_items = [errors]

    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": False,
        "result": result,
        "warnings": list(warnings),
        "errors": [_serialize_error(error) for error in error_items],
    }


def _serialize_error(error: Any) -> dict[str, Any]:
    details = getattr(error, "details", {})
    if not isinstance(details, Mapping):
        details = {"value": details}

    return {
        "code": getattr(error, "code", "operation_failed"),
        "message": str(error),
        "details": dict(details),
    }
