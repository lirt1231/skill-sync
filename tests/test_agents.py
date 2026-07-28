import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.agents import (
    AgentClient,
    AgentFamily,
    aggregate_agent_family,
    detect_agents,
    detect_clients,
    expand_agent_clients,
    get_client,
    get_family,
)


class AgentDetectionTest(unittest.TestCase):
    def test_stable_family_and_client_models(self):
        family = get_family("kimi")
        self.assertIsInstance(family, AgentFamily)
        self.assertEqual(family.id, "kimi")
        self.assertEqual(family.client_ids, ("kimi-code",))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".kimi-code").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                client = get_client("kimi-code", env={}, home=home)
        self.assertIsInstance(client, AgentClient)
        self.assertEqual(client.id, "kimi-code")
        self.assertEqual(client.family_id, "kimi")
        self.assertTrue(client.detected)
        self.assertEqual(client.link_capability, "symlink-or-junction")

    def test_detect_clients_returns_stable_concrete_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                clients = detect_clients(env={}, home=Path(tmp))
        self.assertEqual(
            [client.id for client in clients],
            ["codex", "workbuddy", "kimi-code", "claude-code"],
        )

    def test_expands_family_to_detected_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".kimi-code").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                clients = detect_clients(env={}, home=home)
            expanded = expand_agent_clients("kimi", clients=clients)
        self.assertEqual(
            [client.id for client in expanded],
            ["kimi-code"],
        )

    def test_expands_concrete_client_without_expanding_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".kimi-code").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                clients = detect_clients(env={}, home=home)
            expanded = expand_agent_clients("kimi-code", clients=clients)
        self.assertEqual([client.id for client in expanded], ["kimi-code"])

    def test_family_expansion_omits_undetected_clients_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                clients = detect_clients(env={}, home=home)
            detected = expand_agent_clients("kimi", clients=clients)
            all_clients = expand_agent_clients(
                "kimi", clients=clients, detected_only=False
            )
        self.assertEqual([client.id for client in detected], [])
        self.assertEqual(
            [client.id for client in all_clients],
            ["kimi-code"],
        )

    def test_aggregates_detected_clients_into_family_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code = home / ".config" / "agents" / "skills"
            (home / ".kimi-code").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                clients = detect_clients(env={}, home=home)
            family = aggregate_agent_family("kimi", clients)
        self.assertEqual(family.name, "kimi")
        self.assertTrue(family.detected)
        self.assertEqual(family.skill_dirs, (code,))

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

    def test_kimi_detects_code_user_skill_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".kimi-code").mkdir()
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                agents = {item.name: item for item in detect_agents(env={}, home=home)}
            self.assertTrue(agents["kimi"].detected)
            self.assertEqual(
                agents["kimi"].skills_dir,
                home / ".config" / "agents" / "skills",
            )

    def test_kimi_desktop_is_not_a_supported_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Library" / "Application Support" / "kimi-desktop"
            desktop.mkdir(parents=True)
            with mock.patch("skill_sync.agents.shutil.which", return_value=None):
                agents = {item.name: item for item in detect_agents(env={}, home=home)}
                clients = detect_clients(env={"KIMI_DESKTOP_SKILLS_DIR": str(desktop)}, home=home)
            self.assertFalse(agents["kimi"].detected)
            self.assertNotIn("kimi-desktop", {client.id for client in clients})
            with self.assertRaisesRegex(ValueError, "unknown agent client"):
                get_client("kimi-desktop", env={}, home=home)

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
                    "CLAUDE_HOME": "/custom/claude",
                },
                home=Path("/home/example"),
            )}
        self.assertEqual(agents["codex"].skills_dir, Path("/custom/codex/skills"))
        self.assertEqual(agents["workbuddy"].skills_dir, Path("/custom/workbuddy/skills"))
        self.assertEqual(
            agents["kimi"].skill_dirs,
            (Path("/custom/kimi-code-skills"),),
        )
        self.assertEqual(agents["claude"].skills_dir, Path("/custom/claude/skills"))
