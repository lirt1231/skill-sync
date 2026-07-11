import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.web import STATIC_DIR, _state


class WebUiTest(unittest.TestCase):
    def test_static_ui_assets_exist(self):
        for name in ("index.html", "style.css", "app.js"):
            self.assertTrue((STATIC_DIR / name).is_file())

    def test_state_combines_status_and_link_matrix(self):
        with mock.patch("skill_sync.web.core.doctor", return_value={
            "agents": [{"name": "codex", "detected": True}],
            "matrix": [{"skill": "alpha", "agent": "codex", "state": "linked"}],
            "issues": [],
        }), mock.patch("skill_sync.web.core.status", return_value={
            "repo": {"clean": True}, "skills": [{"name": "alpha"}]
        }), mock.patch("skill_sync.web.core.scan_skills", return_value=[
            {"name": "alpha", "path": "/skills/alpha", "selected": True, "external": False},
            {"name": "beta", "path": "/skills/beta", "selected": False, "external": False},
        ]), mock.patch("skill_sync.web.core.scan_import_candidates", return_value=[
            {"name": "legacy", "agent": "codex", "path": "/codex/legacy", "state": "importable"}
        ]):
            value = _state(None)
        self.assertEqual(value["status"]["skills"][0]["agents"]["codex"], "linked")
        self.assertEqual([item["name"] for item in value["status"]["skills"]], ["alpha", "beta"])
        self.assertEqual(value["import_candidates"][0]["name"], "legacy")
