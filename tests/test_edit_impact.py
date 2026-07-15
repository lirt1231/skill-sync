import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync import cli
import skill_sync.core as core_module
from skill_sync.agents import AgentClient
from skill_sync.config import empty_config, save_config
from skill_sync.core import edit_abort, edit_begin, edit_impact
from skill_sync.deployment import render_base_deployment
from skill_sync.edit_session import EditSessionStore
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.linking import create_directory_link
from skill_sync.registry import save_registry


def run_cli(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class EditImpactTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.skill = self.skills_root / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
        self.data_root = self.root / "data"
        config = empty_config()
        config.update(
            {
                "sync_repo_path": str(self.repo),
                "skills_root": str(self.skills_root),
                "data_root": str(self.data_root),
                "disabled_agents": ["kimi"],
                "skills": {"alpha": {"local_path": str(self.skill)}},
            }
        )
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    "alpha": {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": "alpha",
                    }
                },
            },
        )
        self.clients = [
            AgentClient(
                "codex", "codex", "Codex", self.root / "clients/codex/skills", True
            ),
            AgentClient(
                "workbuddy",
                "workbuddy",
                "WorkBuddy",
                self.root / "clients/workbuddy/skills",
                False,
            ),
            AgentClient(
                "kimi-code",
                "kimi",
                "Kimi Code",
                self.root / "clients/kimi-code/skills",
                True,
            ),
            AgentClient(
                "kimi-desktop",
                "kimi",
                "Kimi Desktop",
                self.root / "clients/kimi-desktop/skills",
                False,
            ),
            AgentClient(
                "claude-code",
                "claude",
                "Claude Code",
                self.root / "clients/claude-code/skills",
                True,
            ),
        ]

    def begin_changed_session(self) -> dict:
        result = edit_begin("alpha", actor="codex", config_path=self.config_path)
        Path(result["workspace_path"]).joinpath("SKILL.md").write_text(
            "# proposed alpha\n", encoding="utf-8"
        )
        return result

    def snapshot(self) -> dict[str, tuple]:
        snapshot: dict[str, tuple] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                snapshot[relative] = ("link", os.readlink(path))
            elif path.is_file():
                snapshot[relative] = ("file", path.read_bytes(), path.stat().st_mode)
            elif path.is_dir():
                snapshot[relative] = ("directory", path.stat().st_mode)
        return snapshot

    def install_current_deployment(self, client: AgentClient) -> None:
        deployment = render_base_deployment(
            self.skill,
            self.data_root / "rendered",
            "alpha",
            client.id,
        )
        create_directory_link(deployment.path, client.skills_dir / "alpha")

    def test_impact_lists_multiple_clients_and_is_completely_read_only(self):
        self.install_current_deployment(self.clients[0])
        self.install_current_deployment(self.clients[1])
        session = self.begin_changed_session()
        metadata_path = EditSessionStore(self.data_root).paths(
            session["session_id"]
        ).metadata
        metadata_before = (metadata_path.read_bytes(), metadata_path.stat().st_mtime_ns)
        before = self.snapshot()

        with mock.patch.object(
            core_module, "detect_clients", return_value=self.clients
        ), mock.patch.object(
            core_module.git,
            "run_git",
            side_effect=AssertionError("impact must not access Git"),
        ):
            result = edit_impact(session["session_id"], config_path=self.config_path)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            (metadata_path.read_bytes(), metadata_path.stat().st_mtime_ns),
            metadata_before,
        )
        self.assertFalse(result["stale_baseline"])
        self.assertTrue(result["has_workspace_changes"])
        self.assertEqual([row["client"] for row in result["clients"]], [
            "codex", "workbuddy", "kimi-code", "kimi-desktop", "claude-code"
        ])
        rows = {row["client"]: row for row in result["clients"]}
        self.assertEqual(rows["codex"]["current_deployment_state"], "valid")
        self.assertEqual(rows["codex"]["current_link_state"], "linked-render")
        self.assertEqual(rows["codex"]["action"], "rebuild")
        self.assertEqual(rows["workbuddy"]["availability"], "undetected")
        self.assertEqual(rows["workbuddy"]["action"], "undetected")
        self.assertEqual(rows["kimi-code"]["availability"], "disabled")
        self.assertEqual(rows["kimi-desktop"]["availability"], "disabled")
        self.assertEqual(rows["claude-code"]["availability"], "available")
        self.assertTrue(all(row["affected"] for row in rows.values()))
        self.assertTrue(all(row["deployment_would_change"] for row in rows.values()))
        self.assertTrue(
            all(not Path(row["proposed_deployment_path"]).exists() for row in rows.values())
        )
        families = {row["agent"]: row for row in result["families"]}
        self.assertEqual(families["kimi"]["clients"], ["kimi-code", "kimi-desktop"])
        self.assertFalse(families["kimi"]["enabled"])

    def test_impact_marks_canonical_changes_as_stale_baseline(self):
        session = edit_begin("alpha", config_path=self.config_path)
        baseline_hash = session["baseline_hash"]
        (self.skill / "SKILL.md").write_text(
            "# canonical changed during session\n", encoding="utf-8"
        )

        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            result = edit_impact(session["session_id"], config_path=self.config_path)

        self.assertTrue(result["stale_baseline"])
        self.assertEqual(result["baseline_hash"], baseline_hash)
        self.assertNotEqual(result["current_hash"], baseline_hash)
        self.assertEqual(result["workspace_hash"], baseline_hash)
        self.assertFalse(result["has_workspace_changes"])
        self.assertTrue(all(row["deployment_would_change"] for row in result["clients"]))
        self.assertTrue(result["blocked"])
        self.assertEqual(
            result["blocked_reason"], "canonical-changed-since-begin"
        )
        enabled_detected = [
            row for row in result["clients"] if row["enabled"] and row["detected"]
        ]
        self.assertTrue(enabled_detected)
        self.assertTrue(all(row["action"] == "blocked" for row in enabled_detected))
        self.assertTrue(
            all(row["blocked_reason"] == "stale-baseline" for row in enabled_detected)
        )

    def test_impact_honors_registry_targets_and_reports_healthy_noop(self):
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    "alpha": {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": "alpha",
                        "targets": "codex",
                    }
                },
            },
        )
        self.install_current_deployment(self.clients[0])
        session = edit_begin("alpha", config_path=self.config_path)
        before = self.snapshot()

        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            result = edit_impact(session["session_id"], config_path=self.config_path)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["registry_targets"], ["codex"])
        self.assertEqual(len(result["clients"]), 1)
        row = result["clients"][0]
        self.assertEqual(row["client"], "codex")
        self.assertEqual(row["action"], "noop")
        self.assertFalse(row["affected"])
        self.assertFalse(row["deployment_would_change"])
        self.assertFalse(row["requires_rebuild"])
        self.assertEqual(result["summary"]["affected"], 0)
        self.assertEqual(result["summary"]["requires_rebuild"], 0)
        self.assertFalse(result["blocked"])
        self.assertIsNone(result["blocked_reason"])

    def test_impact_rejects_terminal_and_corrupt_sessions(self):
        session = edit_begin("alpha", config_path=self.config_path)
        edit_abort(session["session_id"], config_path=self.config_path)

        with self.assertRaises(SkillSyncError) as terminal:
            edit_impact(session["session_id"], config_path=self.config_path)
        self.assertEqual(terminal.exception.code, "edit_session_not_active")
        self.assertEqual(terminal.exception.exit_code, 4)

        second = edit_begin("alpha", config_path=self.config_path)
        metadata_path = EditSessionStore(self.data_root).paths(
            second["session_id"]
        ).metadata
        metadata_path.write_bytes(b'{"schema_version":')
        corrupted = metadata_path.read_bytes()

        with self.assertRaises(SkillSyncError) as invalid:
            edit_impact(second["session_id"], config_path=self.config_path)
        self.assertEqual(invalid.exception.code, "unsafe_edit_session")
        self.assertEqual(invalid.exception.exit_code, 4)
        self.assertEqual(metadata_path.read_bytes(), corrupted)

    def test_impact_reports_missing_workspace_as_incomplete_safety_error(self):
        session = edit_begin("alpha", config_path=self.config_path)
        workspace = Path(session["workspace_path"])
        shutil.rmtree(workspace)

        with self.assertRaises(SkillSyncError) as raised:
            edit_impact(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_session_incomplete")
        self.assertEqual(raised.exception.exit_code, 4)

    def test_impact_treats_missing_metadata_in_existing_session_as_unsafe(self):
        session = edit_begin("alpha", config_path=self.config_path)
        metadata = EditSessionStore(self.data_root).paths(session["session_id"]).metadata
        metadata.unlink()

        with self.assertRaises(SkillSyncError) as raised:
            edit_impact(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "unsafe_edit_session")
        self.assertEqual(raised.exception.exit_code, 4)

    def test_impact_rejects_missing_or_tampered_baseline_snapshot(self):
        session = edit_begin("alpha", config_path=self.config_path)
        baseline = Path(session["baseline_path"])
        baseline.rename(baseline.parent / "missing-baseline")

        with self.assertRaises(SkillSyncError) as missing:
            edit_impact(session["session_id"], config_path=self.config_path)
        self.assertEqual(missing.exception.code, "unsafe_edit_baseline")
        self.assertEqual(missing.exception.exit_code, 4)

    def test_impact_rejects_baseline_hash_mismatch_without_rewriting_it(self):
        session = edit_begin("alpha", config_path=self.config_path)
        skill_file = Path(session["baseline_path"]) / "SKILL.md"
        os.chmod(skill_file, 0o600)
        skill_file.write_text("# tampered baseline\n", encoding="utf-8")
        tampered = skill_file.read_bytes()

        with self.assertRaises(SkillSyncError) as raised:
            edit_impact(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "unsafe_edit_baseline")
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(skill_file.read_bytes(), tampered)

    def test_impact_rejects_linked_baseline_snapshot(self):
        session = edit_begin("alpha", config_path=self.config_path)
        baseline = Path(session["baseline_path"])
        displaced = baseline.parent / "displaced-baseline"
        baseline.rename(displaced)
        baseline.symlink_to(displaced, target_is_directory=True)

        with self.assertRaises(SkillSyncError) as raised:
            edit_impact(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "unsafe_edit_baseline")
        self.assertEqual(raised.exception.exit_code, 4)


class EditImpactCliTest(unittest.TestCase):
    def test_edit_impact_uses_shared_json_envelope(self):
        result = {
            "session_id": "12345678-1234-4234-9234-123456789abc",
            "skill": "alpha",
            "stale_baseline": False,
            "summary": {"affected": 1, "requires_rebuild": 1},
            "clients": [],
        }
        with mock.patch.object(cli.core, "edit_impact", return_value=result) as impact:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "edit",
                    "impact",
                    result["session_id"],
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        impact.assert_called_once_with(
            result["session_id"], config_path="/tmp/config.json"
        )
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "command": "edit impact",
                "ok": True,
                "result": result,
                "warnings": [],
                "errors": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
