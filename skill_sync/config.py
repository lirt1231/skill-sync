"""Local per-machine JSON configuration for the skill-sync CLI."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def default_config_path(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Return the XDG-style local config path for skill-sync.

    The path is ``${XDG_CONFIG_HOME:-~/.config}/skill-sync/config.json``.
    ``env`` and ``home`` are injectable to keep tests deterministic.
    """

    environ = os.environ if env is None else env
    config_home = environ.get("XDG_CONFIG_HOME")
    if config_home:
        base_path = Path(config_home)
    else:
        home_path = Path.home() if home is None else home
        base_path = home_path / ".config"
    return base_path / "skill-sync" / "config.json"


def default_data_root(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    *,
    os_name: str | None = None,
    platform: str | None = None,
) -> Path:
    """Return the machine-local root for rendered deployments and receipts."""

    environ = os.environ if env is None else env
    home_path = Path.home() if home is None else home
    current_os = os.name if os_name is None else os_name
    current_platform = sys.platform if platform is None else platform

    if current_os == "nt":
        base = Path(environ.get("LOCALAPPDATA", home_path / "AppData" / "Local"))
    elif current_platform == "darwin":
        base = home_path / "Library" / "Application Support"
    else:
        base = Path(environ.get("XDG_DATA_HOME", home_path / ".local" / "share"))
    return base / "skill-sync"


def empty_config() -> dict[str, Any]:
    """Return a new empty local config."""

    return {
        "sync_repo_path": None,
        "platform": "codex",
        "skills_root": str(Path.home() / ".agents" / "skills"),
        "branch": "main",
        "disabled_agents": [],
        "skills": {},
    }


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a JSON local config, returning defaults when it is missing."""

    config_path = Path(path)
    if not config_path.exists():
        return empty_config()

    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    _validate_config_shape(config)
    return config


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    """Save a JSON local config, creating parent directories as needed."""

    _validate_config_shape(config)
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2, sort_keys=True)
        config_file.write("\n")


def set_skill_baseline(config: dict[str, Any], skill_name: str, hash_value: str) -> None:
    """Update a skill's last installed content hash in a local config."""

    if not isinstance(config, dict):
        raise ValueError("Config root must be a mapping")
    if not isinstance(hash_value, str) or not hash_value.startswith("sha256:"):
        raise ValueError("Skill baseline hash must be a sha256: string")
    if not isinstance(skill_name, str) or not skill_name:
        raise ValueError("Skill name must be a non-empty string")

    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("Config skills must be a mapping")

    skill_config = skills.setdefault(skill_name, {})
    if not isinstance(skill_config, dict):
        raise ValueError("Config skill entry must be a mapping")
    skill_config["last_installed_hash"] = hash_value


def _validate_config_shape(config: Any) -> None:
    if not isinstance(config, dict):
        raise ValueError("Config root must be a mapping")
    skills = config.get("skills")
    if not isinstance(skills, dict):
        raise ValueError("Config skills must be a mapping")
    data_root = config.get("data_root")
    if data_root is not None and (not isinstance(data_root, str) or not data_root):
        raise ValueError("Config data_root must be a non-empty string")
    if data_root is not None and not Path(data_root).expanduser().is_absolute():
        raise ValueError("Config data_root must be an absolute path")
    disabled_agents = config.get("disabled_agents", [])
    if not isinstance(disabled_agents, list) or not all(
        isinstance(name, str) for name in disabled_agents
    ):
        raise ValueError("Config disabled_agents must be a list of strings")
