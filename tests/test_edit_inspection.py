import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from skill_sync import cli
from skill_sync.edit_session import EditSessionMetadata, EditSessionStore


BASELINE_HASH = "sha256:" + "a" * 64
SESSION_ID = "12345678-1234-4234-9234-123456789abc"


def run_cli(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class EditInspectionCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.data_root = root / "data"
        self.config_path = root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "sync_repo_path": None,
                    "platform": "codex",
                    "skills_root": str(root / "skills"),
                    "branch": "main",
                    "disabled_agents": [],
                    "skills": {},
                    "data_root": str(self.data_root),
                }
            ),
            encoding="utf-8",
        )

    def create_active_session(self):
        metadata = EditSessionMetadata.new(
            logical_skill="alpha",
            baseline_hash=BASELINE_HASH,
            actor="codex",
            now=datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc),
        )
        metadata = EditSessionMetadata.from_dict(
            {**metadata.to_dict(), "session_id": SESSION_ID}
        )
        return metadata, EditSessionStore(self.data_root).create(metadata)

    def test_empty_list_has_stable_text_and_json_contract_without_creating_state(self):
        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "list"]
        )
        self.assertEqual((code, stdout, stderr), (0, "No edit sessions.\n", ""))
        self.assertFalse(self.data_root.exists())

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "list", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "command": "edit list",
                "ok": True,
                "result": {"sessions": []},
                "warnings": [],
                "errors": [],
            },
        )
        self.assertFalse(self.data_root.exists())

    def test_active_session_has_stable_list_and_status_contracts(self):
        metadata, _ = self.create_active_session()

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "list"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            f"- {SESSION_ID} [active] alpha (actor codex, updated 2026-07-15T08:30:00Z)\n",
        )

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "list", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        list_envelope = json.loads(stdout)
        self.assertEqual(list_envelope["command"], "edit list")
        self.assertEqual(list_envelope["result"], {"sessions": [metadata.to_dict()]})

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "status", SESSION_ID]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "\n".join(
                (
                    f"Session: {SESSION_ID}",
                    "Skill: alpha",
                    "Status: active",
                    "Actor: codex",
                    f"Baseline: {BASELINE_HASH}",
                    "Created: 2026-07-15T08:30:00Z",
                    "Updated: 2026-07-15T08:30:00Z",
                    "",
                )
            ),
        )

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "status", SESSION_ID, "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "edit status")
        self.assertEqual(envelope["result"], metadata.to_dict())

    def test_inspection_does_not_invoke_git_or_network_workflows(self):
        self.create_active_session()
        with mock.patch.object(
            cli.core.git,
            "run_git",
            side_effect=AssertionError("inspection must remain local-only"),
        ):
            for action in (["list"], ["status", SESSION_ID]):
                code, stdout, stderr = run_cli(
                    ["--config", str(self.config_path), "edit", *action, "--json"]
                )
                self.assertEqual(code, 0)
                self.assertNotEqual(stdout, "")
                self.assertEqual(stderr, "")

    def test_missing_session_has_stable_text_and_json_error_contracts(self):
        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "status", SESSION_ID]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, f"error: edit session does not exist: {SESSION_ID}\n")

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "status", SESSION_ID, "--json"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr),
            {
                "schema_version": 1,
                "command": "edit status",
                "ok": False,
                "result": None,
                "warnings": [],
                "errors": [
                    {
                        "code": "edit_session_not_found",
                        "message": f"edit session does not exist: {SESSION_ID}",
                        "details": {"session_id": SESSION_ID},
                    }
                ],
            },
        )

    def test_corrupt_metadata_fails_closed_in_list_and_status(self):
        _, paths = self.create_active_session()
        corrupted = b'{"schema_version": 1, "status":'
        paths.metadata.write_bytes(corrupted)

        for action in (["list"], ["status", SESSION_ID]):
            code, stdout, stderr = run_cli(
                ["--config", str(self.config_path), "edit", *action]
            )
            self.assertEqual(code, 4)
            self.assertEqual(stdout, "")
            self.assertTrue(
                stderr.startswith("error: cannot safely read edit session metadata:")
            )

        for action in (["list"], ["status", SESSION_ID]):
            code, stdout, stderr = run_cli(
                ["--config", str(self.config_path), "edit", *action, "--json"]
            )
            self.assertEqual(code, 4)
            self.assertEqual(stdout, "")
            envelope = json.loads(stderr)
            self.assertEqual(envelope["command"], "edit " + action[0])
            self.assertFalse(envelope["ok"])
            self.assertEqual(
                envelope["errors"][0]["code"], "invalid_edit_session_metadata"
            )
        self.assertEqual(paths.metadata.read_bytes(), corrupted)


if __name__ == "__main__":
    unittest.main()
