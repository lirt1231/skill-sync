import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.web import STATIC_DIR, _state


class WebUiTest(unittest.TestCase):
    def test_static_ui_assets_exist(self):
        for name in ("index.html", "style.css", "app.js"):
            self.assertTrue((STATIC_DIR / name).is_file())
        self.assertIn('id="setup-form"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('id="sync"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('id="copy-selected"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn('action("/api/backup", {skill})', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))
        self.assertIn('allSelected ? selected.delete', (STATIC_DIR / "app.js").read_text(encoding="utf-8"))

    def test_state_combines_status_and_link_matrix(self):
        with mock.patch("skill_sync.web.core.sync_preview", return_value={"initialized": True, "action": "noop", "issues": []}), mock.patch("skill_sync.web.core.doctor", return_value={
            "agents": [{"name": "codex", "detected": True}],
            "matrix": [{"skill": "alpha", "agent": "codex", "state": "linked"}],
            "issues": [],
        }), mock.patch("skill_sync.web.core.status", return_value={
            "repo": {"clean": True}, "skills": [{"name": "alpha"}]
        }) as status, mock.patch("skill_sync.web.core.scan_skills", return_value=[
            {"name": "alpha", "path": "/skills/alpha", "selected": True, "external": False},
            {"name": "beta", "path": "/skills/beta", "selected": False, "external": False},
        ]), mock.patch("skill_sync.web.core.scan_import_candidates", return_value=[
            {"name": "legacy", "agent": "codex", "path": "/codex/legacy", "state": "importable"}
        ]):
            value = _state(None)
        status.assert_called_once_with(config_path=None, fetch_remote=False)
        self.assertEqual(value["status"]["skills"][0]["agents"]["codex"], "linked")
        self.assertEqual([item["name"] for item in value["status"]["skills"]], ["alpha", "beta"])
        self.assertEqual(value["import_candidates"][0]["name"], "legacy")

    def test_state_returns_setup_contract_when_uninitialized(self):
        with mock.patch("skill_sync.web.core.sync_preview", return_value={
            "initialized": False, "action": "setup", "issues": [], "skills": []
        }), mock.patch("skill_sync.web.core.detect_agents", return_value=[]):
            value = _state("/tmp/missing-config.json")
        self.assertFalse(value["initialized"])
        self.assertEqual(value["preview"]["action"], "setup")
