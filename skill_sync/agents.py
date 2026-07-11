"""Detect supported Agent clients and their Skill directories."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping


@dataclass(frozen=True)
class AgentTarget:
    name: str
    display_name: str
    skills_dir: Path
    detected: bool


def detect_agents(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[AgentTarget]:
    environ = os.environ if env is None else env
    root = Path.home() if home is None else home
    codex_home = Path(environ.get("CODEX_HOME", root / ".codex"))
    workbuddy_home = Path(environ.get("WORKBUDDY_HOME", root / ".workbuddy"))
    kimi_code_skills = Path(
        environ.get("KIMI_CODE_SKILLS_DIR", root / ".config" / "agents" / "skills")
    )
    kimi_desktop_skills = Path(
        environ.get(
            "KIMI_DESKTOP_SKILLS_DIR",
            root
            / "Library"
            / "Application Support"
            / "kimi-desktop"
            / "daimon-share"
            / "daimon"
            / "skills",
        )
    )
    kimi_desktop_home = (
        root
        / "Library"
        / "Application Support"
        / "kimi-desktop"
    )
    claude_home = Path(environ.get("CLAUDE_HOME", root / ".claude"))
    return [
        AgentTarget(
            "codex",
            "Codex",
            codex_home / "skills",
            codex_home.exists() or shutil.which("codex") is not None,
        ),
        AgentTarget(
            "workbuddy",
            "WorkBuddy",
            workbuddy_home / "skills",
            workbuddy_home.exists() or (root / ".workbuddy-ai").exists(),
        ),
        AgentTarget(
            "kimi-code",
            "Kimi Code",
            kimi_code_skills,
            (root / ".kimi-code").exists()
            or kimi_code_skills.exists()
            or shutil.which("kimi") is not None,
        ),
        AgentTarget(
            "kimi-desktop",
            "Kimi Desktop",
            kimi_desktop_skills,
            kimi_desktop_home.exists() or kimi_desktop_skills.exists(),
        ),
        AgentTarget(
            "claude",
            "Claude Code",
            claude_home / "skills",
            claude_home.exists() or shutil.which("claude") is not None,
        ),
    ]


def get_agent(name: str, **kwargs: object) -> AgentTarget:
    for agent in detect_agents(**kwargs):
        if agent.name == name:
            return agent
    raise ValueError(f"unknown agent: {name}")
