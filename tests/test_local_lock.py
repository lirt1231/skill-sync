import tempfile
import threading
import unittest
from pathlib import Path

from skill_sync.local_lock import local_file_lock


class LocalFileLockTest(unittest.TestCase):
    def test_same_thread_can_reenter_same_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "deployment.lock"

            with local_file_lock(lock_path):
                with local_file_lock(lock_path, timeout=0.01):
                    self.assertTrue(lock_path.exists())

    def test_same_thread_reentry_normalizes_symlink_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)

            with local_file_lock(real / "deployment.lock"):
                with local_file_lock(alias / "deployment.lock", timeout=0.01):
                    self.assertTrue((real / "deployment.lock").exists())

    def test_lock_serializes_same_machine_local_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "locks" / "deployment.lock"
            result: list[str] = []

            def contend() -> None:
                try:
                    with local_file_lock(path, timeout=0.01):
                        result.append("acquired")
                except TimeoutError:
                    result.append("timed-out")

            with local_file_lock(path):
                thread = threading.Thread(target=contend)
                thread.start()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result, ["timed-out"])
            with local_file_lock(path, timeout=0.01):
                self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
