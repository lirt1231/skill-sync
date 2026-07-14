"""Detect supported Agent clients and their Skill directories."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping


@dataclass(frozen=True)
class AgentFamily:
    """A user-facing Agent product grouping one or more concrete clients."""

    id: str
    display_name: str
    client_ids: tuple[str, ...]

    @property
    def name(self) -> str:
        """Compatibility-friendly alias matching the existing target model."""

        return self.id


@dataclass(frozen=True)
class AgentClient:
    """A concrete Agent installation endpoint with one Skill directory."""

    id: str
    family_id: str
    display_name: str
    skills_dir: Path
    detected: bool
    link_capability: str = "symlink-or-junction"

    @property
    def name(self) -> str:
        return self.id

    @property
    def family(self) -> str:
        return self.family_id

    @property
    def skill_dirs(self) -> tuple[Path, ...]:
        return (self.skills_dir,)


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


AGENT_FAMILIES: tuple[AgentFamily, ...] = (
    AgentFamily("codex", "Codex", ("codex",)),
    AgentFamily("workbuddy", "WorkBuddy", ("workbuddy",)),
    AgentFamily("kimi", "Kimi", ("kimi-code", "kimi-desktop")),
    AgentFamily("claude", "Claude Code", ("claude-code",)),
)


def get_family(name: str) -> AgentFamily:
    for family in AGENT_FAMILIES:
        if family.id == name:
            return family
    raise ValueError(f"unknown agent family: {name}")


def detect_clients(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[AgentClient]:
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
    claude_home = Path(environ.get("CLAUDE_HOME", root / ".claude"))
    return [
        AgentClient(
            "codex",
            "codex",
            "Codex",
            codex_home / "skills",
            codex_home.exists() or shutil.which("codex") is not None,
        ),
        AgentClient(
            "workbuddy",
            "workbuddy",
            "WorkBuddy",
            workbuddy_home / "skills",
            workbuddy_home.exists() or (root / ".workbuddy-ai").exists(),
        ),
        AgentClient(
            "kimi-code",
            "kimi",
            "Kimi Code",
            kimi_code_skills,
            kimi_code_detected,
        ),
        AgentClient(
            "kimi-desktop",
            "kimi",
            "Kimi Desktop",
            kimi_desktop_skills,
            kimi_desktop_detected,
        ),
        AgentClient(
            "claude-code",
            "claude",
            "Claude Code",
            claude_home / "skills",
            claude_home.exists() or shutil.which("claude") is not None,
        ),
    ]


def get_client(name: str, **kwargs: object) -> AgentClient:
    for client in detect_clients(**kwargs):
        if client.id == name:
            return client
    raise ValueError(f"unknown agent client: {name}")


def expand_agent_clients(
    name: str,
    *,
    clients: list[AgentClient] | tuple[AgentClient, ...] | None = None,
    detected_only: bool = True,
    **kwargs: object,
) -> tuple[AgentClient, ...]:
    """Resolve a family or concrete client ID to concrete client endpoints.

    Family IDs take precedence where a family and its sole client share an ID,
    as with Codex and WorkBuddy. By default family expansion returns only
    clients detected on the current machine.
    """

    available = tuple(detect_clients(**kwargs) if clients is None else clients)
    family = next((item for item in AGENT_FAMILIES if item.id == name), None)
    if family is not None:
        family_clients = tuple(
            client for client in available if client.id in family.client_ids
        )
        if detected_only:
            family_clients = tuple(
                client for client in family_clients if client.detected
            )
        return family_clients

    client = next((item for item in available if item.id == name), None)
    if client is None:
        raise ValueError(f"unknown agent family or client: {name}")
    if detected_only and not client.detected:
        return ()
    return (client,)


def aggregate_agent_targets(
    clients: list[AgentClient] | tuple[AgentClient, ...],
) -> list[AgentTarget]:
    """Aggregate concrete clients into the legacy family target view."""

    return [aggregate_agent_family(family.id, clients) for family in AGENT_FAMILIES]


def aggregate_agent_family(
    name: str,
    clients: list[AgentClient] | tuple[AgentClient, ...],
) -> AgentTarget:
    """Build one compatibility target from a family's concrete clients."""

    family = get_family(name)
    members = tuple(
        client for client in clients if client.id in family.client_ids
    )
    if not members:
        raise ValueError(f"no clients available for agent family: {name}")
    detected_members = tuple(client for client in members if client.detected)
    # The legacy API always exposes every family. When no endpoint is
    # detected, retain its first client's default path as the target path.
    active_members = detected_members or members[:1]
    return AgentTarget(
        family.id,
        family.display_name,
        active_members[0].skills_dir,
        bool(detected_members),
        tuple(client.skills_dir for client in active_members[1:]),
    )


def detect_agents(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[AgentTarget]:
    """Return the legacy family-level view used by the current CLI and UI."""

    return aggregate_agent_targets(detect_clients(env=env, home=home))


def get_agent(name: str, **kwargs: object) -> AgentTarget:
    for agent in detect_agents(**kwargs):
        if agent.name == name:
            return agent
    raise ValueError(f"unknown agent: {name}")
