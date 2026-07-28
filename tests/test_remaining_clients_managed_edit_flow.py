"""Reproduce managed Skill edits from Claude Code and Kimi Code."""

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


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
CLIENT_CASES = (
    ("claude-code", "claude", ("claude-code",)),
    ("kimi-code", "kimi", ("kimi-code",)),
)


class RemainingClientsManagedEditFlowTest(unittest.TestCase):
    """Run the complete manager-prescribed flow with real client path semantics."""

    @staticmethod
    def tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def run_case(
        self,
        client_id: str,
        family_id: str,
        deployed_clients: tuple[str, ...],
    ) -> None:
        fixture = json.loads(
            (FIXTURE_ROOT / client_id / "managed-edit-flow.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(fixture["client"], client_id)
        self.assertEqual(
            fixture["command_sequence"],
            [
                "managed check",
                "edit begin",
                "edit diff",
                "edit validate",
                "edit impact",
                "edit apply",
            ],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            repo = root / "repo"
            repo.mkdir()
            skills_root = home / ".agents" / "skills"
            skill = skills_root / fixture["skill"]
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                fixture["initial_skill_md"], encoding="utf-8"
            )
            (skill / "references").mkdir()
            (skill / "references" / f"{client_id}.txt").write_text(
                f"{client_id} fixture resource\n", encoding="utf-8"
            )

            data_root = root / "data"
            config_path = root / "config.json"
            config = empty_config()
            disabled = [
                name
                for name in ("codex", "workbuddy", "kimi", "claude")
                if name != family_id
            ]
            config.update(
                {
                    "sync_repo_path": str(repo),
                    "skills_root": str(skills_root),
                    "data_root": str(data_root),
                    "disabled_agents": disabled,
                    "skills": {fixture["skill"]: {"local_path": str(skill)}},
                }
            )
            save_config(config_path, config)
            save_registry(
                repo / "registry.yaml",
                {
                    "version": 1,
                    "skills": {
                        fixture["skill"]: {
                            "selected": True,
                            "source_platform": "global",
                            "display_name": fixture["skill"],
                            "targets": family_id,
                        }
                    },
                },
            )

            client_roots = {
                "claude-code": home / ".claude" / "skills",
                "kimi-code": home / ".config" / "agents" / "skills",
            }
            environment = {
                "HOME": str(home),
                "CODEX_HOME": str(root / "missing-codex"),
                "WORKBUDDY_HOME": str(root / "missing-workbuddy"),
                "CLAUDE_HOME": str(home / ".claude"),
                "KIMI_CODE_SKILLS_DIR": str(client_roots["kimi-code"]),
            }
            links: dict[str, Path] = {}
            original_deployments: dict[str, Path] = {}
            for target_client in deployed_clients:
                deployed = render_base_deployment(
                    skill,
                    data_root / "rendered",
                    fixture["skill"],
                    target_client,
                )
                original_deployments[target_client] = deployed.path
                link = client_roots[target_client] / fixture["skill"]
                create_directory_link(deployed.path, link)
                links[target_client] = link

            def managed_surfaces() -> dict[str, object]:
                return {
                    "canonical_hash": hash_skill_dir(skill),
                    "canonical_files": self.tree_bytes(skill),
                    "deployments": {
                        key: self.tree_bytes(path)
                        for key, path in original_deployments.items()
                    },
                    "links": {
                        key: str(path.resolve()) for key, path in links.items()
                    },
                }

            command_log: list[str] = []

            def run_json(*arguments: str) -> dict[str, object]:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    code = cli.main(
                        ["--config", str(config_path), *arguments, "--json"]
                    )
                self.assertEqual(code, 0, stderr.getvalue())
                self.assertEqual(stderr.getvalue(), "")
                envelope = json.loads(stdout.getvalue())
                self.assertEqual(envelope["schema_version"], 1)
                self.assertTrue(envelope["ok"])
                self.assertEqual(envelope["errors"], [])
                command_log.append(envelope["command"])
                return envelope["result"]

            target_path = links[client_id] / "SKILL.md"
            before = managed_surfaces()
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                core_module.git,
                "run_git",
                side_effect=AssertionError(
                    f"the {client_id} managed edit flow must not access Git"
                ),
            ) as git_command:
                ownership = run_json(
                    "managed", "check", str(target_path), "--client", client_id
                )
                self.assertTrue(ownership["managed"])
                self.assertTrue(ownership["healthy"])
                self.assertEqual(ownership["client"], client_id)
                self.assertEqual(ownership["role"], "rendered-deployment-link")
                self.assertEqual(managed_surfaces(), before)

                begun = run_json(
                    "edit",
                    "begin",
                    fixture["skill"],
                    "--base",
                    "--actor",
                    fixture["actor"],
                )
                workspace = Path(begun["workspace_path"])
                self.assertEqual(begun["actor"], client_id)
                self.assertNotEqual(workspace, skill)
                self.assertEqual(managed_surfaces(), before)

                self.assertEqual(fixture["only_agent_write"], "$WORKSPACE/SKILL.md")
                (workspace / "SKILL.md").write_text(
                    fixture["edited_skill_md"], encoding="utf-8"
                )
                self.assertEqual(managed_surfaces(), before)

                diff = run_json("edit", "diff", begun["session_id"])
                self.assertEqual(diff["summary"]["modified"], 1)
                self.assertEqual(managed_surfaces(), before)

                validation = run_json("edit", "validate", begun["session_id"])
                self.assertTrue(validation["valid"])
                self.assertTrue(validation["changed"])
                self.assertEqual(managed_surfaces(), before)

                impact = run_json("edit", "impact", begun["session_id"])
                self.assertFalse(impact["blocked"])
                self.assertEqual(impact["registry_targets"], [family_id])
                actions = {
                    row["client"]: row["action"] for row in impact["clients"]
                }
                self.assertEqual(
                    {key: actions[key] for key in deployed_clients},
                    {key: "rebuild" for key in deployed_clients},
                )
                self.assertEqual(managed_surfaces(), before)

                applied = run_json("edit", "apply", begun["session_id"])
                self.assertEqual(applied["status"], "applied")
                self.assertEqual(applied["clients_relinked"], len(deployed_clients))
                self.assertEqual(len(applied["deployments"]), len(deployed_clients))
                git_command.assert_not_called()

            self.assertEqual(command_log, fixture["command_sequence"])
            self.assertEqual(
                (skill / "SKILL.md").read_text(encoding="utf-8"),
                fixture["edited_skill_md"],
            )
            for target_client in deployed_clients:
                self.assertEqual(
                    self.tree_bytes(original_deployments[target_client]),
                    before["deployments"][target_client],
                )
                self.assertNotEqual(
                    str(links[target_client].resolve()),
                    before["links"][target_client],
                )
                current = links[target_client].resolve()
                verification = verify_deployment(current)
                self.assertTrue(verification.ok)
                self.assertEqual(
                    verification.provenance["target_client"], target_client
                )
                self.assertEqual(
                    (current / "SKILL.md").read_text(encoding="utf-8"),
                    fixture["edited_skill_md"],
                )

    def test_claude_code_uses_the_managed_workspace_flow(self) -> None:
        self.run_case(*CLIENT_CASES[0])

    def test_kimi_code_updates_its_family_endpoint_only_after_apply(self) -> None:
        self.run_case(*CLIENT_CASES[1])


if __name__ == "__main__":
    unittest.main()
