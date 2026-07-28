import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from http.server import ThreadingHTTPServer

import skill_sync.core as core
from skill_sync.agents import AgentClient
from skill_sync.config import empty_config, save_config
from skill_sync.edit_session import EditSessionStore
from skill_sync.errors import SkillSyncError
from skill_sync.git import GitState
from skill_sync.hash import hash_skill_dir
from skill_sync.registry import save_registry
from skill_sync.web import _handler_factory


def tree_snapshot(root: Path) -> list[tuple[str, str, str]]:
    result = []
    if not root.exists():
        return result
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result.append((relative, "link", os.readlink(path)))
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result.append((relative, "file", digest))
        else:
            result.append((relative, "dir", ""))
    return result


class MutationPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.global_root = self.root / "global" / "skills"
        self.data_root = self.root / "data"
        self.config_path = self.root / "config.json"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / ".git" / "index").write_bytes(b"fixed-index")
        self.write_skill(self.global_root, "alpha")
        self.write_skill(self.repo / "skills", "alpha")
        self.codex_root = self.root / "codex" / "skills"
        self.workbuddy_root = self.root / "workbuddy" / "skills"
        self.write_skill(self.codex_root, "beta")
        config = empty_config()
        config.pop("platform", None)
        config.update(
            {
                "sync_repo_path": str(self.repo),
                "branch": "main",
                "skills_root": str(self.global_root),
                "data_root": str(self.data_root),
                "disabled_agents": [],
                "skills": {"alpha": {"local_path": str(self.global_root / "alpha")}},
            }
        )
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 2,
                "skills": {
                    "alpha": {
                        "selected": True,
                        "display_name": "alpha",
                        "targets": "codex,workbuddy,kimi,claude",
                    }
                },
            },
        )
        self.clients = (
            AgentClient("codex", "codex", "Codex", self.codex_root, True),
            AgentClient(
                "workbuddy", "workbuddy", "WorkBuddy", self.workbuddy_root, True
            ),
            AgentClient(
                "kimi-code", "kimi", "Kimi Code", self.root / "kimi-code", False
            ),
            AgentClient(
                "claude-code", "claude", "Claude Code", self.root / "claude", False
            ),
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_skill(root: Path, name: str) -> Path:
        path = root / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        return path

    def preview(self, operation: str, request: dict):
        with mock.patch.object(core, "detect_clients", return_value=list(self.clients)), mock.patch.object(
            core.git,
            "state",
            return_value=GitState(clean=True, ahead=0, behind=0, diverged=False),
        ), mock.patch.object(core, "_unexpected_dirty_paths", return_value=[]):
            return core.preview_mutation(operation, request, self.config_path)

    def test_all_plans_share_schema_and_are_zero_write(self):
        requests = (
            ("sync", {"skills": ["alpha", "alpha"]}),
            ("import", {"skills": ["beta"], "agent": "codex"}),
            ("agent", {"agent": "workbuddy", "enabled": False}),
            ("link-repair", {"skills": ["alpha"], "agents": ["codex"]}),
            ("delete", {"skills": ["alpha"]}),
        )
        required = {
            "schema_version",
            "operation",
            "request",
            "status",
            "can_execute",
            "summary",
            "targets",
            "steps",
            "conflicts",
            "blockers",
            "warnings",
            "effects",
            "backup",
            "recovery",
            "freshness",
            "details",
        }
        before = tree_snapshot(self.root)
        forbidden = (
            mock.patch.object(core.git, "fetch"),
            mock.patch.object(core.git, "commit_all_if_changed"),
            mock.patch.object(core.git, "push"),
            mock.patch.object(core, "save_config"),
            mock.patch.object(core, "save_registry"),
            mock.patch.object(core, "copy_skill_dir"),
            mock.patch.object(core, "render_base_deployment"),
            mock.patch.object(core, "deploy_migrate"),
            mock.patch.object(core, "create_directory_link"),
            mock.patch.object(core, "replace_directory_link"),
            mock.patch.object(core, "remove_directory_link"),
            mock.patch.object(core, "rename_no_replace"),
            mock.patch.object(core, "_write_json_atomic"),
            mock.patch.object(core.shutil, "rmtree"),
            mock.patch.object(core, "local_file_lock"),
        )
        mocks = [patcher.start() for patcher in forbidden]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(forbidden)])

        plans = [self.preview(operation, request) for operation, request in requests]

        self.assertEqual(before, tree_snapshot(self.root))
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), b"fixed-index")
        for plan in plans:
            self.assertEqual(set(plan), required)
            self.assertFalse(plan["freshness"]["remote_checked"])
            self.assertTrue(plan["freshness"]["replan_required"])
            self.assertFalse(plan["backup"]["created"])
            self.assertIn("git", plan["effects"])
            self.assertIn("writes", plan["effects"])
            json.dumps(plan)
        self.assertEqual(plans[0]["request"]["skills"], ["alpha"])
        self.assertTrue(
            all(client["detected"] for client in plans[0]["targets"]["clients"])
        )
        for mutation in mocks:
            mutation.assert_not_called()

    def test_unknown_fields_and_invalid_lists_fail_closed(self):
        with self.assertRaises(SkillSyncError):
            self.preview("delete", {"skills": "alpha"})
        with self.assertRaises(SkillSyncError):
            self.preview("sync", {"skills": ["alpha"], "force": True})
        with self.assertRaises(SkillSyncError):
            self.preview("other", {})

    def test_one_preview_uses_one_concrete_client_snapshot(self):
        with mock.patch.object(
            core, "detect_clients", return_value=list(self.clients)
        ) as detect:
            plan = core.preview_mutation(
                "agent",
                {"agent": "workbuddy", "enabled": False},
                self.config_path,
            )

        self.assertEqual(plan["request"]["agent"], "workbuddy")
        detect.assert_called_once_with()

    def test_sync_preview_leaves_a_real_git_tree_and_index_unchanged(self):
        core.shutil.rmtree(self.repo / ".git")
        core.git.init_repo(self.repo)
        core.git.run_git(self.repo, ["config", "user.name", "Preview Test"])
        core.git.run_git(
            self.repo,
            ["config", "user.email", "preview@example.invalid"],
        )
        core.git.run_git(self.repo, ["add", "."])
        core.git.run_git(self.repo, ["commit", "-m", "fixture"])
        before = tree_snapshot(self.repo)

        with mock.patch.object(
            core, "detect_clients", return_value=list(self.clients)
        ):
            plan = core.preview_mutation(
                "sync", {"skills": ["alpha"]}, self.config_path
            )

        self.assertEqual(plan["operation"], "sync")
        self.assertFalse(plan["freshness"]["remote_checked"])
        self.assertEqual(before, tree_snapshot(self.repo))

    def test_receipt_is_a_blocker_and_is_returned_for_recovery(self):
        operations = self.data_root / "operations"
        operations.mkdir(parents=True)
        receipt = operations / "deploy-migrate-test.json"
        receipt.write_text(
            json.dumps(
                {
                    "operation_id": "test",
                    "status": "prepared",
                    "in_flight": str(self.codex_root / "alpha"),
                }
            ),
            encoding="utf-8",
        )

        plan = self.preview("link-repair", {"skills": ["alpha"], "agents": ["codex"]})

        self.assertFalse(plan["can_execute"])
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["recovery"]["operations"][0]["path"], str(receipt))
        self.assertEqual(plan["recovery"]["operations"][0]["status"], "prepared")
        self.assertEqual(
            plan["recovery"]["operations"][0]["in_flight"],
            str(self.codex_root / "alpha"),
        )

    def test_import_conflict_plan_matches_action_preflight(self):
        self.write_skill(self.global_root, "beta")
        (self.global_root / "beta" / "SKILL.md").write_text(
            "# different\n", encoding="utf-8"
        )
        with mock.patch.object(
            core, "detect_clients", return_value=list(self.clients)
        ), mock.patch.object(core, "_import_agent_skills_unlocked") as mutate:
            plan = core.preview_mutation(
                "import",
                {"skills": ["beta"], "agent": "codex"},
                self.config_path,
            )
            with self.assertRaises(SkillSyncError):
                core.import_agent_skills(["beta"], "codex", self.config_path)

        self.assertEqual(plan["status"], "conflict")
        self.assertFalse(plan["can_execute"])
        self.assertEqual(plan["conflicts"][0]["code"], "import-conflict")
        mutate.assert_not_called()

    def test_already_linked_import_and_healthy_link_repair_are_noops(self):
        beta = self.write_skill(self.global_root, "beta")
        beta_deployment = core.render_base_deployment(
            beta, self.data_root / "rendered", "beta", "codex"
        )
        core.shutil.rmtree(self.codex_root / "beta")
        core.create_directory_link(beta_deployment.path, self.codex_root / "beta")
        alpha_deployment = core.render_base_deployment(
            self.global_root / "alpha",
            self.data_root / "rendered",
            "alpha",
            "codex",
        )
        core.create_directory_link(alpha_deployment.path, self.codex_root / "alpha")

        import_plan = self.preview(
            "import", {"skills": ["beta"], "agent": "codex"}
        )
        link_plan = self.preview(
            "link-repair", {"skills": ["alpha"], "agents": ["codex"]}
        )

        self.assertTrue(import_plan["can_execute"])
        self.assertEqual(import_plan["steps"][0]["action"], "already-linked")
        self.assertFalse(any(import_plan["effects"]["writes"].values()))
        self.assertTrue(link_plan["can_execute"])
        self.assertEqual(link_plan["steps"][0]["action"], "noop")
        self.assertFalse(any(link_plan["effects"]["writes"].values()))

    def test_link_conflict_reason_describes_the_protected_agent_destination(self):
        self.write_skill(self.codex_root, "alpha")

        plan = self.preview("link-repair", {"skills": ["alpha"]})

        codex = next(
            item for item in plan["targets"]["clients"] if item["client"] == "codex"
        )
        self.assertEqual(codex["current_state"], "conflict")
        self.assertEqual(codex["effect"], "blocked")
        self.assertEqual(
            next(
                item["detail"]
                for item in plan["conflicts"]
                if item["client"] == "codex"
            ),
            "Agent destination contains unmanaged content.",
        )
        self.assertEqual(
            next(
                item["destination"]
                for item in plan["conflicts"]
                if item["client"] == "codex"
            ),
            str(self.codex_root / "alpha"),
        )

    def test_concrete_kimi_request_is_blocked_by_family_disable_in_plan_and_action(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["disabled_agents"] = ["kimi"]
        save_config(self.config_path, config)
        clients = list(self.clients)
        clients[2] = AgentClient(
            "kimi-code", "kimi", "Kimi Code", self.root / "kimi-code", True
        )
        with mock.patch.object(core, "detect_clients", return_value=clients), mock.patch.object(
            core, "deploy_migrate"
        ) as migrate:
            plan = core.preview_mutation(
                "link-repair",
                {"skills": ["alpha"], "agents": ["kimi-code"]},
                self.config_path,
            )
            with self.assertRaises(SkillSyncError):
                core.link_skills(
                    ["alpha"], ["kimi-code"], config_path=self.config_path
                )
        self.assertIn("agent-disabled", {item["code"] for item in plan["blockers"]})
        migrate.assert_not_called()

    def test_agent_enable_is_ready_and_delete_warns_about_unowned_agent_path(self):
        self.write_skill(self.workbuddy_root, "alpha")

        agent = self.preview(
            "agent", {"agent": "workbuddy", "enabled": True}
        )
        delete = self.preview("delete", {"skills": ["alpha"]})

        self.assertEqual(agent["status"], "ready")
        self.assertTrue(agent["can_execute"])
        self.assertIn(
            "unowned-agent-path-skipped",
            {warning["code"] for warning in delete["warnings"]},
        )

    def test_delete_plan_and_action_block_active_edit_session(self):
        EditSessionStore(self.data_root).begin(
            logical_skill="alpha",
            source=self.global_root / "alpha",
            baseline_hash=hash_skill_dir(self.global_root / "alpha"),
            actor="test",
        )
        with mock.patch.object(core, "detect_clients", return_value=list(self.clients)), mock.patch.object(
            core, "_delete_global_skills_unlocked"
        ) as delete:
            plan = core.preview_mutation(
                "delete", {"skills": ["alpha"]}, self.config_path
            )
            with self.assertRaises(SkillSyncError):
                core.delete_global_skills(["alpha"], self.config_path)
        self.assertIn("active-edit-session", {item["code"] for item in plan["blockers"]})
        delete.assert_not_called()

    def test_action_and_preview_use_the_same_link_resolver(self):
        real = core._resolve_link_input
        with mock.patch.object(core, "detect_clients", return_value=list(self.clients)), mock.patch.object(
            core, "_resolve_link_input", wraps=real
        ) as resolver, mock.patch.object(core, "deploy_migrate", return_value={}), mock.patch.object(
            core, "_deployment_plan", return_value=[]
        ):
            plan = core.preview_mutation(
                "link-repair",
                {"skills": ["alpha", "alpha"], "agents": ["codex", "codex"]},
                self.config_path,
            )
            core.link_skills(
                ["alpha", "alpha"], ["codex", "codex"], self.config_path
            )
        self.assertEqual(plan["request"], {"skills": ["alpha"], "agents": ["codex"]})
        self.assertEqual(resolver.call_count, 2)

    def test_all_actions_share_their_preview_normalized_request(self):
        operations = (
            (
                "sync",
                "_resolve_sync_input",
                {"skills": ["alpha", "alpha"]},
                lambda: core.sync(["alpha", "alpha"], self.config_path),
            ),
            (
                "import",
                "_resolve_import_input",
                {"skills": ["beta", "beta"], "agent": "codex"},
                lambda: core.import_agent_skills(
                    ["beta", "beta"], "codex", self.config_path
                ),
            ),
            (
                "agent",
                "_resolve_agent_toggle_input",
                {"agent": "workbuddy", "enabled": True},
                lambda: core.enable_agent_sync("workbuddy", self.config_path),
            ),
            (
                "link-repair",
                "_resolve_link_input",
                {"skills": ["alpha", "alpha"], "agents": ["codex", "codex"]},
                lambda: core.link_skills(
                    ["alpha", "alpha"], ["codex", "codex"], self.config_path
                ),
            ),
            (
                "delete",
                "_resolve_delete_input",
                {"skills": ["alpha", "alpha"]},
                lambda: core.delete_global_skills(
                    ["alpha", "alpha"], self.config_path
                ),
            ),
        )
        for operation, resolver_name, request, execute in operations:
            with self.subTest(operation=operation):
                real_resolver = getattr(core, resolver_name)
                resolved = []

                def record(*args, _real=real_resolver, **kwargs):
                    value = _real(*args, **kwargs)
                    resolved.append(value)
                    return value

                with mock.patch.object(
                    core, "detect_clients", return_value=list(self.clients)
                ), mock.patch.object(
                    core, resolver_name, side_effect=record
                ), mock.patch.object(
                    core.git,
                    "state",
                    return_value=GitState(
                        clean=True, ahead=0, behind=0, diverged=False
                    ),
                ), mock.patch.object(
                    core, "_unexpected_dirty_paths", return_value=[]
                ), mock.patch.object(
                    core.git, "fetch"
                ), mock.patch.object(
                    core, "link_skills", return_value={"links": []}
                ) if operation == "sync" else mock.patch.object(
                    core, "_import_agent_skills_unlocked", return_value={"imported": []}
                ) if operation == "import" else mock.patch.object(
                    core, "save_config"
                ) if operation == "agent" else mock.patch.object(
                    core, "deploy_migrate", return_value={}
                ) if operation == "link-repair" else mock.patch.object(
                    core, "_delete_global_skills_unlocked", return_value={"deleted": []}
                ):
                    plan = core.preview_mutation(
                        operation, request, self.config_path
                    )
                    execute()

                self.assertEqual(len(resolved), 2)
                self.assertEqual(resolved[0], resolved[1])
                expected_request = {
                    key: value
                    for key, value in resolved[0].items()
                    if key in plan["request"]
                }
                self.assertEqual(plan["request"], expected_request)


class MutationPreviewHttpTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_factory("/tmp/config.json", "test-token")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, body: dict, *, token: str = "test-token"):
        return Request(
            f"http://127.0.0.1:{self.server.server_port}/api/plan",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Skill-Sync-Token": token,
            },
            method="POST",
        )

    def test_http_plan_routes_one_normalized_request_without_state_refresh(self):
        plan = {"schema_version": 1, "operation": "delete", "can_execute": True}
        with mock.patch("skill_sync.web.core.preview_mutation", return_value=plan) as preview, mock.patch(
            "skill_sync.web._state"
        ) as state:
            with urlopen(
                self.request(
                    {"operation": "delete", "request": {"skills": ["alpha"]}}
                )
            ) as response:
                body = json.loads(response.read())
        self.assertEqual(body, plan)
        preview.assert_called_once_with(
            "delete", {"skills": ["alpha"]}, config_path="/tmp/config.json"
        )
        state.assert_not_called()

    def test_http_plan_requires_csrf_token_and_fails_closed(self):
        with mock.patch("skill_sync.web.core.preview_mutation") as preview:
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    self.request(
                        {"operation": "delete", "request": {"skills": ["alpha"]}},
                        token="wrong",
                    )
                )
        self.assertEqual(raised.exception.code, 403)
        preview.assert_not_called()
