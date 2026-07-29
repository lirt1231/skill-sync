import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import skill_sync.edit_session as edit_session_module
from skill_sync.config import empty_config, save_config
from skill_sync.core import edit_abort, edit_begin, edit_delete, edit_session_paths
from skill_sync.edit_session import EditSessionStatus, EditSessionStore
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.registry import save_registry


class EditWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.skill = self.skills_root / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
        (self.skill / ".hidden").write_bytes(b"\x00\xffprivate")
        scripts = self.skill / "scripts"
        scripts.mkdir()
        executable = scripts / "run.sh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        config = empty_config()
        config["sync_repo_path"] = str(self.repo)
        config["skills_root"] = str(self.skills_root)
        config["data_root"] = str(self.root / "data")
        config["skills"] = {"alpha": {"local_path": str(self.skill)}}
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    "alpha": {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": "alpha",
                    }
                },
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_begin_snapshots_current_canonical_and_returns_writable_workspace(self):
        initial_hash = hash_skill_dir(self.skill)

        result = edit_begin(
            "alpha", actor="codex", config_path=self.config_path
        )

        baseline = Path(result["baseline_path"])
        workspace = Path(result["workspace_path"])
        self.assertTrue(baseline.is_absolute())
        self.assertTrue(workspace.is_absolute())
        self.assertEqual(result["scope"], "base")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["actor"], "codex")
        self.assertEqual(result["baseline_hash"], initial_hash)
        self.assertEqual(hash_skill_dir(baseline), initial_hash)
        self.assertEqual(hash_skill_dir(workspace), initial_hash)
        self.assertEqual((baseline / ".hidden").read_bytes(), b"\x00\xffprivate")

        (self.skill / "SKILL.md").write_text("# changed directly\n", encoding="utf-8")
        (workspace / "SKILL.md").write_text("# changed in workspace\n", encoding="utf-8")

        self.assertEqual((baseline / "SKILL.md").read_text(), "# alpha\n")
        self.assertEqual((workspace / "SKILL.md").read_text(), "# changed in workspace\n")
        self.assertEqual((self.skill / "SKILL.md").read_text(), "# changed directly\n")

    def test_session_status_returns_stable_absolute_workspace_paths(self):
        started = edit_begin("alpha", config_path=self.config_path)

        status = edit_session_paths(
            started["session_id"], config_path=self.config_path
        )

        self.assertEqual(status["baseline_path"], started["baseline_path"])
        self.assertEqual(status["workspace_path"], started["workspace_path"])
        self.assertTrue(Path(status["workspace_path"]).is_absolute())

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not portable to Windows")
    def test_begin_makes_baseline_private_read_only_and_workspace_private_writable(self):
        result = edit_begin("alpha", config_path=self.config_path)
        baseline = Path(result["baseline_path"])
        workspace = Path(result["workspace_path"])

        self.assertEqual(stat.S_IMODE(baseline.stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE((baseline / "SKILL.md").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((baseline / "scripts" / "run.sh").stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((workspace / "SKILL.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((workspace / "scripts" / "run.sh").stat().st_mode), 0o700)

    def test_duplicate_and_concurrent_begin_publish_only_one_active_session(self):
        results: list[dict] = []
        errors: list[SkillSyncError] = []
        barrier = threading.Barrier(2)

        def begin() -> None:
            barrier.wait()
            try:
                results.append(edit_begin("alpha", config_path=self.config_path))
            except SkillSyncError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=begin) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "active_edit_session")
        self.assertEqual(errors[0].exit_code, 3)
        with self.assertRaises(SkillSyncError) as duplicate:
            edit_begin("alpha", config_path=self.config_path)
        self.assertEqual(duplicate.exception.code, "active_edit_session")

    def test_abort_removes_only_session_content_and_never_changes_canonical(self):
        result = edit_begin("alpha", config_path=self.config_path)
        (self.skill / "SKILL.md").write_text(
            "# canonical changed during session\n", encoding="utf-8"
        )
        source_before = {
            path.relative_to(self.skill).as_posix(): path.read_bytes()
            for path in self.skill.rglob("*")
            if path.is_file()
        }
        workspace = Path(result["workspace_path"])
        (workspace / "SKILL.md").write_text("# discard me\n", encoding="utf-8")

        aborted = edit_abort(result["session_id"], config_path=self.config_path)

        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(Path(result["baseline_path"]).exists())
        self.assertFalse(workspace.exists())
        source_after = {
            path.relative_to(self.skill).as_posix(): path.read_bytes()
            for path in self.skill.rglob("*")
            if path.is_file()
        }
        self.assertEqual(source_after, source_before)
        metadata = EditSessionStore(self.root / "data").load(result["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.ABORTED)

    def test_delete_removes_active_session_without_changing_canonical(self):
        result = edit_begin("alpha", config_path=self.config_path)
        workspace = Path(result["workspace_path"])
        (workspace / "SKILL.md").write_text("# discard me\n", encoding="utf-8")
        canonical_before = (self.skill / "SKILL.md").read_bytes()

        deleted = edit_delete(result["session_id"], config_path=self.config_path)

        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["previous_status"], "active")
        self.assertEqual(deleted["cleanup_pending"], [])
        self.assertFalse(workspace.parent.exists())
        self.assertEqual((self.skill / "SKILL.md").read_bytes(), canonical_before)
        self.assertEqual(EditSessionStore(self.root / "data").list_metadata(), [])
        replacement = edit_begin("alpha", config_path=self.config_path)
        self.assertEqual(replacement["status"], "active")

    def test_delete_blocks_recovery_states_and_rejects_linked_content(self):
        result = edit_begin("alpha", config_path=self.config_path)
        store = EditSessionStore(self.root / "data")
        store.transition(result["session_id"], EditSessionStatus.APPLYING)
        with self.assertRaises(SkillSyncError) as blocked:
            edit_delete(result["session_id"], config_path=self.config_path)
        self.assertEqual(blocked.exception.code, "edit_delete_blocked")
        self.assertTrue(Path(result["workspace_path"]).exists())

        store.transition(result["session_id"], EditSessionStatus.NEEDS_RECOVERY)
        with self.assertRaises(SkillSyncError) as recovery_blocked:
            edit_delete(result["session_id"], config_path=self.config_path)
        self.assertEqual(recovery_blocked.exception.code, "edit_delete_blocked")
        store.transition(result["session_id"], EditSessionStatus.ACTIVE)
        linked = Path(result["workspace_path"]) / "outside"
        linked.symlink_to(self.skill, target_is_directory=True)
        with self.assertRaises(SkillSyncError) as unsafe:
            edit_delete(result["session_id"], config_path=self.config_path)
        self.assertEqual(unsafe.exception.code, "unsafe_edit_session")
        self.assertTrue(Path(result["workspace_path"]).exists())

    def test_delete_removes_aborted_session_history(self):
        result = edit_begin("alpha", config_path=self.config_path)
        edit_abort(result["session_id"], config_path=self.config_path)

        deleted = edit_delete(result["session_id"], config_path=self.config_path)

        self.assertEqual(deleted["previous_status"], "aborted")
        self.assertFalse(Path(result["workspace_path"]).parent.exists())

    def test_begin_rejects_source_change_during_snapshot_without_publishing_session(self):
        store = EditSessionStore(self.root / "data")
        stale_hash = hash_skill_dir(self.skill)
        (self.skill / "SKILL.md").write_text("# newer\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "changed"):
            store.begin(
                logical_skill="alpha",
                source=self.skill,
                baseline_hash=stale_hash,
            )

        published = [
            path for path in store.root.iterdir() if not path.name.startswith(".")
        ]
        self.assertEqual(published, [])

    def test_begin_rejects_symlink_in_source_and_leaves_no_session(self):
        target = self.root / "outside.txt"
        target.write_text("outside\n", encoding="utf-8")
        (self.skill / "linked.txt").symlink_to(target)

        with self.assertRaises(SkillSyncError) as raised:
            edit_begin("alpha", config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_begin_failed")
        store = EditSessionStore(self.root / "data")
        if store.root.exists():
            self.assertEqual(
                [path for path in store.root.iterdir() if not path.name.startswith(".")],
                [],
            )

    def test_begin_no_replace_preserves_a_concurrent_destination_winner(self):
        store = EditSessionStore(self.root / "data")
        original_rename = edit_session_module.rename_no_replace
        winner_paths: list[Path] = []

        def publish_winner(source: Path, destination: Path) -> None:
            destination = Path(destination)
            destination.mkdir()
            (destination / "winner.txt").write_text("keep me\n", encoding="utf-8")
            winner_paths.append(destination)
            original_rename(source, destination)

        with mock.patch.object(
            edit_session_module,
            "rename_no_replace",
            side_effect=publish_winner,
        ):
            with self.assertRaises(FileExistsError):
                store.begin(
                    logical_skill="alpha",
                    source=self.skill,
                    baseline_hash=hash_skill_dir(self.skill),
                )

        self.assertEqual(len(winner_paths), 1)
        self.assertEqual(
            (winner_paths[0] / "winner.txt").read_text(encoding="utf-8"),
            "keep me\n",
        )
        self.assertFalse((winner_paths[0] / "session.json").exists())
        self.assertEqual(
            [path for path in store.root.iterdir() if path.name.startswith(".begin-")],
            [],
        )

    def test_active_begin_staging_makes_inspection_fail_closed_until_publish(self):
        store = EditSessionStore(self.root / "data")
        baseline_hash = hash_skill_dir(self.skill)
        copy_started = threading.Event()
        allow_copy = threading.Event()
        original_copy = edit_session_module.copy_skill_dir
        results: list[tuple] = []
        errors: list[BaseException] = []

        def pause_first_copy(source: Path, destination: Path) -> str:
            if Path(destination).name == "baseline" and not copy_started.is_set():
                copy_started.set()
                if not allow_copy.wait(timeout=3):
                    raise TimeoutError("test did not release begin copy")
            return original_copy(source, destination)

        def begin() -> None:
            try:
                results.append(
                    store.begin(
                        logical_skill="alpha",
                        source=self.skill,
                        baseline_hash=baseline_hash,
                    )
                )
            except BaseException as exc:  # capture thread failure for the assertion
                errors.append(exc)

        with mock.patch.object(
            edit_session_module,
            "copy_skill_dir",
            side_effect=pause_first_copy,
        ):
            thread = threading.Thread(target=begin)
            thread.start()
            self.assertTrue(copy_started.wait(timeout=3))
            with self.assertRaisesRegex(ValueError, "unexpected entry"):
                store.list_metadata()
            allow_copy.set()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(store.list_metadata(), [results[0][0]])

    def test_different_skills_can_publish_while_their_staging_copies_overlap(self):
        beta = self.skills_root / "beta"
        beta.mkdir()
        (beta / "SKILL.md").write_text("# beta\n", encoding="utf-8")
        store = EditSessionStore(self.root / "data")
        alpha_copy_started = threading.Event()
        beta_copy_started = threading.Event()
        original_copy = edit_session_module.copy_skill_dir
        results: list[tuple] = []
        errors: list[BaseException] = []

        def overlap_baseline_copy(source: Path, destination: Path) -> str:
            if Path(destination).name == "baseline":
                if Path(source).name == "alpha":
                    alpha_copy_started.set()
                    if not beta_copy_started.wait(timeout=3):
                        raise TimeoutError("beta begin did not overlap alpha staging")
                elif Path(source).name == "beta":
                    beta_copy_started.set()
            return original_copy(source, destination)

        def begin(name: str, source: Path) -> None:
            try:
                results.append(
                    store.begin(
                        logical_skill=name,
                        source=source,
                        baseline_hash=hash_skill_dir(source),
                    )
                )
            except BaseException as exc:  # capture thread failure for the assertion
                errors.append(exc)

        with mock.patch.object(
            edit_session_module,
            "copy_skill_dir",
            side_effect=overlap_baseline_copy,
        ):
            alpha_thread = threading.Thread(
                target=begin, args=("alpha", self.skill)
            )
            beta_thread = threading.Thread(target=begin, args=("beta", beta))
            threads = [alpha_thread, beta_thread]
            alpha_thread.start()
            self.assertTrue(alpha_copy_started.wait(timeout=3))
            beta_thread.start()
            for thread in threads:
                thread.join(timeout=4)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(
            {metadata.logical_skill for metadata, _ in results},
            {"alpha", "beta"},
        )
        self.assertEqual(
            {metadata.logical_skill for metadata in store.list_metadata()},
            {"alpha", "beta"},
        )


if __name__ == "__main__":
    unittest.main()
