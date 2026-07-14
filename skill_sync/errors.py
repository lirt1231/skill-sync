"""User-facing exceptions for skill-sync workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skill_sync.protocol import EXIT_FAILURE


class SkillSyncError(Exception):
    """An error that should be reported directly to CLI users.

    The single-message form remains compatible with the original exception.
    Structured fields let future CLI and Web integrations expose stable
    machine-readable errors without parsing the human-facing message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "operation_failed",
        exit_code: int = EXIT_FAILURE,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = dict(details) if details is not None else {}
