from __future__ import annotations

import unittest

from skill_sync.errors import SkillSyncError
from skill_sync.protocol import (
    EXIT_CONFLICT,
    EXIT_FAILURE,
    EXIT_SAFETY,
    EXIT_SUCCESS,
    EXIT_USAGE,
    SCHEMA_VERSION,
    error_envelope,
    success_envelope,
)


class ProtocolTests(unittest.TestCase):
    def test_exit_codes_are_stable(self) -> None:
        self.assertEqual(EXIT_SUCCESS, 0)
        self.assertEqual(EXIT_FAILURE, 1)
        self.assertEqual(EXIT_USAGE, 2)
        self.assertEqual(EXIT_CONFLICT, 3)
        self.assertEqual(EXIT_SAFETY, 4)

    def test_success_envelope_has_stable_common_fields(self) -> None:
        result = {"managed": True}

        envelope = success_envelope(
            "managed check",
            result,
            warnings=["deployment is stale"],
        )

        self.assertEqual(
            envelope,
            {
                "schema_version": SCHEMA_VERSION,
                "command": "managed check",
                "ok": True,
                "result": result,
                "warnings": ["deployment is stale"],
                "errors": [],
            },
        )

    def test_success_envelope_does_not_share_mutable_defaults(self) -> None:
        first = success_envelope("doctor", {})
        second = success_envelope("doctor", {})

        first["warnings"].append("first only")

        self.assertEqual(second["warnings"], [])

    def test_error_envelope_serializes_skill_sync_error(self) -> None:
        error = SkillSyncError(
            "local and remote branches diverged",
            code="git_diverged",
            exit_code=EXIT_CONFLICT,
            details={"branch": "main"},
        )

        envelope = error_envelope("sync", error, warnings=["no files changed"])

        self.assertEqual(
            envelope,
            {
                "schema_version": SCHEMA_VERSION,
                "command": "sync",
                "ok": False,
                "result": None,
                "warnings": ["no files changed"],
                "errors": [
                    {
                        "code": "git_diverged",
                        "message": "local and remote branches diverged",
                        "details": {"branch": "main"},
                    }
                ],
            },
        )

    def test_error_envelope_accepts_multiple_errors(self) -> None:
        envelope = error_envelope(
            "validate",
            [
                SkillSyncError("missing SKILL.md", code="missing_skill_file"),
                SkillSyncError("invalid name", code="invalid_skill_name"),
            ],
            result={"valid": False},
        )

        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["result"], {"valid": False})
        self.assertEqual(
            [item["code"] for item in envelope["errors"]],
            ["missing_skill_file", "invalid_skill_name"],
        )


class SkillSyncErrorTests(unittest.TestCase):
    def test_plain_string_behavior_remains_compatible(self) -> None:
        error = SkillSyncError("not initialized")

        self.assertEqual(str(error), "not initialized")
        self.assertEqual(error.args, ("not initialized",))
        self.assertEqual(error.code, "operation_failed")
        self.assertEqual(error.exit_code, EXIT_FAILURE)
        self.assertEqual(error.details, {})

    def test_details_are_copied(self) -> None:
        details = {"path": "/tmp/source"}
        error = SkillSyncError("unsafe path", details=details)

        details["path"] = "/tmp/changed"

        self.assertEqual(error.details, {"path": "/tmp/source"})


if __name__ == "__main__":
    unittest.main()
