import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

from skill_sync.linking import (
    create_directory_link,
    link_state,
    remove_directory_link,
    replace_directory_link,
)
import skill_sync.linking as linking_module


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

    def test_replace_directory_link_swaps_only_an_owned_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_source = root / "global" / "alpha"
            new_source = root / "rendered" / "alpha"
            destination = root / "agent" / "alpha"
            old_source.mkdir(parents=True)
            new_source.mkdir(parents=True)
            create_directory_link(old_source, destination)

            method = replace_directory_link(
                new_source,
                destination,
                allowed_current_sources=[old_source],
            )

            self.assertEqual(method, "symlink")
            self.assertEqual(link_state(new_source, destination), "linked")
            self.assertTrue(old_source.exists())

    def test_replace_directory_link_refuses_wrong_link_and_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected_old = root / "global" / "alpha"
            wrong = root / "other" / "alpha"
            new_source = root / "rendered" / "alpha"
            destination = root / "agent" / "alpha"
            for path in (expected_old, wrong, new_source):
                path.mkdir(parents=True)
            create_directory_link(wrong, destination)

            with self.assertRaises(FileExistsError):
                replace_directory_link(
                    new_source,
                    destination,
                    allowed_current_sources=[expected_old],
                )
            self.assertEqual(link_state(wrong, destination), "linked")

            destination.unlink()
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                replace_directory_link(
                    new_source,
                    destination,
                    allowed_current_sources=[expected_old],
                )

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

    def test_windows_junction_fallback_rejects_cmd_metacharacters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "global&unsafe" / "alpha"
            destination = root / "agent" / "alpha"
            source.mkdir(parents=True)
            with mock.patch(
                "skill_sync.linking.Path.symlink_to", side_effect=OSError("denied")
            ), mock.patch("skill_sync.linking.os.name", "nt"), mock.patch(
                "skill_sync.linking.subprocess.run"
            ) as run:
                with self.assertRaisesRegex(ValueError, "unsafe Windows link path"):
                    create_directory_link(source, destination)
            run.assert_not_called()

    def test_windows_remove_never_rmdirs_a_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "global" / "alpha"
            destination = root / "agent" / "alpha"
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            with mock.patch("skill_sync.linking.os.name", "nt"), mock.patch(
                "skill_sync.linking._same_file", return_value=True
            ), mock.patch(
                "skill_sync.linking._is_windows_reparse_point", return_value=False
            ):
                self.assertFalse(remove_directory_link(source, destination))
            self.assertTrue(destination.is_dir())

    def test_windows_remove_preserves_reparse_replaced_by_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "global" / "alpha"
            destination = root / "agent" / "alpha"
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            with mock.patch("skill_sync.linking.os.name", "nt"), mock.patch(
                "skill_sync.linking._same_file", return_value=True
            ), mock.patch(
                "skill_sync.linking._is_windows_reparse_point",
                side_effect=[True, False],
            ), mock.patch.object(Path, "rmdir") as rmdir:
                self.assertFalse(remove_directory_link(source, destination))
            rmdir.assert_not_called()
            self.assertTrue(destination.is_dir())

    def test_replace_preserves_pre_move_race_path_as_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_source = root / "global" / "alpha"
            new_source = root / "rendered" / "alpha"
            destination = root / "agent" / "alpha"
            old_source.mkdir(parents=True)
            new_source.mkdir(parents=True)
            create_directory_link(old_source, destination)
            real_rename = linking_module.rename_no_replace
            raced = False

            def rename_after_directory_race(left, right):
                nonlocal raced
                left = Path(left)
                right = Path(right)
                if not raced and left == destination and ".previous-" in right.name:
                    raced = True
                    destination.unlink()
                    destination.mkdir()
                    (destination / "keep.txt").write_text("keep", encoding="utf-8")
                return real_rename(left, right)

            with mock.patch(
                "skill_sync.linking.rename_no_replace",
                side_effect=rename_after_directory_race,
            ):
                with self.assertRaisesRegex(FileExistsError, "identity verification"):
                    replace_directory_link(
                        new_source,
                        destination,
                        allowed_current_sources=[old_source],
                    )

            self.assertTrue(raced)
            backups = list(destination.parent.glob(".alpha.previous-*"))
            self.assertEqual(len(backups), 1)
            self.assertFalse(backups[0].is_symlink())
            self.assertEqual((backups[0] / "keep.txt").read_text(), "keep")

    def test_replace_never_overwrites_after_backup_window_winner(self):
        for winner_kind in ("file", "link", "directory"):
            with self.subTest(winner=winner_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                old_source = root / "global" / "alpha"
                new_source = root / "rendered" / "alpha"
                wrong_source = root / "other" / "alpha"
                destination = root / "agent" / "alpha"
                for path in (old_source, new_source, wrong_source):
                    path.mkdir(parents=True)
                create_directory_link(old_source, destination)
                real_create = linking_module.create_directory_link
                injected = False

                def create_after_winner(source, final):
                    nonlocal injected
                    source = Path(source)
                    final = Path(final)
                    if not injected and source == new_source and final == destination:
                        injected = True
                        if winner_kind == "file":
                            final.write_text("keep", encoding="utf-8")
                        elif winner_kind == "link":
                            real_create(wrong_source, final)
                        else:
                            final.mkdir()
                            (final / "keep.txt").write_text("keep", encoding="utf-8")
                    return real_create(source, final)

                with mock.patch(
                    "skill_sync.linking.create_directory_link",
                    side_effect=create_after_winner,
                ), mock.patch(
                    "skill_sync.linking.os.replace",
                    side_effect=AssertionError("must not overwrite window winner"),
                ):
                    with self.assertRaises(FileExistsError):
                        replace_directory_link(
                            new_source,
                            destination,
                            allowed_current_sources=[old_source],
                        )

                self.assertTrue(injected)
                if winner_kind == "file":
                    self.assertTrue(destination.is_file())
                    self.assertEqual(destination.read_text(), "keep")
                elif winner_kind == "link":
                    self.assertEqual(link_state(wrong_source, destination), "linked")
                else:
                    self.assertFalse(destination.is_symlink())
                    self.assertEqual((destination / "keep.txt").read_text(), "keep")
                backups = list(destination.parent.glob(".alpha.previous-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(link_state(old_source, backups[0]), "linked")

    def test_replace_rolls_back_normally_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_source = root / "global" / "alpha"
            new_source = root / "rendered" / "alpha"
            destination = root / "agent" / "alpha"
            old_source.mkdir(parents=True)
            new_source.mkdir(parents=True)
            create_directory_link(old_source, destination)
            real_create = linking_module.create_directory_link

            def fail_new_link(source, final):
                if Path(source) == new_source and Path(final) == destination:
                    raise OSError("injected create failure")
                return real_create(source, final)

            with mock.patch(
                "skill_sync.linking.create_directory_link", side_effect=fail_new_link
            ), mock.patch(
                "skill_sync.linking.os.replace",
                side_effect=AssertionError("rollback must not overwrite"),
            ):
                with self.assertRaisesRegex(OSError, "injected create failure"):
                    replace_directory_link(
                        new_source,
                        destination,
                        allowed_current_sources=[old_source],
                    )

            self.assertEqual(link_state(old_source, destination), "linked")
            self.assertEqual(list(destination.parent.glob(".alpha.previous-*")), [])

    def test_replace_never_deletes_real_directory_at_backup_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_source = root / "global" / "alpha"
            new_source = root / "rendered" / "alpha"
            destination = root / "agent" / "alpha"
            old_source.mkdir(parents=True)
            new_source.mkdir(parents=True)
            create_directory_link(old_source, destination)
            real_remove = linking_module._remove_verified_link_path
            raced_backup: Path | None = None

            def replace_backup_before_cleanup(path, source, *, identity=None):
                nonlocal raced_backup
                path = Path(path)
                if ".previous-" in path.name:
                    raced_backup = path
                    path.unlink()
                    path.mkdir()
                    (path / "keep.txt").write_text("keep", encoding="utf-8")
                return real_remove(path, source, identity=identity)

            with mock.patch(
                "skill_sync.linking._remove_verified_link_path",
                side_effect=replace_backup_before_cleanup,
            ):
                with self.assertRaisesRegex(OSError, "unverified link backup"):
                    replace_directory_link(
                        new_source,
                        destination,
                        allowed_current_sources=[old_source],
                    )

            self.assertIsNotNone(raced_backup)
            assert raced_backup is not None
            self.assertTrue(raced_backup.is_dir())
            self.assertEqual((raced_backup / "keep.txt").read_text(), "keep")
            self.assertEqual(link_state(new_source, destination), "linked")
