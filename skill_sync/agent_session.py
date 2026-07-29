"""Launch interactive coding agents inside managed edit workspaces."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from skill_sync.errors import SkillSyncError
from skill_sync.hash import is_link_or_reparse


SUPPORTED_EDIT_AGENTS = ("codex", "kimi-code")
_EXECUTABLES = {"codex": "codex", "kimi-code": "kimi"}
_DISPLAY_NAMES = {"codex": "Codex", "kimi-code": "Kimi Code"}
_KIMI_INSTRUCTION = (
    "Kimi Code 已加载完整的受管编辑说明，并继续进入同一工作目录的交互会话。"
)


def launch_agent(
    *,
    session_id: str,
    skill: str,
    status: str,
    workspace_path: str | Path,
    agent: str,
    scope: str = "base",
    target: str | None = None,
) -> dict[str, Any]:
    """Open one allowlisted interactive Agent in a visible macOS Terminal."""

    validate_agent(agent)
    if status != "active":
        raise SkillSyncError(
            "only an active edit session can launch an Agent",
            code="edit_agent_session_inactive",
            details={"session_id": session_id, "status": status},
        )
    return _launch_agent(
        session_id=session_id,
        skill=skill,
        workspace_path=workspace_path,
        agent=agent,
        scope=scope,
        target=target,
    )


def validate_agent(agent: str) -> None:
    """Reject Agent identifiers that are not backed by a fixed launcher."""

    if agent not in SUPPORTED_EDIT_AGENTS:
        raise SkillSyncError(
            f"unsupported edit Agent: {agent}",
            code="edit_agent_unsupported",
            details={"agent": agent, "supported_agents": list(SUPPORTED_EDIT_AGENTS)},
        )


def detect_agent_capabilities(platform: str | None = None) -> dict[str, Any]:
    """Report whether each interactive edit launcher is installed and usable."""

    current_platform = sys.platform if platform is None else platform
    terminal_supported = current_platform == "darwin"
    terminal_path = shutil.which("osascript") if terminal_supported else None
    terminal_available = terminal_path is not None
    agents: list[dict[str, Any]] = []
    for agent in SUPPORTED_EDIT_AGENTS:
        executable_name = _EXECUTABLES[agent]
        executable = shutil.which(executable_name)
        installed = executable is not None
        if not installed:
            reason = "not-installed"
        elif not terminal_supported:
            reason = "terminal-unsupported"
        elif not terminal_available:
            reason = "terminal-missing"
        else:
            reason = None
        agents.append(
            {
                "agent": agent,
                "display_name": _DISPLAY_NAMES[agent],
                "executable_name": executable_name,
                "executable_path": executable,
                "installed": installed,
                "available": reason is None,
                "reason": reason,
            }
        )
    return {
        "schema_version": 1,
        "platform": current_platform,
        "terminal": {
            "supported": terminal_supported,
            "available": terminal_available,
            "executable_path": terminal_path,
        },
        "agents": agents,
    }


def _launch_agent(
    *,
    session_id: str,
    skill: str,
    workspace_path: str | Path,
    agent: str,
    scope: str,
    target: str | None,
) -> dict[str, Any]:
    workspace = Path(workspace_path).absolute()
    if is_link_or_reparse(workspace) or not workspace.is_dir():
        raise SkillSyncError(
            "edit session workspace is not a safe real directory",
            code="edit_agent_workspace_unsafe",
            details={"session_id": session_id, "workspace_path": str(workspace)},
        )
    executable_name = _EXECUTABLES[agent]
    executable = shutil.which(executable_name)
    if executable is None:
        raise SkillSyncError(
            f"{_DISPLAY_NAMES[agent]} is not installed or is not available on PATH",
            code="edit_agent_executable_missing",
            details={"agent": agent, "executable": executable_name},
        )
    if sys.platform != "darwin":
        raise SkillSyncError(
            "opening an interactive Agent terminal is currently supported only on macOS",
            code="edit_agent_terminal_unsupported",
            details={"platform": sys.platform},
        )
    terminal = shutil.which("osascript")
    if terminal is None:
        raise SkillSyncError(
            "macOS Terminal launcher is not available on PATH",
            code="edit_agent_terminal_missing",
            details={"executable": "osascript"},
        )

    prompt = _edit_prompt(
        session_id=session_id,
        skill=skill,
        workspace=workspace,
        scope=scope,
        target=target,
    )
    command = _agent_command(agent, executable, workspace, prompt)
    script = """on run argv
  tell application "Terminal"
    activate
    do script (item 1 of argv)
  end tell
