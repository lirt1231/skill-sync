import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from skill_sync.edit_session import (
    EDIT_SESSION_SCHEMA_VERSION,
    EditSessionMetadata,
    EditSessionMetadataError,
    EditSessionStatus,
    EditSessionStore,
    InvalidEditSessionTransition,
)


BASELINE_HASH = "sha256:" + "a" * 64
CREATED_AT = datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc)
UPDATED_AT = datetime(2026, 7, 15, 8, 31, tzinfo=timezone.utc)


class EditSessionStoreTest(unittest.TestCase):
    def make_metadata(self, **changes) -> EditSessionMetadata:
        metadata = EditSessionMetadata.new(
            logical_skill="alpha",
            baseline_hash=BASELINE_HASH,
            actor="codex",
            now=CREATED_AT,
        )
        return replace(metadata, **changes)

    def test_metadata_round_trip_uses_machine_local_layout_without_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            store = EditSessionStore(data_root)
            metadata = self.make_metadata()

            paths = store.create(metadata)

            self.assertEqual(
                paths.root,
                data_root / "edit-sessions" / metadata.session_id,
            )
            self.assertEqual(paths.metadata, paths.root / "session.json")
            self.assertEqual(paths.baseline, paths.root / "baseline")
            self.assertEqual(paths.workspace, paths.root / "workspace")
            self.assertTrue(paths.metadata.is_file())
            self.assertFalse(paths.baseline.exists())
            self.assertFalse(paths.workspace.exists())
            self.assertEqual(store.load(metadata.session_id), metadata)

            stored = json.loads(paths.metadata.read_text(encoding="utf-8"))
            self.assertEqual(
                stored,
                {
                    "actor": "codex",
                    "baseline_hash": BASELINE_HASH,
                    "created_at": "2026-07-15T08:30:00Z",
                    "logical_skill": "alpha",
                    "schema_version": EDIT_SESSION_SCHEMA_VERSION,
                    "session_id": metadata.session_id,
                    "status": "active",
                    "updated_at": "2026-07-15T08:30:00Z",
                },
            )

    def test_new_session_ids_are_canonical_and_unique(self):
        first = self.make_metadata()
        second = self.make_metadata()

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertRegex(
            first.session_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )

    def test_state_machine_persists_only_allowed_transitions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            metadata = self.make_metadata()
            store.create(metadata)

            applying = store.transition(
                metadata.session_id,
                EditSessionStatus.APPLYING,
                now=UPDATED_AT,
            )
            self.assertEqual(applying.status, EditSessionStatus.APPLYING)
            self.assertEqual(applying.updated_at, "2026-07-15T08:31:00Z")
            self.assertEqual(store.load(metadata.session_id), applying)

            with self.assertRaises(InvalidEditSessionTransition):
                store.transition(metadata.session_id, EditSessionStatus.ABORTED)
            self.assertEqual(
                store.load(metadata.session_id).status,
                EditSessionStatus.APPLYING,
            )

            recovery = store.transition(
                metadata.session_id,
                EditSessionStatus.NEEDS_RECOVERY,
                now=CREATED_AT,
            )
            self.assertEqual(recovery.status, EditSessionStatus.NEEDS_RECOVERY)
            self.assertEqual(recovery.updated_at, applying.updated_at)
            resumed = store.transition(
                metadata.session_id,
                EditSessionStatus.ACTIVE,
                now=UPDATED_AT,
            )
            self.assertEqual(resumed.status, EditSessionStatus.ACTIVE)

    def test_terminal_session_cannot_transition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            metadata = self.make_metadata()
            store.create(metadata)
            aborted = store.transition(
                metadata.session_id,
                EditSessionStatus.ABORTED,
            )
            self.assertEqual(aborted.status, EditSessionStatus.ABORTED)

            with self.assertRaises(InvalidEditSessionTransition):
                store.transition(metadata.session_id, EditSessionStatus.ACTIVE)

    def test_malformed_metadata_fails_closed_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            metadata = self.make_metadata()
            paths = store.create(metadata)
            corrupted = b'{"schema_version": 1, "status":'
            paths.metadata.write_bytes(corrupted)

            with self.assertRaises(EditSessionMetadataError):
                store.load(metadata.session_id)
            with self.assertRaises(EditSessionMetadataError):
                store.transition(metadata.session_id, EditSessionStatus.ABORTED)
            self.assertEqual(paths.metadata.read_bytes(), corrupted)

    def test_unknown_or_mismatched_metadata_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            metadata = self.make_metadata()
            paths = store.create(metadata)
            value = json.loads(paths.metadata.read_text(encoding="utf-8"))
            value["credential"] = "must-not-be-accepted"
            paths.metadata.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(EditSessionMetadataError):
                store.load(metadata.session_id)

            value.pop("credential")
            value["session_id"] = EditSessionMetadata.new(
                logical_skill="alpha",
                baseline_hash=BASELINE_HASH,
            ).session_id
            paths.metadata.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(EditSessionMetadataError):
                store.load(metadata.session_id)

    def test_non_integer_schema_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            metadata = self.make_metadata()
            paths = store.create(metadata)
            value = json.loads(paths.metadata.read_text(encoding="utf-8"))
            value["schema_version"] = 1.0
            paths.metadata.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(EditSessionMetadataError):
                store.load(metadata.session_id)

    def test_duplicate_session_id_never_overwrites_existing_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            metadata = self.make_metadata()
            paths = store.create(metadata)
            original = paths.metadata.read_bytes()

            with self.assertRaises(FileExistsError):
                store.create(replace(metadata, actor="workbuddy"))
            self.assertEqual(paths.metadata.read_bytes(), original)

    def test_linked_data_root_is_rejected_before_creating_session_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            real_root = work / "real"
            real_root.mkdir()
            linked_root = work / "linked"
            linked_root.symlink_to(real_root, target_is_directory=True)
            store = EditSessionStore(linked_root)

            with self.assertRaises(EditSessionMetadataError):
                store.create(self.make_metadata())

            self.assertEqual(list(real_root.iterdir()), [])

    def test_per_skill_lock_serializes_same_skill_but_not_other_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EditSessionStore(Path(temp_dir))
            result: list[str] = []

            def contend() -> None:
                try:
                    with store.skill_lock("alpha", timeout=0.01):
                        result.append("same-acquired")
                except TimeoutError:
                    result.append("same-timed-out")
                with store.skill_lock("beta", timeout=0.01):
                    result.append("other-acquired")

            with store.skill_lock("alpha"):
                thread = threading.Thread(target=contend)
                thread.start()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())

            self.assertEqual(result, ["same-timed-out", "other-acquired"])
            self.assertEqual(
                store.skill_lock_path("alpha"),
                store.skill_lock_path("ALPHA"),
            )

    def test_invalid_identifiers_hashes_and_timestamps_are_rejected(self):
        with self.assertRaises(EditSessionMetadataError):
            EditSessionMetadata.new(logical_skill="../alpha", baseline_hash=BASELINE_HASH)
        with self.assertRaises(EditSessionMetadataError):
            EditSessionMetadata.new(logical_skill="alpha", baseline_hash="sha256:short")
        with self.assertRaises(EditSessionMetadataError):
            self.make_metadata(created_at="2026-07-15T08:30:00")


if __name__ == "__main__":
    unittest.main()
