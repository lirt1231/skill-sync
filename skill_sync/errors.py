"""User-facing exceptions for skill-sync workflows."""

from __future__ import annotations


class SkillSyncError(Exception):
    """An error that should be reported directly to CLI users."""

