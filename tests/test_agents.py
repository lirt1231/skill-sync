import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.agents import detect_agents


class AgentDetectionTest(unittest.TestCase):
    def test_detects_codex_and_workbuddy_skill_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".workbuddy" / "skills").mkdir(parents=True)
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                agents = {item.name: item for item in detect_agents(env={}, home=home)}
            self.assertTrue(agents["codex"].detected)
            self.assertTrue(agents["workbuddy"].detected)
            self.assertEqual(agents["codex"].skills_dir, home / ".codex" / "skills")
            self.assertEqual(agents["workbuddy"].skills_dir, home / ".workbuddy" / "skills")

    def test_kimi_code_falls_back_to_recommended_user_skill_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".kimi-code").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                agents = {item.name: item for item in detect_agents(env={}, home=home)}
            self.assertTrue(agents["kimi-code"].detected)
            self.assertEqual(
                agents["kimi-code"].skills_dir,
                home / ".config" / "agents" / "skills",
            )

    def test_kimi_desktop_uses_effective_daimon_managed_skill_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / "Library" / "Application Support" / "kimi-desktop" / "daimon-share" / "daimon" / "skills"
            managed.mkdir(parents=True)
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                agents = {item.name: item for item in detect_agents(env={}, home=home)}
            self.assertTrue(agents["kimi-desktop"].detected)
            self.assertEqual(agents["kimi-desktop"].skills_dir, managed)

    def test_detects_claude_code_skill_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                agents = {item.name: item for item in detect_agents(env={}, home=home)}
            self.assertTrue(agents["claude"].detected)
            self.assertEqual(agents["claude"].skills_dir, home / ".claude" / "skills")

    def test_environment_overrides_agent_homes(self):
        with mock.patch("skill_sync.agents.shutil.which", return_value=None):
            agents = {item.name: item for item in detect_agents(
                env={
                    "CODEX_HOME": "/custom/codex",
                    "WORKBUDDY_HOME": "/custom/workbuddy",
                    "KIMI_CODE_SKILLS_DIR": "/custom/kimi-code-skills",
                    "KIMI_DESKTOP_SKILLS_DIR": "/custom/kimi-desktop-skills",
                    "CLAUDE_HOME": "/custom/claude",
                },
                home=Path("/home/example"),
            )}
        self.assertEqual(agents["codex"].skills_dir, Path("/custom/codex/skills"))
        self.assertEqual(agents["workbuddy"].skills_dir, Path("/custom/workbuddy/skills"))
        self.assertEqual(agents["kimi-code"].skills_dir, Path("/custom/kimi-code-skills"))
        self.assertEqual(agents["kimi-desktop"].skills_dir, Path("/custom/kimi-desktop-skills"))
        self.assertEqual(agents["claude"].skills_dir, Path("/custom/claude/skills"))
