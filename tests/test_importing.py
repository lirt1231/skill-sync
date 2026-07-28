import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_sync.core as core_module

from skill_sync.agents import AgentClient, AgentTarget
from skill_sync.config import empty_config, save_config
from skill_sync.core import (
    disable_agent_sync,
    enable_agent_sync,
    import_agent_skills,
    copy_global_skills_to_agents,
    delete_global_skills,
    link_skills,
    scan_import_candidates,
    unlink_skills,
)
from skill_sync.deployment import render_base_deployment
from skill_sync.errors import SkillSyncError
from skill_sync.registry import empty_registry, load_registry, save_registry


class ImportAgentSkillsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agent_root = self.root / "codex" / "skills"
        self.global_root = self.root / "global" / "skills"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.config_path = self.root / "config.json"
        config = empty_config()
        config.pop("platform", None)
        config["sync_repo_path"] = str(self.repo)
        config["skills_root"] = str(self.global_root)
        config["data_root"] = str(self.root / "data")
        save_config(self.config_path, config)
        save_registry(self.repo / "registry.yaml", empty_registry())
        self.agent = AgentTarget("codex", "Codex", self.agent_root, True)
        self.workbuddy = AgentTarget("workbuddy", "WorkBuddy", self.root / "workbuddy" / "skills", True)
        self.client = AgentClient("codex", "codex", "Codex", self.agent_root, True)
        self.workbuddy_client = AgentClient(
            "workbuddy",
            "workbuddy",
            "WorkBuddy",
            self.workbuddy.skills_dir,
            True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_skill(self, root: Path, name: str, text: str = "# skill\n") -> Path:
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        return skill

    def test_import_moves_real_content_to_global_and_replaces_source_with_link(self):
        source = self.write_skill(self.agent_root, "alpha", "# alpha\n")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ):
            result = import_agent_skills(["alpha"], "codex", config_path=self.config_path)
        destination = self.global_root / "alpha"
        self.assertEqual(result["imported"][0]["state"], "imported")
        self.assertTrue(source.is_symlink())
        self.assertNotEqual(source.resolve(), destination.resolve())
        self.assertTrue((source.resolve() / ".skill-sync-provenance.json").is_file())
        self.assertEqual((destination / "SKILL.md").read_text(), "# alpha\n")
        registry_entry = load_registry(self.repo / "registry.yaml")["skills"]["alpha"]
        self.assertEqual(
            registry_entry["targets"],
            "codex,workbuddy,kimi,claude",
        )

    def test_import_refuses_different_global_content_without_touching_source(self):
        source = self.write_skill(self.agent_root, "alpha", "# local\n")
        self.write_skill(self.global_root, "alpha", "# global\n")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ):
            with self.assertRaisesRegex(SkillSyncError, "different content"):
                import_agent_skills(["alpha"], "codex", config_path=self.config_path)
        self.assertFalse(source.is_symlink())
        self.assertEqual((source / "SKILL.md").read_text(), "# local\n")

    def test_import_already_linked_requires_verified_matching_client_deployment(self):
        canonical = self.write_skill(self.global_root, "alpha")
        deployment = render_base_deployment(
            canonical, self.root / "data" / "rendered", "alpha", "workbuddy"
        )
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(deployment.path, target_is_directory=True)

        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ):
            with self.assertRaisesRegex(SkillSyncError, "not a verified deployment"):
                import_agent_skills(["alpha"], "codex", config_path=self.config_path)

        self.assertTrue(source.is_symlink())

    def test_import_rollback_restores_original_when_migration_fails(self):
        source = self.write_skill(self.agent_root, "alpha", "# original\n")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ), mock.patch(
            "skill_sync.core.deploy_migrate", side_effect=SkillSyncError("failed")
        ):
            with self.assertRaisesRegex(SkillSyncError, "failed"):
                import_agent_skills(["alpha"], "codex", config_path=self.config_path)
        self.assertFalse(source.is_symlink())
        self.assertEqual((source / "SKILL.md").read_text(), "# original\n")

    def test_import_rollback_never_rmdirs_a_real_directory_that_wins_race(self):
        source = self.write_skill(self.agent_root, "alpha", "# original\n")

        def fail_after_real_directory_appears(**_kwargs):
            source.mkdir()
            (source / "keep.txt").write_text("keep", encoding="utf-8")
            raise SkillSyncError("failed")

        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ), mock.patch(
            "skill_sync.core.deploy_migrate", side_effect=fail_after_real_directory_appears
        ), mock.patch.object(Path, "rmdir") as rmdir:
            with self.assertRaisesRegex(SkillSyncError, "failed"):
                import_agent_skills(["alpha"], "codex", config_path=self.config_path)
        rmdir.assert_not_called()
        self.assertEqual((source / "keep.txt").read_text(), "keep")
        backups = list(self.agent_root.glob(".alpha.skill-sync-import-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "SKILL.md").read_text(), "# original\n")

    def test_import_rollback_preserves_changed_new_global_skill_and_needs_recovery(self):
        source = self.write_skill(self.agent_root, "alpha", "# original\n")

        def change_global_then_fail(**_kwargs):
            (self.global_root / "alpha" / "SKILL.md").write_text(
                "# concurrent winner\n", encoding="utf-8"
            )
            raise SkillSyncError("failed")

        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ), mock.patch(
            "skill_sync.core.deploy_migrate", side_effect=change_global_then_fail
        ):
            with self.assertRaises(SkillSyncError) as raised:
                import_agent_skills(["alpha"], "codex", config_path=self.config_path)

        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(raised.exception.code, "import_rollback_needs_recovery")
        self.assertEqual(
            (self.global_root / "alpha" / "SKILL.md").read_text(),
            "# concurrent winner\n",
        )
        self.assertFalse(source.is_symlink())
        self.assertEqual((source / "SKILL.md").read_text(), "# original\n")

    def test_scan_excludes_links_and_reports_importable_and_conflict(self):
        self.write_skill(self.agent_root, "alpha")
        self.write_skill(self.agent_root, "beta", "# local\n")
        self.write_skill(self.global_root, "beta", "# global\n")
        linked_target = self.write_skill(self.global_root, "linked")
        (self.agent_root / "linked").symlink_to(linked_target, target_is_directory=True)
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            result = scan_import_candidates(["codex"], config_path=self.config_path)
        self.assertEqual([(item["name"], item["state"]) for item in result], [("alpha", "importable"), ("beta", "conflict")])

    def test_scan_import_candidates_includes_workbuddy_by_default(self):
        self.write_skill(self.workbuddy.skills_dir, "wb-skill")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent, self.workbuddy]):
            result = scan_import_candidates(config_path=self.config_path)
        self.assertEqual(result, [{
            "name": "wb-skill", "agent": "workbuddy", "path": str(self.workbuddy.skills_dir / "wb-skill"), "state": "importable"
        }])

    def test_copy_global_skill_replaces_managed_link_with_real_agent_copy(self):
        global_skill = self.write_skill(self.global_root, "alpha", "# alpha\n")
        self.agent_root.mkdir(parents=True)
        linked = self.agent_root / "alpha"
        linked.symlink_to(global_skill, target_is_directory=True)
        save_registry(self.repo / "registry.yaml", {
            "version": 2, "skills": {"alpha": {"selected": True, "display_name": "alpha", "targets": "codex"}}
        })
        with mock.patch("skill_sync.core.detect_clients", return_value=[self.client]):
            result = copy_global_skills_to_agents(["alpha"], ["codex"], config_path=self.config_path)
        self.assertEqual(result["copied"][0]["state"], "copied")
        self.assertFalse(linked.is_symlink())
        self.assertEqual((linked / "SKILL.md").read_text(), "# alpha\n")

    def test_copy_global_skill_refuses_existing_real_agent_directory(self):
        self.write_skill(self.global_root, "alpha", "# global\n")
        existing = self.write_skill(self.agent_root, "alpha", "# local\n")
        with mock.patch("skill_sync.core.detect_clients", return_value=[self.client]):
            with self.assertRaisesRegex(SkillSyncError, "refusing to overwrite"):
                copy_global_skills_to_agents(["alpha"], ["codex"], config_path=self.config_path)
        self.assertEqual((existing / "SKILL.md").read_text(), "# local\n")

    def test_delete_global_skill_removes_managed_link_and_registry_entry(self):
        destination = self.write_skill(self.global_root, "alpha", "# alpha\n")
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(destination, target_is_directory=True)
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {"alpha": {"selected": True, "display_name": "alpha", "targets": "codex"}},
        })
        with mock.patch("skill_sync.core.detect_clients", return_value=[self.client]):
            result = delete_global_skills(["alpha"], config_path=self.config_path)
        self.assertEqual(result["deleted"], ["alpha"])
        self.assertFalse(destination.exists())
        self.assertFalse(source.exists())
        self.assertNotIn("alpha", load_registry(self.repo / "registry.yaml")["skills"])

    def test_delete_removes_link_from_existing_undetected_client_root(self):
        destination = self.write_skill(self.global_root, "alpha")
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(destination, target_is_directory=True)
        undetected = AgentClient("codex", "codex", "Codex", self.agent_root, False)

        with mock.patch("skill_sync.core.detect_clients", return_value=[undetected]):
            delete_global_skills(["alpha"], config_path=self.config_path)

        self.assertFalse(source.exists())

    def test_unlink_scans_existing_undetected_client_root(self):
        destination = self.write_skill(self.global_root, "alpha")
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(destination, target_is_directory=True)
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {"alpha": {"selected": True, "display_name": "alpha", "targets": "codex"}},
        })
        undetected = AgentClient("codex", "codex", "Codex", self.agent_root, False)

        with mock.patch("skill_sync.core.detect_clients", return_value=[undetected]):
            result = unlink_skills(["alpha"], config_path=self.config_path)

        self.assertEqual(len(result["unlinked"]), 1)
        self.assertFalse(source.exists())

    def test_delete_rejects_global_symlink(self):
        target = self.write_skill(self.root / "elsewhere", "alpha")
        self.global_root.mkdir(parents=True, exist_ok=True)
        (self.global_root / "alpha").symlink_to(target, target_is_directory=True)
        with mock.patch("skill_sync.core.detect_clients", return_value=[self.client]):
            with self.assertRaisesRegex(SkillSyncError, "symlink"):
                delete_global_skills(["alpha"], config_path=self.config_path)
        self.assertTrue(target.exists())

    def test_link_mutations_fail_closed_on_malformed_deployment_receipt(self):
        operations = self.root / "data" / "operations"
        operations.mkdir(parents=True)
        (operations / "deploy-migrate-broken.json").write_text(
            "{truncated", encoding="utf-8"
        )

        with self.assertRaises(SkillSyncError) as raised:
            delete_global_skills(["alpha"], config_path=self.config_path)

        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(raised.exception.code, "deployment_recovery_required")

    def test_owned_canonical_cleanup_isolates_before_deleting(self):
        owned = self.write_skill(self.global_root, "alpha", "# owned\n")
        identity = core_module._path_identity(owned)
        real_rename = core_module.rename_no_replace
        displaced = self.global_root / ".alpha-race-owned"

        def winner_arrives(source, destination):
            if Path(source) == owned:
                Path(source).rename(displaced)
                Path(source).mkdir()
                (Path(source) / "keep.txt").write_text("winner", encoding="utf-8")
                return real_rename(displaced, destination)
            return real_rename(source, destination)

        with mock.patch.object(
            core_module, "rename_no_replace", side_effect=winner_arrives
        ):
            removed = core_module._quarantine_and_remove_owned_directory(
                owned, identity, self.root / "data" / "trash"
            )

        self.assertTrue(removed)
        self.assertEqual((owned / "keep.txt").read_text(), "winner")

    def test_disable_save_failure_does_not_remove_links(self):
        destination = self.write_skill(self.global_root, "alpha")
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(destination, target_is_directory=True)
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {"alpha": {"selected": True, "display_name": "alpha", "targets": "codex"}},
        })

        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.save_config", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                disable_agent_sync("codex", config_path=self.config_path)

        self.assertTrue(source.is_symlink())

    def test_disable_agent_removes_links_and_blocks_future_link_operations(self):
        destination = self.write_skill(self.global_root, "alpha")
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(destination, target_is_directory=True)
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {"alpha": {"selected": True, "display_name": "alpha", "targets": "codex"}},
        })
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]), mock.patch(
            "skill_sync.core.detect_clients", return_value=[self.client]
        ):
            result = disable_agent_sync("codex", config_path=self.config_path)
            self.assertEqual(result["disabled"], "codex")
            self.assertFalse(source.exists())
            with self.assertRaisesRegex(SkillSyncError, "disabled"):
                link_skills(agent_names=["codex"], config_path=self.config_path)
            self.assertEqual(enable_agent_sync("codex", config_path=self.config_path)["enabled"], "codex")

    def test_kimi_link_uses_kimi_code_directory_and_accepts_client_target(self):
        destination = self.write_skill(self.global_root, "alpha")
        code_root = self.root / "kimi-code" / "skills"
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {
                "alpha": {
                    "selected": True,
                    "display_name": "alpha",
                    "targets": "kimi-code",
                }
            },
        })
        clients = [
            AgentClient("kimi-code", "kimi", "Kimi Code", code_root, True),
        ]

        with mock.patch("skill_sync.core.detect_clients", return_value=clients):
            result = link_skills(agent_names=["kimi"], config_path=self.config_path)

        self.assertEqual(len(result["links"]), 1)
        code_target = (code_root / "alpha").resolve()
        self.assertNotEqual(code_target, destination.resolve())
        self.assertTrue((code_target / ".skill-sync-provenance.json").is_file())
