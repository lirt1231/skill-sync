import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import skill_sync.cli as cli
    from skill_sync.errors import SkillSyncError
except ImportError as exc:  # pragma: no cover - exercised by initial TDD red run
    if "skill_sync.cli" not in str(exc):
        raise
    cli = None
    SkillSyncError = Exception


def run_cli(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        if cli is None:
            self.fail("skill_sync.cli module is missing")

    def test_init_dispatches_arguments_and_prints_text_summary(self):
        with mock.patch.object(
            cli.core,
            "init_sync",
            return_value={
                "sync_repo_path": "/tmp/sync",
                "branch": "dev",
                "platform": "codex",
                "registry_path": "/tmp/sync/registry.yaml",
            },
        ) as init_sync:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "init",
                    "--repo",
                    "git@example.test:skills.git",
                    "--sync-dir",
                    "/tmp/sync",
                    "--branch",
                    "dev",
                    "--platform",
                    "codex",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        init_sync.assert_called_once_with(
            "git@example.test:skills.git",
            sync_dir="/tmp/sync",
            branch="dev",
            platform="codex",
            config_path="/tmp/config.json",
        )
        self.assertIn("Initialized", stdout)
        self.assertIn("/tmp/sync", stdout)
        self.assertIn("dev", stdout)

    def test_scan_supports_text_and_json_output(self):
        candidates = [
            {"name": "alpha", "path": "/skills/alpha", "selected": True, "external": False},
            {"name": "beta", "path": "/other/beta", "selected": False, "external": True},
        ]
        with mock.patch.object(cli.core, "scan_skills", return_value=candidates) as scan_skills:
            code, stdout, stderr = run_cli(["--config", "/tmp/config.json", "scan", "--platform", "codex"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        scan_skills.assert_called_once_with(platform="codex", config_path="/tmp/config.json")
        self.assertIn("alpha", stdout)
        self.assertIn("selected", stdout)
        self.assertIn("external", stdout)

        with mock.patch.object(cli.core, "scan_skills", return_value=candidates):
            code, stdout, stderr = run_cli(["scan", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), candidates)
        self.assertLess(stdout.index('"external"'), stdout.index('"name"'))

    def test_select_and_deselect_dispatch_positional_names(self):
        with mock.patch.object(cli.core, "select_skills", return_value={"selected": ["alpha", "beta"]}) as select:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "select",
                    "alpha",
                    "/tmp/beta",
                    "--platform",
                    "codex",
                    "--allow-external",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        select.assert_called_once_with(
            ["alpha", "/tmp/beta"],
            platform="codex",
            allow_external=True,
            config_path="/tmp/config.json",
        )
        self.assertIn("Selected: alpha, beta", stdout)

        with mock.patch.object(cli.core, "deselect_skills", return_value={"deselected": ["alpha"]}) as deselect:
            code, stdout, stderr = run_cli(["--config", "/tmp/config.json", "deselect", "alpha"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        deselect.assert_called_once_with(["alpha"], config_path="/tmp/config.json")
        self.assertIn("Deselected: alpha", stdout)

    def test_import_dispatches_agent_and_skill_names(self):
        with mock.patch.object(
            cli.core,
            "import_agent_skills",
            return_value={"imported": [{"name": "alpha", "agent": "codex", "state": "imported"}]},
        ) as import_skills:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "import", "--agent", "codex", "alpha"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        import_skills.assert_called_once_with(["alpha"], "codex", config_path="/tmp/config.json")
        self.assertIn("Imported: alpha", stdout)

    def test_copy_dispatches_selected_skills_and_agent(self):
        with mock.patch.object(cli.core, "copy_global_skills_to_agents", return_value={"copied": [{}]}) as copy:
            code, stdout, stderr = run_cli(["copy", "--skill", "alpha", "--agent", "workbuddy"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        copy.assert_called_once_with(["alpha"], ["workbuddy"], config_path=None)
        self.assertIn("Copied: 1", stdout)

    def test_agent_disable_dispatches_to_core(self):
        with mock.patch.object(
            cli.core, "disable_agent_sync", return_value={"disabled": "kimi", "unlinked": []}
        ) as disable:
            code, stdout, stderr = run_cli(["agent", "disable", "kimi"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        disable.assert_called_once_with("kimi", config_path=None)
        self.assertIn("Disabled Agent sync: kimi", stdout)

    def test_repeated_skill_filters_dispatch_to_status_pull_push(self):
        status_result = {
            "schema_version": 1,
            "repo": {"path": "/repo", "branch": "main", "clean": True, "ahead": 0, "behind": 0, "diverged": False},
            "skills": [],
        }
        with mock.patch.object(cli.core, "status", return_value=status_result) as status:
            code, _, stderr = run_cli(["--config", "/tmp/config.json", "status", "--skill", "alpha", "--skill", "beta"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        status.assert_called_once_with(skill_names=["alpha", "beta"], config_path="/tmp/config.json")

        with mock.patch.object(cli.core, "pull", return_value={"pulled": ["alpha", "beta"]}) as pull:
            code, stdout, stderr = run_cli(["pull", "--skill", "alpha", "--skill", "beta"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        pull.assert_called_once_with(skill_names=["alpha", "beta"], config_path=None)
        self.assertIn("Pulled: alpha, beta", stdout)

        with mock.patch.object(cli.core, "push", return_value={"pushed": ["alpha", "beta"], "committed": True}) as push:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "push", "--skill", "alpha", "--skill", "beta", "--message", "sync two"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        push.assert_called_once_with(
            skill_names=["alpha", "beta"],
            config_path="/tmp/config.json",
            message="sync two",
        )
        self.assertIn("Pushed: alpha, beta", stdout)
        self.assertIn("committed", stdout)

    def test_preview_dispatches_without_network_refresh(self):
        with mock.patch.object(cli.core, "sync_preview", return_value={
            "action": "push", "summary": "Local changes are ready.", "initialized": True
        }) as preview:
            code, stdout, stderr = run_cli(["--config", "/tmp/config.json", "preview", "--skill", "alpha"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        preview.assert_called_once_with(skill_names=["alpha"], config_path="/tmp/config.json", fetch_remote=False)
        self.assertIn("push", stdout)

    def test_status_text_output_includes_repo_and_skill_basics(self):
        result = {
            "schema_version": 1,
            "repo": {
                "path": "/repo",
                "branch": "main",
                "clean": False,
                "ahead": 1,
                "behind": 2,
                "diverged": False,
            },
            "skills": [
                {
                    "name": "alpha",
                    "platform": "codex",
                    "local_path": "/skills/alpha",
                    "local_hash": "sha256:local",
                    "remote_hash": "sha256:remote",
                    "changed_local": True,
                    "selected": True,
                }
            ],
        }
        with mock.patch.object(cli.core, "status", return_value=result):
            code, stdout, stderr = run_cli(["status"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Repo: /repo", stdout)
        self.assertIn("branch main", stdout)
        self.assertIn("dirty", stdout)
        self.assertIn("alpha", stdout)
        self.assertIn("changed", stdout)
        self.assertIn("/skills/alpha", stdout)

    def test_status_json_golden_contract_for_filtered_output(self):
        result = {
            "schema_version": 1,
            "repo": {
                "path": "/repo",
                "branch": "main",
                "clean": True,
                "ahead": 0,
                "behind": 0,
                "diverged": False,
            },
            "skills": [
                {
                    "name": "alpha",
                    "platform": "codex",
                    "local_path": "/skills/alpha",
                    "local_hash": "sha256:aaaaaaaa",
                    "remote_hash": "sha256:bbbbbbbb",
                    "changed_local": False,
                    "selected": True,
                }
            ],
        }
        with mock.patch.object(cli.core, "status", return_value=result) as status:
            code, stdout, stderr = run_cli(["status", "--json", "--skill", "alpha"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        status.assert_called_once_with(skill_names=["alpha"], config_path=None)
        parsed = json.loads(stdout)
        self.assertEqual(set(parsed), {"schema_version", "repo", "skills"})
        self.assertEqual(
            set(parsed["repo"]),
            {"path", "branch", "clean", "ahead", "behind", "diverged"},
        )
        self.assertEqual(
            set(parsed["skills"][0]),
            {
                "name",
                "platform",
                "local_path",
                "local_hash",
                "remote_hash",
                "changed_local",
                "selected",
            },
        )
        self.assertEqual(parsed, result)
        self.assertEqual(
            stdout,
            '{"repo": {"ahead": 0, "behind": 0, "branch": "main", "clean": true, "diverged": false, "path": "/repo"}, "schema_version": 1, "skills": [{"changed_local": false, "local_hash": "sha256:aaaaaaaa", "local_path": "/skills/alpha", "name": "alpha", "platform": "codex", "remote_hash": "sha256:bbbbbbbb", "selected": true}]}\n',
        )

    def test_sync_dispatches_and_reports_noop(self):
        with mock.patch.object(cli.core, "sync", return_value={"synced": [], "noop": True}) as sync:
            code, stdout, stderr = run_cli(["--config", "/tmp/config.json", "sync", "--skill", "alpha"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        sync.assert_called_once_with(skill_names=["alpha"], config_path="/tmp/config.json")
        self.assertIn("No changes", stdout)

    def test_user_facing_errors_return_one_and_print_to_stderr(self):
        with mock.patch.object(cli.core, "status", side_effect=SkillSyncError("not initialized")):
            code, stdout, stderr = run_cli(["status"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "error: not initialized\n")

    def test_argparse_errors_raise_system_exit_two(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["init"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("usage:", stderr.getvalue())

    def test_executable_shim_exists_and_invokes_cli_main(self):
        shim = Path(__file__).resolve().parents[1] / "skill-sync"
        self.assertTrue(shim.exists())
        self.assertTrue(os.access(shim, os.X_OK))
        text = shim.read_text(encoding="utf-8")
        self.assertIn("skill_sync.cli", text)
        self.assertIn("SystemExit(main())", text)


if __name__ == "__main__":
    unittest.main()