end run"""
    try:
        completed = subprocess.run(
            [terminal, "-e", script, command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillSyncError(
            f"could not open {_DISPLAY_NAMES[agent]} in Terminal: {exc}",
            code="edit_agent_terminal_launch_failed",
            details={"session_id": session_id, "agent": agent},
        ) from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or "Terminal returned an error"
        raise SkillSyncError(
            f"could not open {_DISPLAY_NAMES[agent]} in Terminal: {message}",
            code="edit_agent_terminal_launch_failed",
            details={"session_id": session_id, "agent": agent},
        )

    result: dict[str, Any] = {
        "session_id": session_id,
        "skill": skill,
        "agent": agent,
        "workspace_path": str(workspace),
        "launched": True,
        "terminal": "macos-terminal",
    }
    if agent == "kimi-code":
        result["instruction"] = _KIMI_INSTRUCTION
    return result


def _edit_prompt(
    *,
    session_id: str,
    skill: str,
    workspace: Path,
    scope: str,
    target: str | None,
) -> str:
    if scope == "base":
        scope_summary = "Base authored source; accepted changes may affect every configured client."
        layer_rules = (
            "This workspace is a complete Base Skill source. Preserve a valid SKILL.md "
            "and all referenced resources."
        )
    elif scope == "family":
        scope_summary = f"Family Variant authored layer for {target}; accepted changes may affect every client in that family."
        layer_rules = (
            "This workspace contains only the authored Variant overlay, not a copy of Base "
            "or resolved deployment output. Keep variant.yaml valid. Do not copy unchanged "
            "Base files into the overlay."
        )
    else:
        scope_summary = f"Exact Client Variant authored layer for {target}; accepted changes must remain client-specific."
        layer_rules = (
            "This workspace contains only the authored Variant overlay, not a copy of Base "
            "or resolved deployment output. Keep variant.yaml valid. Do not copy unchanged "
            "Base files into the overlay or widen the change to another client."
        )
    return f"""You are working in a Skill Sync managed edit session.

Session context
- Skill: {skill}
- Session ID: {session_id}
- Authored scope: {scope_summary}
- Only writable edit workspace: {workspace}

Layer contract
- {layer_rules}
- Treat the workspace contents as the only authored layer you may change. Generated or deployed client output is read-only and is not an editing source.

Hard boundaries
1. Modify only files inside the exact workspace above. Do not modify its parent session directory, baseline snapshot, canonical Skill source, rendered deployments, Agent Skill links, or files elsewhere on the machine.
2. Do not run Skill Sync apply, abort, recover, sync, import, link, unlink, commit, or push operations. Do not run git commit or git push.
3. Do not bypass approvals or sandboxing. Do not create symlinks, junctions, reparse points, device files, sockets, or paths that escape the workspace.
4. Do not edit generated provenance files or treat deployment content as canonical source.

Workflow
1. Inspect the current authored workspace and its existing instructions before proposing changes.
2. This briefing is not the user's edit request. Before changing files, ask the user what behavior or content they want changed, which clients should receive it, and what acceptance criteria matter. If their request conflicts with the selected scope, stop and explain the mismatch instead of widening scope.
3. Make the smallest coherent change within this authored layer. Preserve valid Skill frontmatter, referenced resources, local conventions, and portability unless the user explicitly requests a scoped exception.
4. Review every changed file and run only relevant, non-mutating checks available inside the workspace. Do not apply or publish the session yourself.
5. When finished, summarize changed files, validation performed, and remaining concerns. Tell the user to return to Skill Sync, click "检查更改", review diff/validation/impact, and explicitly confirm "应用更改" there.
"""


def _agent_command(
    agent: str,
    executable: str,
    workspace: Path,
    prompt: str,
) -> str:
    if agent == "codex":
        return "exec " + shlex.join([executable, "-C", str(workspace), prompt])
    change_directory = shlex.join(["cd", "--", str(workspace)])
    bootstrap = shlex.join([executable, "--prompt", prompt])
    resume = shlex.join([executable, "--continue"])
    return f"{change_directory} && {bootstrap} && exec {resume}"
