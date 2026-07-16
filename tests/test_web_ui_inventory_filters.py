import shutil
import subprocess
import unittest
from pathlib import Path


class WebUiInventoryFiltersTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Web UI tests")
    def test_inventory_filter_harness(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests" / "web_ui_inventory_filters_test.js"),
                str(root / "skill_sync" / "web_static" / "app.js"),
                str(root / "skill_sync" / "web_static" / "index.html"),
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("inventory filter tests passed", completed.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Web UI tests")
    def test_inventory_reload_harness(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests" / "web_ui_inventory_reload_test.js"),
                str(root / "skill_sync" / "web_static" / "app.js"),
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("inventory reload tests passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
