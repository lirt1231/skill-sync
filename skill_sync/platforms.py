"""Platform-specific Skill directory discovery for skill-sync."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillCandidate:
    """A Skill directory discovered for a platform."""

    name: str
    path: Path
    selected: bool
    external: bool


class CodexAdapter:
    """Adapter for Codex Skill directories."""

    name = "codex"

    @staticmethod
    def default_skill_dir(
        env: Mapping[str, str] | None = None, home: Path | None = None
    ) -> Path:
        """Return the Codex user Skill directory.

        The path is ``$CODEX_HOME/skills`` when ``CODEX_HOME`` is set, otherwise
        ``~/.codex/skills``. ``env`` and ``home`` are injectable to keep tests
        deterministic.
        """

        environ = os.environ if env is None else env
        codex_home = environ.get("CODEX_HOME")
        if codex_home:
            return Path(codex_home) / "skills"

        home_path = Path.home() if home is None else home
        return home_path / ".codex" / "skills"

    @classmethod
    def discover(
        cls,
        skill_dir: str | Path | None = None,
        selected_names: Iterable[str] | None = None,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> list[SkillCandidate]:
        """Discover child directories containing ``SKILL.md``."""

        default_skill_dir = cls.default_skill_dir(env=env, home=home)
        root = default_skill_dir if skill_dir is None else Path(skill_dir)
        selected = set(selected_names or ())
        external = root != default_skill_dir

        if not root.exists():
            return []

        candidates: list[SkillCandidate] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if not child.is_dir():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            candidates.append(
                SkillCandidate(
                    name=child.name,
                    path=child,
                    selected=child.name in selected,
                    external=external,
                )
            )
        return candidates


def get_adapter(name: str) -> CodexAdapter:
    """Return a platform adapter by name."""

    if name == CodexAdapter.name:
        return CodexAdapter()
    raise ValueError(f"unknown platform: {name}")
