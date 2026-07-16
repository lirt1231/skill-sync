import shutil
import subprocess
import unittest
from pathlib import Path


class WebUiOperationProgressTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Web UI tests")
    def test_operation_progress_harness(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests" / "web_ui_operation_progress_test.js"),
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
        self.assertIn("operation progress tests passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
