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
    ]


def get_agent(name: str, **kwargs: object) -> AgentTarget:
    for agent in detect_agents(**kwargs):
        if agent.name == name:
            return agent
    raise ValueError(f"unknown agent: {name}")
