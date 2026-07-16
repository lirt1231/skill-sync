import shutil
import subprocess
import unittest
from pathlib import Path


class WebUiMutationConfirmationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Web UI tests")
    def test_mutation_confirmation_harness(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                shutil.which("node"),
                str(root / "tests" / "web_ui_mutation_confirmation_test.js"),
                str(root / "skill_sync" / "web_static" / "app.js"),
                str(root / "skill_sync" / "web_static" / "index.html"),
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("mutation confirmation tests passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
