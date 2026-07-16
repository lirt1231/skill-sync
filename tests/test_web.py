import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from http.server import ThreadingHTTPServer

from skill_sync.config import save_config
from skill_sync.errors import SkillSyncError
from skill_sync.git import GitState
from skill_sync.registry import save_registry
from skill_sync.web import STATIC_DIR, _handler_factory, _state


class WebUiTest(unittest.TestCase):
    def test_static_ui_assets_exist(self):
        for name in ("index.html", "style.css", "app.js"):
            self.assertTrue((STATIC_DIR / name).is_file())
        self.assertIn('id="setup-form"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('id="sync"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('id="copy-selected"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('id="detail-description"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('id="retry-load"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('action("/api/backup", {skill})', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))
        self.assertIn('skill.description||"暂无 description"', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))
        self.assertIn('allSelected ? selected.delete', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))
        self.assertIn('await loadViews(["inventory"], force, generation)', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))
        self.assertIn('return ["import-candidates"]', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))

    def test_state_combines_status_and_link_matrix(self):
        with mock.patch("skill_sync.web.core.is_initialized", return_value=True), mock.patch("skill_sync.web.core.sync_preview", return_value={"initialized": True, "action": "noop", "issues": []}), mock.patch("skill_sync.web.core.doctor", return_value={
            "agents": [{"name": "codex", "detected": True}],
            "clients": [{"name": "codex", "family": "codex", "detected": True}],
            "matrix": [{"skill": "alpha", "agent": "codex", "state": "linked"}],
            "client_matrix": [{"skill": "alpha", "client": "codex", "agent": "codex", "state": "linked"}],
            "issues": [],
        }), mock.patch("skill_sync.web.core.status", return_value={
            "repo": {"clean": True}, "skills": [{"name": "alpha"}]
        }) as status, mock.patch("skill_sync.web.core.scan_skills", return_value=[
            {"name": "alpha", "path": "/skills/alpha", "description": "Alpha description", "selected": True, "external": False},
            {"name": "beta", "path": "/skills/beta", "description": "Beta description", "selected": False, "external": False},
        ]), mock.patch("skill_sync.web.core.scan_import_candidates", return_value=[
            {"name": "legacy", "agent": "codex", "path": "/codex/legacy", "state": "importable"}
        ]):
            value = _state(None)
        status.assert_called_once_with(config_path=None, fetch_remote=False)
        self.assertEqual(value["status"]["skills"][0]["agents"]["codex"], "linked")
        self.assertEqual([item["name"] for item in value["status"]["skills"]], ["alpha", "beta"])
        self.assertEqual(value["status"]["skills"][0]["description"], "Alpha description")
        self.assertEqual(value["status"]["skills"][1]["description"], "Beta description")
        self.assertEqual(value["doctor"]["clients"][0]["name"], "codex")
        self.assertEqual(value["doctor"]["client_matrix"][0]["client"], "codex")
        self.assertEqual(value["import_candidates"][0]["name"], "legacy")

    def test_inventory_view_has_a_bounded_core_read_path_for_100_skills(self):
        status_skills = [
            {"name": f"skill-{index:03d}", "changed_local": False, "selected": True}
            for index in range(100)
        ]
        candidates = [
            {
                "name": item["name"],
                "path": f"/skills/{item['name']}",
                "description": f"Description {index}",
                "selected": True,
                "external": False,
            }
            for index, item in enumerate(status_skills)
        ]
        with mock.patch("skill_sync.web.core.is_initialized", return_value=True), mock.patch(
            "skill_sync.web.core.status",
            return_value={"schema_version": 1, "repo": {"clean": True}, "skills": status_skills},
        ) as status, mock.patch(
            "skill_sync.web.core.scan_skills", return_value=candidates
        ) as scan, mock.patch("skill_sync.web.core.sync_preview") as preview, mock.patch(
            "skill_sync.web.core.doctor"
        ) as doctor, mock.patch("skill_sync.web.core.scan_import_candidates") as imports:
            value = _state(None, views=("inventory",))

        self.assertEqual(len(value["status"]["skills"]), 100)
        status.assert_called_once_with(config_path=None, fetch_remote=False)
        scan.assert_called_once_with(platform=None, config_path=None)
        preview.assert_not_called()
        doctor.assert_not_called()
        imports.assert_not_called()

    def test_inventory_core_hash_work_is_linear_for_100_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            global_skills = root / "skills"
            (repo / ".git").mkdir(parents=True)
            registry = {"version": 1, "skills": {}}
            config = {
                "sync_repo_path": str(repo),
                "branch": "main",
                "skills_root": str(global_skills),
                "disabled_agents": [],
                "skills": {},
            }
            for index in range(100):
                name = f"skill-{index:03d}"
                for base in (global_skills, repo / "skills"):
                    skill = base / name
                    skill.mkdir(parents=True)
                    (skill / "SKILL.md").write_text(
                        f"---\nname: {name}\ndescription: Fixture {index}\n---\n",
                        encoding="utf-8",
                    )
                registry["skills"][name] = {"selected": True, "display_name": name}
                config["skills"][name] = {"local_path": str(global_skills / name)}
            save_registry(repo / "registry.yaml", registry)
            config_path = root / "config.json"
            save_config(config_path, config)

            with mock.patch(
                "skill_sync.core.git.state",
                return_value=GitState(clean=True, ahead=0, behind=0, diverged=False),
            ) as git_state, mock.patch(
                "skill_sync.core.hash_skill_dir", return_value="sha256:fixture"
            ) as hash_skill:
                value = _state(str(config_path), views=("inventory",))

        self.assertEqual(len(value["status"]["skills"]), 100)
        git_state.assert_called_once_with(repo, "main", fetch_remote=False)
        self.assertEqual(hash_skill.call_count, 200)

    def test_import_candidates_are_only_scanned_for_the_import_view(self):
        with mock.patch("skill_sync.web.core.is_initialized", return_value=True), mock.patch(
            "skill_sync.web.core.scan_import_candidates",
            return_value=[{"name": "alpha", "agent": "codex"}],
        ) as imports, mock.patch("skill_sync.web.core.status") as status:
            value = _state(None, views=("import-candidates",))

        self.assertEqual(value["loaded_views"], ["import-candidates"])
        self.assertEqual(value["import_candidates"][0]["name"], "alpha")
        imports.assert_called_once_with(config_path=None)
        status.assert_not_called()

    def test_summary_and_agents_share_one_diagnosis_and_never_fetch(self):
        diagnosis = {
            "agents": [],
            "clients": [],
            "matrix": [],
            "client_matrix": [],
            "issues": [],
        }
        preview = {"initialized": True, "action": "noop", "issues": []}
        with mock.patch("skill_sync.web.core.is_initialized", return_value=True), mock.patch(
            "skill_sync.web.core.doctor", return_value=diagnosis
        ) as doctor, mock.patch(
            "skill_sync.web.core.sync_preview", return_value=preview
        ) as sync_preview, mock.patch("skill_sync.web.core.scan_import_candidates") as imports:
            value = _state(None, views=("summary", "agents"))

        self.assertIs(value["doctor"], diagnosis)
        doctor.assert_called_once_with(config_path=None)
        sync_preview.assert_called_once_with(
            config_path=None,
            fetch_remote=False,
            diagnosis=diagnosis,
        )
        imports.assert_not_called()

    def test_state_returns_setup_contract_when_uninitialized(self):
        with mock.patch("skill_sync.web.core.sync_preview", return_value={
            "initialized": False, "action": "setup", "issues": [], "skills": []
        }), mock.patch("skill_sync.web.core.detect_agents", return_value=[]):
            value = _state("/tmp/missing-config.json")
        self.assertFalse(value["initialized"])
        self.assertEqual(value["preview"]["action"], "setup")


