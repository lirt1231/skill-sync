import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.agents import AgentTarget
from skill_sync.config import empty_config, save_config
from skill_sync.core import (
    disable_agent_sync,
    enable_agent_sync,
    import_agent_skills,
    copy_global_skills_to_agents,
    delete_global_skills,
    link_skills,
    scan_import_candidates,
)
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
        save_config(self.config_path, config)
        save_registry(self.repo / "registry.yaml", empty_registry())
        self.agent = AgentTarget("codex", "Codex", self.agent_root, True)
        self.workbuddy = AgentTarget("workbuddy", "WorkBuddy", self.root / "workbuddy" / "skills", True)

    def tearDown(self):
        self.temp.cleanup()

    def write_skill(self, root: Path, name: str, text: str = "# skill\n") -> Path:
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        return skill

    def test_import_moves_real_content_to_global_and_replaces_source_with_link(self):
        source = self.write_skill(self.agent_root, "alpha", "# alpha\n")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            result = import_agent_skills(["alpha"], "codex", config_path=self.config_path)
        destination = self.global_root / "alpha"
        self.assertEqual(result["imported"][0]["state"], "imported")
        self.assertTrue(source.is_symlink())
        self.assertEqual(source.resolve(), destination.resolve())
        self.assertEqual((destination / "SKILL.md").read_text(), "# alpha\n")
        registry_entry = load_registry(self.repo / "registry.yaml")["skills"]["alpha"]
        self.assertEqual(
            registry_entry["targets"],
            "codex,workbuddy,kimi,claude",
        )

    def test_import_refuses_different_global_content_without_touching_source(self):
        source = self.write_skill(self.agent_root, "alpha", "# local\n")
        self.write_skill(self.global_root, "alpha", "# global\n")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            with self.assertRaisesRegex(SkillSyncError, "different content"):
                import_agent_skills(["alpha"], "codex", config_path=self.config_path)
        self.assertFalse(source.is_symlink())
        self.assertEqual((source / "SKILL.md").read_text(), "# local\n")

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
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            result = copy_global_skills_to_agents(["alpha"], ["codex"], config_path=self.config_path)
        self.assertEqual(result["copied"][0]["state"], "copied")
        self.assertFalse(linked.is_symlink())
        self.assertEqual((linked / "SKILL.md").read_text(), "# alpha\n")

    def test_copy_global_skill_refuses_existing_real_agent_directory(self):
        self.write_skill(self.global_root, "alpha", "# global\n")
        existing = self.write_skill(self.agent_root, "alpha", "# local\n")
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
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
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            result = delete_global_skills(["alpha"], config_path=self.config_path)
        self.assertEqual(result["deleted"], ["alpha"])
        self.assertFalse(destination.exists())
        self.assertFalse(source.exists())
        self.assertNotIn("alpha", load_registry(self.repo / "registry.yaml")["skills"])

    def test_delete_rejects_global_symlink(self):
        target = self.write_skill(self.root / "elsewhere", "alpha")
        self.global_root.mkdir(parents=True, exist_ok=True)
        (self.global_root / "alpha").symlink_to(target, target_is_directory=True)
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            with self.assertRaisesRegex(SkillSyncError, "symlink"):
                delete_global_skills(["alpha"], config_path=self.config_path)
        self.assertTrue(target.exists())

    def test_disable_agent_removes_links_and_blocks_future_link_operations(self):
        destination = self.write_skill(self.global_root, "alpha")
        self.agent_root.mkdir(parents=True, exist_ok=True)
        source = self.agent_root / "alpha"
        source.symlink_to(destination, target_is_directory=True)
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {"alpha": {"selected": True, "display_name": "alpha", "targets": "codex"}},
        })
        with mock.patch("skill_sync.core.detect_agents", return_value=[self.agent]):
            result = disable_agent_sync("codex", config_path=self.config_path)
            self.assertEqual(result["disabled"], "codex")
            self.assertFalse(source.exists())
            with self.assertRaisesRegex(SkillSyncError, "disabled"):
                link_skills(agent_names=["codex"], config_path=self.config_path)
            self.assertEqual(enable_agent_sync("codex", config_path=self.config_path)["enabled"], "codex")

    def test_kimi_link_uses_all_detected_skill_directories_and_accepts_split_targets(self):
        destination = self.write_skill(self.global_root, "alpha")
        code_root = self.root / "kimi-code" / "skills"
        desktop_root = self.root / "kimi-desktop" / "skills"
        save_registry(self.repo / "registry.yaml", {
            "version": 2,
            "skills": {
                "alpha": {
                    "selected": True,
                    "display_name": "alpha",
                    "targets": "kimi-code,kimi-desktop",
                }
            },
        })
        kimi = AgentTarget("kimi", "Kimi", code_root, True, (desktop_root,))

        with mock.patch("skill_sync.core.detect_agents", return_value=[kimi]):
            result = link_skills(agent_names=["kimi"], config_path=self.config_path)

        self.assertEqual(len(result["links"]), 2)
        self.assertEqual((code_root / "alpha").resolve(), destination.resolve())
        self.assertEqual((desktop_root / "alpha").resolve(), destination.resolve())
