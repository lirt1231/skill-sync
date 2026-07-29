import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync import agent_session, core
from skill_sync.errors import SkillSyncError


class AgentSessionLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "managed workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def launch(self, agent: str):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        executable = "/opt/tools/codex" if agent == "codex" else "/opt/tools/kimi"
        executables = {"codex": executable, "kimi": executable, "osascript": "/usr/bin/osascript"}
        with mock.patch.object(agent_session.sys, "platform", "darwin"), mock.patch.object(
            agent_session.shutil, "which", side_effect=executables.get
        ), mock.patch.object(
            agent_session.subprocess, "run", return_value=completed
        ) as run:
            result = agent_session.launch_agent(
                session_id="session-1",
                skill="alpha",
                status="active",
                workspace_path=self.workspace,
                agent=agent,
                scope="client",
                target="kimi-code",
            )
        return result, run.call_args

    def test_codex_launches_interactive_session_in_exact_workspace(self):
        result, call = self.launch("codex")
        command = call.args[0][3]
        arguments = shlex.split(command.removeprefix("exec "))

        self.assertEqual(arguments[:3], ["/opt/tools/codex", "-C", str(self.workspace)])
        self.assertIn("Skill Sync managed edit session", arguments[3])
        self.assertIn("Session ID: session-1", arguments[3])
        self.assertIn("Exact Client Variant authored layer for kimi-code", arguments[3])
        self.assertIn(str(self.workspace), arguments[3])
        self.assertIn("Do not copy unchanged Base files", arguments[3])
        self.assertIn('click "检查更改"', arguments[3])
        self.assertNotRegex(command, r"--(?:yolo|full-auto|dangerously|auto-approve)")
        self.assertEqual(result["workspace_path"], str(self.workspace))
        self.assertTrue(result["launched"])
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["timeout"], 10)

    def test_kimi_bootstraps_detailed_prompt_then_continues_interactively(self):
        result, call = self.launch("kimi-code")
        command = call.args[0][3]

        self.assertTrue(command.startswith(shlex.join(["cd", "--", str(self.workspace)]) + " && "))
        self.assertIn("/opt/tools/kimi --prompt ", command)
        self.assertIn("Session ID: session-1", command)
        self.assertIn("Exact Client Variant authored layer for kimi-code", command)
        self.assertTrue(command.endswith(" && exec /opt/tools/kimi --continue"))
        self.assertNotRegex(command, r"--(?:yolo|auto|dangerously|auto-approve)")
        self.assertIn("instruction", result)

    def test_capabilities_report_installation_separately_from_terminal_support(self):
        paths = {
            "osascript": "/usr/bin/osascript",
            "codex": "/opt/tools/codex",
            "kimi": None,
        }
        with mock.patch.object(agent_session.shutil, "which", side_effect=paths.get):
            value = agent_session.detect_agent_capabilities(platform="darwin")

        agents = {item["agent"]: item for item in value["agents"]}
        self.assertTrue(agents["codex"]["installed"])
        self.assertTrue(agents["codex"]["available"])
        self.assertEqual(agents["codex"]["executable_path"], "/opt/tools/codex")
        self.assertFalse(agents["kimi-code"]["installed"])
        self.assertEqual(agents["kimi-code"]["reason"], "not-installed")

        with mock.patch.object(
            agent_session.shutil, "which", side_effect={"codex": "/opt/tools/codex", "kimi": "/opt/tools/kimi"}.get
        ):
            unsupported = agent_session.detect_agent_capabilities(platform="linux")
        self.assertTrue(all(item["installed"] for item in unsupported["agents"]))
        self.assertTrue(all(item["reason"] == "terminal-unsupported" for item in unsupported["agents"]))

    def test_rejects_unknown_agent_inactive_session_and_unsafe_workspace(self):
        cases = (
            ({"agent": "shell", "status": "active", "workspace_path": self.workspace}, "edit_agent_unsupported"),
            ({"agent": "codex", "status": "applied", "workspace_path": self.workspace}, "edit_agent_session_inactive"),
            ({"agent": "codex", "status": "active", "workspace_path": self.workspace / "missing"}, "edit_agent_workspace_unsafe"),
        )
        for values, expected_code in cases:
            with self.subTest(code=expected_code), self.assertRaises(SkillSyncError) as raised:
                agent_session.launch_agent(
                    session_id="session-1",
                    skill="alpha",
                    **values,
                )
            self.assertEqual(raised.exception.code, expected_code)

    def test_rejects_symlink_workspace_and_missing_executable(self):
        link = Path(self.temp_dir.name) / "workspace-link"
        link.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(SkillSyncError) as raised:
            agent_session.launch_agent(
                session_id="session-1",
                skill="alpha",
                status="active",
                workspace_path=link,
                agent="codex",
            )
        self.assertEqual(raised.exception.code, "edit_agent_workspace_unsafe")

        with mock.patch.object(agent_session.sys, "platform", "darwin"), mock.patch.object(
            agent_session.shutil, "which", side_effect={"codex": None, "osascript": "/usr/bin/osascript"}.get
        ), self.assertRaises(SkillSyncError) as raised:
            agent_session.launch_agent(
                session_id="session-1",
                skill="alpha",
                status="active",
                workspace_path=self.workspace,
                agent="codex",
            )
        self.assertEqual(raised.exception.code, "edit_agent_executable_missing")

    def test_terminal_failure_is_structured(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="not allowed")
        with mock.patch.object(agent_session.sys, "platform", "darwin"), mock.patch.object(
            agent_session.shutil, "which", side_effect={"codex": "/opt/tools/codex", "osascript": "/usr/bin/osascript"}.get
        ), mock.patch.object(agent_session.subprocess, "run", return_value=completed), self.assertRaises(
            SkillSyncError
        ) as raised:
            agent_session.launch_agent(
                session_id="session-1",
                skill="alpha",
                status="active",
                workspace_path=self.workspace,
                agent="codex",
            )
        self.assertEqual(raised.exception.code, "edit_agent_terminal_launch_failed")


class CoreAgentSessionTest(unittest.TestCase):
    def test_unknown_agent_is_rejected_before_session_store_reads(self):
        with mock.patch.object(core, "edit_session_status") as status, self.assertRaises(
            SkillSyncError
        ) as raised:
            core.launch_edit_agent("session-1", "shell", config_path="/tmp/config.json")
        self.assertEqual(raised.exception.code, "edit_agent_unsupported")
        status.assert_not_called()

    def test_workspace_is_loaded_from_session_store_not_caller_input(self):
        with mock.patch.object(
            core, "edit_session_status", return_value={"logical_skill": "alpha", "status": "active"}
        ), mock.patch.object(
            core,
            "edit_session_paths",
            return_value={"baseline_path": "/trusted/baseline", "workspace_path": "/trusted/workspace"},
        ), mock.patch.object(core.agent_session, "launch_agent", return_value={"launched": True}) as launch:
            result = core.launch_edit_agent("session-1", "codex", config_path="/tmp/config.json")

        self.assertTrue(result["launched"])
        launch.assert_called_once_with(
            session_id="session-1",
            skill="alpha",
            status="active",
            workspace_path="/trusted/workspace",
            agent="codex",
            scope="base",
            target=None,
        )


if __name__ == "__main__":
    unittest.main()
