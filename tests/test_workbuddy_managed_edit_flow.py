"""Reproduce the managed Skill editing flow initiated by WorkBuddy."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync import cli
import skill_sync.core as core_module
from skill_sync.config import empty_config, save_config
from skill_sync.deployment import render_base_deployment, verify_deployment
from skill_sync.hash import hash_skill_dir
from skill_sync.linking import create_directory_link
from skill_sync.registry import save_registry


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "workbuddy" / "managed-edit-flow.json"
)


class WorkBuddyManagedEditFlowTest(unittest.TestCase):
    """Run the exact check -> workspace -> review -> apply WorkBuddy sequence."""

    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.workbuddy_home = self.home / ".workbuddy"
        self.workbuddy_home.mkdir(parents=True)

        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.home / ".agents" / "skills"
        self.skill = self.skills_root / self.fixture["skill"]
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            self.fixture["initial_skill_md"], encoding="utf-8"
        )
        (self.skill / "references").mkdir()
        (self.skill / "references" / "workbuddy.txt").write_text(
            "WorkBuddy fixture resource\n", encoding="utf-8"
        )

        self.data_root = self.root / "data"
        self.config_path = self.root / "config.json"
        config = empty_config()
        config.update(
            {
                "sync_repo_path": str(self.repo),
                "skills_root": str(self.skills_root),
                "data_root": str(self.data_root),
                "disabled_agents": ["codex", "kimi", "claude"],
                "skills": {self.fixture["skill"]: {"local_path": str(self.skill)}},
            }
        )
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    self.fixture["skill"]: {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": self.fixture["skill"],
                        "targets": "workbuddy",
                    }
                },
            },
        )

        deployed = render_base_deployment(
            self.skill,
            self.data_root / "rendered",
            self.fixture["skill"],
            "workbuddy",
        )
        self.original_deployment = deployed.path
        self.workbuddy_link = (
            self.workbuddy_home / "skills" / self.fixture["skill"]
        )
        create_directory_link(self.original_deployment, self.workbuddy_link)
        self.target_path = self.workbuddy_link / "SKILL.md"
        self.command_log: list[str] = []

        self.environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.root / "missing-codex"),
            "WORKBUDDY_HOME": str(self.workbuddy_home),
            "KIMI_CODE_SKILLS_DIR": str(self.root / "missing-kimi-code"),
            "CLAUDE_HOME": str(self.root / "missing-claude"),
        }

    def run_json(self, *arguments: str) -> dict[str, object]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(
                ["--config", str(self.config_path), *arguments, "--json"]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["schema_version"], 1)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["errors"], [])
        self.command_log.append(envelope["command"])
        return envelope["result"]

    @staticmethod
    def tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def managed_surfaces(self) -> dict[str, object]:
        return {
            "canonical_hash": hash_skill_dir(self.skill),
            "canonical_files": self.tree_bytes(self.skill),
            "deployment_files": self.tree_bytes(self.original_deployment),
            "workbuddy_link_target": str(self.workbuddy_link.resolve()),
        }

    def test_workbuddy_uses_managed_check_and_the_complete_workspace_flow(self) -> None:
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["client"], "workbuddy")
        self.assertEqual(self.fixture["actor"], "workbuddy")
        self.assertEqual(
            self.fixture["target_path"],
            "$WORKBUDDY_HOME/skills/alpha/SKILL.md",
        )
        self.assertEqual(self.fixture["only_agent_write"], "$WORKSPACE/SKILL.md")
        self.assertEqual(
            self.fixture["forbidden_agent_writes"],
            [
                "$CANONICAL/**",
                "$DEPLOYMENT/**",
                "$WORKBUDDY_HOME/skills/alpha/**",
            ],
        )
        self.assertEqual(
            self.fixture["forbidden_implicit_actions"],
            ["git commit", "skill-sync push", "git push"],
        )
        before = self.managed_surfaces()

        with mock.patch.dict(os.environ, self.environment, clear=False), mock.patch.object(
            core_module.git,
            "run_git",
            side_effect=AssertionError(
                "the WorkBuddy managed edit flow must not commit, fetch, or push"
            ),
        ) as git_command:
            ownership = self.run_json(
                "managed",
                "check",
                str(self.target_path),
                "--client",
                self.fixture["client"],
            )
            self.assertTrue(ownership["managed"])
            self.assertTrue(ownership["healthy"])
            self.assertEqual(ownership["client"], "workbuddy")
            self.assertEqual(ownership["role"], "rendered-deployment-link")
            self.assertEqual(self.managed_surfaces(), before)

            begun = self.run_json(
                "edit",
                "begin",
                self.fixture["skill"],
                "--base",
                "--actor",
                self.fixture["actor"],
            )
            self.assertEqual(begun["actor"], "workbuddy")
            workspace = Path(begun["workspace_path"])
            self.assertTrue(workspace.is_absolute())
            self.assertNotEqual(workspace, self.skill)
            self.assertFalse(workspace.is_relative_to(self.original_deployment))
            self.assertFalse(workspace.is_relative_to(self.workbuddy_home))
            self.assertEqual(self.managed_surfaces(), before)

            # This is the only write performed on behalf of WorkBuddy. The CLI
            # owns the later transactional source and deployment replacement.
            workspace_skill = workspace / "SKILL.md"
            workspace_skill.write_text(
                self.fixture["edited_skill_md"], encoding="utf-8"
            )
            self.assertEqual(self.managed_surfaces(), before)

            diff = self.run_json("edit", "diff", begun["session_id"])
            self.assertEqual(diff["summary"]["modified"], 1)
            self.assertEqual(self.managed_surfaces(), before)

            validation = self.run_json(
                "edit", "validate", begun["session_id"]
            )
            self.assertTrue(validation["valid"])
            self.assertTrue(validation["changed"])
            self.assertEqual(self.managed_surfaces(), before)

            impact = self.run_json("edit", "impact", begun["session_id"])
            self.assertFalse(impact["blocked"])
            self.assertEqual(impact["registry_targets"], ["workbuddy"])
            workbuddy = next(
                row
                for row in impact["clients"]
                if row["client"] == "workbuddy"
            )
            self.assertEqual(workbuddy["action"], "rebuild")
            self.assertEqual(self.managed_surfaces(), before)

            applied = self.run_json("edit", "apply", begun["session_id"])
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["clients_relinked"], 1)
            self.assertEqual(len(applied["deployments"]), 1)
            self.assertEqual(applied["deployments"][0]["client"], "workbuddy")
            git_command.assert_not_called()

        self.assertEqual(self.command_log, self.fixture["command_sequence"])
        self.assertEqual(
            (self.skill / "SKILL.md").read_text(encoding="utf-8"),
            self.fixture["edited_skill_md"],
        )
        self.assertEqual(
            self.tree_bytes(self.original_deployment), before["deployment_files"]
        )
        self.assertNotEqual(
            str(self.workbuddy_link.resolve()), before["workbuddy_link_target"]
        )
        current_deployment = self.workbuddy_link.resolve()
        verification = verify_deployment(current_deployment)
        self.assertTrue(verification.ok)
        self.assertEqual(verification.provenance["target_client"], "workbuddy")
        self.assertEqual(
            (current_deployment / "SKILL.md").read_text(encoding="utf-8"),
            self.fixture["edited_skill_md"],
        )


if __name__ == "__main__":
    unittest.main()
