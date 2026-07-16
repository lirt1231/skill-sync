import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import skill_sync.core as core
import skill_sync.edit_session as edit_session_module
from skill_sync.config import empty_config, save_config
from skill_sync.edit_session import EditSessionStore
from skill_sync.errors import SkillSyncError
from skill_sync.registry import save_registry


class DeleteEditLockingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.skills = self.root / "skills"
        self.data_root = self.root / "data"
        self.config_path = self.root / "config.json"
        self.repo.mkdir()
        config = empty_config()
        config.pop("platform", None)
        config.update(
            {
                "sync_repo_path": str(self.repo),
                "skills_root": str(self.skills),
                "data_root": str(self.data_root),
                "skills": {},
            }
        )
        save_config(self.config_path, config)
        save_registry(self.repo / "registry.yaml", {"version": 2, "skills": {}})

    def tearDown(self):
        self.temp.cleanup()

    def add_skill(self, name: str) -> Path:
        skill = self.skills / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        config = core.load_config(self.config_path)
        config["skills"][name] = {"local_path": str(skill)}
        save_config(self.config_path, config)
        registry = core.load_registry(self.repo / "registry.yaml")
        registry["skills"][name] = {
            "selected": True,
            "display_name": name,
            "targets": "codex",
        }
        save_registry(self.repo / "registry.yaml", registry)
        return skill

    @staticmethod
    def join(thread: threading.Thread) -> None:
        thread.join(timeout=3)
        if thread.is_alive():
            raise AssertionError("worker did not finish; possible lock-order deadlock")

    def test_begin_publishes_before_waiting_delete_rechecks_and_stops(self):
        skill = self.add_skill("alpha")
        copy_entered = threading.Event()
        release_copy = threading.Event()
        real_copy = edit_session_module.copy_skill_dir
        blocked_once = False
        results = {}

        def blocking_copy(*args, **kwargs):
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                copy_entered.set()
                self.assertTrue(release_copy.wait(timeout=3))
            return real_copy(*args, **kwargs)

        def begin_worker():
            try:
                results["begin"] = core.edit_begin(
                    "alpha", actor="test", config_path=self.config_path
                )
            except Exception as exc:  # pragma: no cover - assertion reports it
                results["begin_error"] = exc

        def delete_worker():
            try:
                results["delete"] = core.delete_global_skills(
                    ["alpha"], self.config_path
                )
            except Exception as exc:
                results["delete_error"] = exc

        with mock.patch.object(
            edit_session_module, "copy_skill_dir", side_effect=blocking_copy
        ), mock.patch.object(core, "detect_clients", return_value=[]):
            begin = threading.Thread(target=begin_worker)
            begin.start()
            self.assertTrue(copy_entered.wait(timeout=3))
            delete = threading.Thread(target=delete_worker)
            delete.start()
            self.assertFalse(threading.Event().wait(timeout=0.1) or not delete.is_alive())
            release_copy.set()
            self.join(begin)
            self.join(delete)

        self.assertNotIn("begin_error", results)
        self.assertIsInstance(results.get("delete_error"), SkillSyncError)
        self.assertEqual(results["delete_error"].code, "delete_edit_session_blocked")
        self.assertTrue((skill / "SKILL.md").is_file())

    def test_delete_holds_both_locks_until_commit_then_begin_fails_closed(self):
        skill = self.add_skill("alpha")
        delete_entered = threading.Event()
        release_delete = threading.Event()
        begin_done = threading.Event()
        real_delete = core._delete_global_skills_unlocked
        results = {}

        def blocking_delete(*args, **kwargs):
            delete_entered.set()
            self.assertTrue(release_delete.wait(timeout=3))
            return real_delete(*args, **kwargs)

        def delete_worker():
            try:
                results["delete"] = core.delete_global_skills(
                    ["alpha"], self.config_path
                )
            except Exception as exc:  # pragma: no cover - assertion reports it
                results["delete_error"] = exc

        def begin_worker():
            try:
                results["begin"] = core.edit_begin(
                    "alpha", actor="test", config_path=self.config_path
                )
            except Exception as exc:
                results["begin_error"] = exc
            finally:
                begin_done.set()

        with mock.patch.object(
            core, "_delete_global_skills_unlocked", side_effect=blocking_delete
        ), mock.patch.object(core, "detect_clients", return_value=[]):
            delete = threading.Thread(target=delete_worker)
            delete.start()
            self.assertTrue(delete_entered.wait(timeout=3))
            begin = threading.Thread(target=begin_worker)
            begin.start()
            self.assertFalse(begin_done.wait(timeout=0.1))
            release_delete.set()
            self.join(delete)
            self.join(begin)

        self.assertNotIn("delete_error", results)
        self.assertIsInstance(results.get("begin_error"), SkillSyncError)
        self.assertFalse(skill.exists())
        self.assertEqual(EditSessionStore(self.data_root).list_metadata(), [])

    def test_batch_delete_acquires_skill_locks_in_stable_path_order(self):
        self.add_skill("alpha")
        self.add_skill("beta")
        acquired = []
        store = EditSessionStore(self.data_root)

        @contextmanager
        def recording_lock(_store, name, **_kwargs):
            acquired.append(name)
            yield

        with mock.patch.object(
            EditSessionStore, "skill_lock", new=recording_lock
        ), mock.patch.object(
            core, "_delete_global_skills_unlocked", return_value={"deleted": []}
        ), mock.patch.object(core, "detect_clients", return_value=[]):
            core.delete_global_skills(["beta", "alpha"], self.config_path)

        expected = sorted(
            ["alpha", "beta"], key=lambda item: str(store.skill_lock_path(item))
        )
        self.assertEqual(acquired, expected)

    def test_casefold_equivalent_names_resolve_and_delete_one_identity(self):
        skill = self.add_skill("alpha")

        with mock.patch.object(core, "detect_clients", return_value=[]):
            result = core.delete_global_skills(
                ["ALPHA", "alpha"], self.config_path
            )

        self.assertEqual(result["deleted"], ["alpha"])
        self.assertFalse(skill.exists())
        self.assertNotIn("alpha", core.load_config(self.config_path)["skills"])
        self.assertNotIn(
            "alpha", core.load_registry(self.repo / "registry.yaml")["skills"]
        )

    def test_casefold_metadata_ambiguity_fails_under_deployment_lock(self):
        self.add_skill("alpha")
        registry = core.load_registry(self.repo / "registry.yaml")
        registry["skills"]["ALPHA"] = {
            "selected": True,
            "display_name": "ALPHA",
        }
        save_registry(self.repo / "registry.yaml", registry)

        with mock.patch.object(core, "local_file_lock") as deployment_lock:
            with self.assertRaises(SkillSyncError) as raised:
                core.delete_global_skills(["alpha"], self.config_path)

        self.assertEqual(raised.exception.code, "delete_skill_name_ambiguous")
        deployment_lock.assert_called_once()

    def test_casefold_equivalent_active_session_blocks_delete(self):
        skill = self.add_skill("alpha")
        core.edit_begin("alpha", actor="test", config_path=self.config_path)

        with mock.patch.object(core, "detect_clients", return_value=[]), mock.patch.object(
            core, "_delete_global_skills_unlocked"
        ) as delete:
            with self.assertRaises(SkillSyncError) as raised:
                core.delete_global_skills(["ALPHA"], self.config_path)

        self.assertEqual(raised.exception.code, "delete_edit_session_blocked")
        self.assertTrue(skill.exists())
        delete.assert_not_called()

    def test_invalid_name_fails_before_any_lock(self):
        with mock.patch.object(core, "local_file_lock") as deployment_lock:
            with self.assertRaisesRegex(SkillSyncError, "invalid Skill name"):
                core.delete_global_skills(["../alpha"], self.config_path)
        deployment_lock.assert_not_called()

    def test_delete_refreshes_identity_after_waiting_for_deployment_lock(self):
        validation_done = threading.Event()
        results = {}
        real_validate = core._validate_delete_names

        def validating(names):
            value = real_validate(names)
            validation_done.set()
            return value

        def delete_worker():
            try:
                results["delete"] = core.delete_global_skills(
                    ["ALPHA"], self.config_path
                )
            except Exception as exc:  # pragma: no cover - assertion reports it
                results["error"] = exc

        lock_path = self.data_root / "locks" / "deployment.lock"
        with core.local_file_lock(lock_path), mock.patch.object(
            core, "_validate_delete_names", side_effect=validating
        ), mock.patch.object(core, "detect_clients", return_value=[]):
            worker = threading.Thread(target=delete_worker)
            worker.start()
            self.assertTrue(validation_done.wait(timeout=3))
            skill = self.add_skill("alpha")

        self.join(worker)
        self.assertNotIn("error", results)
        self.assertEqual(results["delete"]["deleted"], ["alpha"])
        self.assertFalse(skill.exists())
        self.assertNotIn("alpha", core.load_config(self.config_path)["skills"])
        self.assertNotIn(
            "alpha", core.load_registry(self.repo / "registry.yaml")["skills"]
        )
