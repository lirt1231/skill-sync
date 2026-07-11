import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.linking import create_directory_link, link_state, remove_directory_link


class LinkingTest(unittest.TestCase):
    def test_create_is_idempotent_and_remove_only_removes_correct_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "global" / "alpha"
            destination = root / "agent" / "alpha"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# alpha\n")
            self.assertEqual(create_directory_link(source, destination), "symlink")
            self.assertEqual(link_state(source, destination), "linked")
            self.assertEqual(create_directory_link(source, destination), "linked")
            self.assertTrue(remove_directory_link(source, destination))
            self.assertTrue(source.exists())

    def test_refuses_existing_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "global" / "alpha"
            destination = root / "agent" / "alpha"
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            self.assertEqual(link_state(source, destination), "conflict")
            with self.assertRaises(FileExistsError):
                create_directory_link(source, destination)

    def test_windows_falls_back_to_directory_junction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "global" / "alpha"
            destination = root / "agent" / "alpha"
            source.mkdir(parents=True)
            with mock.patch("skill_sync.linking.Path.symlink_to", side_effect=OSError("denied")), mock.patch(
                "skill_sync.linking.os.name", "nt"
            ), mock.patch("skill_sync.linking.subprocess.run") as run:
                self.assertEqual(create_directory_link(source, destination), "junction")
            self.assertEqual(run.call_args.args[0][:4], ["cmd", "/c", "mklink", "/J"])
