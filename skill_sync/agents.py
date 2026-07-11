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
    extra_skill_dirs: tuple[Path, ...] = ()

    @property
    def skill_dirs(self) -> tuple[Path, ...]:
        return (self.skills_dir, *self.extra_skill_dirs)


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
    kimi_code_detected = (
        "KIMI_CODE_SKILLS_DIR" in environ
        or (root / ".kimi-code").exists()
        or kimi_code_skills.exists()
        or shutil.which("kimi") is not None
    )
    kimi_desktop_detected = (
        "KIMI_DESKTOP_SKILLS_DIR" in environ
        or kimi_desktop_home.exists()
        or kimi_desktop_skills.exists()
    )
    kimi_skill_dirs = []
    if kimi_code_detected:
        kimi_skill_dirs.append(kimi_code_skills)
    if kimi_desktop_detected:
        kimi_skill_dirs.append(kimi_desktop_skills)
    if not kimi_skill_dirs:
        kimi_skill_dirs.append(kimi_code_skills)
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
            "kimi",
            "Kimi",
            kimi_skill_dirs[0],
            kimi_code_detected or kimi_desktop_detected,
            tuple(kimi_skill_dirs[1:]),
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
