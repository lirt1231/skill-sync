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
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "command": "scan",
                "ok": True,
                "result": candidates,
                "warnings": [],
                "errors": [],
            },
        )
        self.assertLess(stdout.index('"external"'), stdout.index('"name"'))

    def test_version_supports_text_and_json_output(self):
        code, stdout, stderr = run_cli(["version"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, f"skill-sync {cli.__version__}\n")

        code, stdout, stderr = run_cli(["version", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "command": "version",
                "ok": True,
                "result": {"version": cli.__version__},
                "warnings": [],
                "errors": [],
            },
        )

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

    def test_managed_check_dispatches_and_prints_human_guidance(self):
        result = {
            "managed": True,
            "healthy": False,
            "state": "broken-link",
            "role": "direct-source-link",
            "skill": "alpha",
            "input_path": "/clients/codex/skills/alpha/SKILL.md",
            "source_path": "/global/skills/alpha",
            "client": "codex",
            "migration_required": True,
        }
        with mock.patch.object(cli.core, "managed_check", return_value=result) as check:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "managed",
                    "check",
                    result["input_path"],
                    "--client",
                    "codex",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        check.assert_called_once_with(
            result["input_path"],
            client="codex",
            config_path="/tmp/config.json",
        )
        self.assertIn("Ownership: managed", stdout)
        self.assertIn("Health: unhealthy (broken-link)", stdout)
        self.assertIn("Source: /global/skills/alpha", stdout)
        self.assertIn("Client: codex", stdout)
        self.assertIn("Recommended action:", stdout)

    def test_managed_check_json_uses_full_command_name_envelope(self):
        result = {
            "managed": False,
            "healthy": True,
            "state": "unmanaged",
            "role": "unmanaged",
            "skill": None,
            "input_path": "/project/.agents/skills/alpha/SKILL.md",
            "source_path": None,
            "client": None,
            "migration_required": False,
        }
        with mock.patch.object(cli.core, "managed_check", return_value=result):
            code, stdout, stderr = run_cli(
                ["managed", "check", result["input_path"], "--json"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "managed check")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"], result)

    def test_managed_check_ambiguous_json_is_structured_safety_error(self):
        inspection = {
            "managed": False,
            "healthy": False,
            "state": "ambiguous",
            "role": "unknown",
            "skill": None,
            "input_path": "unknown",
            "source_path": None,
            "client": None,
            "migration_required": False,
        }
        error = SkillSyncError(
            "Skill ownership is ambiguous",
            code="ownership_ambiguous",
            exit_code=4,
            details={"inspection": inspection},
        )
        with mock.patch.object(cli.core, "managed_check", side_effect=error):
            code, stdout, stderr = run_cli(
                ["managed", "check", "unknown", "--json"]
            )

        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        envelope = json.loads(stderr)
        self.assertEqual(envelope["command"], "managed check")
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["errors"][0]["code"], "ownership_ambiguous")
        self.assertEqual(
            envelope["errors"][0]["details"]["inspection"], inspection
        )

    def test_edit_begin_dispatches_base_session_and_uses_shared_json_envelope(self):
        result = {
            "session_id": "4f92500f-832f-40f7-a417-c474f0425ce0",
            "skill": "alpha",
            "scope": "base",
            "status": "active",
            "actor": "codex",
            "baseline_hash": "sha256:" + "a" * 64,
            "baseline_path": "/data/edit-sessions/id/baseline",
            "workspace_path": "/data/edit-sessions/id/workspace",
        }
        with mock.patch.object(cli.core, "edit_begin", return_value=result) as begin:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "edit",
                    "begin",
                    "alpha",
                    "--base",
                    "--actor",
                    "codex",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        begin.assert_called_once_with(
            "alpha",
            scope="base",
            target=None,
            actor="codex",
            config_path="/tmp/config.json",
        )
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 1,
                "command": "edit begin",
                "ok": True,
                "result": result,
                "warnings": [],
                "errors": [],
            },
        )

    def test_edit_begin_dispatches_family_and_client_scopes(self):
        result = {
            "session_id": "4f92500f-832f-40f7-a417-c474f0425ce0",
            "skill": "alpha",
            "scope": "family",
            "target": "kimi",
            "status": "active",
            "actor": None,
            "baseline_hash": "sha256:" + "a" * 64,
            "baseline_path": "/data/edit-sessions/id/baseline",
            "workspace_path": "/data/edit-sessions/id/workspace",
            "affected_clients": ["kimi-code"],
            "layer_baseline": {"state": "absent", "hash": None},
        }
        for option, scope, target in (
            ("--family", "family", "kimi"),
            ("--client", "client", "kimi-code"),
        ):
            with self.subTest(option=option), mock.patch.object(
                cli.core, "edit_begin", return_value={**result, "scope": scope, "target": target}
            ) as begin:
                code, stdout, stderr = run_cli(
                    ["edit", "begin", "alpha", option, target, "--json"]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            begin.assert_called_once_with(
                "alpha",
                scope=scope,
                target=target,
                actor=None,
                config_path=None,
            )
            self.assertEqual(json.loads(stdout)["result"]["target"], target)

    def test_edit_begin_requires_exactly_one_scope(self):
        parser = cli._build_parser()
        for arguments in (
            ["edit", "begin", "alpha"],
            ["edit", "begin", "alpha", "--base", "--client", "codex"],
        ):
            stderr = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                parser.parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("skill-sync edit begin", stderr.getvalue())

    def test_edit_diff_dispatches_resolved_client_filter(self):
        session_id = "4f92500f-832f-40f7-a417-c474f0425ce0"
        result = {
            "session_id": session_id,
            "skill": "alpha",
            "scope": "family",
            "target": "kimi",
            "status": "active",
            "changed": False,
            "summary": {"added": 0, "modified": 0, "deleted": 0, "total": 0},
            "files": [],
            "resolved_diffs": [],
        }
        with mock.patch.object(cli.core, "edit_diff", return_value=result) as diff:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "edit",
                    "diff",
                    session_id,
                    "--resolved-client",
                    "kimi-code",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        diff.assert_called_once_with(
            session_id,
            resolved_client="kimi-code",
            config_path="/tmp/config.json",
        )
        self.assertEqual(json.loads(stdout)["result"], result)

    def test_edit_abort_dispatches_and_prints_human_summary(self):
        session_id = "4f92500f-832f-40f7-a417-c474f0425ce0"
        result = {
            "session_id": session_id,
            "skill": "alpha",
            "scope": "base",
            "status": "aborted",
        }
        with mock.patch.object(cli.core, "edit_abort", return_value=result) as abort:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "edit", "abort", session_id]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        abort.assert_called_once_with(session_id, config_path="/tmp/config.json")
        self.assertIn(f"Aborted edit session: {session_id}", stdout)

    def test_edit_begin_json_conflict_uses_full_command_name_and_exit_three(self):
        error = SkillSyncError(
            "already active",
            code="active_edit_session",
            exit_code=3,
            details={"skill": "alpha", "session_id": "existing"},
        )
        with mock.patch.object(cli.core, "edit_begin", side_effect=error):
            code, stdout, stderr = run_cli(
                ["edit", "begin", "alpha", "--base", "--json"]
            )

        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["command"], "edit begin")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["code"], "active_edit_session")

    def test_edit_recover_dispatches_preview_and_requires_explicit_single_action(self):
        preview = {
            "skill": "alpha",
            "client": "codex",
            "state": "tampered-render",
            "action": "preview",
            "canonical_path": "/skills/alpha",
            "canonical_hash": "sha256:" + "1" * 64,
            "deployment_path": "/rendered/hash/alpha",
            "tampered_authored_hash": "sha256:" + "2" * 64,
            "diff": {
                "changed": True,
                "summary": {"added": 0, "modified": 1, "deleted": 0, "total": 1},
                "files": [
                    {
                        "path": "SKILL.md",
                        "change": "modified",
                        "kind": "text",
                        "diff": "--- a/SKILL.md\n+++ b/SKILL.md\n",
                    }
                ],
            },
            "allowed_actions": ["capture", "discard"],
            "blocked_by_session": None,
        }
        with mock.patch.object(
            cli.core, "edit_recover", return_value=preview
        ) as recover:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "edit",
                    "recover",
                    "alpha",
                    "--client",
                    "codex",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        recover.assert_called_once_with(
            "alpha", client="codex", action=None, config_path="/tmp/config.json"
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["command"], "edit recover")
        self.assertEqual(payload["result"]["allowed_actions"], ["capture", "discard"])

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli._build_parser().parse_args(
                    [
                        "edit",
                        "recover",
                        "alpha",
                        "--client",
                        "codex",
                        "--capture",
                        "--discard",
                    ]
                )
        self.assertEqual(raised.exception.code, 2)

    def test_deploy_preview_dispatches_and_prints_each_skill_client(self):
        result = {
            "rendered_root": "/data/rendered",
            "skills": [
                {
                    "name": "alpha",
                    "source_path": "/global/alpha",
                    "source_hash": "sha256:source",
                    "clients": [
                        {
                            "client": "codex",
                            "agent": "codex",
                            "destination": "/codex/alpha",
                            "deployment_path": "/data/rendered/hash/alpha",
                            "current_state": "direct-source-link",
                            "action": "migrate",
                        },
                        {
                            "client": "kimi-code",
                            "agent": "kimi",
                            "destination": "/kimi/alpha",
                            "deployment_path": "/data/rendered/hash/alpha",
                            "current_state": "missing",
                            "action": "link",
                        },
                    ],
                }
            ],
            "blocked": False,
        }
        with mock.patch.object(cli.core, "deploy_preview", return_value=result, create=True) as preview:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "deploy", "preview"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        preview.assert_called_once_with(config_path="/tmp/config.json")
        self.assertIn("alpha", stdout)
        self.assertIn("codex [codex]", stdout)
        self.assertIn("kimi-code [kimi]", stdout)
        self.assertIn("direct-source-link -> migrate", stdout)
        self.assertIn("/data/rendered/hash/alpha", stdout)

    def test_deploy_status_supports_text_and_json_envelope(self):
        result = {
            "rendered_root": "/data/rendered",
            "skills": [
                {
                    "name": "alpha",
                    "source_path": "/global/alpha",
                    "source_hash": "sha256:source",
                    "clients": [
                        {
                            "client": "workbuddy",
                            "agent": "workbuddy",
                            "destination": "/workbuddy/alpha",
                            "deployment_path": "/data/rendered/hash/alpha",
                            "deployment_state": "healthy",
                            "link_state": "managed-deployment",
                            "migration_required": False,
                        }
                    ],
                }
            ],
        }
        with mock.patch.object(cli.core, "deploy_status", return_value=result, create=True) as status:
            code, stdout, stderr = run_cli(["deploy", "status"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        status.assert_called_once_with(config_path=None)
        self.assertIn("alpha", stdout)
        self.assertIn("workbuddy [workbuddy]", stdout)
        self.assertIn("deployment healthy, link managed-deployment, current", stdout)

        with mock.patch.object(cli.core, "deploy_status", return_value=result, create=True):
            code, stdout, stderr = run_cli(["deploy", "status", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "deploy status")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"], result)

    def test_deploy_migrate_dispatches_and_summarizes_migrated_links(self):
        result = {
            "rendered_root": "/data/rendered",
            "migrated": [
                {
                    "skill": "alpha",
                    "client": "codex",
                    "from": "/global/alpha",
                    "to": "/data/rendered/hash/alpha",
                    "state": "migrated",
                }
            ],
            "deployments": [],
            "noop": False,
        }
        with mock.patch.object(cli.core, "deploy_migrate", return_value=result, create=True) as migrate:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "deploy", "migrate"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        migrate.assert_called_once_with(config_path="/tmp/config.json")
        self.assertIn("Migrated: 1 Skill/client links", stdout)
        self.assertIn("alpha / codex: migrated", stdout)
        self.assertIn("/global/alpha -> /data/rendered/hash/alpha", stdout)

    def test_deploy_migrate_reports_noop(self):
        result = {
            "rendered_root": "/data/rendered",
            "migrated": [],
            "deployments": [],
            "noop": True,
        }
        with mock.patch.object(cli.core, "deploy_migrate", return_value=result, create=True):
            code, stdout, stderr = run_cli(["deploy", "migrate"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("No deployment migrations needed.", stdout)

    def test_deploy_gc_supports_dry_run_json_envelope(self):
        result = {
            "rendered_root": "/data/rendered",
            "dry_run": True,
            "candidates": ["/data/rendered/hash/alpha"],
            "removed": [],
            "skipped": [],
        }
        with mock.patch.object(cli.core, "deploy_gc", return_value=result) as gc:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "deploy", "gc", "--dry-run", "--json"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        gc.assert_called_once_with(config_path="/tmp/config.json", dry_run=True)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "deploy gc")
        self.assertEqual(envelope["result"], result)

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
        self.assertEqual(
            set(parsed),
            {"schema_version", "command", "ok", "result", "warnings", "errors"},
        )
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["command"], "status")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(
            set(parsed["result"]["repo"]),
            {"path", "branch", "clean", "ahead", "behind", "diverged"},
        )
        self.assertEqual(
            set(parsed["result"]["skills"][0]),
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
        self.assertEqual(parsed["result"], result)
        self.assertEqual(
            stdout,
            '{"command": "status", "errors": [], "ok": true, "result": {"repo": {"ahead": 0, "behind": 0, "branch": "main", "clean": true, "diverged": false, "path": "/repo"}, "schema_version": 1, "skills": [{"changed_local": false, "local_hash": "sha256:aaaaaaaa", "local_path": "/skills/alpha", "name": "alpha", "platform": "codex", "remote_hash": "sha256:bbbbbbbb", "selected": true}]}, "schema_version": 1, "warnings": []}\n',
        )

    def test_preview_and_doctor_json_use_success_envelopes(self):
        preview_result = {"action": "pull", "summary": "Remote changes are ready."}
        with mock.patch.object(cli.core, "sync_preview", return_value=preview_result):
            code, stdout, stderr = run_cli(["preview", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["command"], "preview")
        self.assertEqual(json.loads(stdout)["result"], preview_result)

        doctor_result = {"agents": [], "issues": []}
        with mock.patch.object(cli.core, "doctor", return_value=doctor_result):
            code, stdout, stderr = run_cli(["doctor", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["command"], "doctor")
        self.assertEqual(json.loads(stdout)["result"], doctor_result)

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

    def test_json_mode_errors_use_error_envelope_and_structured_exit_code(self):
        error = SkillSyncError(
            "unsafe deployment",
            code="unsafe_deployment",
            exit_code=4,
            details={"skill": "alpha"},
        )
        with mock.patch.object(cli.core, "doctor", side_effect=error):
            code, stdout, stderr = run_cli(["doctor", "--json"])

        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr),
            {
                "schema_version": 1,
                "command": "doctor",
                "ok": False,
                "result": None,
                "warnings": [],
                "errors": [
                    {
                        "code": "unsafe_deployment",
                        "message": "unsafe deployment",
                        "details": {"skill": "alpha"},
                    }
                ],
            },
        )

    def test_text_mode_errors_use_structured_exit_code_without_json(self):
        with mock.patch.object(
            cli.core,
            "status",
            side_effect=SkillSyncError("conflict", exit_code=3),
        ):
            code, stdout, stderr = run_cli(["status"])

        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "error: conflict\n")

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
