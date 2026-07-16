import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync import cli
import skill_sync.core as core_module
import skill_sync.edit_apply as edit_apply_module
import skill_sync.edit_session as edit_session_module
from skill_sync.config import empty_config, save_config
from skill_sync.core import edit_apply, edit_begin
from skill_sync.edit_session import EditSessionStatus, EditSessionStore
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.registry import save_registry


def run_cli(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


SKILL_TEXT = "---\nname: alpha\ndescription: Test Skill\n---\n\n# Alpha\n"
UPDATED_TEXT = (
    "---\nname: alpha\ndescription: Updated Test Skill\n---\n\n# Updated Alpha\n"
)


class EditApplyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.skill = self.skills_root / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        (self.skill / "kept.bin").write_bytes(b"\x00original\xff")
        self.data_root = self.root / "data"

        config = empty_config()
        config.update(
            {
                "sync_repo_path": str(self.repo),
                "skills_root": str(self.skills_root),
                "data_root": str(self.data_root),
                "skills": {"alpha": {"local_path": str(self.skill)}},
            }
        )
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

    def begin_changed(self) -> dict:
        session = edit_begin("alpha", actor="codex", config_path=self.config_path)
        workspace = Path(session["workspace_path"])
        (workspace / "SKILL.md").write_text(UPDATED_TEXT, encoding="utf-8")
        (workspace / "secret.txt").write_text(
            "TOP-SECRET-CONTENT-MUST-NOT-ENTER-RECEIPT", encoding="utf-8"
        )
        return session

    def receipts(self) -> list[Path]:
        operations = self.data_root / "operations"
        return sorted(operations.glob("edit-apply-*.json")) if operations.exists() else []

    def test_apply_replaces_canonical_keeps_backup_and_writes_redacted_receipt(self):
        baseline_hash = hash_skill_dir(self.skill)
        session = self.begin_changed()
        workspace_hash = hash_skill_dir(Path(session["workspace_path"]))

        with mock.patch.object(
            core_module.git,
            "run_git",
            side_effect=AssertionError("edit apply must not access Git"),
        ), mock.patch.object(
            core_module,
            "detect_clients",
            side_effect=AssertionError("5.6 must not rebuild deployments"),
        ):
            result = edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["previous_hash"], baseline_hash)
        self.assertEqual(result["applied_hash"], workspace_hash)
        self.assertFalse(result["deployments_rebuilt"])
        self.assertEqual(hash_skill_dir(self.skill), workspace_hash)
        self.assertEqual((self.skill / "SKILL.md").read_text(), UPDATED_TEXT)

        backup = Path(result["backup_path"])
        receipt_path = Path(result["receipt_path"])
        self.assertTrue(backup.is_dir())
        self.assertEqual(hash_skill_dir(backup), baseline_hash)
        self.assertEqual(self.receipts(), [receipt_path])
        receipt_text = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn("TOP-SECRET-CONTENT-MUST-NOT-ENTER-RECEIPT", receipt_text)
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["baseline_hash"], baseline_hash)
        self.assertEqual(receipt["workspace_hash"], workspace_hash)
        self.assertNotIn("files", receipt)
        self.assertNotIn("diff", receipt)
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.APPLIED)
        self.assertEqual(
            [path.name for path in self.skills_root.iterdir()],
            ["alpha"],
        )

    def test_apply_rejects_stale_canonical_without_creating_backup_or_receipt(self):
        session = self.begin_changed()
        (self.skill / "SKILL.md").write_text(
            SKILL_TEXT + "\nDirect canonical winner\n", encoding="utf-8"
        )
        winner_hash = hash_skill_dir(self.skill)

        with self.assertRaises(SkillSyncError) as raised:
            edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_baseline_conflict")
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertEqual(hash_skill_dir(self.skill), winner_hash)
        self.assertEqual(self.receipts(), [])
        self.assertFalse((self.data_root / "backups").exists())
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.ACTIVE,
        )

    def test_apply_rejects_invalid_or_unchanged_workspace_before_mutation(self):
        invalid = edit_begin("alpha", config_path=self.config_path)
        Path(invalid["workspace_path"]).joinpath("SKILL.md").unlink()
        with self.assertRaises(SkillSyncError) as invalid_error:
            edit_apply(invalid["session_id"], config_path=self.config_path)
        self.assertEqual(invalid_error.exception.code, "invalid_edit_workspace")
        self.assertEqual(invalid_error.exception.exit_code, 4)
        self.assertEqual(self.receipts(), [])

        EditSessionStore(self.data_root).abort(invalid["session_id"])
        unchanged = edit_begin("alpha", config_path=self.config_path)
        with self.assertRaises(SkillSyncError) as unchanged_error:
            edit_apply(unchanged["session_id"], config_path=self.config_path)
        self.assertEqual(unchanged_error.exception.code, "edit_workspace_unchanged")
        self.assertEqual(unchanged_error.exception.exit_code, 3)
        self.assertEqual(self.receipts(), [])

    def test_install_failure_rolls_back_canonical_and_reactivates_session(self):
        session = self.begin_changed()
        original_hash = hash_skill_dir(self.skill)
        original_rename = edit_apply_module.rename_no_replace

        def fail_stage_install(source: Path, destination: Path) -> None:
            if (
                Path(source).name.startswith(".alpha.apply-stage-")
                and Path(destination) == self.skill
            ):
                raise OSError("simulated install interruption")
            original_rename(source, destination)

        with mock.patch.object(
            edit_apply_module,
            "rename_no_replace",
            side_effect=fail_stage_install,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(hash_skill_dir(self.skill), original_hash)
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.ACTIVE)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "rolled-back")
        self.assertNotIn("simulated install interruption", json.dumps(receipt))

    def test_concurrent_canonical_winner_is_preserved_and_requires_recovery(self):
        session = self.begin_changed()
        original_rename = edit_apply_module.rename_no_replace
        winner_text = SKILL_TEXT + "\nConcurrent winner\n"

        def publish_winner(source: Path, destination: Path) -> None:
            if (
                Path(source).name.startswith(".alpha.apply-stage-")
                and Path(destination) == self.skill
            ):
                self.skill.mkdir()
                (self.skill / "SKILL.md").write_text(winner_text, encoding="utf-8")
            original_rename(source, destination)

        with mock.patch.object(
            edit_apply_module,
            "rename_no_replace",
            side_effect=publish_winner,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_recovery_required")
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual((self.skill / "SKILL.md").read_text(), winner_text)
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.NEEDS_RECOVERY)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "needs-recovery")
        self.assertTrue(Path(receipt["recovery_path"]).is_dir())

    def test_metadata_failure_after_swap_rolls_back_source_and_receipt(self):
        session = self.begin_changed()
        original_hash = hash_skill_dir(self.skill)
        original_transition = EditSessionStore.transition_locked

        def fail_applied_transition(store, session_id, status, **kwargs):
            if status is EditSessionStatus.APPLIED:
                raise OSError("simulated metadata fsync failure")
            return original_transition(store, session_id, status, **kwargs)

        with mock.patch.object(
            EditSessionStore,
            "transition_locked",
            autospec=True,
            side_effect=fail_applied_transition,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertEqual(hash_skill_dir(self.skill), original_hash)
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.ACTIVE)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "rolled-back")

    def test_receipt_atomic_writes_fsync_the_operations_directory(self):
        session = self.begin_changed()
        fsynced: list[Path] = []
        original_fsync = edit_apply_module.fsync_directory

        def record_fsync(path: Path) -> None:
            fsynced.append(Path(path))
            original_fsync(path)

        with mock.patch.object(
            edit_apply_module, "fsync_directory", side_effect=record_fsync
        ):
            result = edit_apply(session["session_id"], config_path=self.config_path)

        self.assertIn(Path(result["receipt_path"]).parent, fsynced)

    def test_apply_uses_deployment_lock_before_the_per_skill_lock(self):
        session = self.begin_changed()
        events: list[str] = []
        original_skill_lock = EditSessionStore.skill_lock

        @contextlib.contextmanager
        def deployment_lock(path, **kwargs):
            events.append("deployment-enter")
            yield
            events.append("deployment-exit")

        @contextlib.contextmanager
        def skill_lock(store, logical_skill, **kwargs):
            events.append("skill-enter")
            with original_skill_lock(store, logical_skill, **kwargs):
                yield
            events.append("skill-exit")

        with mock.patch.object(
            core_module, "local_file_lock", side_effect=deployment_lock
        ), mock.patch.object(
            EditSessionStore, "skill_lock", autospec=True, side_effect=skill_lock
        ):
            edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(
            events,
            ["deployment-enter", "skill-enter", "skill-exit", "deployment-exit"],
        )

    def test_apply_rejects_terminal_session_and_existing_receipt(self):
        session = self.begin_changed()
        store = EditSessionStore(self.data_root)
        store.abort(session["session_id"])
        with self.assertRaises(SkillSyncError) as terminal:
            edit_apply(session["session_id"], config_path=self.config_path)
        self.assertEqual(terminal.exception.code, "edit_session_not_active")

        second = self.begin_changed()
        receipt = self.data_root / "operations" / (
            f"edit-apply-{second['session_id']}.json"
        )
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{tampered", encoding="utf-8")
        canonical_hash = hash_skill_dir(self.skill)
        with self.assertRaises(SkillSyncError) as existing:
            edit_apply(second["session_id"], config_path=self.config_path)
        self.assertEqual(existing.exception.code, "edit_apply_recovery_required")
        self.assertEqual(hash_skill_dir(self.skill), canonical_hash)
        self.assertEqual(receipt.read_text(encoding="utf-8"), "{tampered")

    def test_candidate_copy_failure_never_deletes_an_external_winner(self):
        session = self.begin_changed()
        candidate = self.skills_root / f".alpha.apply-stage-{session['session_id']}"

        def publish_candidate_winner(source: Path, destination: Path) -> str:
            destination = Path(destination)
            destination.mkdir()
            (destination / "winner.txt").write_text("preserve me\n", encoding="utf-8")
            raise FileExistsError("candidate winner")

        with mock.patch.object(
            edit_apply_module,
            "copy_skill_dir",
            side_effect=publish_candidate_winner,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertTrue(candidate.is_dir())
        self.assertEqual(
            (candidate / "winner.txt").read_text(encoding="utf-8"),
            "preserve me\n",
        )

    def test_same_content_pre_move_winner_is_restored_to_canonical_slot(self):
        session = self.begin_changed()
        original_rename = edit_apply_module.rename_no_replace
        external_original = self.skills_root / "external-original"
        winner_identity: list[tuple[int, int]] = []

        def replace_before_source_move(source: Path, destination: Path) -> None:
            source = Path(source)
            destination = Path(destination)
            if source == self.skill and destination.name.startswith(
                ".alpha.apply-previous-"
            ):
                original_rename(self.skill, external_original)
                shutil.copytree(external_original, self.skill)
                metadata = os.lstat(self.skill)
                winner_identity.append((metadata.st_dev, metadata.st_ino))
            original_rename(source, destination)

        with mock.patch.object(
            edit_apply_module,
            "rename_no_replace",
            side_effect=replace_before_source_move,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertEqual(len(winner_identity), 1)
        current = os.lstat(self.skill)
        self.assertEqual((current.st_dev, current.st_ino), winner_identity[0])
        self.assertEqual(hash_skill_dir(self.skill), session["baseline_hash"])

    def test_ambiguous_applying_metadata_fsync_enters_recovery(self):
        session = self.begin_changed()
        original_fsync = edit_session_module._fsync_directory
        calls = 0

        def fail_after_first_metadata_replace(path: Path) -> None:
            nonlocal calls
            calls += 1
            original_fsync(path)
            if calls == 1:
                raise OSError("ambiguous applying fsync")

        with mock.patch.object(
            edit_session_module,
            "_fsync_directory",
            side_effect=fail_after_first_metadata_replace,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_recovery_required")
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.NEEDS_RECOVERY)
        self.assertEqual(hash_skill_dir(self.skill), session["baseline_hash"])
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "needs-recovery")

    def test_ambiguous_applied_metadata_fsync_keeps_new_source_recoverable(self):
        session = self.begin_changed()
        workspace_hash = hash_skill_dir(Path(session["workspace_path"]))
        original_fsync = edit_session_module._fsync_directory
        calls = 0

        def fail_after_second_metadata_replace(path: Path) -> None:
            nonlocal calls
            calls += 1
            original_fsync(path)
            if calls == 2:
                raise OSError("ambiguous applied fsync")

        with mock.patch.object(
            edit_session_module,
            "_fsync_directory",
            side_effect=fail_after_second_metadata_replace,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_recovery_required")
        self.assertEqual(hash_skill_dir(self.skill), workspace_hash)
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.APPLIED)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "needs-recovery")
        self.assertTrue(Path(receipt["recovery_path"]).is_dir())

    def test_process_interruption_after_applying_is_restart_inspectable(self):
        session = self.begin_changed()
        with mock.patch.object(
            edit_apply_module.CanonicalSwap,
            "apply",
            side_effect=SystemExit("simulated process exit"),
        ):
            with self.assertRaises(SystemExit):
                edit_apply(session["session_id"], config_path=self.config_path)

        restarted = EditSessionStore(self.data_root)
        self.assertEqual(
            restarted.load(session["session_id"]).status,
            EditSessionStatus.APPLYING,
        )
        self.assertEqual(hash_skill_dir(self.skill), session["baseline_hash"])
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "applying")
        self.assertEqual(receipt["phase"], "canonical-replace")

    def test_process_interruption_after_candidate_publish_preserves_both_versions(self):
        session = self.begin_changed()
        workspace_hash = hash_skill_dir(Path(session["workspace_path"]))
        original_rename = edit_apply_module.rename_no_replace

        def exit_after_candidate_publish(source: Path, destination: Path) -> None:
            source = Path(source)
            destination = Path(destination)
            original_rename(source, destination)
            if (
                source.name.startswith(".alpha.apply-stage-")
                and destination == self.skill
            ):
                raise SystemExit("simulated process exit after canonical publish")

        with mock.patch.object(
            edit_apply_module,
            "rename_no_replace",
            side_effect=exit_after_candidate_publish,
        ):
            with self.assertRaises(SystemExit):
                edit_apply(session["session_id"], config_path=self.config_path)

        restarted = EditSessionStore(self.data_root)
        self.assertEqual(
            restarted.load(session["session_id"]).status,
            EditSessionStatus.APPLYING,
        )
        self.assertEqual(hash_skill_dir(self.skill), workspace_hash)
        previous = self.skills_root / (
            f".alpha.apply-previous-{session['session_id']}"
        )
        self.assertEqual(hash_skill_dir(previous), session["baseline_hash"])
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["phase"], "canonical-replace")

    def test_receipt_tamper_during_apply_is_preserved_and_blocks_canonical(self):
        session = self.begin_changed()
        original_prepare = edit_apply_module.CanonicalSwap.prepare

        def tamper_receipt(*args, **kwargs):
            swap = original_prepare(*args, **kwargs)
            receipt = self.receipts()[0]
            receipt.write_text("external receipt winner", encoding="utf-8")
            return swap

        with mock.patch.object(
            edit_apply_module.CanonicalSwap,
            "prepare",
            side_effect=tamper_receipt,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_recovery_required")
        self.assertEqual(
            self.receipts()[0].read_text(encoding="utf-8"),
            "external receipt winner",
        )
        self.assertEqual(hash_skill_dir(self.skill), session["baseline_hash"])
        metadata = EditSessionStore(self.data_root).load(session["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.NEEDS_RECOVERY)

    def test_rollback_race_restores_external_winner_instead_of_deleting_it(self):
        session = self.begin_changed()
        original_rename = edit_apply_module.rename_no_replace
        original_hash = edit_apply_module.hash_skill_dir
        installed_displaced = self.skills_root / "installed-displaced"
        installed = False
        force_verification_failure = False
        winner_identity: list[tuple[int, int]] = []
        winner_text = SKILL_TEXT + "\nRollback winner\n"

        def race_rename(source: Path, destination: Path) -> None:
            nonlocal installed, force_verification_failure
            source = Path(source)
            destination = Path(destination)
            if source == self.skill and destination.name.startswith(
                ".alpha.apply-failed-"
            ):
                original_rename(self.skill, installed_displaced)
                self.skill.mkdir()
                (self.skill / "SKILL.md").write_text(winner_text, encoding="utf-8")
                metadata = os.lstat(self.skill)
                winner_identity.append((metadata.st_dev, metadata.st_ino))
            original_rename(source, destination)
            if (
                source.name.startswith(".alpha.apply-stage-")
                and destination == self.skill
            ):
                installed = True
                force_verification_failure = True

        def fail_first_installed_hash(path: Path) -> str:
            nonlocal force_verification_failure
            if installed and Path(path) == self.skill and force_verification_failure:
                force_verification_failure = False
                return "sha256:" + "0" * 64
            return original_hash(path)

        with mock.patch.object(
            edit_apply_module, "rename_no_replace", side_effect=race_rename
        ), mock.patch.object(
            edit_apply_module, "hash_skill_dir", side_effect=fail_first_installed_hash
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_recovery_required")
        self.assertEqual(len(winner_identity), 1)
        current = os.lstat(self.skill)
        self.assertEqual((current.st_dev, current.st_ino), winner_identity[0])
        self.assertEqual((self.skill / "SKILL.md").read_text(), winner_text)
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.NEEDS_RECOVERY,
        )


class EditApplyCliTest(unittest.TestCase):
    def test_edit_apply_uses_shared_json_envelope_and_text_summary(self):
        result = {
            "session_id": "12345678-1234-4234-9234-123456789abc",
            "skill": "alpha",
            "scope": "base",
            "status": "applied",
            "previous_hash": "sha256:" + "a" * 64,
            "applied_hash": "sha256:" + "b" * 64,
            "backup_path": "/tmp/backup",
            "receipt_path": "/tmp/receipt.json",
            "deployments_rebuilt": False,
        }
        with mock.patch.object(cli.core, "edit_apply", return_value=result) as apply:
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    "/tmp/config.json",
                    "edit",
                    "apply",
                    result["session_id"],
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        apply.assert_called_once_with(
            result["session_id"], config_path="/tmp/config.json"
        )
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "edit apply")
        self.assertEqual(envelope["result"], result)


if __name__ == "__main__":
    unittest.main()