class WebHttpViewTest(unittest.TestCase):
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

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def test_http_state_routes_repeated_view_parameters(self):
        response = {
            "schema_version": 1,
            "initialized": True,
            "loaded_views": ["summary", "inventory"],
        }
        with mock.patch("skill_sync.web._state", return_value=response) as state:
            with urlopen(self.url("/api/state?view=summary&view=inventory")) as request:
                body = json.loads(request.read())

        self.assertEqual(body, response)
        state.assert_called_once_with(
            "/tmp/config.json", views=("summary", "inventory")
        )

    def test_http_state_rejects_unknown_views_before_core_reads(self):
        with mock.patch("skill_sync.web._state") as state:
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.url("/api/state?view=inventory&view=variants"))

        self.assertEqual(raised.exception.code, 400)
        state.assert_not_called()

    def test_http_post_rejects_unknown_views_before_mutation(self):
        request = Request(
            self.url("/api/sync"),
            data=json.dumps({"views": ["inventory", "variants"]}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Skill-Sync-Token": "test-token",
            },
            method="POST",
        )
        with mock.patch("skill_sync.web.core.sync") as sync, mock.patch(
            "skill_sync.web._state"
        ) as state:
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)

        self.assertEqual(raised.exception.code, 400)
        sync.assert_not_called()
        state.assert_not_called()

    def test_http_post_distinguishes_applied_mutation_from_state_read_failure(self):
        request = Request(
            self.url("/api/sync"),
            data=json.dumps({"views": ["inventory"]}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Skill-Sync-Token": "test-token",
            },
            method="POST",
        )
        with mock.patch("skill_sync.web.core.sync", return_value={"action": "push"}) as sync, mock.patch(
            "skill_sync.web._state", side_effect=SkillSyncError("diagnosis failed")
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)

        self.assertEqual(raised.exception.code, 500)
        body = json.loads(raised.exception.read())
        self.assertTrue(body["mutation_applied"])
        self.assertIn("state refresh failed", body["error"])
        self.assertEqual(body["result"], {"action": "push"})
        self.assertIsNone(body["state"])
        self.assertEqual(body["state_error"], "diagnosis failed")
        sync.assert_called_once_with(skill_names=None, config_path="/tmp/config.json")

    def test_http_post_core_failure_is_not_marked_as_applied(self):
        request = Request(
            self.url("/api/sync"),
            data=json.dumps({"views": ["inventory"]}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Skill-Sync-Token": "test-token",
            },
            method="POST",
        )
        with mock.patch(
            "skill_sync.web.core.sync", side_effect=SkillSyncError("repository blocked")
        ), mock.patch("skill_sync.web._state") as state:
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)

        self.assertEqual(raised.exception.code, 400)
        body = json.loads(raised.exception.read())
        self.assertNotIn("mutation_applied", body)
        state.assert_not_called()
