"""Installed-wheel end-to-end coverage for the managed edit CLI."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDIT_COMMANDS = (
    "list",
    "status",
    "begin",
    "abort",
    "diff",
    "validate",
    "impact",
    "apply",
    "recover",
)


class InstalledEditCliTest(unittest.TestCase):
    """Exercise the console entry point from an installed wheel, not the checkout."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.installation = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.installation.cleanup)
        root = Path(cls.installation.name)
        source = root / "source"
        shutil.copytree(
            PROJECT_ROOT,
            source,
            ignore=shutil.ignore_patterns(
                ".git", ".pytest_cache", "__pycache__", "build", "dist", "*.egg-info"
            ),
        )
        wheels = root / "wheels"
        wheels.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheels),
                str(source),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheel = next(wheels.glob("*.whl"))
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(wheel),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cls.executable = environment / (
            "Scripts/skill-sync.exe" if os.name == "nt" else "bin/skill-sync"
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        (self.codex_home / "skills").mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.skill = self.skills_root / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Original\n---\n\n# Alpha\n",
            encoding="utf-8",
        )
        self.data_root = self.root / "data"
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "sync_repo_path": str(self.repo),
                    "platform": "codex",
                    "skills_root": str(self.skills_root),
                    "branch": "main",
                    "data_root": str(self.data_root),
                    "disabled_agents": ["workbuddy", "kimi", "claude"],
                    "skills": {"alpha": {"local_path": str(self.skill)}},
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "registry.yaml").write_text(
            "version: 1\nskills:\n  alpha:\n    selected: true\n"
            "    source_platform: global\n    display_name: alpha\n",
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "PATH": str(self.executable.parent),
                "PYTHONNOUSERSITE": "1",
            }
        )
        self.env.pop("PYTHONPATH", None)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), *arguments],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

    def run_json(self, *arguments: str) -> dict[str, object]:
        completed = self.run_cli(
            "--config", str(self.config_path), *arguments, "--json"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        envelope = json.loads(completed.stdout)
        self.assertEqual(
            list(envelope),
            ["command", "errors", "ok", "result", "schema_version", "warnings"],
        )
        self.assertEqual(envelope["schema_version"], 1)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["warnings"], [])
        self.assertEqual(envelope["errors"], [])
        return envelope

    def test_installed_wheel_exposes_help_for_every_edit_command(self) -> None:
        completed = self.run_cli("edit", "--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("isolated workspaces", completed.stdout)
        self.assertIn("never runs", completed.stdout)
        self.assertIn("Git commit or push", completed.stdout)
        for command in EDIT_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, completed.stdout)
                detail = self.run_cli("edit", command, "--help")
                self.assertEqual(detail.returncode, 0, detail.stderr)
                self.assertIn(f"skill-sync edit {command}", detail.stdout)

    def test_installed_json_workflow_covers_every_edit_command(self) -> None:
        empty = self.run_json("edit", "list")
        self.assertEqual(empty["command"], "edit list")
        self.assertEqual(empty["result"], {"sessions": []})

        begun = self.run_json("edit", "begin", "alpha", "--base", "--actor", "codex")
        self.assertEqual(begun["command"], "edit begin")
        session_id = begun["result"]["session_id"]
        workspace = Path(begun["result"]["workspace_path"])
        self.assertEqual(begun["result"]["status"], "active")

        listed = self.run_json("edit", "list")
        self.assertEqual(listed["result"]["sessions"][0]["session_id"], session_id)
        status = self.run_json("edit", "status", session_id)
        self.assertEqual(status["command"], "edit status")
        self.assertEqual(status["result"]["logical_skill"], "alpha")

        (workspace / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Edited\n---\n\n# Alpha edited\n",
            encoding="utf-8",
        )
        diff = self.run_json("edit", "diff", session_id)
        self.assertEqual(diff["command"], "edit diff")
        self.assertEqual(diff["result"]["summary"]["modified"], 1)
        validated = self.run_json("edit", "validate", session_id)
        self.assertEqual(validated["result"]["valid"], True)
        self.assertEqual(validated["result"]["changed"], True)
        impact = self.run_json("edit", "impact", session_id)
        self.assertEqual(impact["command"], "edit impact")
        codex = next(row for row in impact["result"]["clients"] if row["client"] == "codex")
        self.assertEqual(codex["action"], "rebuild")

        applied = self.run_json("edit", "apply", session_id)
        self.assertEqual(applied["command"], "edit apply")
        self.assertEqual(applied["result"]["status"], "applied")
        self.assertEqual(applied["result"]["clients_relinked"], 1)
        self.assertEqual(len(applied["result"]["deployments"]), 1)

        deployment = Path(applied["result"]["deployments"][0]["path"])
        deployment.chmod(stat.S_IMODE(deployment.stat().st_mode) | stat.S_IWUSR)
        skill_file = deployment / "SKILL.md"
        skill_file.chmod(stat.S_IMODE(skill_file.stat().st_mode) | stat.S_IWUSR)
        skill_file.write_text(
            "---\nname: alpha\ndescription: Tampered\n---\n\n# Tampered\n",
            encoding="utf-8",
        )
        recovered = self.run_json("edit", "recover", "alpha", "--client", "codex")
        self.assertEqual(recovered["command"], "edit recover")
        self.assertEqual(recovered["result"]["action"], "preview")
        self.assertEqual(recovered["result"]["allowed_actions"], ["capture", "discard"])

        second = self.run_json("edit", "begin", "alpha", "--base")
        aborted = self.run_json("edit", "abort", second["result"]["session_id"])
        self.assertEqual(aborted["command"], "edit abort")
        self.assertEqual(aborted["result"]["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
