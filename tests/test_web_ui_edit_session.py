import subprocess
import unittest
from pathlib import Path


class WebUiEditSessionTest(unittest.TestCase):
    def test_edit_session_harness(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                "node",
                str(root / "tests" / "web_ui_edit_session_test.js"),
                str(root / "skill_sync" / "web_static" / "app.js"),
                str(root / "skill_sync" / "web_static" / "index.html"),
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("web ui edit session tests passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
