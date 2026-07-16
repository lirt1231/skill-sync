import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_sync.core as core_module
import skill_sync.edit_recovery as recovery_module
import skill_sync.edit_session as edit_session_module
from skill_sync.agents import AgentClient
from skill_sync.config import empty_config, save_config
from skill_sync.core import edit_begin, edit_recover
from skill_sync.deployment import (
    PROVENANCE_FILE,
    render_base_deployment,
    verify_deployment,
)
from skill_sync.edit_session import EditSessionStatus, EditSessionStore
from skill_sync.edit_apply import PrivateJsonReceipt, ReceiptRecoveryRequired
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.linking import create_directory_link
from skill_sync.registry import save_registry


CANONICAL_TEXT = (
    "---\nname: alpha\ndescription: Canonical Skill\n---\n\n# Alpha\n"
)
TAMPERED_TEXT = (
    "---\nname: alpha\ndescription: Captured Agent Edit\n---\n\n# Alpha changed\n"
)
SECRET = "RECOVERY-SECRET-MUST-NOT-ENTER-RECEIPT"


class EditRecoveryTest(unittest.TestCase):
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
        (self.skill / "SKILL.md").write_text(CANONICAL_TEXT, encoding="utf-8")
        (self.skill / "notes.txt").write_text("canonical note\n", encoding="utf-8")
        self.data_root = self.root / "data"
        self.client = AgentClient(
            "codex",
            "codex",
            "Codex",
            self.root / "clients" / "codex" / "skills",
            True,
        )

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
        self.deployment = render_base_deployment(
            self.skill,
            self.data_root / "rendered",
            "alpha",
            self.client.id,
        ).path
        self.destination = self.client.skills_dir / "alpha"
        create_directory_link(self.deployment, self.destination)

        detected = mock.patch.object(
            core_module, "detect_clients", return_value=[self.client]
        )
        detected.start()
        self.addCleanup(detected.stop)

    def _make_writable(self, path: Path) -> None:
        path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)

    def tamper_authored_content(self) -> None:
        self._make_writable(self.deployment)
        skill_file = self.deployment / "SKILL.md"
        self._make_writable(skill_file)
        skill_file.write_text(TAMPERED_TEXT, encoding="utf-8")
        (self.deployment / "agent-note.txt").write_text(
            f"agent change: {SECRET}\n", encoding="utf-8"
        )

    def receipts(self) -> list[Path]:
        operations = self.data_root / "operations"
        if not operations.exists():
            return []
        return sorted(operations.glob("edit-recover-*.json"))

    def test_preview_is_read_only_and_excludes_provenance_from_tampered_diff(self):
        self.tamper_authored_content()
        canonical_hash = hash_skill_dir(self.skill)
        canonical_before = (self.skill / "SKILL.md").read_bytes()
        link_before = os.readlink(self.destination) if self.destination.is_symlink() else None
        provenance_before = (self.deployment / PROVENANCE_FILE).read_bytes()

        result = edit_recover(
            "alpha", client="codex", config_path=self.config_path
        )

        self.assertEqual(result["skill"], "alpha")
        self.assertEqual(result["client"], "codex")
        self.assertEqual(result["state"], "tampered-render")
        self.assertEqual(result["action"], "preview")
        self.assertEqual(Path(result["canonical_path"]), self.skill)
        self.assertEqual(result["canonical_hash"], canonical_hash)
        self.assertEqual(Path(result["deployment_path"]), self.deployment)
        self.assertRegex(result["tampered_authored_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result["allowed_actions"], ["capture", "discard"])
        self.assertIsNone(result["blocked_by_session"])
        self.assertTrue(result["diff"]["changed"])
        files = {item["path"]: item for item in result["diff"]["files"]}
        self.assertIn("SKILL.md", files)
        self.assertIn("agent-note.txt", files)
        self.assertNotIn(PROVENANCE_FILE, files)
        self.assertEqual((self.skill / "SKILL.md").read_bytes(), canonical_before)
        self.assertEqual((self.deployment / PROVENANCE_FILE).read_bytes(), provenance_before)
        if link_before is not None:
            self.assertEqual(os.readlink(self.destination), link_before)
        self.assertEqual(self.receipts(), [])

    def test_capture_creates_active_base_session_without_changing_source_or_link(self):
        self.tamper_authored_content()
        canonical_hash = hash_skill_dir(self.skill)
        canonical_before = (self.skill / "SKILL.md").read_bytes()
        link_before = os.readlink(self.destination) if self.destination.is_symlink() else None
        deployment_before = self.deployment.resolve()

        result = edit_recover(
            "alpha", client="codex", action="capture", config_path=self.config_path
        )

        self.assertEqual(result["action"], "capture")
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["state"], "tampered-render")
        self.assertEqual(result["canonical_hash"], canonical_hash)
        self.assertEqual(Path(result["deployment_path"]), self.deployment)
        workspace = Path(result["workspace_path"])
        self.assertEqual((workspace / "SKILL.md").read_text(encoding="utf-8"), TAMPERED_TEXT)
        self.assertEqual(
            (workspace / "agent-note.txt").read_text(encoding="utf-8"),
            f"agent change: {SECRET}\n",
        )
        self.assertFalse((workspace / PROVENANCE_FILE).exists())
        metadata = EditSessionStore(self.data_root).load(result["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.ACTIVE)
        self.assertEqual(metadata.logical_skill, "alpha")
        self.assertEqual(metadata.baseline_hash, canonical_hash)
        self.assertEqual((self.skill / "SKILL.md").read_bytes(), canonical_before)
        self.assertEqual(self.destination.resolve(), deployment_before)
        if link_before is not None:
            self.assertEqual(os.readlink(self.destination), link_before)

        receipt_path = Path(result["receipt_path"])
        self.assertEqual(self.receipts(), [receipt_path])
        receipt_text = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, receipt_text)
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["operation"], "edit-recover")
        self.assertEqual(receipt["action"], "capture")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["session_id"], result["session_id"])
        self.assertNotIn("diff", receipt)
        self.assertNotIn("files", receipt)

    def test_discard_rebuilds_same_expected_deployment_and_preserves_link(self):
        self.tamper_authored_content()
        canonical_hash = hash_skill_dir(self.skill)
        link_before = os.readlink(self.destination) if self.destination.is_symlink() else None
        deployment_before = self.destination.resolve()

        result = edit_recover(
            "alpha", client="codex", action="discard", config_path=self.config_path
        )

        self.assertEqual(result["action"], "discard")
        self.assertEqual(result["status"], "discarded")
        self.assertEqual(result["canonical_hash"], canonical_hash)
        self.assertEqual(Path(result["deployment_path"]), self.deployment)
        self.assertEqual(result["cleanup_pending"], [])
        self.assertEqual(self.destination.resolve(), deployment_before)
        if link_before is not None:
            self.assertEqual(os.readlink(self.destination), link_before)
        verification = verify_deployment(self.deployment)
        self.assertTrue(verification.ok)
        self.assertEqual(
            (self.deployment / "SKILL.md").read_text(encoding="utf-8"),
            CANONICAL_TEXT,
        )
        self.assertFalse((self.deployment / "agent-note.txt").exists())
        self.assertEqual(hash_skill_dir(self.skill), canonical_hash)

        receipt_path = Path(result["receipt_path"])
        receipt_text = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, receipt_text)
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["operation"], "edit-recover")
        self.assertEqual(receipt["action"], "discard")
        self.assertEqual(receipt["status"], "completed")
        self.assertNotIn("session_id", receipt)
        self.assertNotIn("diff", receipt)
        self.assertNotIn("files", receipt)

    def test_active_applying_and_needs_recovery_sessions_block_recovery_actions(self):
        self.tamper_authored_content()
        session = edit_begin("alpha", config_path=self.config_path)
        store = EditSessionStore(self.data_root)

        for expected_status in (
            EditSessionStatus.ACTIVE,
            EditSessionStatus.APPLYING,
            EditSessionStatus.NEEDS_RECOVERY,
        ):
            if expected_status is EditSessionStatus.APPLYING:
                store.transition(
                    session["session_id"],
                    EditSessionStatus.APPLYING,
                )
            elif expected_status is EditSessionStatus.NEEDS_RECOVERY:
                store.transition(
                    session["session_id"],
                    EditSessionStatus.NEEDS_RECOVERY,
                )
            with self.subTest(status=expected_status.value):
                preview = edit_recover(
                    "alpha", client="codex", config_path=self.config_path
                )
                self.assertEqual(
                    preview["blocked_by_session"],
                    {
                        "session_id": session["session_id"],
                        "status": expected_status.value,
                    },
                )
                for action in ("capture", "discard"):
                    with self.subTest(action=action):
                        with self.assertRaises(SkillSyncError) as raised:
                            edit_recover(
                                "alpha",
                                client="codex",
                                action=action,
                                config_path=self.config_path,
                            )
                        self.assertEqual(raised.exception.exit_code, 4)
                        self.assertEqual(
                            raised.exception.details["session_id"],
                            session["session_id"],
                        )
                        self.assertEqual(
                            raised.exception.details["status"],
                            expected_status.value,
                        )
        self.assertEqual(self.receipts(), [])

    def test_healthy_wrong_link_and_unsafe_tamper_fail_closed(self):
        with self.assertRaises(SkillSyncError) as healthy:
            edit_recover("alpha", client="codex", config_path=self.config_path)
        self.assertEqual(healthy.exception.exit_code, 3)
        self.assertEqual(self.receipts(), [])

        other = self.skills_root / "other"
        other.mkdir()
        (other / "SKILL.md").write_text("# other\n", encoding="utf-8")
        self.destination.unlink()
        self.destination.symlink_to(other, target_is_directory=True)
        with self.assertRaises(SkillSyncError) as wrong:
            edit_recover(
                "alpha",
                client="codex",
                action="discard",
                config_path=self.config_path,
            )
        self.assertEqual(wrong.exception.exit_code, 3)
        self.assertTrue(self.destination.samefile(other))
        self.assertEqual(self.receipts(), [])

        self.destination.unlink()
        self.destination.symlink_to(self.deployment, target_is_directory=True)
        self._make_writable(self.deployment)
        skill_file = self.deployment / "SKILL.md"
        self._make_writable(skill_file)
        skill_file.unlink()
        outside = self.root / "outside-secret.txt"
        outside.write_text(SECRET, encoding="utf-8")
        skill_file.symlink_to(outside)
        with self.assertRaises(SkillSyncError) as unsafe:
            edit_recover(
                "alpha",
                client="codex",
                action="capture",
                config_path=self.config_path,
            )
        self.assertEqual(unsafe.exception.exit_code, 4)
        self.assertEqual(outside.read_text(encoding="utf-8"), SECRET)
        self.assertTrue(skill_file.is_symlink())
        self.assertEqual(self.receipts(), [])

    def test_discard_render_failure_restores_exact_tampered_deployment(self):
        self.tamper_authored_content()
        identity = recovery_module._path_identity(self.deployment)
        link_before = os.readlink(self.destination)

        with mock.patch.object(
            core_module,
            "render_base_deployment",
            side_effect=OSError("render failed"),
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_recover(
                    "alpha",
                    client="codex",
                    action="discard",
                    config_path=self.config_path,
                )

        self.assertEqual(raised.exception.code, "edit_recovery_failed")
        self.assertEqual(recovery_module._path_identity(self.deployment), identity)
        self.assertEqual(
            (self.deployment / "SKILL.md").read_text(encoding="utf-8"),
            TAMPERED_TEXT,
        )
        self.assertEqual(os.readlink(self.destination), link_before)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "rolled-back")

    def test_discard_cleanup_error_is_terminal_cleanup_pending(self):
        self.tamper_authored_content()

        with mock.patch.object(
            core_module.DeploymentQuarantine,
            "finalize",
            autospec=True,
            side_effect=OSError("cleanup failed"),
        ):
            result = edit_recover(
                "alpha",
                client="codex",
                action="discard",
                config_path=self.config_path,
            )

        self.assertEqual(result["status"], "discarded")
        self.assertEqual(len(result["cleanup_pending"]), 1)
        self.assertTrue(verify_deployment(self.deployment).ok)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "cleanup-pending")
        self.assertEqual(core_module._deployment_receipt_health(self.data_root), [])

    def test_discard_external_winner_requires_recovery_and_blocks_deployments(self):
        self.tamper_authored_content()

        def publish_winner(*args, **kwargs):
            self.deployment.mkdir(parents=True)
            (self.deployment / "winner.txt").write_text("keep", encoding="utf-8")
            raise OSError("external winner")

        with mock.patch.object(
            core_module,
            "render_base_deployment",
            side_effect=publish_winner,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_recover(
                    "alpha",
                    client="codex",
                    action="discard",
                    config_path=self.config_path,
                )

        self.assertEqual(raised.exception.code, "edit_recovery_required")
        self.assertEqual(
            (self.deployment / "winner.txt").read_text(encoding="utf-8"), "keep"
        )
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "needs-recovery")
        health = core_module._deployment_receipt_health(self.data_root)
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["status"], "needs-recovery")

    def test_terminal_session_does_not_block_capture_and_recovery_never_uses_git(self):
        from skill_sync.core import edit_abort

        session = edit_begin("alpha", config_path=self.config_path)
        edit_abort(session["session_id"], config_path=self.config_path)
        self.tamper_authored_content()

        with mock.patch.object(
            core_module.git,
            "run_git",
            side_effect=AssertionError("edit recovery must not access Git"),
        ):
            result = edit_recover(
                "alpha",
                client="codex",
                action="capture",
                config_path=self.config_path,
            )

        self.assertEqual(result["status"], "captured")
        self.assertNotEqual(result["session_id"], session["session_id"])

    def test_capture_preserves_hidden_binary_and_executable_files(self):
        self.tamper_authored_content()
        hidden = self.deployment / ".hidden"
        hidden.write_bytes(b"\x00\xff")
        script = self.deployment / "run.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)

        result = edit_recover(
            "alpha",
            client="codex",
            action="capture",
            config_path=self.config_path,
        )

        workspace = Path(result["workspace_path"])
        self.assertEqual((workspace / ".hidden").read_bytes(), b"\x00\xff")
        self.assertTrue(stat.S_IMODE((workspace / "run.sh").stat().st_mode) & stat.S_IXUSR)

    def test_invalid_authored_content_can_be_previewed_but_not_captured(self):
        self._make_writable(self.deployment)
        skill_file = self.deployment / "SKILL.md"
        self._make_writable(skill_file)
        skill_file.write_text("not valid frontmatter\n", encoding="utf-8")

        preview = edit_recover(
            "alpha", client="codex", config_path=self.config_path
        )
        self.assertTrue(preview["diff"]["changed"])
        with self.assertRaises(SkillSyncError) as raised:
            edit_recover(
                "alpha",
                client="codex",
                action="capture",
                config_path=self.config_path,
            )

        self.assertEqual(raised.exception.code, "unsafe_tampered_deployment")
        self.assertEqual(self.receipts(), [])
        self.assertEqual(EditSessionStore(self.data_root).list_metadata(), [])

    def test_capture_receipt_tamper_preserves_session_and_requires_recovery(self):
        self.tamper_authored_content()
        real_update = PrivateJsonReceipt.update
        calls = 0

        def tamper_completed_receipt(writer, value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ReceiptRecoveryRequired("receipt changed")
            return real_update(writer, value)

        with mock.patch.object(
            PrivateJsonReceipt,
            "update",
            autospec=True,
            side_effect=tamper_completed_receipt,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_recover(
                    "alpha",
                    client="codex",
                    action="capture",
                    config_path=self.config_path,
                )

        self.assertEqual(raised.exception.code, "edit_recovery_required")
        sessions = EditSessionStore(self.data_root).list_metadata()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].status, EditSessionStatus.ACTIVE)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "applying")
        self.assertEqual(len(core_module._deployment_receipt_health(self.data_root)), 1)

    def test_prepare_failures_do_not_publish_active_receipts(self):
        self.tamper_authored_content()
        with mock.patch.object(
            core_module,
            "prepare_private_directory",
            side_effect=OSError("snapshot root failed"),
        ):
            with self.assertRaises(SkillSyncError):
                edit_recover(
                    "alpha",
                    client="codex",
                    action="capture",
                    config_path=self.config_path,
                )
        self.assertEqual(self.receipts(), [])

        with mock.patch.object(
            core_module.DeploymentQuarantine,
            "prepare",
            side_effect=FileExistsError("quarantine conflict"),
        ):
            with self.assertRaises(SkillSyncError):
                edit_recover(
                    "alpha",
                    client="codex",
                    action="discard",
                    config_path=self.config_path,
                )
        self.assertEqual(self.receipts(), [])

    def test_capture_initial_receipt_update_failure_is_terminalized(self):
        self.tamper_authored_content()
        real_update = PrivateJsonReceipt.update
        failed = False

        def fail_once(writer, value):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("initial update failed")
            return real_update(writer, value)

        with mock.patch.object(
            PrivateJsonReceipt,
            "update",
            autospec=True,
            side_effect=fail_once,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_recover(
                    "alpha",
                    client="codex",
                    action="capture",
                    config_path=self.config_path,
                )

        self.assertEqual(raised.exception.code, "edit_recovery_failed")
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "rolled-back")
        self.assertEqual(core_module._deployment_receipt_health(self.data_root), [])

    def test_session_publish_fsync_failure_is_needs_recovery_not_rolled_back(self):
        self.tamper_authored_content()
        store_root = self.data_root / "edit-sessions"
        real_fsync = edit_session_module._fsync_directory

        def fail_published_parent(directory):
            if Path(directory) == store_root:
                raise OSError("session parent fsync failed")
            return real_fsync(directory)

        with mock.patch.object(
            edit_session_module,
            "_fsync_directory",
            side_effect=fail_published_parent,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_recover(
                    "alpha",
                    client="codex",
                    action="capture",
                    config_path=self.config_path,
                )

        self.assertEqual(raised.exception.code, "edit_recovery_required")
        sessions = EditSessionStore(self.data_root).list_metadata()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].status, EditSessionStatus.ACTIVE)
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "needs-recovery")
        self.assertEqual(receipt["session_id"], sessions[0].session_id)

    def test_committed_capture_cleanup_error_preserves_success_and_external_winner(self):
        self.tamper_authored_content()
        real_finalize = recovery_module.CapturedSnapshot.finalize
        displaced: Path | None = None

        def replace_snapshot(owner):
            nonlocal displaced
            displaced = owner.path.with_name(owner.path.name + "-owned")
            owner.path.rename(displaced)
            owner.path.mkdir()
            (owner.path / "winner.txt").write_text("keep", encoding="utf-8")
            return real_finalize(owner)

        with mock.patch.object(
            recovery_module.CapturedSnapshot,
            "finalize",
            autospec=True,
            side_effect=replace_snapshot,
        ):
            result = edit_recover(
                "alpha",
                client="codex",
                action="capture",
                config_path=self.config_path,
            )

        self.assertEqual(result["status"], "captured")
        self.assertEqual(len(result["cleanup_pending"]), 1)
        snapshot_path = Path(result["cleanup_pending"][0])
        self.assertEqual((snapshot_path / "winner.txt").read_text(), "keep")
        receipt = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "cleanup-pending")
        shutil.rmtree(snapshot_path)
        if displaced is not None:
            shutil.rmtree(displaced)

    def test_terminal_but_malformed_recovery_receipt_blocks_deployments(self):
        operation_id = "00000000-0000-4000-8000-000000000001"
        operations = self.data_root / "operations"
        operations.mkdir(parents=True, exist_ok=True)
        receipt = operations / f"edit-recover-{operation_id}.json"
        receipt.write_text('{"status":"completed"}', encoding="utf-8")

        health = core_module._deployment_receipt_health(self.data_root)

        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["status"], "malformed")
        with self.assertRaises(SkillSyncError) as raised:
            core_module._assert_no_incomplete_deployment_receipts(self.data_root)
        self.assertEqual(raised.exception.code, "deployment_recovery_required")

    def test_structurally_complete_terminal_receipt_with_wrong_phase_is_malformed(self):
        self.tamper_authored_content()
        result = edit_recover(
            "alpha",
            client="codex",
            action="discard",
            config_path=self.config_path,
        )
        receipt_path = Path(result["receipt_path"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["phase"] = "prepared"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        health = core_module._deployment_receipt_health(self.data_root)

        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["status"], "malformed")
        self.assertIn("does not match status", health[0]["error"])


if __name__ == "__main__":
    unittest.main()
