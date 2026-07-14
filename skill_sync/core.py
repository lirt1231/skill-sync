"""Core skill-sync workflows.

This module is intentionally UI-free so an argparse CLI or future frontend can
reuse the same behavior and handle :class:`SkillSyncError` consistently.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from skill_sync import git
from skill_sync.agents import aggregate_agent_targets, detect_agents, detect_clients
from skill_sync.config import default_config_path, load_config, save_config
from skill_sync.copying import copy_skill_dir
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.linking import create_directory_link, link_state, remove_directory_link
from skill_sync.ownership import inspect_ownership
from skill_sync.platforms import get_adapter
from skill_sync.protocol import EXIT_SAFETY
from skill_sync.registry import empty_registry, load_registry, save_registry
from skill_sync.skill_metadata import read_skill_description


REGISTRY_FILE = "registry.yaml"
DEFAULT_AGENT_TARGETS = "codex,workbuddy,kimi,claude"


def is_initialized(config_path: str | Path | None = None) -> bool:
    """Return whether this machine has a usable local sync checkout configured."""
    config = load_config(_config_path(config_path))
    repo_text = config.get("sync_repo_path")
    return isinstance(repo_text, str) and bool(repo_text) and (Path(repo_text) / ".git").is_dir()


def sync_preview(
    skill_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
    *,
    fetch_remote: bool = False,
) -> dict[str, Any]:
    """Describe the next safe synchronization action without changing state.

    ``fetch_remote`` is deliberately opt-in: dashboards stay fast and offline,
    while an explicit sync operation asks Git for fresh remote state.
    """
    if not is_initialized(config_path):
        return {
            "schema_version": 1,
            "initialized": False,
            "action": "setup",
            "summary": "Set up a private Git repository before syncing.",
            "skills": [],
            "issues": [],
        }
    try:
        config = _load_local_config(config_path)
        repo = _repo_path(config)
        branch = _branch(config)
        registry = _load_local_registry(config)
        if fetch_remote:
            git.fetch(repo, branch)
        git_state = git.state(repo, branch, fetch_remote=False)
        targets = _target_names(registry, skill_names)
        issues: list[dict[str, str]] = []
        unexpected_dirty = _unexpected_dirty_paths(repo)
        if unexpected_dirty:
            issues.append({"type": "dirty-repository", "detail": ", ".join(unexpected_dirty)})
        install_needed = _any_missing_local_install(config, registry, targets)
        local_changed = _any_local_changed(
            config, registry, [name for name in targets if not _needs_local_install(config, registry, name)]
        )
        link_issues = [
            issue for issue in doctor(config_path=config_path)["issues"]
            if issue.get("type") not in {"missing-skill"}
        ]
        issues.extend(link_issues)
        registry_dirty = not git_state.clean and not unexpected_dirty
        if unexpected_dirty:
            action, summary = "blocked", "The sync repository has unrelated local changes."
        elif git_state.diverged or (git_state.behind > 0 and local_changed):
            action, summary = "conflict", "Both the remote repository and local Skills changed."
            issues.append({"type": "content-conflict", "detail": "Choose and merge content manually."})
        elif git_state.behind > 0 or install_needed:
            action, summary = "pull", "Remote Skill changes are ready to install."
        elif local_changed or registry_dirty or git_state.ahead > 0:
            action, summary = "push", "Local Skill or selection changes are ready to publish."
        elif link_issues:
            action, summary = "repair-links", "Skill contents are current; some Agent links need repair."
        else:
            action, summary = "noop", "Everything is up to date."
        return {
            "schema_version": 1,
            "initialized": True,
            "action": action,
            "summary": summary,
            "skills": targets,
            "repo": {
                "path": str(repo), "branch": branch, "clean": git_state.clean,
                "ahead": git_state.ahead, "behind": git_state.behind, "diverged": git_state.diverged,
                "remote_checked": fetch_remote,
            },
            "issues": issues,
        }
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def init_sync(
    repo: str | Path,
    sync_dir: str | Path | None = None,
    branch: str = "main",
    platform: str | None = "codex",
    skills_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Initialize local state and connect it to a sync repository."""

    try:
        git.ensure_git_available()
        if platform is not None:
            get_adapter(platform)

        repo_text = str(repo)
        repo_path = Path(repo_text).expanduser()
        destination = _default_sync_dir() if sync_dir is None else Path(sync_dir).expanduser()

        if _looks_like_local_path(repo_text) and not repo_path.exists():
            if sync_dir is not None:
                raise SkillSyncError(
                    "cannot initialize a missing local repo path when sync_dir is explicit"
                )
            git.init_repo(repo_path, branch)
            destination = repo_path if sync_dir is None else destination
        elif destination.exists() and (destination / ".git").exists():
            if _has_origin(destination):
                git.clone_or_use_existing(repo_text, destination, branch)
            else:
                _checkout_or_create_branch(destination, branch)
        else:
            git.clone_repo(repo_text, destination, branch)

        registry_path = destination / REGISTRY_FILE
        if not registry_path.exists():
            save_registry(registry_path, empty_registry())

        config_file = _config_path(config_path)
        config = load_config(config_file)
        config["sync_repo_path"] = str(destination.absolute())
        config["branch"] = branch
        if platform is not None:
            config["platform"] = platform
        else:
            config.pop("platform", None)
        config["skills_root"] = str(
            Path(skills_root).expanduser().resolve()
            if skills_root is not None
            else (
                get_adapter(platform).default_skill_dir()
                if platform is not None
                else Path.home() / ".agents" / "skills"
            )
        )
        config.setdefault("skills", {})
        save_config(config_file, config)

        return {
            "sync_repo_path": str(destination.absolute()),
            "branch": branch,
            "platform": platform,
            "skills_root": config["skills_root"],
            "registry_path": str(registry_path.absolute()),
        }
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def scan_skills(
    platform: str | None = "codex",
    skill_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List candidate local Skills without modifying sync state."""

    try:
        config = _load_local_config(config_path)
        registry = _load_local_registry(config)
        selected_names = _selected_names(registry)
        if platform is None:
            root = _global_skill_root(config, skill_dir)
            adapter = get_adapter("codex")
            skill_dir = root
        else:
            adapter = get_adapter(platform)
        candidates = adapter.discover(skill_dir=skill_dir, selected_names=selected_names)
        return [
            {
                "name": candidate.name,
                "path": str(candidate.path),
                "description": read_skill_description(candidate.path),
                "selected": candidate.selected,
                "external": candidate.external,
            }
            for candidate in candidates
        ]
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def managed_check(
    input_path: str | Path,
    *,
    client: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect local Skill ownership without fetching or changing state.

    A completed inspection is successful for both managed and unmanaged paths.
    Ambiguous ownership is a safety error because callers must not interpret an
    inconclusive result as permission to edit.
    """

    try:
        config = _load_local_config(config_path)
        registry = _load_local_registry(config)
        inspection = inspect_ownership(
            input_path,
            skills_root=_global_skill_root(config),
            registry=registry,
            clients=detect_clients(),
            client=client,
        ).to_dict()
    except SkillSyncError as exc:
        raise SkillSyncError(
            f"could not inspect Skill ownership: {exc}",
            code="ownership_check_failed",
            exit_code=exc.exit_code,
            details={"input_path": str(input_path), "client": client},
        ) from exc
    except (ValueError, OSError) as exc:
        raise SkillSyncError(
            f"could not inspect Skill ownership: {exc}",
            code="ownership_check_failed",
            details={"input_path": str(input_path), "client": client},
        ) from exc

    if inspection["state"] == "ambiguous":
        raise SkillSyncError(
            "Skill ownership is ambiguous; stop before editing and verify the path or client",
            code="ownership_ambiguous",
            exit_code=EXIT_SAFETY,
            details={"inspection": inspection},
        )
    return inspection


def select_skills(
    items: Iterable[str | Path],
    platform: str | None = "codex",
    allow_external: bool = False,
    config_path: str | Path | None = None,
    skill_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Select one or more local Skills for synchronization."""

    try:
        item_list = list(items)
        if not item_list:
            raise SkillSyncError("select requires at least one Skill name or path")

        config_file = _config_path(config_path)
        config = load_config(config_file)
        repo = _repo_path(config)
        registry_path = repo / REGISTRY_FILE
        registry = _read_or_empty_registry(registry_path)
        registry.setdefault("skills", {})
        skills_config = config.setdefault("skills", {})
        root = _global_skill_root(config, skill_dir) if platform is None else _skill_root(platform, skill_dir)

        for item in item_list:
            skill_path = _resolve_skill_item(item, root)
            _validate_skill_path(skill_path)
            if not _is_inside(skill_path.resolve(), root.resolve()) and not allow_external:
                raise SkillSyncError(
                    f"{skill_path} is outside the global Skill root; pass allow_external to select it"
                )
            name = skill_path.name
            existing = skills_config.get(name, {})
            existing_path = existing.get("local_path") if isinstance(existing, dict) else None
            currently_selected = name in _selected_names(registry)
            if (
                currently_selected
                and existing_path
                and Path(existing_path).resolve() != skill_path.resolve()
            ):
                raise SkillSyncError(
                    f"{name} is already selected from a different path; deselect it first"
                )
            registry["version"] = 2 if platform is None else registry.get("version", 1)
            registry["skills"][name] = {
                "selected": True,
                "display_name": name,
            }
            if platform is not None:
                registry["skills"][name]["source_platform"] = platform
            else:
                registry["skills"][name]["targets"] = DEFAULT_AGENT_TARGETS
            skill_config = dict(existing) if isinstance(existing, dict) else {}
            skill_config["local_path"] = str(skill_path.resolve())
            skills_config[name] = skill_config

        save_registry(registry_path, registry)
        save_config(config_file, config)
        return {"selected": [Path(item).name for item in item_list]}
    except SkillSyncError:
        raise
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def deselect_skills(
    names: Iterable[str],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Remove Skills from registry and local config without deleting files."""

    try:
        name_list = list(names)
        if not name_list:
            raise SkillSyncError("deselect requires at least one Skill name")
        config_file = _config_path(config_path)
        config = load_config(config_file)
        repo = _repo_path(config)
        registry_path = repo / REGISTRY_FILE
        registry = _read_or_empty_registry(registry_path)
        registry.setdefault("skills", {})
        config.setdefault("skills", {})

        for name in name_list:
            registry["skills"].pop(name, None)
            config["skills"].pop(name, None)

        save_registry(registry_path, registry)
        save_config(config_file, config)
        return {"deselected": name_list}
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def status(
    skill_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
    fetch_remote: bool = True,
) -> dict[str, Any]:
    """Return a JSON-friendly status object."""

    try:
        config = _load_local_config(config_path)
        repo = _repo_path(config)
        branch = _branch(config)
        registry = _load_local_registry(config)
        git_state = git.state(repo, branch, fetch_remote=fetch_remote)
        skills = [_skill_status(config, registry, name) for name in _target_names(registry, skill_names)]
        return {
            "schema_version": 1,
            "repo": {
                "path": str(repo),
                "branch": branch,
                "clean": git_state.clean,
                "ahead": git_state.ahead,
                "behind": git_state.behind,
                "diverged": git_state.diverged,
            },
            "skills": skills,
        }
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def pull(
    skill_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fetch, fast-forward, and install selected remote Skills locally."""

    try:
        config_file = _config_path(config_path)
        config = load_config(config_file)
        repo = _repo_path(config)
        branch = _branch(config)
        if not git.is_clean(repo):
            raise SkillSyncError("sync repository is dirty; commit or discard changes before pull")
        git.fetch(repo, branch)
        git.merge_ff_only(repo, branch)

        registry = _load_local_registry(config)
        _reconcile_config_to_registry(config, registry)
        targets = _target_names(registry, skill_names)
        _refuse_local_overwrite(config, registry, targets)
        installed: list[str] = []
        for name in targets:
            remote_skill = repo / "skills" / name
            if not remote_skill.exists():
                continue
            destination = _local_skill_path_or_default(config, registry, name)
            copied_hash = copy_skill_dir(remote_skill, destination)
            config.setdefault("skills", {}).setdefault(name, {})["local_path"] = str(destination)
            config["skills"][name]["last_installed_hash"] = copied_hash
            installed.append(name)

        save_config(config_file, config)
        return {"pulled": installed}
    except SkillSyncError:
        raise
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def push(
    skill_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Copy selected local Skills into the sync repo, commit, and push."""

    try:
        config_file = _config_path(config_path)
        config = load_config(config_file)
        repo = _repo_path(config)
        branch = _branch(config)
        registry = _load_local_registry(config)
        targets = _target_names(registry, skill_names)

        _ensure_only_expected_registry_dirty(repo)
        git.fetch(repo, branch)
        current = git.state(repo, branch)
        if current.diverged:
            raise SkillSyncError("local and remote branches diverged")
        if current.behind > 0:
            raise SkillSyncError("remote has commits not present locally; pull before push")

        pushed_hashes: dict[str, str] = {}
        for name in targets:
            source = _local_skill_path(config, name)
            _validate_skill_path(source)
            destination = repo / "skills" / name
            pushed_hashes[name] = copy_skill_dir(source, destination)

        committed = git.commit_all_if_changed(repo, message or "Sync selected skills")
        git.push(repo, branch)

        for name, hash_value in pushed_hashes.items():
            config.setdefault("skills", {}).setdefault(name, {})["last_installed_hash"] = hash_value
        save_config(config_file, config)
        return {"pushed": list(pushed_hashes), "committed": committed}
    except SkillSyncError:
        raise
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def sync(
    skill_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the safe default synchronization workflow."""

    try:
        preview = sync_preview(skill_names=skill_names, config_path=config_path, fetch_remote=True)
        action = preview["action"]
        if action == "setup":
            raise SkillSyncError("skill-sync is not initialized; run init first")
        if action == "blocked":
            raise SkillSyncError("sync repository is dirty: " + preview["summary"])
        if action == "conflict":
            raise SkillSyncError("both remote and local selected Skills changed; resolve manually")
        if action == "pull":
            result = pull(skill_names=skill_names, config_path=config_path)
            if "platform" not in _load_local_config(config_path):
                result["links"] = link_skills(skill_names=skill_names, config_path=config_path)["links"]
            return result
        if action == "push":
            return push(skill_names=skill_names, config_path=config_path)
        if action == "repair-links":
            return {"synced": [], "links": link_skills(skill_names=skill_names, config_path=config_path)["links"]}
        result: dict[str, Any] = {"synced": [], "noop": True}
        if "platform" not in _load_local_config(config_path):
            result["links"] = link_skills(skill_names=skill_names, config_path=config_path)["links"]
        return result
    except SkillSyncError:
        raise
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def _config_path(path: str | Path | None) -> Path:
    return default_config_path() if path is None else Path(path)


def _default_sync_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "skill-sync" / "repo"


def _looks_like_local_path(value: str) -> bool:
    return (
        value.startswith(("/", "./", "../", "~"))
        or "://" not in value
        and not value.startswith(("git@", "ssh://", "https://", "http://"))
    )


def _has_origin(repo: Path) -> bool:
    try:
        git.run_git(repo, ["remote", "get-url", "origin"])
    except git.GitError:
        return False
    return True


def _checkout_or_create_branch(repo: Path, branch: str) -> None:
    try:
        git.run_git(repo, ["checkout", branch])
    except git.GitError:
        git.run_git(repo, ["checkout", "-b", branch])


def _load_local_config(config_path: str | Path | None) -> dict[str, Any]:
    return load_config(_config_path(config_path))


def _repo_path(config: dict[str, Any]) -> Path:
    repo = config.get("sync_repo_path")
    if not repo:
        raise SkillSyncError("skill-sync is not initialized; run init first")
    return Path(repo)


def _branch(config: dict[str, Any]) -> str:
    branch = config.get("branch") or "main"
    if not isinstance(branch, str):
        raise SkillSyncError("configured branch must be a string")
    return branch


def _read_or_empty_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_registry()
    return load_registry(path)


def _load_local_registry(config: dict[str, Any]) -> dict[str, Any]:
    return _read_or_empty_registry(_repo_path(config) / REGISTRY_FILE)


def _selected_names(registry: dict[str, Any]) -> set[str]:
    skills = registry.get("skills", {})
    if not isinstance(skills, dict):
        raise SkillSyncError("registry skills must be a mapping")
    return {
        name
        for name, entry in skills.items()
        if isinstance(entry, dict) and entry.get("selected", True)
    }


def _target_names(registry: dict[str, Any], skill_names: Iterable[str] | None) -> list[str]:
    selected = _selected_names(registry)
    if skill_names is None:
        return sorted(selected)
    targets = list(skill_names)
    missing = [name for name in targets if name not in selected]
    if missing:
        raise SkillSyncError(f"Skill is not selected: {', '.join(missing)}")
    return targets


def _skill_root(platform: str, skill_dir: str | Path | None) -> Path:
    if skill_dir is not None:
        return Path(skill_dir).resolve()
    return get_adapter(platform).default_skill_dir().resolve()


def _global_skill_root(config: dict[str, Any], skill_dir: str | Path | None = None) -> Path:
    if skill_dir is not None:
        return Path(skill_dir).expanduser().resolve()
    return Path(config.get("skills_root") or Path.home() / ".agents" / "skills").expanduser().resolve()


def _resolve_skill_item(item: str | Path, root: Path) -> Path:
    raw = Path(item).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        return raw.resolve()
    return (root / raw).resolve()


def _validate_skill_path(path: Path) -> None:
    if not path.exists():
        raise SkillSyncError(f"Skill path does not exist: {path}")
    if not path.is_dir():
        raise SkillSyncError(f"Skill path is not a directory: {path}")
    if not (path / "SKILL.md").is_file():
        raise SkillSyncError(f"Skill path does not contain SKILL.md: {path}")


def _is_inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _local_skill_path(config: dict[str, Any], name: str) -> Path:
    skills = config.get("skills", {})
    if not isinstance(skills, dict):
        raise SkillSyncError("config skills must be a mapping")
    entry = skills.get(name)
    if not isinstance(entry, dict):
        raise SkillSyncError(f"missing local config for selected Skill: {name}")
    local_path = entry.get("local_path")
    if not isinstance(local_path, str) or not local_path:
        raise SkillSyncError(f"missing local path for selected Skill: {name}")
    return Path(local_path)


def _local_skill_path_or_default(
    config: dict[str, Any], registry: dict[str, Any], name: str
) -> Path:
    skills = config.setdefault("skills", {})
    entry = skills.get(name)
    if isinstance(entry, dict):
        local_path = entry.get("local_path")
        if isinstance(local_path, str) and local_path:
            return Path(local_path)

    if config.get("skills_root") and "platform" not in config:
        return _global_skill_root(config) / name
    registry_entry = registry.get("skills", {}).get(name, {})
    platform = config.get("platform", "codex")
    if isinstance(registry_entry, dict):
        platform = registry_entry.get("source_platform", platform)
    if not isinstance(platform, str):
        raise SkillSyncError(f"invalid platform for selected Skill: {name}")
    return get_adapter(platform).default_skill_dir() / name


def _skill_status(config: dict[str, Any], registry: dict[str, Any], name: str) -> dict[str, Any]:
    local_path = _local_skill_path(config, name)
    _validate_skill_path(local_path)
    local_hash = hash_skill_dir(local_path)
    repo = _repo_path(config)
    remote_path = repo / "skills" / name
    remote_hash = hash_skill_dir(remote_path) if remote_path.exists() else None
    baseline = config.get("skills", {}).get(name, {}).get("last_installed_hash")
    changed_local = local_hash != (baseline or remote_hash)
    entry = registry.get("skills", {}).get(name, {})
    platform = entry.get("source_platform", config.get("platform", "global")) if isinstance(entry, dict) else config.get("platform", "global")
    return {
        "name": name,
        "description": read_skill_description(local_path),
        "platform": platform,
        "local_path": str(local_path),
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "changed_local": changed_local,
        "selected": name in _selected_names(registry),
    }


def _any_local_changed(config: dict[str, Any], registry: dict[str, Any], targets: list[str]) -> bool:
    return any(_skill_status(config, registry, name)["changed_local"] for name in targets)


def _any_missing_local_install(
    config: dict[str, Any], registry: dict[str, Any], targets: list[str]
) -> bool:
    return any(_needs_local_install(config, registry, name) for name in targets)


def _needs_local_install(config: dict[str, Any], registry: dict[str, Any], name: str) -> bool:
    local_path = _local_skill_path_or_default(config, registry, name)
    return not local_path.exists()


def _reconcile_config_to_registry(config: dict[str, Any], registry: dict[str, Any]) -> None:
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise SkillSyncError("config skills must be a mapping")
    selected = _selected_names(registry)
    for name in list(skills):
        if name not in selected:
            del skills[name]


def _refuse_local_overwrite(
    config: dict[str, Any], registry: dict[str, Any], targets: list[str]
) -> None:
    repo = _repo_path(config)
    for name in targets:
        local_path = _local_skill_path_or_default(config, registry, name)
        if not local_path.exists():
            continue
        _validate_skill_path(local_path)
        baseline = config.get("skills", {}).get(name, {}).get("last_installed_hash")
        local_hash = hash_skill_dir(local_path)
        if baseline:
            if local_hash != baseline:
                raise SkillSyncError(
                    f"refusing to overwrite locally changed Skill: {name}"
                )
            continue

        remote_skill = repo / "skills" / name
        if remote_skill.exists():
            if local_hash != hash_skill_dir(remote_skill):
                raise SkillSyncError(
                    f"refusing to overwrite locally changed Skill: {name}"
                )
            continue

        if local_path.exists():
            raise SkillSyncError(
                f"refusing to overwrite locally changed Skill: {name}"
            )


def _ensure_only_expected_registry_dirty(repo: Path) -> None:
    unexpected = _unexpected_dirty_paths(repo)
    if unexpected:
        raise SkillSyncError(
            "sync repository has unexpected dirty changes: " + ", ".join(unexpected)
        )


def _unexpected_dirty_paths(repo: Path) -> list[str]:
    porcelain = git.run_git(repo, ["status", "--porcelain"])
    unexpected: list[str] = []
    for line in porcelain.splitlines():
        path_text = line[2:].strip() if len(line) > 2 else ""
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        if path_text != REGISTRY_FILE:
            unexpected.append(path_text or line)
    return unexpected


def link_skills(
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Link selected canonical Skills into detected Agent directories."""
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    targets = _target_names(registry, skill_names)
    requested_agents = set(agent_names or ())
    disabled_agents = _disabled_agents(config)
    if requested_agents & disabled_agents:
        raise SkillSyncError(
            "Agent synchronization is disabled: "
            + ", ".join(sorted(requested_agents & disabled_agents))
        )
    agents = [
        a
        for a in detect_agents()
        if a.detected and a.name not in disabled_agents and (not requested_agents or a.name in requested_agents)
    ]
    unknown = requested_agents - {a.name for a in detect_agents()}
    if unknown:
        raise SkillSyncError("unknown Agent: " + ", ".join(sorted(unknown)))
    results: list[dict[str, str]] = []
    for name in targets:
        source = _local_skill_path_or_default(config, registry, name)
        _validate_skill_path(source)
        entry = registry.get("skills", {}).get(name, {})
        configured = _configured_agent_targets(entry)
        for agent in agents:
            if configured and agent.name not in configured:
                continue
            for skills_dir in agent.skill_dirs:
                destination = skills_dir / name
                try:
                    method = create_directory_link(source, destination)
                    state = "linked"
                except FileExistsError:
                    method = "none"
                    state = link_state(source, destination)
                results.append({"skill": name, "agent": agent.name, "state": state, "method": method, "path": str(destination)})
    return {"links": results}


def unlink_skills(
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    targets = _target_names(registry, skill_names)
    requested = set(agent_names or ())
    removed: list[dict[str, str]] = []
    for agent in detect_agents():
        if requested and agent.name not in requested:
            continue
        for name in targets:
            source = _local_skill_path_or_default(config, registry, name)
            for skills_dir in agent.skill_dirs:
                destination = skills_dir / name
                if remove_directory_link(source, destination):
                    removed.append({"skill": name, "agent": agent.name, "path": str(destination)})
    return {"unlinked": removed}


def doctor(config_path: str | Path | None = None) -> dict[str, Any]:
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    clients = detect_clients()
    agents = aggregate_agent_targets(clients)
    disabled_agents = _disabled_agents(config)
    issues: list[dict[str, str]] = []
    matrix: list[dict[str, str]] = []
    client_matrix: list[dict[str, str]] = []
    for name in sorted(_selected_names(registry)):
        source = _local_skill_path_or_default(config, registry, name)
        if not (source / "SKILL.md").is_file():
            issues.append({"type": "missing-skill", "skill": name, "path": str(source)})
            continue
        client_states: dict[str, str] = {}
        for client in clients:
            if not client.detected:
                continue
            state = (
                "disabled"
                if client.family_id in disabled_agents
                else _agent_skill_state(source, client.skills_dir / name)
            )
            client_states[client.id] = state
            client_matrix.append(
                {
                    "skill": name,
                    "client": client.id,
                    "agent": client.family_id,
                    "state": state,
                }
            )
        for agent in agents:
            if not agent.detected:
                continue
            if agent.name in disabled_agents:
                matrix.append({"skill": name, "agent": agent.name, "state": "disabled"})
                continue
            states = [
                client_states[client.id]
                for client in clients
                if client.detected
                and client.family_id == agent.name
                and client.id in client_states
            ]
            state = _combined_link_state(states)
            matrix.append({"skill": name, "agent": agent.name, "state": state})
            if state not in {"linked", "missing", "copied"}:
                issues.append({"type": state, "skill": name, "agent": agent.name})
    return {
        "skills_root": str(_global_skill_root(config)),
        "agents": [{"name": a.name, "display_name": a.display_name, "skills_dir": str(a.skills_dir), "skills_dirs": [str(path) for path in a.skill_dirs], "detected": a.detected, "enabled": a.name not in disabled_agents} for a in agents],
        "clients": [
            {
                "name": client.id,
                "family": client.family_id,
                "display_name": client.display_name,
                "skills_dir": str(client.skills_dir),
                "detected": client.detected,
                "enabled": client.family_id not in disabled_agents,
                "status": (
                    "disabled"
                    if client.family_id in disabled_agents
                    else "detected"
                    if client.detected
                    else "not-detected"
                ),
            }
            for client in clients
        ],
        "matrix": matrix,
        "client_matrix": client_matrix,
        "issues": issues,
    }


def disable_agent_sync(
    agent_name: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Persistently disable an Agent target and remove its managed links."""
    config_file = _config_path(config_path)
    config = load_config(config_file)
    known_agents = {agent.name for agent in detect_agents()}
    if agent_name not in known_agents:
        raise SkillSyncError(f"unknown Agent: {agent_name}")
    disabled_agents = _disabled_agents(config)
    disabled_agents.add(agent_name)
    config["disabled_agents"] = sorted(disabled_agents)
    save_config(config_file, config)
    removed = unlink_skills(agent_names=[agent_name], config_path=config_file)
    return {"disabled": agent_name, **removed}


def enable_agent_sync(
    agent_name: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Re-enable an Agent target without creating links automatically."""
    config_file = _config_path(config_path)
    config = load_config(config_file)
    known_agents = {agent.name for agent in detect_agents()}
    if agent_name not in known_agents:
        raise SkillSyncError(f"unknown Agent: {agent_name}")
    config["disabled_agents"] = sorted(_disabled_agents(config) - {agent_name})
    save_config(config_file, config)
    return {"enabled": agent_name}


def _disabled_agents(config: dict[str, Any]) -> set[str]:
    value = config.get("disabled_agents", [])
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise SkillSyncError("configured disabled_agents must be a list of strings")
    return {_normalize_agent_name(name) for name in value}


def _configured_agent_targets(entry: Any) -> set[str]:
    if not isinstance(entry, dict):
        return set()
    raw_targets = str(entry.get("targets", DEFAULT_AGENT_TARGETS)).split(",")
    return {_normalize_agent_name(name.strip()) for name in raw_targets if name.strip()}


def _normalize_agent_name(name: str) -> str:
    if name in {"kimi-code", "kimi-desktop"}:
        return "kimi"
    return name


def _combined_link_state(states: list[str]) -> str:
    if not states:
        return "missing"
    unique = set(states)
    if len(unique) == 1:
        return states[0]
    if unique <= {"linked", "missing"}:
        return "partial"
    if unique <= {"linked", "copied"}:
        return "copied"
    return next(state for state in states if state not in {"linked", "missing"})


def _agent_skill_state(source: Path, destination: Path) -> str:
    state = link_state(source, destination)
    if state != "conflict" or not destination.is_dir() or destination.is_symlink():
        return state
    try:
        return "copied" if hash_skill_dir(source) == hash_skill_dir(destination) else state
    except (OSError, ValueError):
        return state


def scan_import_candidates(
    agent_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List real Agent-local Skill directories that can be globalized."""
    config = _load_local_config(config_path)
    global_root = _global_skill_root(config)
    agents = {agent.name: agent for agent in detect_agents()}
    requested = set(agent_names) if agent_names is not None else {"codex", "claude", "workbuddy"} & set(agents)
    unknown = requested - set(agents)
    if unknown:
        raise SkillSyncError("unknown Agent: " + ", ".join(sorted(unknown)))
    candidates: list[dict[str, Any]] = []
    for agent_name in sorted(requested):
        agent = agents[agent_name]
        if not agent.detected or not agent.skills_dir.exists():
            continue
        for source in sorted(agent.skills_dir.iterdir(), key=lambda path: path.name):
            if source.is_symlink() or not source.is_dir() or not (source / "SKILL.md").is_file():
                continue
            destination = global_root / source.name
            state = "importable"
            if destination.exists():
                state = "same" if hash_skill_dir(source) == hash_skill_dir(destination) else "conflict"
            candidates.append(
                {"name": source.name, "agent": agent_name, "path": str(source), "state": state}
            )
    return candidates


def import_agent_skills(
    names: Iterable[str],
    agent_name: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Move Agent-local Skills into the global root and replace them with links."""
    name_list = list(names)
    if not name_list:
        raise SkillSyncError("import requires at least one Skill name")
    config = _load_local_config(config_path)
    agents = {agent.name: agent for agent in detect_agents()}
    agent = agents.get(agent_name)
    if agent is None:
        raise SkillSyncError(f"unknown Agent: {agent_name}")
    if not agent.detected:
        raise SkillSyncError(f"Agent is not detected: {agent_name}")
    global_root = _global_skill_root(config)
    imported: list[dict[str, str]] = []
    for name in name_list:
        if Path(name).name != name or name in {".", ".."}:
            raise SkillSyncError(f"invalid Skill name: {name}")
        source = agent.skills_dir / name
        destination = global_root / name
        if source.is_symlink():
            if link_state(destination, source) != "linked":
                raise SkillSyncError(f"{name} is linked to a different location")
            imported.append({"name": name, "agent": agent_name, "state": "already-linked"})
            continue
        _validate_skill_path(source)
        destination_existed = destination.exists()
        if destination_existed:
            _validate_skill_path(destination)
            if hash_skill_dir(source) != hash_skill_dir(destination):
                raise SkillSyncError(f"global Skill has different content: {name}")
        else:
            copy_skill_dir(source, destination)
        backup = source.parent / f".{name}.skill-sync-import-{time.time_ns()}"
        try:
            os.replace(source, backup)
            create_directory_link(destination, source)
            if link_state(destination, source) != "linked":
                raise SkillSyncError(f"failed to verify imported Skill link: {name}")
        except Exception:
            if source.is_symlink():
                source.unlink()
            if backup.exists():
                os.replace(backup, source)
            if not destination_existed and destination.exists():
                shutil.rmtree(destination)
            raise
        shutil.rmtree(backup)
        imported.append({"name": name, "agent": agent_name, "state": "imported"})
    select_skills(name_list, platform=None, config_path=config_path)
    return {"imported": imported}


def copy_global_skills_to_agents(
    names: Iterable[str],
    agent_names: Iterable[str],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Copy canonical Skills into Agent directories without creating links.

    A matching managed link is safely replaced with a real copy. Existing real
    directories are never overwritten, including directories with the same
    content, so this operation can also be used to detach a previously imported
    Skill before it is removed from the global library.
    """
    name_list = list(dict.fromkeys(names))
    requested_agents = set(agent_names)
    if not name_list:
        raise SkillSyncError("copy requires at least one Skill name")
    if not requested_agents:
        raise SkillSyncError("copy requires at least one Agent")
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    agents = {agent.name: agent for agent in detect_agents()}
    unknown = requested_agents - set(agents)
    if unknown:
        raise SkillSyncError("unknown Agent: " + ", ".join(sorted(unknown)))
    unavailable = [name for name in requested_agents if not agents[name].detected]
    if unavailable:
        raise SkillSyncError("Agent is not detected: " + ", ".join(sorted(unavailable)))
    copied: list[dict[str, str]] = []
    for name in name_list:
        if Path(name).name != name or name in {".", ".."}:
            raise SkillSyncError(f"invalid Skill name: {name}")
        source = _local_skill_path_or_default(config, registry, name)
        _validate_skill_path(source)
        for agent_name in sorted(requested_agents):
            agent = agents[agent_name]
            for skills_dir in agent.skill_dirs:
                destination = skills_dir / name
                state = link_state(source, destination)
                if state == "linked":
                    remove_directory_link(source, destination)
                elif state != "missing":
                    raise SkillSyncError(
                        f"refusing to overwrite existing {state} directory: {destination}"
                    )
                try:
                    copy_skill_dir(source, destination)
                except Exception:
                    if not destination.exists() and state == "linked":
                        create_directory_link(source, destination)
                    raise
                copied.append({"skill": name, "agent": agent_name, "path": str(destination), "state": "copied"})
    return {"copied": copied}


def delete_global_skills(
    names: Iterable[str],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Permanently delete canonical Skills and their managed Agent links."""
    name_list = list(dict.fromkeys(names))
    if not name_list:
        raise SkillSyncError("delete requires at least one Skill name")
    config_file = _config_path(config_path)
    config = load_config(config_file)
    repo = _repo_path(config)
    registry_path = repo / REGISTRY_FILE
    registry = _read_or_empty_registry(registry_path)
    global_root = _global_skill_root(config)
    agents = detect_agents()
    moved: list[tuple[str, Path, Path]] = []
    removed_links: list[tuple[Path, Path]] = []
    try:
        for name in name_list:
            if Path(name).name != name or name in {".", ".."}:
                raise SkillSyncError(f"invalid Skill name: {name}")
            source = global_root / name
            if source.is_symlink():
                raise SkillSyncError(f"refusing to delete global Skill symlink: {name}")
            _validate_skill_path(source)
            backup = global_root / f".{name}.skill-sync-delete-{time.time_ns()}"
            os.replace(source, backup)
            moved.append((name, source, backup))
            for agent in agents:
                for skills_dir in agent.skill_dirs:
                    destination = skills_dir / name
                    if remove_directory_link(source, destination):
                        removed_links.append((source, destination))
        skills = registry.setdefault("skills", {})
        local_skills = config.setdefault("skills", {})
        for name in name_list:
            skills.pop(name, None)
            local_skills.pop(name, None)
        save_registry(registry_path, registry)
        save_config(config_file, config)
    except Exception:
        for _, source, backup in reversed(moved):
            if backup.exists() and not source.exists():
                os.replace(backup, source)
        for source, destination in removed_links:
            if source.exists() and not destination.exists():
                create_directory_link(source, destination)
        raise
    for _, _, backup in moved:
        shutil.rmtree(backup)
    return {"deleted": name_list}


def backup_global_skill(
    name: str,
    config_path: str | Path | None = None,
) -> dict[str, str]:
    """Create a timestamped local backup without changing the live Skill."""
    if Path(name).name != name or name in {".", ".."}:
        raise SkillSyncError(f"invalid Skill name: {name}")
    config = _load_local_config(config_path)
    source = _global_skill_root(config) / name
    if source.is_symlink():
        raise SkillSyncError(f"refusing to back up global Skill symlink: {name}")
    _validate_skill_path(source)
    destination = _global_skill_root(config) / ".skill-sync-backups" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    copy_skill_dir(source, destination)
    return {"skill": name, "backup_path": str(destination)}
