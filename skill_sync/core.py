"""Core skill-sync workflows.

This module is intentionally UI-free so an argparse CLI or future frontend can
reuse the same behavior and handle :class:`SkillSyncError` consistently.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from skill_sync import agent_session, git, portable_sync, variant_source
from skill_sync.agents import (
    AGENT_FAMILIES,
    aggregate_agent_targets,
    detect_agents,
    detect_clients,
    expand_agent_clients,
)
from skill_sync.config import default_config_path, default_data_root, load_config, save_config
from skill_sync.copying import copy_skill_dir, rename_no_replace
from skill_sync.deployment import (
    deployment_path,
    expected_layered_provenance,
    expected_provenance,
    remove_verified_deployment,
    render_base_deployment,
    render_layered_deployment,
    resolution_hash,
    verify_deployment,
)
from skill_sync.edit_recovery import (
    CapturedSnapshot,
    DeploymentQuarantine,
    DeploymentQuarantineRecoveryRequired,
    copy_authored_deployment,
    inspect_authored_deployment,
)
from skill_sync.edit_apply import (
    AbsentCanonicalSwap,
    CanonicalSwap,
    CanonicalSwapRecoveryRequired,
    PrivateJsonReceipt,
    ReceiptRecoveryRequired,
    fsync_tree,
    prepare_private_directory,
)
from skill_sync.edit_session import (
    ActiveEditSessionError,
    CanonicalSkillChangedError,
    EditLayerBaseline,
    EditLayerBaselineState,
    EditSessionMetadata,
    EditSessionMetadataError,
    EditSessionPaths,
    EditSessionPublicationRecoveryRequired,
    EditSessionScope,
    EditSessionScopeKind,
    EditSessionStatus,
    EditSessionStore,
    InvalidEditSessionTransition,
)
from skill_sync.edit_validation import (
    EditTreeInspectionError,
    FileRecord,
    TreeInspection,
    TreeIssue,
    build_diff,
    inspect_tree,
    validate_workspace,
)
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir, hash_skill_files, is_link_or_reparse
from skill_sync.linking import (
    DirectoryLinkSwap,
    DirectoryLinkSwapRecoveryRequired,
    create_directory_link,
    link_state,
    remove_directory_link,
    replace_directory_link,
)
from skill_sync.local_lock import local_file_lock
from skill_sync.ownership import inspect_ownership
from skill_sync.platforms import get_adapter
from skill_sync.protocol import EXIT_CONFLICT, EXIT_SAFETY
from skill_sync.registry import (
    empty_registry,
    load_registry,
    parse_registry_text,
    register_variant_target,
    registry_variant_targets,
    save_registry,
)
from skill_sync.skill_metadata import read_skill_description
from skill_sync.variant import (
    VARIANT_MANIFEST_FILE,
    build_minimal_variant_manifest,
)
from skill_sync.variant_overlay import VariantOverlayLayer, plan_variant_overlay
from skill_sync.variant_resolution import (
    LayeredVariantResolution,
    resolve_variant_for_client,
)


REGISTRY_FILE = "registry.yaml"
DEFAULT_AGENT_TARGETS = "codex,workbuddy,kimi,claude"
WEB_MUTATION_ACTIONS = ("sync", "import", "agent", "link-repair", "delete")


@dataclass(frozen=True, slots=True)
class _ClientDeploymentSpec:
    """One client's current content-addressed deployment contract."""

    path: Path
    resolution_hash: str
    expected_provenance: dict[str, Any]
    layered_resolution: LayeredVariantResolution | None


class _EditTransitionRecoveryRequired(RuntimeError):
    """A metadata transition may have committed before a durability error."""


def list_edit_sessions(
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect all machine-local edit sessions without network or mutations."""

    store = EditSessionStore(_data_root(_load_local_config(config_path)))
    try:
        sessions = [item.to_dict() for item in store.list_metadata()]
    except (EditSessionMetadataError, OSError) as exc:
        raise SkillSyncError(
            str(exc),
            code="invalid_edit_session_metadata",
            exit_code=EXIT_SAFETY,
        ) from exc
    return {"sessions": sessions}


def edit_session_status(
    session_id: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect one machine-local edit session without network or mutations."""

    store = EditSessionStore(_data_root(_load_local_config(config_path)))
    try:
        return store.load(session_id).to_dict()
    except FileNotFoundError as exc:
        raise SkillSyncError(
            f"edit session does not exist: {session_id}",
            code="edit_session_not_found",
            details={"session_id": session_id},
        ) from exc
    except (EditSessionMetadataError, OSError) as exc:
        raise SkillSyncError(
            str(exc),
            code="invalid_edit_session_metadata",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc


def edit_session_paths(
    session_id: str,
    config_path: str | Path | None = None,
) -> dict[str, str]:
    """Return machine-local paths without changing the stable metadata contract."""

    store = EditSessionStore(_data_root(_load_local_config(config_path)))
    edit_session_status(session_id, config_path=config_path)
    paths = store.paths(session_id)
    return {
        "baseline_path": str(paths.baseline.absolute()),
        "workspace_path": str(paths.workspace.absolute()),
    }


def launch_edit_agent(
    session_id: str,
    agent: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Launch an interactive Agent using only paths from the trusted session store."""

    agent_session.validate_agent(agent)
    session = edit_session_status(session_id, config_path=config_path)
    paths = edit_session_paths(session_id, config_path=config_path)
    target_scope = session.get("target_scope") or {"kind": "base", "target": None}
    return agent_session.launch_agent(
        session_id=session_id,
        skill=session["logical_skill"],
        status=session["status"],
        workspace_path=paths["workspace_path"],
        agent=agent,
        scope=target_scope.get("kind", "base"),
        target=target_scope.get("target"),
    )


def edit_agent_capabilities() -> dict[str, Any]:
    """Return local CLI and Terminal availability for managed Web edits."""

    return agent_session.detect_agent_capabilities()


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
    diagnosis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the next safe synchronization action without changing state.

    ``fetch_remote`` is deliberately opt-in: dashboards stay fast and offline,
    while an explicit sync operation asks Git for fresh remote state.
    A caller that already built ``diagnosis`` may supply it so one Web request
    does not repeat the same read-only deployment inspection.
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
        remote_registry = (
            _cached_remote_registry(repo, branch)
            if git_state.behind > 0
            else registry
        )
        remote_changes = (
            _remote_sync_unit_keys(repo, branch, registry, remote_registry)
            if git_state.behind > 0
            else set()
        )
        units = _sync_unit_rows(config, registry, targets, remote_changes)
        unit_conflicts = [row for row in units if row["state"] == "conflict"]
        issues: list[dict[str, str]] = []
        unexpected_dirty = _unexpected_dirty_paths(repo)
        if unexpected_dirty:
            issues.append({"type": "dirty-repository", "detail": ", ".join(unexpected_dirty)})
        install_needed = _any_missing_local_install(config, registry, targets)
        local_changed = any(row["changed_local"] for row in units)
        current_diagnosis = (
            doctor(config_path=config_path, _sync_units=units)
            if diagnosis is None
            else diagnosis
        )
        link_issues = [
            issue for issue in current_diagnosis["issues"]
            if issue.get("type") not in {"missing-skill"}
        ]
        issues.extend(link_issues)
        registry_dirty = not git_state.clean and not unexpected_dirty
        if unexpected_dirty:
            action, summary = "blocked", "The sync repository has unrelated local changes."
        elif git_state.diverged or unit_conflicts:
            action, summary = "conflict", "Both the remote repository and local Skills changed."
            issues.append(
                {
                    "type": "content-conflict",
                    "detail": "The same Base or Variant unit changed locally and remotely.",
                }
            )
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
            "units": units,
            "repo": {
                "path": str(repo), "branch": branch, "clean": git_state.clean,
                "ahead": git_state.ahead, "behind": git_state.behind, "diverged": git_state.diverged,
                "remote_checked": fetch_remote,
            },
            "issues": issues,
        }
    except (git.GitError, ValueError, OSError) as exc:
        raise SkillSyncError(str(exc)) from exc


def preview_mutation(
    operation: str,
    request: dict[str, Any],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a read-only Web mutation plan from normalized core inputs.

    The plan deliberately contains only names, paths, hashes, states and
    recovery guidance.  It never reads Skill bodies or credentials, contacts
    a remote, creates a backup, writes configuration, or changes a link.
    """

    if operation not in WEB_MUTATION_ACTIONS:
        raise SkillSyncError(
            f"unknown mutation preview operation: {operation}",
            code="invalid_mutation_preview",
            exit_code=EXIT_SAFETY,
        )
    if not isinstance(request, dict):
        raise SkillSyncError(
            "mutation preview request must be an object",
            code="invalid_mutation_preview",
            exit_code=EXIT_SAFETY,
        )
    allowed_fields = {
        "sync": {"skills"},
        "import": {"skills", "agent"},
        "agent": {"agent", "enabled"},
        "link-repair": {"skills", "agents"},
        "delete": {"skills"},
    }[operation]
    unknown = set(request) - allowed_fields
    if unknown:
        raise SkillSyncError(
            "unknown mutation preview field: " + ", ".join(sorted(unknown)),
            code="invalid_mutation_preview",
            exit_code=EXIT_SAFETY,
        )
    clients = tuple(detect_clients())
    try:
        if operation == "sync":
            return _sync_mutation_preview(request.get("skills"), config_path, clients)
        if operation == "import":
            return _import_mutation_preview(
                request.get("skills"), request.get("agent"), config_path, clients
            )
        if operation == "agent":
            return _agent_mutation_preview(
                request.get("agent"), request.get("enabled"), config_path, clients
            )
        if operation == "link-repair":
            return _link_repair_mutation_preview(
                request.get("skills"), request.get("agents"), config_path, clients
            )
        return _delete_mutation_preview(request.get("skills"), config_path, clients)
    except SkillSyncError:
        raise
    except (git.GitError, OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not safely preview {operation}: {exc}",
            code="mutation_preview_failed",
            exit_code=EXIT_SAFETY,
            details={"operation": operation},
        ) from exc


def _mutation_plan(
    operation: str,
    normalized_input: dict[str, Any],
    *,
    affected_skills: list[dict[str, Any]],
    affected_clients: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    backup: dict[str, Any],
    recovery: dict[str, Any],
    summary: str,
    details: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the stable, additive schema shared by every Web plan."""

    detail_rows = {} if details is None else details
    return {
        "schema_version": 1,
        "operation": operation,
        "request": normalized_input,
        "status": "conflict" if conflicts else "blocked" if blockers else "ready",
        "can_execute": not conflicts and not blockers,
        "summary": summary,
        "targets": {
            "skills": affected_skills,
            "clients": affected_clients,
        },
        "steps": _mutation_steps(
            operation, normalized_input, affected_skills, affected_clients
        ),
        "conflicts": conflicts,
        "blockers": blockers,
        "warnings": [] if warnings is None else warnings,
        "effects": _mutation_effects(
            operation,
            skill_count=len(affected_skills),
            client_count=len(affected_clients),
            conflicts=conflicts,
            details=detail_rows,
        ),
        "backup": {"created": False, **backup},
        "recovery": recovery,
        "freshness": {
            "remote_checked": False,
            "replan_required": True,
            "hint": "Rebuild this plan immediately before executing the mutation.",
        },
        "details": detail_rows,
    }


def _mutation_steps(
    operation: str,
    normalized_input: dict[str, Any],
    skills: list[dict[str, Any]],
    clients: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if operation == "agent":
        steps.append(
            {
                "order": 1,
                "action": "enable-agent-config"
                if normalized_input["enabled"]
                else "disable-agent-config",
                "skill": None,
                "family": normalized_input["agent"],
                "client": None,
                "path": None,
                "state": None,
                "reason": "Update the persisted disabled Agent families.",
            }
        )
    for skill in skills:
        steps.append(
            {
                "order": len(steps) + 1,
                "action": skill.get("effect", operation),
                "skill": skill.get("name"),
                "path": skill.get("canonical_path") or skill.get("source_path"),
                "state": skill.get("state"),
                "reason": skill.get("reason"),
            }
        )
    for client in clients:
        steps.append(
            {
                "order": len(steps) + 1,
                "action": client.get("effect", operation),
                "skill": client.get("skill"),
                "family": client.get("agent"),
                "client": client.get("client"),
                "path": client.get("destination"),
                "state": client.get("current_state"),
                "reason": client.get("reason"),
            }
        )
    return steps


def _mutation_effects(
    operation: str,
    *,
    skill_count: int,
    client_count: int,
    conflicts: list[dict[str, Any]],
    details: dict[str, Any],
) -> dict[str, Any]:
    sync_action = details.get("sync_action")
    effects = {
        "predicted": True,
        "network": False,
        "git": {"fetch": False, "commit": False, "push": False},
        "writes": {
            "config": False,
            "registry": False,
            "canonical": False,
            "rendered": False,
            "agent_links": False,
        },
        "permanent": False,
        "skills": skill_count,
        "clients": client_count,
        "conflicts": conflicts,
    }
    if operation == "sync":
        effects["network"] = True
        effects["git"] = {
            "fetch": True,
            "commit": sync_action == "push",
            "push": sync_action == "push",
        }
        effects["writes"].update(
            {
                "config": sync_action in {"pull", "push"},
                "registry": sync_action == "pull",
                "canonical": sync_action == "pull",
                "rendered": sync_action in {"pull", "repair-links"},
                "agent_links": sync_action in {"pull", "repair-links"},
            }
        )
    elif operation == "import":
        if not details.get("noop", False):
            effects["writes"].update(
                {
                    "config": True,
                    "registry": True,
                    "canonical": True,
                    "rendered": True,
                    "agent_links": True,
                }
            )
    elif operation == "agent":
        effects["writes"]["config"] = True
        effects["writes"]["agent_links"] = bool(details.get("link_changes", 0))
    elif operation == "link-repair":
        if not details.get("noop", False):
            effects["writes"]["rendered"] = True
            effects["writes"]["agent_links"] = True
    elif operation == "delete":
        effects["writes"].update(
            {
                "config": True,
                "registry": True,
                "canonical": True,
                "agent_links": True,
            }
        )
        effects["permanent"] = True
    return effects


def _sync_mutation_preview(
    skill_names: Iterable[str] | None,
    config_path: str | Path | None,
    clients: tuple[Any, ...],
) -> dict[str, Any]:
    normalized = _resolve_sync_input(skill_names, config_path)
    config = _load_local_config(config_path)
    safety_blockers = _sync_safety_blockers(config)
    diagnosis = doctor(config_path=config_path, clients=clients)
    preview = sync_preview(
        skill_names=normalized["skills"],
        config_path=config_path,
        fetch_remote=False,
        diagnosis=diagnosis,
    )
    conflicts = [
        {"code": issue.get("type", "sync-issue"), "detail": issue.get("detail", "")}
        for issue in preview.get("issues", [])
        if issue.get("type") in {"content-conflict", "dirty-repository"}
    ]
    blockers = []
    if preview["action"] in {"blocked", "conflict", "setup"}:
        blockers.append(
            {
                "code": f"sync-{preview['action']}",
                "detail": preview["summary"],
            }
        )
    blockers.extend(safety_blockers)
    affected_skills = [
        {"name": name, "effect": preview["action"]}
        for name in normalized["skills"]
    ]
    affected_clients = _configured_clients_for_skills(
        config, normalized["skills"], clients
    )
    return _mutation_plan(
        "sync",
        normalized,
        affected_skills=affected_skills,
        affected_clients=affected_clients,
        conflicts=conflicts,
        blockers=blockers,
        backup={
            "strategy": "git-history",
            "automatic": False,
            "persistent": False,
            "hint": "The preview creates nothing. A predicted push commits later; pull has no automatic local backup.",
        },
        recovery={
            "strategy": "stop-on-conflict",
            "hint": "Resolve repository or content conflicts before running sync.",
            "operations": _deployment_receipt_health(_data_root(config)),
        },
        summary=preview["summary"],
        details={"sync_action": preview["action"], "remote_checked": False},
    )


def _import_mutation_preview(
    skill_names: Iterable[str] | None,
    agent_name: str | None,
    config_path: str | Path | None,
    clients: tuple[Any, ...],
) -> dict[str, Any]:
    resolved = _resolve_import_input(
        skill_names, agent_name, config_path, clients=clients
    )
    conflicts, blockers = _import_safety_findings(resolved)
    affected_source_clients = [
        {
            "skill": row["name"],
            "agent": target["agent"],
            "client": target["client"],
            "destination": str(Path(target["destination_root"]) / row["name"]),
            "role": (
                "source-and-deployment"
                if target["client"] == resolved["source_client"]
                else "deployment"
            ),
            "effect": "replace-source-with-managed-link",
        }
        for row in resolved["rows"]
        if row["state"] != "already-linked"
        for target in resolved["deployment_clients"]
    ]
    normalized = {"skills": resolved["skills"], "agent": resolved["agent"]}
    return _mutation_plan(
        "import",
        normalized,
        affected_skills=[
            {
                "name": row["name"],
                "source_path": row["source_path"],
                "canonical_path": row["canonical_path"],
                "effect": row["state"],
            }
            for row in resolved["rows"]
        ],
        affected_clients=affected_source_clients,
        conflicts=conflicts,
        blockers=blockers,
        backup={
            "strategy": "temporary-source-rename",
            "persistent": False,
            "hint": "The action keeps a temporary rollback copy until link verification succeeds.",
        },
        recovery={
            "strategy": "restore-source-on-failure",
            "hint": "A failed rollback reports the preserved backup path and stops further mutations.",
            "operations": [],
        },
        summary=f"Import {len(resolved['skills'])} Skill(s) from {resolved['agent']}.",
        details={
            "noop": all(
                row["state"] == "already-linked" for row in resolved["rows"]
            )
        },
    )


def _agent_mutation_preview(
    agent_name: str | None,
    enabled: bool | None,
    config_path: str | Path | None,
    clients: tuple[Any, ...],
) -> dict[str, Any]:
    resolved = _resolve_agent_toggle_input(
        agent_name, enabled, config_path, clients=clients
    )
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    affected_clients = (
        []
        if resolved["enabled"]
        else _agent_unlink_targets(
            config, registry, resolved["agent"], clients
        )
    )
    affected_skill_names = sorted(
        {row["skill"] for row in affected_clients}
    )
    blockers = _agent_safety_blockers(config, enabled=resolved["enabled"])
    return _mutation_plan(
        "agent",
        resolved,
        affected_skills=[
            {"name": name, "effect": "unlink-and-disable"}
            for name in affected_skill_names
        ],
        affected_clients=affected_clients,
        conflicts=[],
        blockers=blockers,
        backup={
            "strategy": "previous-config-value",
            "persistent": False,
            "hint": "Disabling rolls the config and managed links back if unlinking fails.",
        },
        recovery={
            "strategy": "relink-from-managed-deployments",
            "hint": "If rollback cannot restore links, the action reports recovery details.",
            "operations": _deployment_receipt_health(_data_root(config)),
        },
        summary=("Enable" if resolved["enabled"] else "Disable")
        + f" {resolved['agent']} synchronization.",
        details={**resolved, "link_changes": len(affected_clients)},
    )


def _agent_unlink_targets(
    config: dict[str, Any],
    registry: dict[str, Any],
    agent_name: str,
    clients: Iterable[Any],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    rendered_root = _rendered_root(config)
    for client in clients:
        if client.family_id != agent_name or not client.skills_dir.is_dir():
            continue
        for name in sorted(_selected_names(registry)):
            source = _local_skill_path_or_default(config, registry, name)
            if not (source / "SKILL.md").is_file():
                continue
            entry = registry.get("skills", {}).get(name, {})
            configured = _configured_client_targets(entry)
            if configured and not {client.id, client.family_id}.intersection(
                configured
            ):
                continue
            destination = client.skills_dir / name
            if _owned_link_target(
                source, destination, rendered_root, name, client.id
            ) is None:
                continue
            targets.append(
                {
                    "skill": name,
                    "agent": client.family_id,
                    "client": client.id,
                    "destination": str(destination),
                    "current_state": "managed-link",
                    "effect": "remove-managed-link",
                }
            )
    return targets


def _link_repair_mutation_preview(
    skill_names: Iterable[str] | None,
    agent_names: Iterable[str] | None,
    config_path: str | Path | None,
    clients: tuple[Any, ...],
) -> dict[str, Any]:
    resolved = _resolve_link_input(
        skill_names, agent_names, config_path, clients=clients
    )
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    plan = _deployment_plan(
        config,
        registry,
        resolved["skills"],
        resolved["agents"],
        clients=clients,
    )
    conflicts, blockers = _link_safety_findings(
        config, resolved, plan, clients
    )
    affected_clients = [
        {
            "skill": skill["name"],
            "agent": client["agent"],
            "client": client["client"],
            "destination": client["destination"],
            "current_state": client["current_state"],
            "effect": client["action"],
        }
        for skill in plan
        for client in skill["clients"]
    ]
    return _mutation_plan(
        "link-repair",
        resolved,
        affected_skills=[
            {
                "name": skill["name"],
                "source_path": skill["source_path"],
                "effect": (
                    "noop"
                    if all(
                        client["action"] == "noop"
                        for client in skill["clients"]
                    )
                    else "repair-links"
                ),
            }
            for skill in plan
        ],
        affected_clients=affected_clients,
        conflicts=conflicts,
        blockers=blockers,
        backup={
            "strategy": "transactional-link-swap",
            "persistent": False,
            "hint": "The action records previous managed targets before swapping links.",
        },
        recovery={
            "strategy": "deployment-receipt",
            "hint": "Interrupted swaps stop with a receipt that identifies links requiring recovery.",
            "operations": _deployment_receipt_health(_data_root(config)),
        },
        summary=f"Repair managed links for {len(plan)} Skill(s).",
        details={
            "noop": all(
                client["action"] == "noop"
                for skill in plan
                for client in skill["clients"]
            )
        },
    )


def _delete_mutation_preview(
    skill_names: Iterable[str] | None,
    config_path: str | Path | None,
    clients: tuple[Any, ...],
) -> dict[str, Any]:
    resolved = _resolve_delete_input(skill_names, config_path, clients=clients)
    config = _load_local_config(config_path)
    blockers = _delete_safety_blockers(config, resolved["skills"])
    return _mutation_plan(
        "delete",
        {"skills": resolved["skills"]},
        affected_skills=[
            {
                "name": row["name"],
                "canonical_path": row["canonical_path"],
                "effect": "permanent-delete",
            }
            for row in resolved["rows"]
        ],
        affected_clients=[
            {
                "skill": row["name"],
                "agent": client["agent"],
                "client": client["client"],
                "destination": client["destination"],
                "effect": "remove-managed-link",
            }
            for row in resolved["rows"]
            for client in row["clients"]
        ],
        conflicts=[],
        blockers=blockers,
        backup={
            "strategy": "temporary-canonical-rename",
            "persistent": False,
            "hint": "No persistent backup remains after success; create an explicit backup first if needed.",
        },
        recovery={
            "strategy": "restore-before-commit",
            "hint": "The action restores canonical paths and managed links if registry/config updates fail.",
            "operations": _deployment_receipt_health(_data_root(config)),
        },
        summary=f"Permanently delete {len(resolved['skills'])} global Skill(s).",
        warnings=resolved["warnings"],
    )


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
            scan_root = Path(
                skill_dir
                if skill_dir is not None
                else config.get("skills_root") or Path.home() / ".agents" / "skills"
            ).expanduser().absolute()
            _reject_reparse_scan_root(scan_root)
            root = _global_skill_root(config, skill_dir)
            adapter = get_adapter("codex")
            skill_dir = root
        else:
            adapter = get_adapter(platform)
            root = adapter.default_skill_dir() if skill_dir is None else Path(skill_dir).expanduser().absolute()
            _reject_reparse_scan_root(root)
        candidates = adapter.discover(skill_dir=skill_dir, selected_names=selected_names)
        candidates = [
            candidate
            for candidate in candidates
            if not is_link_or_reparse(candidate.path)
        ]
        for candidate in candidates:
            _validate_skill_path(candidate.path)
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
            rendered_root=_rendered_root(config),
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


def edit_begin(
    skill_name: str,
    *,
    scope: str = "base",
    target: str | None = None,
    actor: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a Base, family, or client source-layer edit session."""

    config = _load_local_config(config_path)
    try:
        registry = _load_local_registry(config)
        _target_names(registry, [skill_name])
        store = EditSessionStore(_data_root(config))
        target_scope, affected_clients = _edit_target_scope(scope, target)
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            locked_config = _load_local_config(config_path)
            if _data_root(locked_config) != store.data_root:
                raise SkillSyncError(
                    "skill-sync data root changed while beginning edit session",
                    code="unsafe_edit_session",
                    exit_code=EXIT_SAFETY,
                    details={"skill": skill_name},
                )
            locked_registry = _load_local_registry(locked_config)
            _target_names(locked_registry, [skill_name])
            if target_scope.kind is EditSessionScopeKind.BASE:
                source = _local_skill_path_or_default(
                    locked_config, locked_registry, skill_name
                ).absolute()
                _validate_skill_path(source)
                baseline_hash = hash_skill_dir(source)
                metadata, paths = store.begin(
                    logical_skill=skill_name,
                    source=source,
                    baseline_hash=baseline_hash,
                    actor=actor,
                )
            else:
                with store.skill_lock(skill_name):
                    metadata, paths = _begin_scoped_edit_locked(
                        store=store,
                        skill_name=skill_name,
                        target_scope=target_scope,
                        actor=actor,
                        config_path=config_path,
                    )
        result = {
            "session_id": metadata.session_id,
            "skill": metadata.logical_skill,
            "scope": metadata.target_scope.kind.value,
            "status": metadata.status.value,
            "actor": metadata.actor,
            "baseline_hash": metadata.baseline_hash,
            "baseline_path": str(paths.baseline.absolute()),
            "workspace_path": str(paths.workspace.absolute()),
        }
        if metadata.target_scope.kind is not EditSessionScopeKind.BASE:
            result.update(
                {
                    "target": metadata.target_scope.target,
                    "affected_clients": list(affected_clients),
                    "layer_baseline": metadata.layer_baseline.to_dict(),
                }
            )
        return result
    except ActiveEditSessionError as exc:
        raise SkillSyncError(
            str(exc),
            code="active_edit_session",
            exit_code=EXIT_CONFLICT,
            details={
                "skill": exc.logical_skill,
                "session_id": exc.session_id,
            },
        ) from exc
    except CanonicalSkillChangedError as exc:
        raise SkillSyncError(
            str(exc),
            code="canonical_changed",
            exit_code=EXIT_CONFLICT,
            details={"skill": skill_name},
        ) from exc
    except EditSessionMetadataError as exc:
        raise SkillSyncError(
            f"could not safely begin edit session: {exc}",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"skill": skill_name},
        ) from exc
    except SkillSyncError:
        raise
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not begin edit session: {exc}",
            code="edit_begin_failed",
            details={"skill": skill_name},
        ) from exc


def _edit_target_scope(
    scope: str,
    target: str | None,
) -> tuple[EditSessionScope, tuple[str, ...]]:
    if scope == "base":
        if target is not None:
            raise SkillSyncError(
                "Base edit scope must not have a target",
                code="edit_scope_invalid",
                exit_code=EXIT_SAFETY,
            )
        return EditSessionScope.base(), tuple(
            client_id for family in AGENT_FAMILIES for client_id in family.client_ids
        )
    if target is None:
        raise SkillSyncError(
            f"{scope} edit scope requires a target",
            code="edit_scope_invalid",
            exit_code=EXIT_SAFETY,
        )
    try:
        family, affected_clients, _ = variant_source.variant_target_metadata(
            scope, target
        )
    except SkillSyncError as exc:
        raise SkillSyncError(
            str(exc),
            code=exc.code,
            exit_code=EXIT_SAFETY,
            details=exc.details,
        ) from exc
    if scope == "family":
        return EditSessionScope.family(family), affected_clients
    return EditSessionScope.client(target), affected_clients


def _begin_scoped_edit_locked(
    *,
    store: EditSessionStore,
    skill_name: str,
    target_scope: EditSessionScope,
    actor: str | None,
    config_path: str | Path | None,
) -> tuple[EditSessionMetadata, EditSessionPaths]:
    target = target_scope.target
    if target is None:
        raise SkillSyncError(
            "scoped edit target is missing",
            code="edit_scope_invalid",
            exit_code=EXIT_SAFETY,
        )
    sources = variant_source.resolve_variant_source_paths(
        skill_name,
        config_path=config_path,
    )
    if sources.skill != skill_name:
        raise SkillSyncError(
            "selected Skill spelling does not match canonical Variant source",
            code="variant_source_changed",
            exit_code=EXIT_SAFETY,
            details={"skill": skill_name, "canonical_skill": sources.skill},
        )
    matches = [
        observation
        for observation in sources.observations
        if observation.role.startswith("variant-target:")
        and observation.path.name.casefold() == target.casefold()
    ]
    if matches and (len(matches) != 1 or matches[0].path.name != target):
        raise SkillSyncError(
            "Variant target has a case-insensitive ambiguity",
            code="variant_target_ambiguous",
            exit_code=EXIT_SAFETY,
            details={"skill": skill_name, "target": target},
        )
    variant_source.verify_variant_source_paths(sources)
    if matches:
        layer_path = matches[0].path
        layer_snapshot = _capture_scoped_layer_snapshot(
            sources=sources,
            layer_path=layer_path,
            skill_name=skill_name,
            target=target,
        )
        layer_hash = hash_skill_files(
            (entry.relative_path, entry.content) for entry in layer_snapshot.files
        )

        def verify_existing_layer() -> None:
            variant_source.verify_variant_source_paths(sources)
            current = _capture_scoped_layer_snapshot(
                sources=sources,
                layer_path=layer_path,
                skill_name=skill_name,
                target=target,
            )
            if current != layer_snapshot:
                raise SkillSyncError(
                    "Variant edit layer changed while beginning the session",
                    code="variant_source_changed",
                    exit_code=EXIT_SAFETY,
                    details={"skill": skill_name, "target": target},
                )

        return store.begin_locked(
            logical_skill=skill_name,
            source=layer_path,
            baseline_hash=layer_hash,
            actor=actor,
            target_scope=target_scope,
            layer_baseline=EditLayerBaseline.present(layer_hash),
            publication_guard=verify_existing_layer,
        )

    with tempfile.TemporaryDirectory(
        prefix=".scoped-begin-", dir=store.data_root
    ) as root:
        template = Path(root) / target
        template.mkdir(mode=0o700)
        (template / VARIANT_MANIFEST_FILE).write_bytes(
            build_minimal_variant_manifest(target)
        )
        template_hash = hash_skill_dir(template)
        return store.begin_locked(
            logical_skill=skill_name,
            source=template,
            baseline_hash=template_hash,
            actor=actor,
            target_scope=target_scope,
            layer_baseline=EditLayerBaseline.absent(),
            publication_guard=lambda: variant_source.verify_variant_source_paths(
                sources
            ),
        )


def _capture_scoped_layer_snapshot(
    *,
    sources: variant_source.VariantSourcePaths,
    layer_path: Path,
    skill_name: str,
    target: str,
) -> VariantOverlayLayer:
    try:
        plan = plan_variant_overlay(sources.base_root, (layer_path,))
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"Variant edit layer is unsafe: {exc}",
            code="variant_source_unsafe",
            exit_code=EXIT_SAFETY,
            details={"skill": skill_name, "target": target},
        ) from exc
    if len(plan.layers) != 2 or plan.layers[0] != sources.initial_base_layer:
        raise SkillSyncError(
            "Variant Base source changed while beginning the session",
            code="variant_source_changed",
            exit_code=EXIT_SAFETY,
            details={"skill": skill_name, "target": target},
        )
    return plan.layers[1]


def edit_abort(
    session_id: str,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Abort an edit session without reading or writing canonical content."""

    config = _load_local_config(config_path)
    try:
        metadata = EditSessionStore(_data_root(config)).abort(session_id)
        result = {
            "session_id": metadata.session_id,
            "skill": metadata.logical_skill,
            "scope": metadata.target_scope.kind.value,
            "status": metadata.status.value,
        }
        if metadata.target_scope.target is not None:
            result["target"] = metadata.target_scope.target
        return result
    except EditSessionMetadataError as exc:
        raise SkillSyncError(
            f"could not safely abort edit session: {exc}",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not abort edit session: {exc}",
            code="edit_abort_failed",
            details={"session_id": session_id},
        ) from exc


def edit_delete(
    session_id: str,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Permanently remove one machine-local edit session and its local copies."""

    config = _load_local_config(config_path)
    store = EditSessionStore(_data_root(config))
    try:
        metadata, cleanup_pending = store.delete(session_id)
    except FileNotFoundError as exc:
        raise SkillSyncError(
            f"edit session does not exist: {session_id}",
            code="edit_session_not_found",
            details={"session_id": session_id},
        ) from exc
    except InvalidEditSessionTransition as exc:
        raise SkillSyncError(
            str(exc),
            code="edit_delete_blocked",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc
    except EditSessionMetadataError as exc:
        raise SkillSyncError(
            f"could not safely delete edit session: {exc}",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not delete edit session: {exc}",
            code="edit_delete_failed",
            details={"session_id": session_id},
        ) from exc

    result: dict[str, Any] = {
        "session_id": metadata.session_id,
        "skill": metadata.logical_skill,
        "scope": metadata.target_scope.kind.value,
        "previous_status": metadata.status.value,
        "deleted": True,
        "cleanup_pending": [str(cleanup_pending)] if cleanup_pending else [],
    }
    if metadata.target_scope.target is not None:
        result["target"] = metadata.target_scope.target
    return result


def edit_diff(
    session_id: str,
    *,
    resolved_client: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Diff an active authored layer and any affected client resolutions."""

    metadata, baseline, workspace = _active_edit_trees(
        session_id, config_path, operation="diff"
    )
    try:
        result = build_diff(baseline, workspace)
    except EditTreeInspectionError as exc:
        raise SkillSyncError(
            f"could not safely diff edit workspace: {exc}",
            code="invalid_edit_workspace",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc
    response = {
        "session_id": metadata.session_id,
        "skill": metadata.logical_skill,
        "scope": metadata.target_scope.kind.value,
        "status": metadata.status.value,
        **result,
    }
    if metadata.target_scope.kind is EditSessionScopeKind.BASE:
        if resolved_client is not None:
            raise SkillSyncError(
                "--resolved-client is only available for family or client sessions",
                code="edit_resolved_client_invalid",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id, "client": resolved_client},
            )
        return response

    target = metadata.target_scope.target
    if target is None:  # guarded by strict session metadata validation
        raise SkillSyncError(
            "scoped edit session has no target",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        )
    clients = _scoped_edit_clients(metadata)
    if resolved_client is not None:
        if resolved_client not in clients:
            raise SkillSyncError(
                "requested resolved client is outside this edit scope",
                code="edit_resolved_client_invalid",
                exit_code=EXIT_SAFETY,
                details={
                    "session_id": session_id,
                    "client": resolved_client,
                    "affected_clients": list(clients),
                },
            )
        clients = (resolved_client,)
    current_layer = _scoped_current_layer(metadata, config_path=config_path)
    resolved_diffs = [
        _scoped_resolved_diff(
            metadata,
            workspace_path=EditSessionStore(
                _data_root(_load_local_config(config_path))
            ).paths(session_id).workspace,
            client=client,
            config_path=config_path,
        )
        for client in clients
    ]
    return {
        **response,
        "target": target,
        "source_diff": result,
        "layer_baseline": metadata.layer_baseline.to_dict(),
        "current_layer": current_layer,
        "stale_baseline": current_layer != metadata.layer_baseline.to_dict(),
        "resolved_diffs": resolved_diffs,
    }


def edit_validate(
    session_id: str,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate an active Base or Variant authored-layer workspace."""

    metadata, baseline, workspace = _active_edit_trees(
        session_id, config_path, operation="validate"
    )
    if metadata.target_scope.kind is EditSessionScopeKind.BASE:
        issues = validate_workspace(workspace, logical_skill=metadata.logical_skill)
    else:
        issues = _validate_scoped_workspace(
            metadata,
            workspace,
            workspace_path=EditSessionStore(
                _data_root(_load_local_config(config_path))
            ).paths(session_id).workspace,
            config_path=config_path,
        )
    workspace_hash = workspace.hash
    result = {
        "session_id": metadata.session_id,
        "skill": metadata.logical_skill,
        "scope": metadata.target_scope.kind.value,
        "status": metadata.status.value,
        "valid": not issues,
        "changed": workspace_hash != baseline.hash,
        "baseline_hash": baseline.hash,
        "workspace_hash": workspace_hash,
        "issues": [issue.to_dict() for issue in issues],
    }
    if metadata.target_scope.kind is not EditSessionScopeKind.BASE:
        current_layer = _scoped_current_layer(metadata, config_path=config_path)
        result.update(
            {
                "target": metadata.target_scope.target,
                "layer_baseline": metadata.layer_baseline.to_dict(),
                "current_layer": current_layer,
                "stale_baseline": current_layer
                != metadata.layer_baseline.to_dict(),
                "affected_clients": list(_scoped_edit_clients(metadata)),
            }
        )
    return result


def edit_recover(
    skill_name: str,
    *,
    client: str,
    action: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Preview, capture, or discard one tampered concrete-client deployment."""

    if action not in {None, "capture", "discard"}:
        raise SkillSyncError(
            f"unknown edit recovery action: {action}",
            code="edit_recovery_invalid_action",
            exit_code=EXIT_SAFETY,
        )
    config = _load_local_config(config_path)
    clients = detect_clients()
    if action is None:
        return _edit_recovery_preview(config, skill_name, client, clients=clients)

    data_root = _data_root(config)
    store = EditSessionStore(data_root)
    try:
        with local_file_lock(data_root / "locks" / "deployment.lock"):
            preview = _edit_recovery_preview(
                config, skill_name, client, clients=clients
            )
            blocked = preview["blocked_by_session"]
            if blocked is not None:
                raise SkillSyncError(
                    "an unfinished edit session blocks deployment recovery",
                    code="edit_recovery_session_blocked",
                    exit_code=EXIT_SAFETY,
                    details={
                        "skill": skill_name,
                        "session_id": blocked["session_id"],
                        "status": blocked["status"],
                    },
                )
            _assert_no_incomplete_deployment_receipts(data_root)
            if action == "capture":
                if "capture" not in preview["allowed_actions"]:
                    raise SkillSyncError(
                        "capturing a layered deployment requires a scoped recovery workflow",
                        code="edit_recovery_scoped_capture_unsupported",
                        exit_code=EXIT_SAFETY,
                        details={"skill": skill_name, "client": client},
                    )
                return _edit_recovery_capture(store, preview)
            with store.skill_lock(skill_name):
                refreshed = _edit_recovery_preview(
                    config, skill_name, client, clients=clients
                )
                if refreshed["observed_hash"] != preview["observed_hash"]:
                    raise SkillSyncError(
                        "tampered deployment changed before recovery",
                        code="edit_recovery_conflict",
                        exit_code=EXIT_CONFLICT,
                    )
                return _edit_recovery_discard(config, refreshed)
    except SkillSyncError:
        raise
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="edit_recovery_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc
    except (EditSessionMetadataError, EditTreeInspectionError, OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not safely recover tampered deployment: {exc}",
            code="edit_recovery_failed",
            exit_code=EXIT_SAFETY,
            details={"skill": skill_name, "client": client, "action": action},
        ) from exc


def _edit_recovery_preview(
    config: dict[str, Any],
    skill_name: str,
    client_id: str,
    *,
    clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    registry = _load_local_registry(config)
    _target_names(registry, [skill_name])
    matches = [
        item for item in (detect_clients() if clients is None else clients)
        if item.id == client_id
    ]
    if len(matches) != 1:
        raise SkillSyncError(
            f"recovery requires one concrete client ID: {client_id}",
            code=(
                "edit_recovery_client_unknown"
                if not matches
                else "edit_recovery_client_ambiguous"
            ),
            exit_code=EXIT_SAFETY,
            details={"client": client_id},
        )
    target_client = matches[0]
    if not target_client.detected:
        raise SkillSyncError(
            f"client is not detected: {client_id}",
            code="edit_recovery_client_unavailable",
            exit_code=EXIT_SAFETY,
        )
    entry = registry.get("skills", {}).get(skill_name, {})
    configured = _configured_client_targets(entry)
    if configured and not {client_id, target_client.family_id}.intersection(configured):
        raise SkillSyncError(
            f"Skill is not configured for client: {client_id}",
            code="edit_recovery_client_excluded",
            exit_code=EXIT_SAFETY,
        )
    if target_client.family_id in _disabled_agents(config):
        raise SkillSyncError(
            f"client family is disabled: {target_client.family_id}",
            code="edit_recovery_client_disabled",
            exit_code=EXIT_SAFETY,
        )

    canonical = _local_skill_path_or_default(config, registry, skill_name).absolute()
    _validate_skill_path(canonical)
    canonical_hash = hash_skill_dir(canonical)
    rendered_root = _rendered_root(config)
    spec = _client_deployment_spec(
        canonical,
        rendered_root,
        skill_name,
        client_id,
        source_hash=canonical_hash,
    )
    expected = spec.path
    destination = target_client.skills_dir / skill_name
    if link_state(expected, destination) != "linked":
        raise SkillSyncError(
            "Agent path is not linked to the current canonical deployment",
            code="edit_recovery_not_tampered",
            exit_code=EXIT_CONFLICT,
            details={"client": client_id, "destination": str(destination)},
        )
    verification = verify_deployment(
        expected,
        expected_provenance=spec.expected_provenance,
    )
    if verification.state != "tampered":
        raise SkillSyncError(
            f"deployment is not tampered: {verification.state}",
            code="edit_recovery_not_tampered",
            exit_code=EXIT_CONFLICT,
            details={"client": client_id, "state": verification.state},
        )
    try:
        observed_before = hash_skill_dir(expected)
        canonical_tree = inspect_tree(canonical)
        expected_tree = canonical_tree
        if spec.layered_resolution is not None:
            expected_tree = TreeInspection(
                files={
                    entry.relative_path: FileRecord(
                        entry.relative_path,
                        entry.content,
                    )
                    for entry in spec.layered_resolution.overlay_plan.files
                },
                issues=(),
            )
        authored = inspect_authored_deployment(expected)
        if expected_tree.issues or authored.issues or authored.hash is None:
            raise EditTreeInspectionError("tampered deployment contains unsafe paths")
        if canonical_tree.hash != canonical_hash:
            raise EditTreeInspectionError("canonical Skill changed during recovery preview")
        diff = build_diff(expected_tree, authored)
        observed_hash = hash_skill_dir(expected)
        if observed_hash != observed_before:
            raise EditTreeInspectionError(
                "tampered deployment changed during recovery preview"
            )
        refreshed_spec = _client_deployment_spec(
            canonical,
            rendered_root,
            skill_name,
            client_id,
            source_hash=canonical_hash,
        )
        if refreshed_spec.resolution_hash != spec.resolution_hash:
            raise EditTreeInspectionError(
                "deployment sources changed during recovery preview"
            )
    except (EditTreeInspectionError, OSError, ValueError) as exc:
        raise SkillSyncError(
            f"tampered deployment cannot be inspected safely: {exc}",
            code="unsafe_tampered_deployment",
            exit_code=EXIT_SAFETY,
            details={"client": client_id, "deployment_path": str(expected)},
        ) from exc
    blocked = _unfinished_edit_session(
        store=EditSessionStore(_data_root(config)), skill_name=skill_name
    )
    return {
        "skill": skill_name,
        "client": client_id,
        "state": "tampered-render",
        "action": "preview",
        "canonical_path": str(canonical),
        "canonical_hash": canonical_hash,
        "deployment_path": str(expected),
        "resolution_hash": spec.resolution_hash,
        "resolver_version": spec.expected_provenance["resolver_version"],
        "applied_layers": spec.expected_provenance["applied_layers"],
        "destination": str(destination),
        "tampered_authored_hash": authored.hash,
        "observed_hash": observed_hash,
        "reason": verification.reason,
        "diff": diff,
        "allowed_actions": (
            ["capture", "discard"]
            if spec.layered_resolution is None
            else ["discard"]
        ),
        "blocked_by_session": blocked,
    }


def _unfinished_edit_session(
    *, store: EditSessionStore, skill_name: str
) -> dict[str, str] | None:
    for metadata in store.list_metadata():
        if (
            metadata.logical_skill.casefold() == skill_name.casefold()
            and metadata.status
            in {
                EditSessionStatus.ACTIVE,
                EditSessionStatus.APPLYING,
                EditSessionStatus.NEEDS_RECOVERY,
            }
        ):
            return {
                "session_id": metadata.session_id,
                "status": metadata.status.value,
            }
    return None


def _recovery_receipt(
    data_root: Path,
    preview: dict[str, Any],
    action: str,
    *,
    operation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path, PrivateJsonReceipt]:
    operation_id = str(uuid.uuid4()) if operation_id is None else operation_id
    receipt_path = data_root / "operations" / f"edit-recover-{operation_id}.json"
    receipt = {
        "schema_version": 1,
        "operation": "edit-recover",
        "operation_id": operation_id,
        "skill": preview["skill"],
        "client": preview["client"],
        "action": action,
        "status": "prepared",
        "phase": "prepared",
        "created_at": time.time(),
        "canonical_path": preview["canonical_path"],
        "canonical_hash": preview["canonical_hash"],
        "deployment_path": preview["deployment_path"],
        "observed_hash": preview["observed_hash"],
        "tampered_authored_hash": preview["tampered_authored_hash"],
        **(extra or {}),
    }
    writer = PrivateJsonReceipt.create(receipt_path, receipt)
    return receipt, receipt_path, writer


def _edit_recovery_capture(
    store: EditSessionStore, preview: dict[str, Any]
) -> dict[str, Any]:
    if not preview["diff"]["changed"]:
        raise SkillSyncError(
            "tampering affects only deployment metadata; use discard",
            code="edit_recovery_no_authored_changes",
            exit_code=EXIT_CONFLICT,
        )
    authored = inspect_authored_deployment(preview["deployment_path"])
    capture_issues = validate_workspace(authored, logical_skill=preview["skill"])
    if capture_issues:
        raise SkillSyncError(
            "tampered authored content is not a valid Base Skill workspace",
            code="unsafe_tampered_deployment",
            exit_code=EXIT_SAFETY,
            details={
                "client": preview["client"],
                "issues": [issue.to_dict() for issue in capture_issues],
            },
        )
    snapshot_root = prepare_private_directory(
        store.data_root / "recovery-snapshots" / preview["skill"]
    )
    operation_id = str(uuid.uuid4())
    snapshot = snapshot_root / operation_id
    receipt, receipt_path, writer = _recovery_receipt(
        store.data_root,
        preview,
        "capture",
        operation_id=operation_id,
        extra={"snapshot_path": str(snapshot)},
    )
    metadata: EditSessionMetadata | None = None
    published_session_id: str | None = None
    snapshot_owner: CapturedSnapshot | None = None
    committed = False
    cleanup_pending: list[str] = []
    try:
        receipt["status"] = "applying"
        receipt["phase"] = "snapshotting"
        writer.update(receipt)
        copy_authored_deployment(
            preview["deployment_path"],
            snapshot,
            expected_hash=preview["tampered_authored_hash"],
        )
        snapshot_owner = CapturedSnapshot.prepare(
            snapshot, expected_hash=preview["tampered_authored_hash"]
        )
        metadata, paths = store.begin(
            logical_skill=preview["skill"],
            source=preview["canonical_path"],
            baseline_hash=preview["canonical_hash"],
            actor=f"recovery:{preview['client']}",
            workspace_source=snapshot,
            workspace_hash=preview["tampered_authored_hash"],
        )
        receipt["session_id"] = metadata.session_id
        receipt["status"] = "completed"
        receipt["phase"] = "completed"
        receipt["completed_at"] = time.time()
        writer.update(receipt)
        committed = True
        try:
            snapshot_owner.finalize()
        except Exception as exc:
            cleanup_pending.append(
                str(
                    exc.recovery_path
                    if isinstance(exc, DeploymentQuarantineRecoveryRequired)
                    else snapshot
                )
            )
        if cleanup_pending:
            receipt["status"] = "cleanup-pending"
            receipt["phase"] = "cleanup-pending"
            receipt["cleanup_pending"] = cleanup_pending
            try:
                writer.update(receipt)
            except Exception:
                cleanup_pending.append(str(receipt_path))
        return {
            **{
                key: preview[key]
                for key in (
                    "skill",
                    "client",
                    "state",
                    "canonical_path",
                    "canonical_hash",
                    "deployment_path",
                    "tampered_authored_hash",
                )
            },
            "action": "capture",
            "status": "captured",
            "session_id": metadata.session_id,
            "workspace_path": str(paths.workspace.absolute()),
            "receipt_path": str(receipt_path),
            "cleanup_pending": cleanup_pending,
        }
    except ActiveEditSessionError as exc:
        receipt["status"] = "rolled-back"
        receipt["phase"] = "rolled-back"
        receipt["error_code"] = "active_edit_session"
        receipt["completed_at"] = time.time()
        writer.update(receipt)
        raise SkillSyncError(
            str(exc),
            code="active_edit_session",
            exit_code=EXIT_CONFLICT,
            details={"skill": exc.logical_skill, "session_id": exc.session_id},
        ) from exc
    except ReceiptRecoveryRequired as exc:
        raise SkillSyncError(
            "edit recovery receipt changed and requires reconciliation",
            code="edit_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "receipt_path": str(receipt_path),
                "session_id": None if metadata is None else metadata.session_id,
            },
        ) from exc
    except EditSessionPublicationRecoveryRequired as exc:
        published_session_id = exc.session_id
        receipt["status"] = "needs-recovery"
        receipt["phase"] = "needs-recovery"
        receipt["error_code"] = "edit_recovery_session_publication_ambiguous"
        receipt["session_id"] = exc.session_id
        receipt["recovery_path"] = str(store.paths(exc.session_id).root)
        receipt["completed_at"] = time.time()
        try:
            writer.update(receipt)
        except Exception:
            pass
        raise SkillSyncError(
            "captured edit session publication requires reconciliation",
            code="edit_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "receipt_path": str(receipt_path),
                "session_id": exc.session_id,
                "recovery_path": str(store.paths(exc.session_id).root),
            },
        ) from exc
    except Exception as exc:
        if committed:
            raise
        receipt["status"] = "needs-recovery" if metadata is not None else "rolled-back"
        receipt["phase"] = receipt["status"]
        receipt["error_code"] = "edit_recovery_failed"
        if metadata is not None:
            receipt["session_id"] = metadata.session_id
            receipt["recovery_path"] = str(store.paths(metadata.session_id).root)
        receipt["completed_at"] = time.time()
        receipt_update_failed = False
        try:
            writer.update(receipt)
        except Exception:
            receipt_update_failed = True
        if receipt_update_failed:
            raise SkillSyncError(
                "edit recovery receipt could not be terminalized",
                code="edit_recovery_required",
                exit_code=EXIT_SAFETY,
                details={"receipt_path": str(receipt_path)},
            ) from exc
        raise
    finally:
        if not committed and published_session_id is None and snapshot_owner is not None:
            try:
                snapshot_owner.finalize()
            except Exception:
                pass


def _edit_recovery_discard(
    config: dict[str, Any], preview: dict[str, Any]
) -> dict[str, Any]:
    data_root = _data_root(config)
    operation_id = str(uuid.uuid4())
    canonical = Path(preview["canonical_path"])
    rendered_root = _rendered_root(config)
    spec = _client_deployment_spec(
        canonical,
        rendered_root,
        preview["skill"],
        preview["client"],
        source_hash=preview["canonical_hash"],
    )
    if (
        str(spec.path) != preview["deployment_path"]
        or spec.resolution_hash != preview["resolution_hash"]
    ):
        raise SkillSyncError(
            "deployment sources changed before recovery",
            code="edit_recovery_conflict",
            exit_code=EXIT_CONFLICT,
        )
    quarantine = DeploymentQuarantine.prepare(
        preview["deployment_path"],
        expected_hash=preview["observed_hash"],
        token=operation_id,
    )
    receipt, receipt_path, writer = _recovery_receipt(
        data_root,
        preview,
        "discard",
        operation_id=operation_id,
        extra={"quarantine_path": str(quarantine.quarantine)},
    )
    committed = False
    cleanup_pending: list[str] = []
    try:
        receipt["status"] = "applying"
        receipt["phase"] = "quarantining"
        writer.update(receipt)
        quarantine.apply()
        receipt["phase"] = "rebuilding"
        writer.update(receipt)
        spec = _client_deployment_spec(
            canonical,
            rendered_root,
            preview["skill"],
            preview["client"],
            source_hash=preview["canonical_hash"],
        )
        if spec.resolution_hash != preview["resolution_hash"]:
            raise FileExistsError("deployment sources changed during recovery")
        deployed = _render_client_deployment(
            spec,
            canonical,
            rendered_root,
            preview["skill"],
            preview["client"],
        )
        verification = verify_deployment(
            deployed.path,
            expected_provenance=spec.expected_provenance,
        )
        if not verification.ok or link_state(
            deployed.path, Path(preview["destination"])
        ) != "linked":
            raise OSError("rebuilt deployment or Agent link failed verification")
        receipt["status"] = "completed"
        receipt["phase"] = "completed"
        receipt["completed_at"] = time.time()
        writer.update(receipt)
        committed = True
        try:
            quarantine.finalize()
        except Exception as exc:
            cleanup_pending.append(
                str(
                    exc.recovery_path
                    if isinstance(exc, DeploymentQuarantineRecoveryRequired)
                    else quarantine.quarantine
                )
            )
        if cleanup_pending:
            receipt["status"] = "cleanup-pending"
            receipt["phase"] = "cleanup-pending"
            receipt["cleanup_pending"] = cleanup_pending
            try:
                writer.update(receipt)
            except Exception:
                cleanup_pending.append(str(receipt_path))
        return {
            "skill": preview["skill"],
            "client": preview["client"],
            "state": "valid",
            "action": "discard",
            "status": "discarded",
            "canonical_hash": preview["canonical_hash"],
            "deployment_path": str(deployed.path),
            "receipt_path": str(receipt_path),
            "cleanup_pending": cleanup_pending,
        }
    except Exception as exc:
        if committed:
            raise
        receipt_changed = isinstance(exc, ReceiptRecoveryRequired)
        rolled_back = False
        try:
            rolled_back = quarantine.rollback()
        except Exception:
            rolled_back = False
        receipt["status"] = "rolled-back" if rolled_back else "needs-recovery"
        receipt["phase"] = receipt["status"]
        receipt["error_code"] = "edit_recovery_failed"
        receipt["completed_at"] = time.time()
        if not rolled_back:
            receipt["recovery_path"] = str(quarantine.quarantine)
        if not receipt_changed:
            try:
                writer.update(receipt)
            except Exception:
                receipt_changed = True
        if not rolled_back or receipt_changed:
            raise SkillSyncError(
                "discard recovery requires manual reconciliation",
                code="edit_recovery_required",
                exit_code=EXIT_SAFETY,
                details={
                    "receipt_path": str(receipt_path),
                    "recovery_path": (
                        str(receipt_path)
                        if receipt_changed
                        else str(quarantine.quarantine)
                    ),
                },
            ) from exc
        raise SkillSyncError(
            "discard recovery failed; the tampered deployment was restored",
            code="edit_recovery_failed",
            exit_code=EXIT_SAFETY,
            details={"receipt_path": str(receipt_path)},
        ) from exc


def _require_base_edit_operation(
    metadata: EditSessionMetadata,
    operation: str,
) -> None:
    if metadata.target_scope.kind is EditSessionScopeKind.BASE:
        return
    raise SkillSyncError(
        (
            f"edit {operation} does not support "
            f"{metadata.target_scope.kind.value} scoped sessions yet"
        ),
        code="scoped_edit_operation_unsupported",
        exit_code=EXIT_SAFETY,
        details={
            "session_id": metadata.session_id,
            "scope": metadata.target_scope.kind.value,
            "target": metadata.target_scope.target,
            "required_capability": (
                "scoped-edit-apply"
                if operation == "apply"
                else "scoped-edit-inspection"
            ),
        },
    )


def _active_edit_trees(
    session_id: str,
    config_path: str | Path | None,
    *,
    operation: str,
) -> tuple[EditSessionMetadata, TreeInspection, TreeInspection]:
    config = _load_local_config(config_path)
    store = EditSessionStore(_data_root(config))
    try:
        metadata = store.load(session_id)
    except FileNotFoundError as exc:
        raise SkillSyncError(
            f"edit session does not exist: {session_id}",
            code="edit_session_not_found",
            details={"session_id": session_id},
        ) from exc
    except (EditSessionMetadataError, OSError) as exc:
        raise SkillSyncError(
            f"could not safely inspect edit session: {exc}",
            code="invalid_edit_session_metadata",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc

    if metadata.status is not EditSessionStatus.ACTIVE:
        raise SkillSyncError(
            f"edit session is not active: {session_id} ({metadata.status.value})",
            code="edit_session_not_active",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id, "status": metadata.status.value},
        )
    paths = store.paths(session_id)
    for label, path in (("baseline", paths.baseline), ("workspace", paths.workspace)):
        if is_link_or_reparse(path) or not path.is_dir():
            raise SkillSyncError(
                f"edit session {label} is missing or unsafe: {path}",
                code="edit_session_incomplete",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id, "component": label},
            )
    try:
        baseline = inspect_tree(paths.baseline)
        workspace = inspect_tree(paths.workspace)
    except (EditTreeInspectionError, OSError) as exc:
        raise SkillSyncError(
            f"could not safely inspect edit session trees: {exc}",
            code="edit_session_incomplete",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc

    if baseline.issues or baseline.hash != metadata.baseline_hash:
        raise SkillSyncError(
            "edit session baseline is damaged or does not match its recorded hash",
            code="unsafe_edit_baseline",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        )
    return metadata, baseline, workspace


def _scoped_edit_clients(metadata: EditSessionMetadata) -> tuple[str, ...]:
    target = metadata.target_scope.target
    if target is None:
        return ()
    if metadata.target_scope.kind is EditSessionScopeKind.FAMILY:
        return next(
            family.client_ids for family in AGENT_FAMILIES if family.id == target
        )
    return (target,)


def _scoped_current_layer(
    metadata: EditSessionMetadata,
    *,
    config_path: str | Path | None,
) -> dict[str, Any]:
    target = metadata.target_scope.target
    if target is None:
        raise EditSessionMetadataError("scoped edit session target is missing")
    sources = variant_source.resolve_variant_source_paths(
        metadata.logical_skill,
        config_path=config_path,
    )
    matches = [
        observation
        for observation in sources.observations
        if observation.role.startswith("variant-target:")
        and observation.path.name == target
    ]
    if not matches:
        variant_source.verify_variant_source_paths(sources)
        return EditLayerBaseline.absent().to_dict()
    if len(matches) != 1:
        raise SkillSyncError(
            "Variant target is ambiguous while inspecting edit baseline",
            code="variant_target_ambiguous",
            exit_code=EXIT_SAFETY,
            details={"skill": metadata.logical_skill, "target": target},
        )
    layer = _capture_scoped_layer_snapshot(
        sources=sources,
        layer_path=matches[0].path,
        skill_name=metadata.logical_skill,
        target=target,
    )
    variant_source.verify_variant_source_paths(sources)
    layer_hash = hash_skill_files(
        (entry.relative_path, entry.content) for entry in layer.files
    )
    return EditLayerBaseline.present(layer_hash).to_dict()


def _resolution_tree(resolution: LayeredVariantResolution) -> TreeInspection:
    return TreeInspection(
        files={
            entry.relative_path: FileRecord(entry.relative_path, entry.content)
            for entry in resolution.overlay_plan.files
        },
        issues=(),
    )


def _scoped_resolutions(
    metadata: EditSessionMetadata,
    *,
    workspace_path: Path,
    client: str,
    config_path: str | Path | None,
) -> tuple[LayeredVariantResolution, LayeredVariantResolution]:
    target = metadata.target_scope.target
    if target is None:
        raise EditSessionMetadataError("scoped edit session target is missing")
    sources = variant_source.resolve_variant_source_paths(
        metadata.logical_skill,
        config_path=config_path,
    )
    variant_source.verify_variant_source_paths(sources)
    try:
        current = resolve_variant_for_client(
            sources.base_root,
            sources.variant_skill_root,
            client,
        )
        proposed = resolve_variant_for_client(
            sources.base_root,
            sources.variant_skill_root,
            client,
            override_target=target,
            override_root=workspace_path,
        )
    except (OSError, ValueError) as exc:
        variant_source.verify_variant_source_paths(sources)
        raise SkillSyncError(
            f"scoped Variant resolution is invalid: {exc}",
            code="variant_resolution_invalid",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": metadata.session_id,
                "skill": metadata.logical_skill,
                "client": client,
                "target": target,
            },
        ) from exc
    variant_source.verify_variant_source_paths(sources, resolution=current)
    variant_source.verify_variant_source_paths(sources)
    return current, proposed


def _scoped_resolved_diff(
    metadata: EditSessionMetadata,
    *,
    workspace_path: Path,
    client: str,
    config_path: str | Path | None,
) -> dict[str, Any]:
    current, proposed = _scoped_resolutions(
        metadata,
        workspace_path=workspace_path,
        client=client,
        config_path=config_path,
    )
    diff = build_diff(_resolution_tree(current), _resolution_tree(proposed))
    return {
        "client": client,
        "family": current.family,
        "current_resolution_hash": current.resolution_hash,
        "proposed_resolution_hash": proposed.resolution_hash,
        "current_output_hash": current.output_hash,
        "proposed_output_hash": proposed.output_hash,
        **diff,
    }


def _validate_scoped_workspace(
    metadata: EditSessionMetadata,
    workspace: TreeInspection,
    *,
    workspace_path: Path,
    config_path: str | Path | None,
) -> list[TreeIssue]:
    issues = list(workspace.issues)
    if VARIANT_MANIFEST_FILE not in workspace.files:
        issues.append(
            TreeIssue(
                "missing_variant_manifest",
                VARIANT_MANIFEST_FILE,
                f"Variant workspace must contain {VARIANT_MANIFEST_FILE}",
            )
        )
    if issues:
        return sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))
    try:
        for client in _scoped_edit_clients(metadata):
            _scoped_resolutions(
                metadata,
                workspace_path=workspace_path,
                client=client,
                config_path=config_path,
            )
    except SkillSyncError as exc:
        issues.append(
            TreeIssue(
                "invalid_variant_overlay",
                VARIANT_MANIFEST_FILE,
                str(exc),
            )
        )
    return sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))


def edit_impact(
    session_id: str,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Preview Base deployment impact without changing any local state."""

    config = _load_local_config(config_path)
    store = EditSessionStore(_data_root(config))
    try:
        metadata = store.load(session_id)
    except FileNotFoundError as exc:
        session_root = store.paths(session_id).root
        if session_root.exists() or is_link_or_reparse(session_root):
            raise SkillSyncError(
                f"edit session metadata is missing or incomplete: {session_id}",
                code="unsafe_edit_session",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id},
            ) from exc
        raise SkillSyncError(
            f"edit session does not exist: {session_id}",
            code="edit_session_not_found",
            details={"session_id": session_id},
        ) from exc
    except (EditSessionMetadataError, OSError) as exc:
        raise SkillSyncError(
            f"could not safely load edit session: {exc}",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc

    try:
        if metadata.status is not EditSessionStatus.ACTIVE:
            raise SkillSyncError(
                f"edit session is not active: {session_id} ({metadata.status.value})",
                code="edit_session_not_active",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id, "status": metadata.status.value},
            )
        if metadata.target_scope.kind is not EditSessionScopeKind.BASE:
            return _scoped_edit_impact(
                metadata,
                config=config,
                config_path=config_path,
            )
        _require_base_edit_operation(metadata, "impact")
        registry = _load_local_registry(config)
        _target_names(registry, [metadata.logical_skill])
        source = _local_skill_path_or_default(
            config, registry, metadata.logical_skill
        ).absolute()
        _validate_skill_path(source)
        paths = store.paths(session_id)
        try:
            baseline_snapshot_hash = hash_skill_dir(paths.baseline)
        except (OSError, ValueError) as exc:
            raise SkillSyncError(
                f"edit session baseline is unsafe or incomplete: {exc}",
                code="unsafe_edit_baseline",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id},
            ) from exc
        if baseline_snapshot_hash != metadata.baseline_hash:
            raise SkillSyncError(
                "edit session baseline no longer matches recorded metadata",
                code="unsafe_edit_baseline",
                exit_code=EXIT_SAFETY,
                details={
                    "session_id": session_id,
                    "expected_hash": metadata.baseline_hash,
                    "actual_hash": baseline_snapshot_hash,
                },
            )
        try:
            workspace_hash = hash_skill_dir(paths.workspace)
        except (OSError, ValueError) as exc:
            raise SkillSyncError(
                f"edit session workspace is unsafe or incomplete: {exc}",
                code="edit_session_incomplete",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id},
            ) from exc
        current_hash = hash_skill_dir(source)
        stale_baseline = current_hash != metadata.baseline_hash
        disabled = _disabled_agents(config)
        clients = detect_clients()
        entry = registry.get("skills", {}).get(metadata.logical_skill, {})
        configured = _configured_client_targets(entry)
        rendered_root = _rendered_root(config)
        variant_skill_root = source.parent.parent / "variants" / metadata.logical_skill
        rows: list[dict[str, Any]] = []

        for client in clients:
            if configured and not {client.id, client.family_id}.intersection(configured):
                continue
            enabled = client.family_id not in disabled
            current_spec = _client_deployment_spec(
                source,
                rendered_root,
                metadata.logical_skill,
                client.id,
                source_hash=current_hash,
                variant_skill_root=variant_skill_root,
            )
            proposed_spec = _client_deployment_spec(
                paths.workspace,
                rendered_root,
                metadata.logical_skill,
                client.id,
                source_hash=workspace_hash,
                variant_skill_root=variant_skill_root,
            )
            current_resolution = current_spec.resolution_hash
            proposed_resolution = proposed_spec.resolution_hash
            current_deployment = current_spec.path
            proposed_deployment = proposed_spec.path
            current_verification = verify_deployment(
                current_deployment,
                expected_provenance=current_spec.expected_provenance,
            )
            proposed_verification = verify_deployment(
                proposed_deployment,
                expected_provenance=proposed_spec.expected_provenance,
            )
            destination = client.skills_dir / metadata.logical_skill
            current_link_state, current_target = _deployment_link_state(
                source,
                destination,
                current_deployment,
                rendered_root,
                metadata.logical_skill,
                client.id,
            )
            proposed_link_state, proposed_target = _deployment_link_state(
                source,
                destination,
                proposed_deployment,
                rendered_root,
                metadata.logical_skill,
                client.id,
            )
            planned_action = _deployment_action(
                proposed_link_state, proposed_verification.state
            )
            deployment_would_change = current_resolution != proposed_resolution
            requires_rebuild = proposed_verification.state != "valid"
            affected = deployment_would_change or planned_action != "noop"
            if not enabled:
                availability = "disabled"
                action = "disabled"
            elif not client.detected:
                availability = "undetected"
                action = "undetected"
            elif stale_baseline:
                availability = "available"
                action = "blocked"
            elif planned_action == "blocked":
                availability = "available"
                action = "blocked"
            elif requires_rebuild:
                availability = "available"
                action = "rebuild"
            elif planned_action != "noop":
                availability = "available"
                action = "relink"
            else:
                availability = "available"
                action = "noop"

            rows.append(
                {
                    "client": client.id,
                    "agent": client.family_id,
                    "display_name": client.display_name,
                    "detected": client.detected,
                    "enabled": enabled,
                    "availability": availability,
                    "destination": str(destination),
                    "current_resolution_hash": current_resolution,
                    "current_applied_layers": current_spec.expected_provenance[
                        "applied_layers"
                    ],
                    "current_deployment_path": str(current_deployment),
                    "current_deployment_state": current_verification.state,
                    "current_link_state": current_link_state,
                    "current_target": (
                        None if current_target is None else str(current_target)
                    ),
                    "proposed_resolution_hash": proposed_resolution,
                    "proposed_applied_layers": proposed_spec.expected_provenance[
                        "applied_layers"
                    ],
                    "proposed_deployment_path": str(proposed_deployment),
                    "proposed_deployment_state": proposed_verification.state,
                    "proposed_link_state": proposed_link_state,
                    "proposed_target": (
                        None if proposed_target is None else str(proposed_target)
                    ),
                    "deployment_would_change": deployment_would_change,
                    "requires_rebuild": requires_rebuild,
                    "affected": affected,
                    "hypothetical_action": planned_action,
                    "action": action,
                    "blocked_reason": (
                        "stale-baseline"
                        if stale_baseline and enabled and client.detected
                        else None
                    ),
                }
            )

        families: list[dict[str, Any]] = []
        for agent in aggregate_agent_targets(clients):
            members = [row for row in rows if row["agent"] == agent.name]
            if not members:
                continue
            families.append(
                {
                    "agent": agent.name,
                    "display_name": agent.display_name,
                    "detected": agent.detected,
                    "enabled": agent.name not in disabled,
                    "clients": [row["client"] for row in members],
                    "affected_clients": [
                        row["client"] for row in members if row["affected"]
                    ],
                }
            )

        return {
            "session_id": metadata.session_id,
            "skill": metadata.logical_skill,
            "scope": "base",
            "status": metadata.status.value,
            "baseline_hash": metadata.baseline_hash,
            "current_hash": current_hash,
            "workspace_hash": workspace_hash,
            "stale_baseline": stale_baseline,
            "blocked": stale_baseline,
            "blocked_reason": (
                "canonical-changed-since-begin" if stale_baseline else None
            ),
            "has_workspace_changes": workspace_hash != metadata.baseline_hash,
            "registry_targets": sorted(configured),
            "families": families,
            "clients": rows,
            "summary": {
                "affected": sum(bool(row["affected"]) for row in rows),
                "requires_rebuild": sum(bool(row["requires_rebuild"]) for row in rows),
                "disabled": sum(row["availability"] == "disabled" for row in rows),
                "undetected": sum(row["availability"] == "undetected" for row in rows),
            },
        }
    except SkillSyncError:
        raise
    except (EditSessionMetadataError, OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not safely preview edit impact: {exc}",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc


def _scoped_edit_impact(
    metadata: EditSessionMetadata,
    *,
    config: dict[str, Any],
    config_path: str | Path | None,
) -> dict[str, Any]:
    _, baseline, workspace = _active_edit_trees(
        metadata.session_id,
        config_path,
        operation="impact",
    )
    store = EditSessionStore(_data_root(config))
    workspace_path = store.paths(metadata.session_id).workspace
    issues = _validate_scoped_workspace(
        metadata,
        workspace,
        workspace_path=workspace_path,
        config_path=config_path,
    )
    if issues or workspace.hash is None:
        raise SkillSyncError(
            "scoped edit workspace failed validation",
            code="invalid_edit_workspace",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": metadata.session_id,
                "issues": [issue.to_dict() for issue in issues],
            },
        )

    registry = _load_local_registry(config)
    _target_names(registry, [metadata.logical_skill])
    current_layer = _scoped_current_layer(metadata, config_path=config_path)
    stale_baseline = current_layer != metadata.layer_baseline.to_dict()
    scope_clients = set(_scoped_edit_clients(metadata))
    disabled = _disabled_agents(config)
    clients = detect_clients()
    entry = registry.get("skills", {}).get(metadata.logical_skill, {})
    configured = _configured_client_targets(entry)
    rendered_root = _rendered_root(config)
    rows: list[dict[str, Any]] = []

    for client in clients:
        if configured and not {client.id, client.family_id}.intersection(configured):
            continue
        sources = variant_source.resolve_variant_source_paths(
            metadata.logical_skill,
            config_path=config_path,
        )
        try:
            current = resolve_variant_for_client(
                sources.base_root,
                sources.variant_skill_root,
                client.id,
            )
        except (OSError, ValueError) as exc:
            raise SkillSyncError(
                f"current Variant resolution is invalid: {exc}",
                code="variant_resolution_invalid",
                exit_code=EXIT_SAFETY,
                details={"skill": metadata.logical_skill, "client": client.id},
            ) from exc
        variant_source.verify_variant_source_paths(sources, resolution=current)
        scope_affected = client.id in scope_clients
        proposed = current
        if scope_affected:
            _, proposed = _scoped_resolutions(
                metadata,
                workspace_path=workspace_path,
                client=client.id,
                config_path=config_path,
            )
        deployment_would_change = (
            current.resolution_hash != proposed.resolution_hash
        )
        current_deployment = deployment_path(
            rendered_root,
            metadata.logical_skill,
            current.resolution_hash,
        )
        proposed_deployment = deployment_path(
            rendered_root,
            metadata.logical_skill,
            proposed.resolution_hash,
        )
        current_state = verify_deployment(current_deployment).state
        proposed_state = verify_deployment(proposed_deployment).state
        enabled = client.family_id not in disabled
        affected = scope_affected and deployment_would_change
        requires_rebuild = affected and proposed_state != "valid"
        if not enabled:
            availability, action = "disabled", "disabled"
        elif not client.detected:
            availability, action = "undetected", "undetected"
        elif stale_baseline and scope_affected:
            availability, action = "available", "blocked"
        elif requires_rebuild:
            availability, action = "available", "rebuild"
        elif affected:
            availability, action = "available", "relink"
        else:
            availability, action = "available", "noop"
        rows.append(
            {
                "client": client.id,
                "agent": client.family_id,
                "display_name": client.display_name,
                "detected": client.detected,
                "enabled": enabled,
                "availability": availability,
                "scope_affected": scope_affected,
                "current_resolution_hash": current.resolution_hash,
                "proposed_resolution_hash": proposed.resolution_hash,
                "current_output_hash": current.output_hash,
                "proposed_output_hash": proposed.output_hash,
                "current_deployment_path": str(current_deployment),
                "proposed_deployment_path": str(proposed_deployment),
                "current_deployment_state": current_state,
                "proposed_deployment_state": proposed_state,
                "deployment_would_change": deployment_would_change,
                "requires_rebuild": requires_rebuild,
                "affected": affected,
                "hypothetical_action": "rebuild" if requires_rebuild else "noop",
                "action": action,
                "blocked_reason": (
                    "stale-baseline"
                    if stale_baseline
                    and scope_affected
                    and enabled
                    and client.detected
                    else None
                ),
            }
        )

    families = []
    for agent in aggregate_agent_targets(clients):
        members = [row for row in rows if row["agent"] == agent.name]
        if members:
            families.append(
                {
                    "agent": agent.name,
                    "display_name": agent.display_name,
                    "detected": agent.detected,
                    "enabled": agent.name not in disabled,
                    "clients": [row["client"] for row in members],
                    "affected_clients": [
                        row["client"] for row in members if row["affected"]
                    ],
                }
            )

    return {
        "session_id": metadata.session_id,
        "skill": metadata.logical_skill,
        "scope": metadata.target_scope.kind.value,
        "target": metadata.target_scope.target,
        "status": metadata.status.value,
        "baseline_hash": metadata.baseline_hash,
        "current_hash": current_layer["hash"],
        "workspace_hash": workspace.hash,
        "layer_baseline": metadata.layer_baseline.to_dict(),
        "current_layer": current_layer,
        "stale_baseline": stale_baseline,
        "blocked": stale_baseline,
        "blocked_reason": (
            "canonical-layer-changed-since-begin" if stale_baseline else None
        ),
        "has_workspace_changes": workspace.hash != baseline.hash,
        "registry_targets": sorted(configured),
        "families": families,
        "clients": rows,
        "summary": {
            "affected": sum(bool(row["affected"]) for row in rows),
            "requires_rebuild": sum(bool(row["requires_rebuild"]) for row in rows),
            "disabled": sum(row["availability"] == "disabled" for row in rows),
            "undetected": sum(row["availability"] == "undetected" for row in rows),
        },
    }


def edit_apply(
    session_id: str,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Transactionally replace a canonical Base Skill without rebuilding clients."""

    config = _load_local_config(config_path)
    data_root = _data_root(config)
    store = EditSessionStore(data_root)
    try:
        store.load(session_id)
    except FileNotFoundError as exc:
        raise SkillSyncError(
            f"edit session does not exist: {session_id}",
            code="edit_session_not_found",
            details={"session_id": session_id},
        ) from exc
    except (EditSessionMetadataError, OSError) as exc:
        raise SkillSyncError(
            f"could not safely load edit session: {exc}",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc

    try:
        with local_file_lock(data_root / "locks" / "deployment.lock"):
            lock_metadata = store.load(session_id)
            with store.skill_lock(lock_metadata.logical_skill):
                current = store.load(session_id)
                if current.logical_skill != lock_metadata.logical_skill:
                    raise SkillSyncError(
                        "edit session identity changed while acquiring its lock",
                        code="unsafe_edit_session",
                        exit_code=EXIT_SAFETY,
                        details={"session_id": session_id},
                    )
                return _edit_apply_locked(
                    config,
                    store,
                    session_id,
                    expected_skill=lock_metadata.logical_skill,
                    config_path=config_path,
                )
    except SkillSyncError:
        raise
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="edit_apply_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc
    except (EditSessionMetadataError, OSError, ValueError) as exc:
        raise SkillSyncError(
            f"could not safely apply edit session: {exc}",
            code="edit_apply_failed",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc


def _edit_apply_locked(
    config: dict[str, Any],
    store: EditSessionStore,
    session_id: str,
    *,
    expected_skill: str,
    config_path: str | Path | None,
) -> dict[str, Any]:
    metadata = store.load(session_id)
    if metadata.logical_skill != expected_skill:
        raise SkillSyncError(
            "edit session identity does not match the held Skill lock",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        )
    if metadata.status is not EditSessionStatus.ACTIVE:
        raise SkillSyncError(
            f"edit session is not active: {session_id} ({metadata.status.value})",
            code="edit_session_not_active",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id, "status": metadata.status.value},
        )
    if metadata.target_scope.kind is not EditSessionScopeKind.BASE:
        return _edit_apply_scoped_locked(
            config,
            store,
            metadata,
            config_path=config_path,
        )
    _require_base_edit_operation(metadata, "apply")

    registry = _load_local_registry(config)
    _target_names(registry, [metadata.logical_skill])
    source = _local_skill_path_or_default(
        config, registry, metadata.logical_skill
    ).absolute()
    _validate_skill_path(source)
    paths = store.paths(session_id)
    try:
        baseline = inspect_tree(paths.baseline)
        workspace = inspect_tree(paths.workspace)
    except (EditTreeInspectionError, OSError) as exc:
        raise SkillSyncError(
            f"edit session trees are unsafe or incomplete: {exc}",
            code="edit_session_incomplete",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc
    if baseline.issues or baseline.hash != metadata.baseline_hash:
        raise SkillSyncError(
            "edit session baseline is damaged or does not match its recorded hash",
            code="unsafe_edit_baseline",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        )
    issues = validate_workspace(workspace, logical_skill=metadata.logical_skill)
    if issues or workspace.hash is None:
        raise SkillSyncError(
            "edit workspace failed validation",
            code="invalid_edit_workspace",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "issues": [issue.to_dict() for issue in issues],
            },
        )
    workspace_hash = workspace.hash
    if workspace_hash == metadata.baseline_hash:
        raise SkillSyncError(
            "edit workspace has no changes to apply",
            code="edit_workspace_unchanged",
            exit_code=EXIT_CONFLICT,
            details={"session_id": session_id},
        )
    current_hash = hash_skill_dir(source)
    if current_hash != metadata.baseline_hash:
        raise SkillSyncError(
            "canonical Skill changed since this edit session began",
            code="edit_baseline_conflict",
            exit_code=EXIT_CONFLICT,
            details={
                "session_id": session_id,
                "expected_hash": metadata.baseline_hash,
                "actual_hash": current_hash,
            },
        )

    data_root_path = store.data_root
    receipt_path = data_root_path / "operations" / f"edit-apply-{session_id}.json"
    if receipt_path.exists() or is_link_or_reparse(receipt_path):
        raise SkillSyncError(
            "an edit apply receipt already exists for this session",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id, "receipt_path": str(receipt_path)},
        )
    backup_parent = prepare_private_directory(
        data_root_path / "backups" / "edit-apply" / metadata.logical_skill
    )
    backup_path = backup_parent / f"{time.time_ns()}-{session_id}"
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "operation": "edit-apply",
        "operation_id": session_id,
        "session_id": session_id,
        "skill": metadata.logical_skill,
        "scope": "base",
        "status": "prepared",
        "phase": "backup-pending",
        "created_at": time.time(),
        "canonical_path": str(source),
        "backup_path": str(backup_path),
        "baseline_hash": metadata.baseline_hash,
        "workspace_hash": workspace_hash,
        "deployments_rebuilt": False,
    }
    receipt_writer = PrivateJsonReceipt.create(receipt_path, receipt)
    try:
        backup_hash = copy_skill_dir(source, backup_path)
        if backup_hash != metadata.baseline_hash:
            raise OSError("canonical backup does not match the edit baseline")
        fsync_tree(backup_path)
        receipt["phase"] = "backup-ready"
        receipt_writer.update(receipt)
    except (OSError, ValueError) as exc:
        receipt["status"] = "rolled-back"
        receipt["phase"] = "backup-failed"
        receipt["error_code"] = "edit_backup_failed"
        receipt["completed_at"] = time.time()
        receipt_writer.update(receipt)
        raise SkillSyncError(
            "could not create a durable canonical backup",
            code="edit_backup_failed",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
            },
        ) from exc

    swap: CanonicalSwap | None = None
    deployment_transaction: dict[str, Any] | None = None
    applied_link_swaps: list[DirectoryLinkSwap] = []
    cleanup_pending: list[str] = []
    applying = False
    applied_metadata = False
    try:
        swap = CanonicalSwap.prepare(
            source,
            paths.workspace,
            expected_old_hash=metadata.baseline_hash,
            expected_new_hash=workspace_hash,
            token=session_id,
        )
        _transition_edit_metadata(
            store,
            session_id,
            expected=EditSessionStatus.ACTIVE,
            target=EditSessionStatus.APPLYING,
        )
        applying = True
        _assert_no_incomplete_deployment_receipts(store.data_root)
        receipt["phase"] = "deployments-preparing"
        receipt_writer.update(receipt)
        deployment_transaction = _prepare_edit_deployments(
            config,
            registry,
            metadata.logical_skill,
            source,
            paths.workspace,
            workspace_hash,
            session_id,
        )
        receipt["clients"] = deployment_transaction["clients"]
        receipt["phase"] = "deployments-ready"
        receipt_writer.update(receipt)
        _verify_base_edit_deployments(
            metadata.logical_skill,
            source,
            paths.workspace,
            deployment_transaction,
            proposed=True,
        )
        receipt["status"] = "applying"
        receipt["phase"] = "canonical-replace"
        receipt_writer.update(receipt)

        swap.apply()
        receipt["phase"] = "canonical-applied"
        receipt_writer.update(receipt)
        receipt["phase"] = "links-swapping"
        receipt["completed_clients"] = []
        receipt_writer.update(receipt)
        for link_swap in deployment_transaction["swaps"]:
            link_swap.apply()
            applied_link_swaps.append(link_swap)
            receipt["completed_clients"].append(
                deployment_transaction["swap_clients"][link_swap.destination]
            )
            receipt_writer.update(receipt)
        if hash_skill_dir(source) != workspace_hash:
            raise SkillSyncError(
                "canonical Skill changed during deployment link swaps",
                code="edit_canonical_changed_during_deploy",
                exit_code=EXIT_CONFLICT,
            )
        _verify_base_edit_deployments(
            metadata.logical_skill,
            source,
            paths.workspace,
            deployment_transaction,
            proposed=False,
        )
        for row in deployment_transaction["active"]:
            if link_state(Path(row["deployment_path"]), Path(row["destination"])) != "linked":
                raise SkillSyncError(
                    "an applied deployment link failed final verification",
                    code="edit_deployment_verification_failed",
                    exit_code=EXIT_SAFETY,
                    details={"client": row["client"]},
                )
        receipt["phase"] = "links-applied"
        receipt_writer.update(receipt)
        _transition_edit_metadata(
            store,
            session_id,
            expected=EditSessionStatus.APPLYING,
            target=EditSessionStatus.APPLIED,
        )
        applied_metadata = True
        receipt["status"] = "completed"
        receipt["phase"] = "completed"
        receipt["completed_at"] = time.time()
        receipt_writer.update(receipt)
        for link_swap in applied_link_swaps:
            try:
                link_swap.finalize()
            except Exception as exc:
                cleanup_pending.append(
                    str(
                        exc.recovery_path
                        if isinstance(exc, DirectoryLinkSwapRecoveryRequired)
                        else link_swap.backup
                    )
                )
        try:
            swap.finalize()
        except Exception as exc:
            cleanup_pending.append(
                str(
                    exc.recovery_path
                    if isinstance(exc, CanonicalSwapRecoveryRequired)
                    else swap.previous
                )
            )
        if cleanup_pending:
            receipt["status"] = "cleanup-pending"
            receipt["phase"] = "cleanup-pending"
            receipt["cleanup_pending"] = cleanup_pending
            try:
                receipt_writer.update(receipt)
            except Exception:
                cleanup_pending.append(str(receipt_path))
    except DirectoryLinkSwapRecoveryRequired as exc:
        _mark_edit_apply_recovery(
            store,
            session_id,
            receipt,
            receipt_path,
            receipt_writer,
            recovery_path=exc.recovery_path,
        )
        raise SkillSyncError(
            "deployment link swap requires recovery",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(exc.recovery_path),
            },
        ) from exc
    except ReceiptRecoveryRequired as exc:
        recovery_path = (
            swap.previous
            if swap is not None and swap.previous_moved
            else backup_path
        )
        try:
            current = store.load(session_id)
            if current.status is EditSessionStatus.APPLYING:
                _transition_edit_metadata(
                    store,
                    session_id,
                    expected=EditSessionStatus.APPLYING,
                    target=EditSessionStatus.NEEDS_RECOVERY,
                )
        except (EditSessionMetadataError, OSError, _EditTransitionRecoveryRequired):
            pass
        raise SkillSyncError(
            "edit apply receipt changed and requires recovery",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(recovery_path),
            },
        ) from exc
    except CanonicalSwapRecoveryRequired as exc:
        _mark_edit_apply_recovery(
            store,
            session_id,
            receipt,
            receipt_path,
            receipt_writer,
            recovery_path=exc.recovery_path,
        )
        raise SkillSyncError(
            "edit apply could not safely restore the previous canonical Skill",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(exc.recovery_path),
            },
        ) from exc
    except _EditTransitionRecoveryRequired as exc:
        recovery_path = (
            swap.previous
            if swap is not None and swap.previous_moved
            else backup_path
        )
        _mark_edit_apply_recovery(
            store,
            session_id,
            receipt,
            receipt_path,
            receipt_writer,
            recovery_path=recovery_path,
        )
        raise SkillSyncError(
            "edit session metadata durability is ambiguous and requires recovery",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(recovery_path),
            },
        ) from exc
    except Exception as exc:
        if applied_metadata:
            recovery_path = (
                swap.previous
                if swap is not None and swap.previous_moved
                else backup_path
            )
            _mark_edit_apply_recovery(
                store,
                session_id,
                receipt,
                receipt_path,
                receipt_writer,
                recovery_path=recovery_path,
            )
            raise SkillSyncError(
                "canonical Skill was applied but final receipt cleanup needs recovery",
                code="edit_apply_recovery_required",
                exit_code=EXIT_SAFETY,
                details={
                    "session_id": session_id,
                    "receipt_path": str(receipt_path),
                },
            ) from exc
        links_rolled_back = True
        for link_swap in reversed(applied_link_swaps):
            try:
                if not link_swap.rollback():
                    links_rolled_back = False
            except Exception:
                links_rolled_back = False
        try:
            rolled_back = links_rolled_back and (swap is None or swap.rollback())
        except Exception:
            rolled_back = False
        if rolled_back and swap is not None:
            for link_swap in applied_link_swaps:
                try:
                    link_swap.finalize()
                except Exception as cleanup_exc:
                    cleanup_pending.append(
                        str(
                            cleanup_exc.recovery_path
                            if isinstance(
                                cleanup_exc, DirectoryLinkSwapRecoveryRequired
                            )
                            else link_swap.backup
                        )
                    )
            try:
                swap.finalize()
            except Exception as cleanup_exc:
                cleanup_pending.append(
                    str(
                        cleanup_exc.recovery_path
                        if isinstance(cleanup_exc, CanonicalSwapRecoveryRequired)
                        else swap.previous
                    )
                )
        if applying and rolled_back:
            try:
                _transition_edit_metadata(
                    store,
                    session_id,
                    expected=EditSessionStatus.APPLYING,
                    target=EditSessionStatus.ACTIVE,
                )
            except (
                EditSessionMetadataError,
                OSError,
                _EditTransitionRecoveryRequired,
            ):
                rolled_back = False
        if not rolled_back:
            recovery_path = swap.previous if swap is not None else backup_path
            _mark_edit_apply_recovery(
                store,
                session_id,
                receipt,
                receipt_path,
                receipt_writer,
                recovery_path=recovery_path,
            )
            raise SkillSyncError(
                "edit apply failed and requires recovery",
                code="edit_apply_recovery_required",
                exit_code=EXIT_SAFETY,
                details={
                    "session_id": session_id,
                    "receipt_path": str(receipt_path),
                    "recovery_path": str(recovery_path),
                },
            ) from exc
        receipt["status"] = "rolled-back"
        receipt["phase"] = "rolled-back"
        receipt["error_code"] = "edit_apply_failed"
        receipt["completed_at"] = time.time()
        if cleanup_pending:
            receipt["cleanup_pending"] = cleanup_pending
        receipt_writer.update(receipt)
        raise SkillSyncError(
            "edit apply failed; the previous canonical Skill was restored",
            code="edit_apply_failed",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
            },
        ) from exc

    return {
        "session_id": session_id,
        "skill": metadata.logical_skill,
        "scope": "base",
        "status": "applied",
        "previous_hash": metadata.baseline_hash,
        "applied_hash": workspace_hash,
        "backup_path": str(backup_path),
        "receipt_path": str(receipt_path),
        "deployments_rebuilt": bool(deployment_transaction["deployments"]),
        "deployments": deployment_transaction["deployments"],
        "clients_relinked": len(applied_link_swaps),
        "skipped_clients": deployment_transaction["skipped"],
        "cleanup_pending": cleanup_pending,
    }


def _edit_apply_scoped_locked(
    config: dict[str, Any],
    store: EditSessionStore,
    metadata: EditSessionMetadata,
    *,
    config_path: str | Path | None,
) -> dict[str, Any]:
    session_id = metadata.session_id
    target = metadata.target_scope.target
    if target is None:
        raise SkillSyncError(
            "scoped edit session target is missing",
            code="unsafe_edit_session",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        )
    registry = _load_local_registry(config)
    _target_names(registry, [metadata.logical_skill])
    previous_registry = copy.deepcopy(registry)
    registry_changed = register_variant_target(
        registry,
        metadata.logical_skill,
        target,
    )
    registry_path = _repo_path(config) / REGISTRY_FILE
    paths = store.paths(session_id)
    try:
        baseline = inspect_tree(paths.baseline)
        workspace = inspect_tree(paths.workspace)
    except (EditTreeInspectionError, OSError) as exc:
        raise SkillSyncError(
            f"edit session trees are unsafe or incomplete: {exc}",
            code="edit_session_incomplete",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        ) from exc
    if baseline.issues or baseline.hash != metadata.baseline_hash:
        raise SkillSyncError(
            "edit session baseline is damaged or does not match its recorded hash",
            code="unsafe_edit_baseline",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id},
        )
    issues = _validate_scoped_workspace(
        metadata,
        workspace,
        workspace_path=paths.workspace,
        config_path=config_path,
    )
    if issues or workspace.hash is None:
        raise SkillSyncError(
            "scoped edit workspace failed validation",
            code="invalid_edit_workspace",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "issues": [issue.to_dict() for issue in issues],
            },
        )
    workspace_hash = workspace.hash
    if workspace_hash == metadata.baseline_hash:
        raise SkillSyncError(
            "edit workspace has no changes to apply",
            code="edit_workspace_unchanged",
            exit_code=EXIT_CONFLICT,
            details={"session_id": session_id},
        )
    current_layer = _scoped_current_layer(metadata, config_path=config_path)
    if current_layer != metadata.layer_baseline.to_dict():
        raise SkillSyncError(
            "canonical Variant layer changed since this edit session began",
            code="edit_baseline_conflict",
            exit_code=EXIT_CONFLICT,
            details={
                "session_id": session_id,
                "expected_layer": metadata.layer_baseline.to_dict(),
                "actual_layer": current_layer,
            },
        )
    sources = variant_source.resolve_variant_source_paths(
        metadata.logical_skill,
        config_path=config_path,
    )
    variant_source.verify_variant_source_paths(sources)
    source = sources.variant_skill_root / target
    layer_present = metadata.layer_baseline.state is EditLayerBaselineState.PRESENT

    data_root_path = store.data_root
    receipt_path = data_root_path / "operations" / f"edit-apply-{session_id}.json"
    if receipt_path.exists() or is_link_or_reparse(receipt_path):
        raise SkillSyncError(
            "an edit apply receipt already exists for this session",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id, "receipt_path": str(receipt_path)},
        )
    backup_parent = prepare_private_directory(
        data_root_path / "backups" / "edit-apply" / metadata.logical_skill
    )
    backup_path = (
        backup_parent / f"{time.time_ns()}-{session_id}-{target}"
        if layer_present
        else None
    )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "operation": "edit-apply",
        "operation_id": session_id,
        "session_id": session_id,
        "skill": metadata.logical_skill,
        "scope": metadata.target_scope.kind.value,
        "target": target,
        "status": "prepared",
        "phase": "backup-pending",
        "created_at": time.time(),
        "canonical_path": str(source),
        "backup_path": None if backup_path is None else str(backup_path),
        "layer_baseline": metadata.layer_baseline.to_dict(),
        "baseline_hash": metadata.baseline_hash,
        "workspace_hash": workspace_hash,
        "deployments_rebuilt": False,
    }
    receipt_writer = PrivateJsonReceipt.create(receipt_path, receipt)
    try:
        if backup_path is not None:
            backup_hash = copy_skill_dir(source, backup_path)
            if backup_hash != metadata.baseline_hash:
                raise OSError("canonical Variant backup does not match the edit baseline")
            fsync_tree(backup_path)
        receipt["phase"] = "backup-ready"
        receipt_writer.update(receipt)
    except (OSError, ValueError) as exc:
        receipt["status"] = "rolled-back"
        receipt["phase"] = "backup-failed"
        receipt["error_code"] = "edit_backup_failed"
        receipt["completed_at"] = time.time()
        receipt_writer.update(receipt)
        raise SkillSyncError(
            "could not create a durable canonical Variant backup",
            code="edit_backup_failed",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id, "receipt_path": str(receipt_path)},
        ) from exc

    swap: CanonicalSwap | AbsentCanonicalSwap | None = None
    deployment_transaction: dict[str, Any] | None = None
    applied_link_swaps: list[DirectoryLinkSwap] = []
    cleanup_pending: list[str] = []
    applying = False
    applied_metadata = False
    registry_applied = False

    def recovery_path() -> Path:
        if swap is not None and swap.previous_moved:
            return swap.previous
        if backup_path is not None:
            return backup_path
        return paths.workspace

    try:
        _transition_edit_metadata(
            store,
            session_id,
            expected=EditSessionStatus.ACTIVE,
            target=EditSessionStatus.APPLYING,
        )
        applying = True
        _assert_no_incomplete_deployment_receipts(store.data_root)
        receipt["phase"] = "deployments-preparing"
        receipt_writer.update(receipt)
        deployment_transaction = _prepare_scoped_edit_deployments(
            config,
            registry,
            metadata,
            sources,
            paths.workspace,
            session_id,
        )
        receipt["clients"] = deployment_transaction["clients"]
        receipt["phase"] = "deployments-ready"
        receipt_writer.update(receipt)
        _verify_scoped_edit_deployments(
            metadata,
            sources,
            paths.workspace,
            deployment_transaction,
            proposed=True,
        )
        if layer_present:
            swap = CanonicalSwap.prepare(
                source,
                paths.workspace,
                expected_old_hash=metadata.baseline_hash,
                expected_new_hash=workspace_hash,
                token=session_id,
            )
        else:
            swap = AbsentCanonicalSwap.prepare(
                source,
                paths.workspace,
                expected_new_hash=workspace_hash,
                token=session_id,
                allowed_root=sources.portable_root,
            )
        receipt["status"] = "applying"
        receipt["phase"] = "canonical-replace"
        receipt_writer.update(receipt)

        swap.apply()
        receipt["phase"] = "canonical-applied"
        receipt_writer.update(receipt)
        if registry_changed:
            receipt["phase"] = "registry-applying"
            receipt_writer.update(receipt)
            save_registry(registry_path, registry)
            registry_applied = True
            receipt["phase"] = "registry-applied"
            receipt_writer.update(receipt)
        receipt["phase"] = "links-swapping"
        receipt["completed_clients"] = []
        receipt_writer.update(receipt)
        for link_swap in deployment_transaction["swaps"]:
            link_swap.apply()
            applied_link_swaps.append(link_swap)
            receipt["completed_clients"].append(
                deployment_transaction["swap_clients"][link_swap.destination]
            )
            receipt_writer.update(receipt)
        applied_layer = _scoped_current_layer(metadata, config_path=config_path)
        if applied_layer != EditLayerBaseline.present(workspace_hash).to_dict():
            raise SkillSyncError(
                "canonical Variant layer changed during deployment link swaps",
                code="edit_canonical_changed_during_deploy",
                exit_code=EXIT_CONFLICT,
            )
        _verify_scoped_edit_deployments(
            metadata,
            variant_source.resolve_variant_source_paths(
                metadata.logical_skill,
                config_path=config_path,
            ),
            paths.workspace,
            deployment_transaction,
            proposed=False,
        )
        for row in deployment_transaction["active"]:
            if link_state(Path(row["deployment_path"]), Path(row["destination"])) != "linked":
                raise SkillSyncError(
                    "an applied deployment link failed final verification",
                    code="edit_deployment_verification_failed",
                    exit_code=EXIT_SAFETY,
                    details={"client": row["client"]},
                )
        receipt["phase"] = "links-applied"
        receipt_writer.update(receipt)
        _transition_edit_metadata(
            store,
            session_id,
            expected=EditSessionStatus.APPLYING,
            target=EditSessionStatus.APPLIED,
        )
        applied_metadata = True
        receipt["status"] = "completed"
        receipt["phase"] = "completed"
        receipt["completed_at"] = time.time()
        receipt_writer.update(receipt)
        for link_swap in applied_link_swaps:
            try:
                link_swap.finalize()
            except Exception as exc:
                cleanup_pending.append(
                    str(
                        exc.recovery_path
                        if isinstance(exc, DirectoryLinkSwapRecoveryRequired)
                        else link_swap.backup
                    )
                )
        try:
            swap.finalize()
        except Exception as exc:
            cleanup_pending.append(
                str(
                    exc.recovery_path
                    if isinstance(exc, CanonicalSwapRecoveryRequired)
                    else swap.previous
                )
            )
        if cleanup_pending:
            receipt["status"] = "cleanup-pending"
            receipt["phase"] = "cleanup-pending"
            receipt["cleanup_pending"] = cleanup_pending
            try:
                receipt_writer.update(receipt)
            except Exception:
                cleanup_pending.append(str(receipt_path))
    except DirectoryLinkSwapRecoveryRequired as exc:
        _mark_edit_apply_recovery(
            store,
            session_id,
            receipt,
            receipt_path,
            receipt_writer,
            recovery_path=exc.recovery_path,
        )
        raise SkillSyncError(
            "deployment link swap requires recovery",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(exc.recovery_path),
            },
        ) from exc
    except ReceiptRecoveryRequired as exc:
        try:
            current = store.load(session_id)
            if current.status is EditSessionStatus.APPLYING:
                _transition_edit_metadata(
                    store,
                    session_id,
                    expected=EditSessionStatus.APPLYING,
                    target=EditSessionStatus.NEEDS_RECOVERY,
                )
        except (EditSessionMetadataError, OSError, _EditTransitionRecoveryRequired):
            pass
        raise SkillSyncError(
            "edit apply receipt changed and requires recovery",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(recovery_path()),
            },
        ) from exc
    except CanonicalSwapRecoveryRequired as exc:
        _mark_edit_apply_recovery(
            store,
            session_id,
            receipt,
            receipt_path,
            receipt_writer,
            recovery_path=exc.recovery_path,
        )
        raise SkillSyncError(
            "edit apply could not safely restore the previous Variant layer",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(exc.recovery_path),
            },
        ) from exc
    except _EditTransitionRecoveryRequired as exc:
        path = recovery_path()
        _mark_edit_apply_recovery(
            store,
            session_id,
            receipt,
            receipt_path,
            receipt_writer,
            recovery_path=path,
        )
        raise SkillSyncError(
            "edit session metadata durability is ambiguous and requires recovery",
            code="edit_apply_recovery_required",
            exit_code=EXIT_SAFETY,
            details={
                "session_id": session_id,
                "receipt_path": str(receipt_path),
                "recovery_path": str(path),
            },
        ) from exc
    except Exception as exc:
        if applied_metadata:
            path = recovery_path()
            _mark_edit_apply_recovery(
                store,
                session_id,
                receipt,
                receipt_path,
                receipt_writer,
                recovery_path=path,
            )
            raise SkillSyncError(
                "Variant layer was applied but final receipt cleanup needs recovery",
                code="edit_apply_recovery_required",
                exit_code=EXIT_SAFETY,
                details={"session_id": session_id, "receipt_path": str(receipt_path)},
            ) from exc
        links_rolled_back = True
        for link_swap in reversed(applied_link_swaps):
            try:
                if not link_swap.rollback():
                    links_rolled_back = False
            except Exception:
                links_rolled_back = False
        try:
            registry_rolled_back = True
            if registry_applied:
                try:
                    save_registry(registry_path, previous_registry)
                except Exception:
                    registry_rolled_back = False
            rolled_back = (
                links_rolled_back
                and registry_rolled_back
                and (swap is None or swap.rollback())
            )
        except Exception:
            rolled_back = False
        if rolled_back and swap is not None:
            for link_swap in applied_link_swaps:
                try:
                    link_swap.finalize()
                except Exception as cleanup_exc:
                    cleanup_pending.append(
                        str(
                            cleanup_exc.recovery_path
                            if isinstance(
                                cleanup_exc,
                                DirectoryLinkSwapRecoveryRequired,
                            )
                            else link_swap.backup
                        )
                    )
            try:
                swap.finalize()
            except Exception as cleanup_exc:
                cleanup_pending.append(
                    str(
                        cleanup_exc.recovery_path
                        if isinstance(cleanup_exc, CanonicalSwapRecoveryRequired)
                        else swap.previous
                    )
                )
        if applying and rolled_back:
            try:
                _transition_edit_metadata(
                    store,
                    session_id,
                    expected=EditSessionStatus.APPLYING,
                    target=EditSessionStatus.ACTIVE,
                )
            except (EditSessionMetadataError, OSError, _EditTransitionRecoveryRequired):
                rolled_back = False
        if not rolled_back:
            path = recovery_path()
            _mark_edit_apply_recovery(
                store,
                session_id,
                receipt,
                receipt_path,
                receipt_writer,
                recovery_path=path,
            )
            raise SkillSyncError(
                "edit apply failed and requires recovery",
                code="edit_apply_recovery_required",
                exit_code=EXIT_SAFETY,
                details={
                    "session_id": session_id,
                    "receipt_path": str(receipt_path),
                    "recovery_path": str(path),
                },
            ) from exc
        receipt["status"] = "rolled-back"
        receipt["phase"] = "rolled-back"
        receipt["error_code"] = "edit_apply_failed"
        receipt["completed_at"] = time.time()
        if cleanup_pending:
            receipt["cleanup_pending"] = cleanup_pending
        receipt_writer.update(receipt)
        raise SkillSyncError(
            "edit apply failed; the previous Variant layer was restored",
            code="edit_apply_failed",
            exit_code=EXIT_SAFETY,
            details={"session_id": session_id, "receipt_path": str(receipt_path)},
        ) from exc

    if deployment_transaction is None:  # pragma: no cover - success invariant
        raise RuntimeError("scoped deployment transaction was not prepared")
    return {
        "session_id": session_id,
        "skill": metadata.logical_skill,
        "scope": metadata.target_scope.kind.value,
        "target": target,
        "status": "applied",
        "previous_layer": metadata.layer_baseline.to_dict(),
        "applied_hash": workspace_hash,
        "backup_path": None if backup_path is None else str(backup_path),
        "receipt_path": str(receipt_path),
        "deployments_rebuilt": bool(deployment_transaction["deployments"]),
        "deployments": deployment_transaction["deployments"],
        "clients_relinked": len(applied_link_swaps),
        "registry_updated": registry_changed,
        "registry_version": registry["version"],
        "skipped_clients": deployment_transaction["skipped"],
        "cleanup_pending": cleanup_pending,
    }


def _mark_edit_apply_recovery(
    store: EditSessionStore,
    session_id: str,
    receipt: dict[str, Any],
    receipt_path: Path,
    receipt_writer: PrivateJsonReceipt,
    *,
    recovery_path: Path,
) -> None:
    transition_error: Exception | None = None
    try:
        current = store.load(session_id)
        if current.status is EditSessionStatus.APPLYING:
            _transition_edit_metadata(
                store,
                session_id,
                expected=EditSessionStatus.APPLYING,
                target=EditSessionStatus.NEEDS_RECOVERY,
            )
    except (
        EditSessionMetadataError,
        OSError,
        _EditTransitionRecoveryRequired,
    ) as exc:
        transition_error = exc
    receipt["status"] = "needs-recovery"
    receipt["phase"] = "needs-recovery"
    receipt["error_code"] = "edit_apply_recovery_required"
    receipt["recovery_path"] = str(recovery_path)
    if transition_error is not None:
        receipt["metadata_transition_failed"] = True
    receipt_writer.update(receipt)


def _prepare_edit_deployments(
    config: dict[str, Any],
    registry: dict[str, Any],
    skill_name: str,
    canonical_source: Path,
    workspace: Path,
    workspace_hash: str,
    session_id: str,
) -> dict[str, Any]:
    """Render and verify one immutable deployment per applicable client."""

    clients = detect_clients()
    disabled = _disabled_agents(config)
    entry = registry.get("skills", {}).get(skill_name, {})
    configured = _configured_client_targets(entry)
    rendered_root = _rendered_root(config)
    rows: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    deployments: list[dict[str, Any]] = []
    swaps: list[DirectoryLinkSwap] = []
    swap_clients: dict[Path, str] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    variant_skill_root = canonical_source.parent.parent / "variants" / skill_name

    for client in clients:
        reason: str | None = None
        if configured and not {client.id, client.family_id}.intersection(configured):
            reason = "config-excluded"
        elif client.family_id in disabled:
            reason = "disabled"
        elif not client.detected:
            reason = "undetected"
        if reason is not None:
            row = {
                "client": client.id,
                "agent": client.family_id,
                "status": "skipped",
                "reason": reason,
                "destination": str(client.skills_dir / skill_name),
            }
            rows.append(row)
            skipped.append({"client": client.id, "reason": reason})
            continue

        spec = _client_deployment_spec(
            workspace,
            rendered_root,
            skill_name,
            client.id,
            source_hash=workspace_hash,
            variant_skill_root=variant_skill_root,
        )
        deployed = _render_client_deployment(
            spec,
            workspace,
            rendered_root,
            skill_name,
            client.id,
        )
        verification = verify_deployment(
            deployed.path,
            expected_provenance=spec.expected_provenance,
        )
        if not verification.ok:
            raise SkillSyncError(
                "rendered edit deployment failed verification",
                code="edit_deployment_verification_failed",
                exit_code=EXIT_SAFETY,
                details={"client": client.id, "state": verification.state},
            )
        destination = client.skills_dir / skill_name
        current_state, current_target = _deployment_link_state(
            canonical_source,
            destination,
            deployed.path,
            rendered_root,
            skill_name,
            client.id,
        )
        action = _deployment_action(current_state, "valid")
        if action == "blocked":
            raise SkillSyncError(
                "edit deployment is blocked by an unsafe Agent path",
                code="edit_deployment_blocked",
                exit_code=EXIT_SAFETY,
                details={"client": client.id, "state": current_state},
            )
        row = {
            "client": client.id,
            "agent": client.family_id,
            "status": "ready",
            "action": action,
            "destination": str(destination),
            "deployment_path": str(deployed.path),
            "deployment_created": deployed.created,
            "resolution_hash": spec.resolution_hash,
            "applied_layers": spec.expected_provenance["applied_layers"],
            "previous_state": current_state,
            "previous_target": None if current_target is None else str(current_target),
        }
        rows.append(row)
        active.append(row)
        deployments.append(
            {
                "client": client.id,
                "path": str(deployed.path),
                "created": deployed.created,
                "resolution_hash": spec.resolution_hash,
            }
        )
        resolutions[client.id] = {
            "resolution_hash": spec.resolution_hash,
            "expected_provenance": spec.expected_provenance,
        }
        if action == "noop":
            continue
        allowed = () if current_target is None else (current_target,)
        link_swap = DirectoryLinkSwap.prepare(
            deployed.path,
            destination,
            allowed_current_sources=allowed,
            token=f"{session_id}-{client.id}",
        )
        swaps.append(link_swap)
        swap_clients[destination] = client.id

    return {
        "clients": rows,
        "active": active,
        "skipped": skipped,
        "deployments": deployments,
        "swaps": swaps,
        "swap_clients": swap_clients,
        "resolutions": resolutions,
    }


def _verify_base_edit_deployments(
    skill_name: str,
    canonical_source: Path,
    workspace: Path,
    transaction: dict[str, Any],
    *,
    proposed: bool,
) -> None:
    if not transaction["active"]:
        return
    base_source = workspace if proposed else canonical_source
    variant_skill_root = canonical_source.parent.parent / "variants" / skill_name
    rendered_root = Path(transaction["active"][0]["deployment_path"]).parent.parent
    for row in transaction["active"]:
        client = row["client"]
        try:
            spec = _client_deployment_spec(
                base_source,
                rendered_root,
                skill_name,
                client,
                variant_skill_root=variant_skill_root,
            )
        except (OSError, ValueError) as exc:
            raise SkillSyncError(
                f"Base edit deployment inputs changed during apply: {exc}",
                code="edit_canonical_changed_during_deploy",
                exit_code=EXIT_CONFLICT,
                details={"client": client},
            ) from exc
        expected = transaction["resolutions"][client]
        if spec.resolution_hash != expected["resolution_hash"]:
            raise SkillSyncError(
                "Base edit deployment resolution changed during apply",
                code="edit_canonical_changed_during_deploy",
                exit_code=EXIT_CONFLICT,
                details={"client": client},
            )
        verification = verify_deployment(
            Path(row["deployment_path"]),
            expected_provenance=expected["expected_provenance"],
        )
        if not verification.ok:
            raise SkillSyncError(
                "Base edit deployment changed during apply",
                code="edit_deployment_verification_failed",
                exit_code=EXIT_SAFETY,
                details={"client": client, "state": verification.state},
            )


def _prepare_scoped_edit_deployments(
    config: dict[str, Any],
    registry: dict[str, Any],
    metadata: EditSessionMetadata,
    sources: variant_source.VariantSourcePaths,
    workspace: Path,
    session_id: str,
) -> dict[str, Any]:
    """Render and prepare link swaps only for clients inside a scoped edit."""

    target = metadata.target_scope.target
    if target is None:
        raise EditSessionMetadataError("scoped edit session target is missing")
    scope_clients = set(_scoped_edit_clients(metadata))
    clients = detect_clients()
    disabled = _disabled_agents(config)
    entry = registry.get("skills", {}).get(metadata.logical_skill, {})
    configured = _configured_client_targets(entry)
    rendered_root = _rendered_root(config)
    rows: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    deployments: list[dict[str, Any]] = []
    swaps: list[DirectoryLinkSwap] = []
    swap_clients: dict[Path, str] = {}
    resolutions: dict[str, dict[str, Any]] = {}

    for client in clients:
        if client.id not in scope_clients:
            continue
        reason: str | None = None
        if configured and not {client.id, client.family_id}.intersection(configured):
            reason = "config-excluded"
        elif client.family_id in disabled:
            reason = "disabled"
        elif not client.detected:
            reason = "undetected"
        if reason is not None:
            rows.append(
                {
                    "client": client.id,
                    "agent": client.family_id,
                    "status": "skipped",
                    "reason": reason,
                    "destination": str(client.skills_dir / metadata.logical_skill),
                }
            )
            skipped.append({"client": client.id, "reason": reason})
            continue

        try:
            resolution = resolve_variant_for_client(
                sources.base_root,
                sources.variant_skill_root,
                client.id,
                override_target=target,
                override_root=workspace,
            )
        except (OSError, ValueError) as exc:
            variant_source.verify_variant_source_paths(sources)
            raise SkillSyncError(
                f"could not resolve scoped edit deployment: {exc}",
                code="variant_resolution_invalid",
                exit_code=EXIT_SAFETY,
                details={"client": client.id, "target": target},
            ) from exc
        variant_source.verify_variant_source_paths(sources)
        deployed = render_layered_deployment(
            resolution,
            rendered_root,
            metadata.logical_skill,
        )
        expected = expected_layered_provenance(
            metadata.logical_skill,
            resolution,
        )
        verification = verify_deployment(
            deployed.path,
            expected_provenance=expected,
        )
        if not verification.ok:
            raise SkillSyncError(
                "rendered scoped edit deployment failed verification",
                code="edit_deployment_verification_failed",
                exit_code=EXIT_SAFETY,
                details={"client": client.id, "state": verification.state},
            )
        destination = client.skills_dir / metadata.logical_skill
        current_state, current_target = _deployment_link_state(
            sources.base_root,
            destination,
            deployed.path,
            rendered_root,
            metadata.logical_skill,
            client.id,
        )
        action = _deployment_action(current_state, "valid")
        if action == "blocked":
            raise SkillSyncError(
                "scoped edit deployment is blocked by an unsafe Agent path",
                code="edit_deployment_blocked",
                exit_code=EXIT_SAFETY,
                details={"client": client.id, "state": current_state},
            )
        row = {
            "client": client.id,
            "agent": client.family_id,
            "status": "ready",
            "action": action,
            "destination": str(destination),
            "deployment_path": str(deployed.path),
            "deployment_created": deployed.created,
            "resolution_hash": resolution.resolution_hash,
            "output_hash": resolution.output_hash,
            "previous_state": current_state,
            "previous_target": None if current_target is None else str(current_target),
        }
        rows.append(row)
        active.append(row)
        deployments.append(
            {
                "client": client.id,
                "path": str(deployed.path),
                "created": deployed.created,
                "resolution_hash": resolution.resolution_hash,
            }
        )
        resolutions[client.id] = {
            "resolution_hash": resolution.resolution_hash,
            "output_hash": resolution.output_hash,
            "expected_provenance": expected,
        }
        if action == "noop":
            continue
        allowed = () if current_target is None else (current_target,)
        link_swap = DirectoryLinkSwap.prepare(
            deployed.path,
            destination,
            allowed_current_sources=allowed,
            token=f"{session_id}-{client.id}",
        )
        swaps.append(link_swap)
        swap_clients[destination] = client.id

    return {
        "clients": rows,
        "active": active,
        "skipped": skipped,
        "deployments": deployments,
        "swaps": swaps,
        "swap_clients": swap_clients,
        "resolutions": resolutions,
    }


def _verify_scoped_edit_deployments(
    metadata: EditSessionMetadata,
    sources: variant_source.VariantSourcePaths,
    workspace: Path,
    transaction: dict[str, Any],
    *,
    proposed: bool,
) -> None:
    target = metadata.target_scope.target
    if target is None:
        raise EditSessionMetadataError("scoped edit session target is missing")
    for row in transaction["active"]:
        client = row["client"]
        try:
            resolution = resolve_variant_for_client(
                sources.base_root,
                sources.variant_skill_root,
                client,
                **(
                    {"override_target": target, "override_root": workspace}
                    if proposed
                    else {}
                ),
            )
        except (OSError, ValueError) as exc:
            raise SkillSyncError(
                f"scoped deployment inputs changed during apply: {exc}",
                code="edit_canonical_changed_during_deploy",
                exit_code=EXIT_CONFLICT,
                details={"client": client, "target": target},
            ) from exc
        expected = transaction["resolutions"][client]
        if (
            resolution.resolution_hash != expected["resolution_hash"]
            or resolution.output_hash != expected["output_hash"]
        ):
            raise SkillSyncError(
                "scoped deployment resolution changed during apply",
                code="edit_canonical_changed_during_deploy",
                exit_code=EXIT_CONFLICT,
                details={"client": client, "target": target},
            )
        verification = verify_deployment(
            Path(row["deployment_path"]),
            expected_provenance=expected["expected_provenance"],
        )
        if not verification.ok:
            raise SkillSyncError(
                "scoped deployment changed during apply",
                code="edit_deployment_verification_failed",
                exit_code=EXIT_SAFETY,
                details={"client": client, "state": verification.state},
            )
    if proposed:
        variant_source.verify_variant_source_paths(sources)
    else:
        for row in transaction["active"]:
            resolution = resolve_variant_for_client(
                sources.base_root,
                sources.variant_skill_root,
                row["client"],
            )
            variant_source.verify_variant_source_paths(sources, resolution=resolution)


def _transition_edit_metadata(
    store: EditSessionStore,
    session_id: str,
    *,
    expected: EditSessionStatus,
    target: EditSessionStatus,
) -> EditSessionMetadata:
    """Transition metadata or fail closed when the durable result is ambiguous."""

    current = store.load(session_id)
    if current.status is not expected:
        raise _EditTransitionRecoveryRequired(
            f"expected {expected.value}, found {current.status.value}"
        )
    try:
        return store.transition_locked(session_id, target)
    except (EditSessionMetadataError, OSError) as exc:
        try:
            durable = store.load(session_id)
        except (EditSessionMetadataError, OSError) as load_exc:
            raise _EditTransitionRecoveryRequired(
                "could not determine durable edit session status"
            ) from load_exc
        if durable.status is expected:
            raise
        raise _EditTransitionRecoveryRequired(
            f"metadata transition durability is ambiguous: {durable.status.value}"
        ) from exc


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
        targets = _target_names(registry, skill_names)
        remote_registry = (
            _cached_remote_registry(repo, branch)
            if git_state.behind > 0
            else registry
        )
        remote_changes = (
            _remote_sync_unit_keys(repo, branch, registry, remote_registry)
            if git_state.behind > 0
            else set()
        )
        units = _sync_unit_rows(config, registry, targets, remote_changes)
        units_by_skill = {
            name: [row for row in units if row["skill"] == name]
            for name in targets
        }
        skills = []
        for name in targets:
            base_unit = next(
                unit for unit in units_by_skill[name] if unit["kind"] == "base"
            )
            row = _skill_status(config, registry, name, base_unit=base_unit)
            row["units"] = units_by_skill[name]
            skills.append(row)
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
    *,
    _merge_local_changes: bool = False,
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
        preflight_git_state = git.state(repo, branch, fetch_remote=False)
        previous_registry = _load_local_registry(config)
        remote_registry = _cached_remote_registry(repo, branch)
        preflight_targets = _target_names(remote_registry, skill_names)
        remote_changes = (
            _remote_sync_unit_keys(
                repo,
                branch,
                previous_registry,
                remote_registry,
            )
            if preflight_git_state.behind > 0
            else set()
        )
        preflight_units = _sync_unit_rows(
            config,
            previous_registry,
            preflight_targets,
            remote_changes,
        )
        conflicts = [row for row in preflight_units if row["state"] == "conflict"]
        if conflicts:
            raise SkillSyncError(
                "the same Base or Variant unit changed locally and remotely",
                code="sync_unit_conflict",
                exit_code=EXIT_CONFLICT,
                details={"units": conflicts},
            )
        preserved_bases = {
            row["skill"]
            for row in preflight_units
            if row["kind"] == "base"
            and row["changed_local"]
            and not row["changed_remote"]
        }
        preserved_variants: dict[str, set[str]] = {}
        preserved_baselines: dict[str, dict[str, str]] = {}
        for row in preflight_units:
            if (
                row["kind"] != "variant"
                or not row["changed_local"]
                or row["changed_remote"]
                or row["target"] is None
            ):
                continue
            preserved_variants.setdefault(row["skill"], set()).add(row["target"])
            if row["baseline_hash"] is not None:
                preserved_baselines.setdefault(row["skill"], {})[row["target"]] = row[
                    "baseline_hash"
                ]
        if not _merge_local_changes and (preserved_bases or preserved_variants):
            changed_label = (
                "Variant"
                if preserved_variants and not preserved_bases
                else "Base"
                if preserved_bases and not preserved_variants
                else "Base or Variant"
            )
            raise SkillSyncError(
                f"refusing to overwrite locally changed {changed_label} source",
                code="local_source_changed",
                exit_code=EXIT_CONFLICT,
                details={
                    "bases": sorted(preserved_bases),
                    "variants": {
                        name: sorted(targets)
                        for name, targets in sorted(preserved_variants.items())
                    },
                },
            )
        git.merge_ff_only(repo, branch)

        registry = _load_local_registry(config)
        _reconcile_config_to_registry(config, registry)
        targets = _target_names(registry, skill_names)
        _verify_preflight_local_units(
            config,
            registry,
            targets,
            preflight_units,
        )
        variant_plans = _prepare_variant_pull_plans(
            config,
            registry,
            targets,
            preserved_targets=preserved_variants,
            preserved_baselines=preserved_baselines,
            config_path=config_path,
        )
        installed: list[str] = []
        installed_variants: list[dict[str, str]] = []
        kept_variants: list[dict[str, str]] = []
        for name in targets:
            remote_skill = repo / "skills" / name
            if not remote_skill.exists():
                continue
            destination = _local_skill_path_or_default(config, registry, name)
            if name not in preserved_bases:
                copied_hash = copy_skill_dir(remote_skill, destination)
                config.setdefault("skills", {}).setdefault(name, {})["local_path"] = str(destination)
                config["skills"][name]["last_installed_hash"] = copied_hash
                installed.append(name)
            if name in variant_plans:
                (
                    units,
                    variant_destination,
                    next_baselines,
                    preserved,
                ) = variant_plans[name]
                hashes = portable_sync.replace_variant_set(units, variant_destination)
                _set_variant_baselines(config, name, next_baselines)
                installed_variants.extend(
                    {
                        "skill": name,
                        "target": target,
                        "hash": hash_value,
                    }
                    for target, hash_value in sorted(hashes.items())
                    if target not in preserved
                )
                kept_variants.extend(
                    {
                        "skill": name,
                        "target": target,
                        "hash": hashes[target],
                    }
                    for target in sorted(preserved)
                    if target in hashes
                )

        save_config(config_file, config)
        return {
            "pulled": installed,
            "pulled_variants": installed_variants,
            "preserved_variants": kept_variants,
        }
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

        variant_plans = _prepare_variant_push_plans(
            config,
            registry,
            targets,
            config_path=config_path,
        )
        pushed_hashes: dict[str, str] = {}
        pushed_variant_hashes: dict[str, dict[str, str]] = {}
        for name in targets:
            source = _local_skill_path(config, name)
            _validate_skill_path(source)
            destination = repo / "skills" / name
            pushed_hashes[name] = copy_skill_dir(source, destination)
            if name in variant_plans:
                units = variant_plans[name]
                variant_destination = repo / "variants" / name
                if units or variant_destination.exists() or is_link_or_reparse(
                    variant_destination
                ):
                    pushed_variant_hashes[name] = portable_sync.replace_variant_set(
                        units,
                        variant_destination,
                    )
                else:
                    pushed_variant_hashes[name] = {}

        committed = git.commit_all_if_changed(repo, message or "Sync selected skills")
        git.push(repo, branch)

        for name, hash_value in pushed_hashes.items():
            config.setdefault("skills", {}).setdefault(name, {})["last_installed_hash"] = hash_value
            if name in pushed_variant_hashes:
                _set_variant_baselines(config, name, pushed_variant_hashes[name])
        save_config(config_file, config)
        return {
            "pushed": list(pushed_hashes),
            "pushed_variants": [
                {
                    "skill": name,
                    "target": target,
                    "hash": hash_value,
                }
                for name in sorted(pushed_variant_hashes)
                for target, hash_value in sorted(pushed_variant_hashes[name].items())
            ],
            "committed": committed,
        }
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
        normalized = _resolve_sync_input(skill_names, config_path)
        config = _load_local_config(config_path)
        _raise_preflight_findings(
            "sync", blockers=_sync_safety_blockers(config)
        )
        targets = None if normalized["all_selected"] else normalized["skills"]
        preview = sync_preview(
            skill_names=targets, config_path=config_path, fetch_remote=True
        )
        action = preview["action"]
        if action == "setup":
            raise SkillSyncError("skill-sync is not initialized; run init first")
        if action == "blocked":
            raise SkillSyncError("sync repository is dirty: " + preview["summary"])
        if action == "conflict":
            raise SkillSyncError("both remote and local selected Skills changed; resolve manually")
        if action == "pull":
            result = pull(
                skill_names=targets,
                config_path=config_path,
                _merge_local_changes=True,
            )
            if "platform" not in _load_local_config(config_path):
                result["links"] = link_skills(skill_names=targets, config_path=config_path)["links"]
            return result
        if action == "push":
            return push(skill_names=targets, config_path=config_path)
        if action == "repair-links":
            return {"synced": [], "links": link_skills(skill_names=targets, config_path=config_path)["links"]}
        result: dict[str, Any] = {"synced": [], "noop": True}
        if "platform" not in _load_local_config(config_path):
            result["links"] = link_skills(skill_names=targets, config_path=config_path)["links"]
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


def _data_root(config: dict[str, Any]) -> Path:
    configured = config.get("data_root")
    if configured is not None and (not isinstance(configured, str) or not configured):
        raise SkillSyncError("configured data_root must be a non-empty string")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise SkillSyncError("configured data_root must be an absolute path")
        return path
    return default_data_root()


def _rendered_root(config: dict[str, Any]) -> Path:
    return _data_root(config) / "rendered"


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


def _normalize_skill_names(
    skill_names: Iterable[str] | None,
    *,
    required: bool,
) -> list[str] | None:
    if skill_names is None:
        if required:
            raise SkillSyncError("at least one Skill name is required")
        return None
    if isinstance(skill_names, (str, bytes)):
        raise SkillSyncError("skills must be a list of Skill names")
    try:
        names = list(skill_names)
    except TypeError as exc:
        raise SkillSyncError("skills must be a list of Skill names") from exc
    if not all(isinstance(name, str) for name in names):
        raise SkillSyncError("skills must contain only Skill names")
    names = list(dict.fromkeys(names))
    if required and not names:
        raise SkillSyncError("at least one Skill name is required")
    for name in names:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise SkillSyncError(f"invalid Skill name: {name}")
    return names


def _normalize_agent_names(
    agent_names: Iterable[str] | None,
) -> list[str]:
    if agent_names is None:
        return []
    if isinstance(agent_names, (str, bytes)):
        raise SkillSyncError("agents must be a list of Agent or client names")
    try:
        names = list(agent_names)
    except TypeError as exc:
        raise SkillSyncError("agents must be a list of Agent or client names") from exc
    if not all(isinstance(name, str) and name for name in names):
        raise SkillSyncError("agents must contain only non-empty names")
    return list(dict.fromkeys(names))


def _resolve_sync_input(
    skill_names: Iterable[str] | None,
    config_path: str | Path | None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    requested = _normalize_skill_names(skill_names, required=False)
    return {
        "skills": _target_names(registry, requested),
        "all_selected": requested is None,
    }


def _resolve_link_input(
    skill_names: Iterable[str] | None,
    agent_names: Iterable[str] | None,
    config_path: str | Path | None,
    *,
    clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    requested_skills = _normalize_skill_names(skill_names, required=False)
    requested_agents = _normalize_agent_names(agent_names)
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    known = {client.id for client in client_snapshot} | {
        client.family_id for client in client_snapshot
    }
    unknown = set(requested_agents) - known
    if unknown:
        raise SkillSyncError(
            "unknown Agent or client: " + ", ".join(sorted(unknown))
        )
    return {
        "skills": _target_names(registry, requested_skills),
        "agents": requested_agents,
    }


def _resolve_agent_toggle_input(
    agent_name: str | None,
    enabled: bool | None,
    config_path: str | Path | None,
    *,
    clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    # Load and validate local config so preview and action fail the same way on
    # malformed machine state, even though target resolution itself is local.
    _load_local_config(config_path)
    if not isinstance(agent_name, str) or not agent_name:
        raise SkillSyncError("agent must be a non-empty Agent family name")
    if not isinstance(enabled, bool):
        raise SkillSyncError("enabled must be a boolean")
    normalized = _normalize_agent_name(agent_name)
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    known = {client.family_id for client in client_snapshot}
    if normalized not in known:
        raise SkillSyncError(f"unknown Agent: {agent_name}")
    return {"agent": normalized, "enabled": enabled}


def _resolve_import_input(
    skill_names: Iterable[str] | None,
    agent_name: str | None,
    config_path: str | Path | None,
    *,
    clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    names = _normalize_skill_names(skill_names, required=True)
    assert names is not None
    if not isinstance(agent_name, str) or not agent_name:
        raise SkillSyncError("agent must be a non-empty Agent family name")
    config = _load_local_config(config_path)
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    known_families = {client.family_id for client in client_snapshot}
    if agent_name not in known_families:
        raise SkillSyncError(f"unknown Agent: {agent_name}")
    global_root = _global_skill_root(config)
    try:
        deployment_clients = expand_agent_clients(
            agent_name, clients=client_snapshot, detected_only=True
        )
    except ValueError as exc:
        raise SkillSyncError(str(exc)) from exc
    if not deployment_clients:
        raise SkillSyncError(f"Agent is not detected: {agent_name}")
    blockers: list[dict[str, Any]] = []
    if len(deployment_clients) != 1:
        blockers.append(
            {
                "code": "ambiguous-import-source-client",
                "agent": agent_name,
                "detail": "Import requires exactly one detected concrete source client.",
            }
        )
    source_client = deployment_clients[0] if len(deployment_clients) == 1 else None
    source_root = deployment_clients[0].skills_dir
    expected_clients = sorted(
        _import_client_ids(
            agent_name,
            source_root,
            clients=client_snapshot,
        )
    )
    rows: list[dict[str, Any]] = []
    for name in names:
        source = source_root / name
        destination = global_root / name
        installed = _imported_deployment_target(
            source,
            _rendered_root(config),
            name,
            set(expected_clients),
        )
        if installed is not None:
            state = "already-linked"
            reason = "Agent path already points to a verified managed deployment."
        elif is_link_or_reparse(source):
            state = "blocked"
            reason = (
                f"{name} link is not a verified deployment for {agent_name}"
            )
        else:
            try:
                _validate_skill_path(source)
            except SkillSyncError as exc:
                state = "blocked"
                reason = str(exc)
            else:
                if destination.exists() or is_link_or_reparse(destination):
                    try:
                        _validate_skill_path(destination)
                        same = hash_skill_dir(source) == hash_skill_dir(destination)
                    except (SkillSyncError, OSError, ValueError) as exc:
                        state = "blocked"
                        reason = str(exc)
                    else:
                        state = "same" if same else "conflict"
                        reason = (
                            "Canonical content already matches the Agent Skill."
                            if same
                            else "Canonical Skill has different content."
                        )
                else:
                    state = "importable"
                    reason = "Agent Skill can be moved into the global library."
        rows.append(
            {
                "name": name,
                "source_path": str(source),
                "canonical_path": str(destination),
                "state": state,
                "reason": reason,
            }
        )
    return {
        "skills": names,
        "agent": agent_name,
        "source_client": None if source_client is None else source_client.id,
        "deployment_clients": [
            {
                "agent": client.family_id,
                "client": client.id,
                "destination_root": str(client.skills_dir),
            }
            for client in deployment_clients
        ],
        "blockers": blockers,
        "rows": rows,
    }


def _resolve_delete_input(
    skill_names: Iterable[str] | None,
    config_path: str | Path | None,
    *,
    clients: Iterable[Any] | None = None,
    config: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested = _normalize_skill_names(skill_names, required=True)
    assert requested is not None
    config = _load_local_config(config_path) if config is None else config
    registry = _load_local_registry(config) if registry is None else registry
    global_root = _global_skill_root(config)
    names = _resolve_delete_names(
        requested,
        global_root=global_root,
        config=config,
        registry=registry,
    )
    rendered_root = _rendered_root(config)
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    rows: list[dict[str, Any]] = []
    skipped_clients: list[dict[str, Any]] = []
    for name in names:
        source = global_root / name
        _validate_skill_path(source)
        affected_clients: list[dict[str, str]] = []
        for client in client_snapshot:
            destination = client.skills_dir / name
            owned_target = _owned_link_target(
                source, destination, rendered_root, name, client.id
            )
            if owned_target is not None:
                affected_clients.append(
                    {
                        "agent": client.family_id,
                        "client": client.id,
                        "destination": str(destination),
                    }
                )
            elif (
                destination.exists()
                or destination.is_symlink()
                or is_link_or_reparse(destination)
            ):
                skipped_clients.append(
                    {
                        "code": "unowned-agent-path-skipped",
                        "skill": name,
                        "agent": client.family_id,
                        "client": client.id,
                        "path": str(destination),
                        "detail": "The existing Agent path is not owned and will not be deleted.",
                    }
                )
        rows.append(
            {
                "name": name,
                "canonical_path": str(source),
                "clients": affected_clients,
            }
        )
    return {"skills": names, "rows": rows, "warnings": skipped_clients}


def _configured_clients_for_skills(
    config: dict[str, Any],
    skill_names: Iterable[str],
    clients: Iterable[Any],
) -> list[dict[str, Any]]:
    registry = _load_local_registry(config)
    disabled = _disabled_agents(config)
    rows: list[dict[str, Any]] = []
    for name in skill_names:
        entry = registry.get("skills", {}).get(name, {})
        configured = _configured_client_targets(entry)
        for client in clients:
            if configured and not {client.id, client.family_id}.intersection(
                configured
            ):
                continue
            if not client.detected or client.family_id in disabled:
                continue
            rows.append(
                {
                    "skill": name,
                    "agent": client.family_id,
                    "client": client.id,
                    "destination": str(client.skills_dir / name),
                    "detected": client.detected,
                    "enabled": client.family_id not in disabled,
                    "effect": "sync-and-reconcile",
                }
            )
    return rows


def _sync_safety_blockers(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not _deployment_receipt_health(_data_root(config)):
        return []
    return [
        {
            "code": "deployment-recovery-required",
            "detail": "An incomplete deployment operation must be recovered before sync.",
        }
    ]


def _import_safety_findings(
    resolved: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conflicts = [
        {
            "code": "import-conflict",
            "skill": row["name"],
            "detail": row["reason"],
        }
        for row in resolved["rows"]
        if row["state"] == "conflict"
    ]
    blockers = [
        {
            "code": "import-blocked",
            "skill": row["name"],
            "detail": row["reason"],
        }
        for row in resolved["rows"]
        if row["state"] == "blocked"
    ]
    blockers.extend(resolved["blockers"])
    return conflicts, blockers


def _agent_safety_blockers(
    config: dict[str, Any], *, enabled: bool
) -> list[dict[str, Any]]:
    if enabled or not _deployment_receipt_health(_data_root(config)):
        return []
    return [
        {
            "code": "deployment-recovery-required",
            "detail": "An incomplete deployment operation must be recovered first.",
        }
    ]


def _link_safety_findings(
    config: dict[str, Any],
    resolved: dict[str, Any],
    plan: list[dict[str, Any]],
    clients: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers = [
        {
            "code": "unsafe-agent-path",
            "skill": skill["name"],
            "client": client["client"],
            "destination": client["destination"],
            "detail": client.get("reason") or client["current_state"],
        }
        for skill in plan
        for client in skill["clients"]
        if client["action"] == "blocked"
    ]
    conflicts = [
        {
            "code": client["current_state"],
            "skill": skill["name"],
            "client": client["client"],
            "destination": client["destination"],
            "detail": client.get("reason")
            or "Agent path is not safely replaceable.",
        }
        for skill in plan
        for client in skill["clients"]
        if client["current_state"]
        in {
            "conflict",
            "wrong-link",
            "broken-link",
            "tampered-render",
            "missing-render",
        }
    ]
    disabled = _disabled_agents(config)
    client_map = {client.id: client.family_id for client in clients}
    requested_families = {
        client_map.get(name, _normalize_agent_name(name))
        for name in resolved["agents"]
    }
    for family in sorted(requested_families & disabled):
        blockers.append(
            {
                "code": "agent-disabled",
                "agent": family,
                "detail": "Agent synchronization is disabled; enable it before repairing links.",
            }
        )
    if _deployment_receipt_health(_data_root(config)):
        blockers.append(
            {
                "code": "deployment-recovery-required",
                "detail": "An incomplete deployment operation must be recovered before repairing links.",
            }
        )
    return conflicts, blockers


def _delete_safety_blockers(
    config: dict[str, Any], skill_names: Iterable[str]
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if _deployment_receipt_health(_data_root(config)):
        blockers.append(
            {
                "code": "deployment-recovery-required",
                "detail": "An incomplete deployment operation must be recovered first.",
            }
        )
    selected = {name.casefold() for name in skill_names}
    store = EditSessionStore(_data_root(config))
    active_statuses = {
        EditSessionStatus.ACTIVE,
        EditSessionStatus.APPLYING,
        EditSessionStatus.NEEDS_RECOVERY,
    }
    for session in store.list_metadata():
        if (
            session.logical_skill.casefold() in selected
            and session.status in active_statuses
        ):
            blockers.append(
                {
                    "code": "active-edit-session",
                    "skill": session.logical_skill,
                    "detail": f"Edit session {session.session_id} is {session.status.value}.",
                }
            )
    return blockers


def _raise_preflight_findings(
    operation: str,
    *,
    conflicts: Iterable[dict[str, Any]] = (),
    blockers: Iterable[dict[str, Any]] = (),
) -> None:
    conflict_rows = list(conflicts)
    blocker_rows = list(blockers)
    if conflict_rows:
        detail = conflict_rows[0].get("detail") or "conflicts require a decision"
        raise SkillSyncError(
            f"{operation} is blocked: {detail}",
            code=f"{operation}_conflict",
            exit_code=EXIT_CONFLICT,
            details={"conflicts": conflict_rows, "blockers": blocker_rows},
        )
    if blocker_rows:
        detail = blocker_rows[0].get("detail") or "a safety preflight failed"
        raise SkillSyncError(
            f"{operation} is blocked: {detail}",
            code=f"{operation}_blocked",
            exit_code=EXIT_SAFETY,
            details={"blockers": blocker_rows},
        )


def _skill_root(platform: str, skill_dir: str | Path | None) -> Path:
    if skill_dir is not None:
        return Path(skill_dir).expanduser().absolute()
    return get_adapter(platform).default_skill_dir().expanduser().absolute()


def _global_skill_root(config: dict[str, Any], skill_dir: str | Path | None = None) -> Path:
    if skill_dir is not None:
        return Path(skill_dir).expanduser().absolute()
    return Path(
        config.get("skills_root") or Path.home() / ".agents" / "skills"
    ).expanduser().absolute()


def _resolve_skill_item(item: str | Path, root: Path) -> Path:
    raw = Path(item).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        return raw.absolute()
    return (root / raw).absolute()


def _validate_skill_path(path: Path) -> None:
    if is_link_or_reparse(path):
        raise SkillSyncError(f"Skill path is a symlink or reparse point: {path}")
    if not path.exists():
        raise SkillSyncError(f"Skill path does not exist: {path}")
    if not path.is_dir():
        raise SkillSyncError(f"Skill path is not a directory: {path}")
    if not (path / "SKILL.md").is_file():
        raise SkillSyncError(f"Skill path does not contain SKILL.md: {path}")


def _reject_reparse_scan_root(root: Path) -> None:
    if is_link_or_reparse(root):
        raise SkillSyncError(f"Skill scan root is a symlink or reparse point: {root}")


def _is_inside(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved_path == resolved_root or resolved_path.is_relative_to(
        resolved_root
    )


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
    skills = config.get("skills", {})
    if not isinstance(skills, dict):
        raise SkillSyncError("config skills must be a mapping")
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


def _cached_remote_registry(repo: Path, branch: str) -> dict[str, Any]:
    return parse_registry_text(git.read_remote_file(repo, REGISTRY_FILE, branch))


def _remote_sync_unit_keys(
    repo: Path,
    branch: str,
    registry: dict[str, Any],
    remote_registry: dict[str, Any],
) -> set[tuple[str, str, str | None]]:
    changed: set[tuple[str, str, str | None]] = set()
    for raw_path in git.remote_changed_paths(repo, branch):
        parts = Path(raw_path).parts
        if len(parts) >= 2 and parts[0] == "skills":
            changed.add((parts[1], "base", None))
        elif len(parts) >= 3 and parts[0] == "variants":
            changed.add((parts[1], "variant", parts[2]))

    current_skills = registry.get("skills", {})
    remote_skills = remote_registry.get("skills", {})
    if not isinstance(current_skills, dict) or not isinstance(remote_skills, dict):
        raise SkillSyncError("registry skills must be a mapping")
    for name in set(current_skills) | set(remote_skills):
        current_targets = (
            set(registry_variant_targets(registry, name))
            if name in current_skills
            else set()
        )
        remote_targets = (
            set(registry_variant_targets(remote_registry, name))
            if name in remote_skills
            else set()
        )
        for target in current_targets ^ remote_targets:
            changed.add((name, "variant", target))
    return changed


def _sync_unit_rows(
    config: dict[str, Any],
    registry: dict[str, Any],
    targets: list[str],
    remote_changes: set[tuple[str, str, str | None]],
) -> list[dict[str, Any]]:
    repo = _repo_path(config)
    rows: list[dict[str, Any]] = []
    skills_config = config.get("skills", {})
    if not isinstance(skills_config, dict):
        raise SkillSyncError("config skills must be a mapping")
    skills_root = _global_skill_root(config)
    variants_root = skills_root.parent / "variants"

    for name in targets:
        entry = skills_config.get(name, {})
        if not isinstance(entry, dict):
            raise SkillSyncError(f"invalid local Skill config: {name}")
        local_base = _local_skill_path_or_default(config, registry, name)
        local_base_hash: str | None = None
        if local_base.exists() or is_link_or_reparse(local_base):
            _validate_skill_path(local_base)
            local_base_hash = hash_skill_dir(local_base)
        repo_base = repo / "skills" / name
        repo_base_hash = (
            hash_skill_dir(repo_base)
            if repo_base.is_dir() and not is_link_or_reparse(repo_base)
            else None
        )
        base_baseline = entry.get("last_installed_hash")
        if base_baseline is not None and not isinstance(base_baseline, str):
            raise SkillSyncError(f"invalid local Skill baseline: {name}")
        base_changed_local = (
            local_base_hash is not None
            and local_base_hash != (base_baseline or repo_base_hash)
        )
        base_changed_remote = (name, "base", None) in remote_changes
        rows.append(
            _sync_unit_row(
                skill=name,
                kind="base",
                target=None,
                local_hash=local_base_hash,
                repository_hash=repo_base_hash,
                baseline_hash=base_baseline,
                changed_local=base_changed_local,
                changed_remote=base_changed_remote,
            )
        )

        if registry.get("version") != 3:
            continue

        registry_skills = registry.get("skills", {})
        declared = (
            set(registry_variant_targets(registry, name))
            if registry.get("version") == 3
            and isinstance(registry_skills, dict)
            and name in registry_skills
            else set()
        )
        baselines = _variant_baselines(config, name)
        local_hashes = portable_sync.inspect_materialized_targets(
            variants_root / name
        )
        repo_hashes = portable_sync.inspect_materialized_targets(
            repo / "variants" / name
        )
        remote_targets = {
            target
            for skill, kind, target in remote_changes
            if skill == name and kind == "variant" and target is not None
        }
        variant_targets = sorted(
            declared
            | set(baselines)
            | set(local_hashes)
            | set(repo_hashes)
            | remote_targets
        )
        for target in variant_targets:
            local_hash = local_hashes.get(target)
            baseline = baselines.get(target) or repo_hashes.get(target)
            changed_local = local_hash is not None and local_hash != baseline
            changed_remote = (name, "variant", target) in remote_changes
            rows.append(
                _sync_unit_row(
                    skill=name,
                    kind="variant",
                    target=target,
                    local_hash=local_hash,
                    repository_hash=repo_hashes.get(target),
                    baseline_hash=baselines.get(target),
                    changed_local=changed_local,
                    changed_remote=changed_remote,
                )
            )
    return rows


def _sync_unit_row(
    *,
    skill: str,
    kind: str,
    target: str | None,
    local_hash: str | None,
    repository_hash: str | None,
    baseline_hash: str | None,
    changed_local: bool,
    changed_remote: bool,
) -> dict[str, Any]:
    if changed_local and changed_remote:
        state = "conflict"
    elif changed_local:
        state = "local-changed"
    elif changed_remote:
        state = "remote-changed"
    elif local_hash is None:
        state = "missing"
    else:
        state = "current"
    return {
        "id": f"{skill}:{kind}" + ("" if target is None else f":{target}"),
        "skill": skill,
        "kind": kind,
        "target": target,
        "local_hash": local_hash,
        "repository_hash": repository_hash,
        "baseline_hash": baseline_hash,
        "changed_local": changed_local,
        "changed_remote": changed_remote,
        "state": state,
    }


def _verify_preflight_local_units(
    config: dict[str, Any],
    registry: dict[str, Any],
    targets: list[str],
    preflight_units: list[dict[str, Any]],
) -> None:
    current = {
        row["id"]: row
        for row in _sync_unit_rows(config, registry, targets, set())
    }
    changed = [
        row["id"]
        for row in preflight_units
        if row["id"] in current
        and row["local_hash"] != current[row["id"]]["local_hash"]
    ]
    if changed:
        raise SkillSyncError(
            "local Base or Variant source changed during pull preflight",
            code="sync_source_changed",
            exit_code=EXIT_CONFLICT,
            details={"units": sorted(changed)},
        )


def _skill_status(
    config: dict[str, Any],
    registry: dict[str, Any],
    name: str,
    *,
    base_unit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    local_path = _local_skill_path(config, name)
    if base_unit is None:
        _validate_skill_path(local_path)
        local_hash = hash_skill_dir(local_path)
        repo = _repo_path(config)
        remote_path = repo / "skills" / name
        remote_hash = hash_skill_dir(remote_path) if remote_path.exists() else None
        baseline = config.get("skills", {}).get(name, {}).get("last_installed_hash")
        changed_local = local_hash != (baseline or remote_hash)
    else:
        local_hash = base_unit["local_hash"]
        remote_hash = base_unit["repository_hash"]
        changed_local = base_unit["changed_local"]
        if local_hash is None:
            raise SkillSyncError(f"local Skill is missing: {name}")
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


def _prepare_variant_push_plans(
    config: dict[str, Any],
    registry: dict[str, Any],
    targets: list[str],
    *,
    config_path: str | Path | None,
) -> dict[str, tuple[portable_sync.PortableVariantUnit, ...]]:
    if registry.get("version") != 3:
        return {}
    plans: dict[str, tuple[portable_sync.PortableVariantUnit, ...]] = {}
    for name in targets:
        declared = registry_variant_targets(registry, name)
        sources = variant_source.resolve_variant_source_paths(
            name,
            config_path=config_path,
        )
        local_source = _local_skill_path(config, name).absolute()
        if sources.base_root != local_source:
            raise SkillSyncError(
                f"selected Base path does not match the portable Variant root: {name}",
                code="portable_variant_source_mismatch",
                exit_code=EXIT_SAFETY,
                details={
                    "skill": name,
                    "selected_base": str(local_source),
                    "portable_base": str(sources.base_root),
                },
            )
        units = portable_sync.inspect_variant_units(
            skill=name,
            base_root=sources.base_root,
            variant_skill_root=sources.variant_skill_root,
            targets=declared,
        )
        variant_source.verify_variant_source_paths(sources)
        plans[name] = units
    return plans


def _prepare_variant_pull_plans(
    config: dict[str, Any],
    registry: dict[str, Any],
    targets: list[str],
    *,
    preserved_targets: dict[str, set[str]],
    preserved_baselines: dict[str, dict[str, str]],
    config_path: str | Path | None,
) -> dict[
    str,
    tuple[
        tuple[portable_sync.PortableVariantUnit, ...],
        Path,
        dict[str, str],
        set[str],
    ],
]:
    if registry.get("version") != 3:
        return {}
    repo = _repo_path(config)
    raw_skills_root = config.get("skills_root") or Path.home() / ".agents" / "skills"
    skills_root = Path(raw_skills_root).expanduser()
    if not skills_root.is_absolute():
        raise SkillSyncError(
            "configured skills_root must be an absolute path",
            code="variant_config_invalid",
            exit_code=EXIT_SAFETY,
        )
    variants_root = skills_root.absolute().parent / "variants"
    plans: dict[
        str,
        tuple[
            tuple[portable_sync.PortableVariantUnit, ...],
            Path,
            dict[str, str],
            set[str],
        ],
    ] = {}
    for name in targets:
        declared = registry_variant_targets(registry, name)
        remote_base = repo / "skills" / name
        if not remote_base.is_dir() or is_link_or_reparse(remote_base):
            if declared:
                raise SkillSyncError(
                    f"portable Base source is missing for registered Variants: {name}",
                    code="portable_variant_source_missing",
                    exit_code=EXIT_SAFETY,
                    details={"skill": name},
                )
            continue
        remote_units = portable_sync.inspect_variant_units(
            skill=name,
            base_root=remote_base,
            variant_skill_root=repo / "variants" / name,
            targets=declared,
        )
        destination = variants_root / name
        preserved = set(declared) & preserved_targets.get(name, set())
        local_units: tuple[portable_sync.PortableVariantUnit, ...] = ()
        if preserved:
            sources = variant_source.resolve_variant_source_paths(
                name,
                config_path=config_path,
            )
            local_units = portable_sync.inspect_variant_units(
                skill=name,
                base_root=sources.base_root,
                variant_skill_root=sources.variant_skill_root,
                targets=preserved,
            )
            variant_source.verify_variant_source_paths(sources)
        units_by_target = {unit.target: unit for unit in remote_units}
        units_by_target.update({unit.target: unit for unit in local_units})
        ordered_units = tuple(units_by_target[target] for target in declared)
        next_baselines = {
            unit.target: unit.content_hash
            for unit in remote_units
            if unit.target not in preserved
        }
        for target in preserved:
            baseline = preserved_baselines.get(name, {}).get(target)
            if baseline is None:
                raise SkillSyncError(
                    f"missing baseline for locally changed Variant source: {name}/{target}",
                    code="variant_baseline_invalid",
                    exit_code=EXIT_SAFETY,
                )
            next_baselines[target] = baseline
        plans[name] = (
            ordered_units,
            destination,
            next_baselines,
            preserved,
        )
    return plans


def _variant_baselines(config: dict[str, Any], name: str) -> dict[str, str]:
    entry = config.get("skills", {}).get(name, {})
    if not isinstance(entry, dict):
        raise SkillSyncError(f"invalid local Skill config: {name}")
    raw = entry.get("variant_baselines", {})
    if not isinstance(raw, dict) or not all(
        isinstance(target, str)
        and isinstance(hash_value, str)
        and hash_value.startswith("sha256:")
        for target, hash_value in raw.items()
    ):
        raise SkillSyncError(
            f"invalid local Variant baselines: {name}",
            code="variant_baseline_invalid",
            exit_code=EXIT_SAFETY,
        )
    return dict(raw)


def _set_variant_baselines(
    config: dict[str, Any],
    name: str,
    hashes: dict[str, str],
) -> None:
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise SkillSyncError("config skills must be a mapping")
    entry = skills.setdefault(name, {})
    if not isinstance(entry, dict):
        raise SkillSyncError(f"invalid local Skill config: {name}")
    if hashes:
        entry["variant_baselines"] = {
            target: hashes[target] for target in sorted(hashes)
        }
    else:
        entry.pop("variant_baselines", None)


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
    porcelain = git.run_git(repo, ["status", "--porcelain"], read_only=True)
    unexpected: list[str] = []
    for line in porcelain.splitlines():
        path_text = line[2:].strip() if len(line) > 2 else ""
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        if path_text != REGISTRY_FILE:
            unexpected.append(path_text or line)
    return unexpected


def deploy_preview(
    config_path: str | Path | None = None,
    *,
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Describe base deployment builds and link swaps without mutating state."""

    try:
        config = _load_local_config(config_path)
        registry = _load_local_registry(config)
        plan = _deployment_plan(config, registry, skill_names, agent_names)
        return {
            "rendered_root": str(_rendered_root(config)),
            "skills": plan,
            "blocked": any(
                client["action"] == "blocked"
                for skill in plan
                for client in skill["clients"]
            ),
        }
    except SkillSyncError:
        raise
    except (OSError, ValueError) as exc:
        raise SkillSyncError(str(exc)) from exc


def deploy_status(config_path: str | Path | None = None) -> dict[str, Any]:
    """Return current deployment and Agent link health for selected Skills."""

    preview = deploy_preview(config_path=config_path)
    skills: list[dict[str, Any]] = []
    for skill in preview["skills"]:
        clients = []
        for client in skill["clients"]:
            clients.append(
                {
                    "client": client["client"],
                    "agent": client["agent"],
                    "destination": client["destination"],
                    "deployment_path": client["deployment_path"],
                    "deployment_state": client["deployment_state"],
                    "link_state": client["current_state"],
                    "migration_required": client["action"] != "noop",
                }
            )
        skills.append(
            {
                "name": skill["name"],
                "source_path": skill["source_path"],
                "source_hash": skill["source_hash"],
                "clients": clients,
            }
        )
    operations = _deployment_receipt_health(_data_root(_load_local_config(config_path)))
    return {
        "rendered_root": preview["rendered_root"],
        "skills": skills,
        "operations": operations,
        "recovery_required": bool(operations),
    }


def deploy_migrate(
    config_path: str | Path | None = None,
    *,
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    _clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    try:
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            return _deploy_migrate_unlocked(
                config_path=config_path,
                skill_names=skill_names,
                agent_names=agent_names,
                _config=config,
                _clients=_clients,
            )
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc),
            code="deployment_lock_timeout",
            exit_code=EXIT_SAFETY,
        ) from exc


def _deploy_migrate_unlocked(
    config_path: str | Path | None = None,
    *,
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    _config: dict[str, Any] | None = None,
    _clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Build deployments, then transactionally swap owned Agent links."""

    config = _load_local_config(config_path) if _config is None else _config
    _assert_no_incomplete_deployment_receipts(_data_root(config))
    registry = _load_local_registry(config)
    clients = tuple(detect_clients() if _clients is None else _clients)
    initial = _deployment_plan(
        config, registry, skill_names, agent_names, clients=clients
    )
    blockers = [
        {"skill": skill["name"], **client}
        for skill in initial
        for client in skill["clients"]
        if client["action"] == "blocked"
    ]
    if blockers:
        raise SkillSyncError(
            "deployment migration is blocked by unsafe Agent paths",
            code="deployment_migration_blocked",
            exit_code=EXIT_SAFETY,
            details={"blockers": blockers},
        )

    rendered_root = _rendered_root(config)
    deployments: list[dict[str, Any]] = []
    for skill in initial:
        source = Path(skill["source_path"])
        for client in skill["clients"]:
            if client["deployment_state"] == "valid":
                continue
            spec = _client_deployment_spec(
                source,
                rendered_root,
                skill["name"],
                client["client"],
                source_hash=skill["source_hash"],
            )
            if spec.resolution_hash != client["resolution_hash"]:
                raise SkillSyncError(
                    "deployment inputs changed while preparing deployments",
                    code="deployment_source_changed",
                    exit_code=EXIT_CONFLICT,
                    details={
                        "skill": skill["name"],
                        "client": client["client"],
                    },
                )
            deployed = _render_client_deployment(
                spec,
                source,
                rendered_root,
                skill["name"],
                client["client"],
            )
            deployments.append(
                {
                    "skill": skill["name"],
                    "client": client["client"],
                    "path": str(deployed.path),
                    "created": deployed.created,
                }
            )

    prepared = _deployment_plan(
        config, registry, skill_names, agent_names, clients=clients
    )
    initial_hashes = {skill["name"]: skill["source_hash"] for skill in initial}
    changed_sources = [
        skill["name"]
        for skill in prepared
        if skill["source_hash"] != initial_hashes.get(skill["name"])
    ]
    initial_resolutions = {
        (skill["name"], client["client"]): client["resolution_hash"]
        for skill in initial
        for client in skill["clients"]
    }
    changed_resolutions = [
        {"skill": skill["name"], "client": client["client"]}
        for skill in prepared
        for client in skill["clients"]
        if client["resolution_hash"]
        != initial_resolutions.get((skill["name"], client["client"]))
    ]
    if changed_sources or changed_resolutions:
        raise SkillSyncError(
            "deployment sources changed while preparing deployments",
            code="deployment_source_changed",
            exit_code=EXIT_CONFLICT,
            details={
                "skills": changed_sources,
                "clients": changed_resolutions,
            },
        )
    unprepared = [
        {"skill": skill["name"], **client}
        for skill in prepared
        for client in skill["clients"]
        if client["action"] != "noop"
        and (
            client["deployment_state"] != "valid"
            or client["action"] not in {"create", "swap"}
        )
    ]
    if unprepared:
        raise SkillSyncError(
            "deployment state changed while preparing migration",
            code="deployment_state_changed",
            exit_code=EXIT_SAFETY,
            details={"clients": unprepared},
        )
    operation_id = uuid.uuid4().hex
    receipt_path = _data_root(config) / "operations" / f"deploy-migrate-{operation_id}.json"
    receipt = {
        "schema_version": 1,
        "operation_id": operation_id,
        "operation": "deploy-migrate",
        "status": "prepared",
        "created_at": time.time(),
        "completed": [],
        "links": [
            {
                "skill": skill["name"],
                "client": client["client"],
                "destination": client["destination"],
                "old_target": client.get("current_target"),
                "new_target": client["deployment_path"],
                "action": client["action"],
            }
            for skill in prepared
            for client in skill["clients"]
            if client["action"] != "noop"
        ],
    }
    _write_json_atomic(receipt_path, receipt)
    migrated: list[dict[str, Any]] = []
    swapped: list[tuple[Path, Path | None, Path]] = []
    try:
        for skill in prepared:
            for client in skill["clients"]:
                if client["action"] == "noop":
                    continue
                if client["action"] == "blocked":
                    raise SkillSyncError(
                        "deployment state changed while preparing migration",
                        code="deployment_state_changed",
                        exit_code=EXIT_SAFETY,
                        details={"skill": skill["name"], "client": client},
                    )
                destination = Path(client["destination"])
                new_target = Path(client["deployment_path"])
                old_target_text = client.get("current_target")
                old_target = Path(old_target_text) if old_target_text else None
                source = Path(skill["source_path"])
                if hash_skill_dir(source) != skill["source_hash"]:
                    raise SkillSyncError(
                        f"canonical Skill changed before link swap: {skill['name']}",
                        code="deployment_source_changed",
                        exit_code=EXIT_CONFLICT,
                        details={"skill": skill["name"]},
                    )
                current_spec = _client_deployment_spec(
                    source,
                    rendered_root,
                    skill["name"],
                    client["client"],
                    source_hash=skill["source_hash"],
                )
                if current_spec.resolution_hash != client["resolution_hash"]:
                    raise SkillSyncError(
                        "deployment inputs changed before link swap",
                        code="deployment_source_changed",
                        exit_code=EXIT_CONFLICT,
                        details={
                            "skill": skill["name"],
                            "client": client["client"],
                        },
                    )
                receipt["status"] = "applying"
                receipt["in_flight"] = str(destination)
                _write_json_atomic(receipt_path, receipt)
                replace_directory_link(
                    new_target,
                    destination,
                    allowed_current_sources=() if old_target is None else (old_target,),
                )
                # A failed replacement did not install ``new_target`` and
                # must not be rolled back as though it had. Record it only
                # after the swap succeeds, but before post-swap verification.
                swapped.append((destination, old_target, new_target))
                if link_state(new_target, destination) != "linked":
                    raise SkillSyncError(
                        f"deployment link verification failed: {destination}",
                        code="deployment_link_verification_failed",
                        exit_code=EXIT_SAFETY,
                    )
                migrated.append(
                    {
                        "skill": skill["name"],
                        "client": client["client"],
                        "from": client["current_state"],
                        "to": str(new_target),
                        "state": "linked-render",
                    }
                )
                receipt["completed"].append(str(destination))
                receipt.pop("in_flight", None)
                _write_json_atomic(receipt_path, receipt)
        changed_after_swaps: list[dict[str, str]] = []
        for skill in prepared:
            source = Path(skill["source_path"])
            if hash_skill_dir(source) != skill["source_hash"]:
                changed_after_swaps.append({"skill": skill["name"], "client": "base"})
                continue
            for client in skill["clients"]:
                current_spec = _client_deployment_spec(
                    source,
                    rendered_root,
                    skill["name"],
                    client["client"],
                    source_hash=skill["source_hash"],
                )
                if current_spec.resolution_hash != client["resolution_hash"]:
                    changed_after_swaps.append(
                        {"skill": skill["name"], "client": client["client"]}
                    )
        if changed_after_swaps:
            raise SkillSyncError(
                "deployment sources changed during link migration",
                code="deployment_source_changed",
                exit_code=EXIT_CONFLICT,
                details={"clients": changed_after_swaps},
            )
    except Exception as exc:
        rollback_errors: list[str] = []
        for destination, old_target, new_target in reversed(swapped):
            try:
                if old_target is None:
                    if link_state(new_target, destination) == "linked":
                        if not remove_directory_link(new_target, destination):
                            raise OSError(f"could not remove new link {destination}")
                    elif destination.exists() or destination.is_symlink():
                        raise OSError(
                            f"destination changed during rollback: {destination}"
                        )
                else:
                    if link_state(old_target, destination) != "linked":
                        replace_directory_link(
                            old_target,
                            destination,
                            allowed_current_sources=(new_target,),
                        )
            except Exception as rollback_exc:  # pragma: no cover - defensive safety path
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            receipt["status"] = "needs-recovery"
            receipt["error"] = str(exc)
            receipt["rollback_errors"] = rollback_errors
            _write_json_atomic(receipt_path, receipt)
            raise SkillSyncError(
                "deployment migration failed and rollback needs recovery",
                code="deployment_rollback_failed",
                exit_code=EXIT_SAFETY,
                details={"cause": str(exc), "rollback_errors": rollback_errors},
            ) from exc
        receipt["status"] = "rolled-back"
        receipt["error"] = str(exc)
        receipt["completed"] = []
        receipt.pop("in_flight", None)
        _write_json_atomic(receipt_path, receipt)
        if isinstance(exc, SkillSyncError):
            raise
        raise SkillSyncError(
            f"deployment migration failed; previous links were restored: {exc}",
            code="deployment_migration_failed",
            exit_code=EXIT_SAFETY,
        ) from exc

    receipt["status"] = "completed"
    receipt["completed_at"] = time.time()
    _write_json_atomic(receipt_path, receipt)
    return {
        "operation_id": operation_id,
        "receipt_path": str(receipt_path),
        "rendered_root": str(rendered_root),
        "migrated": migrated,
        "deployments": deployments,
        "noop": not migrated and not deployments,
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def deploy_gc(
    config_path: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    try:
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            return _deploy_gc_unlocked(
                config_path=config_path, dry_run=dry_run, _config=config
            )
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc),
            code="deployment_lock_timeout",
            exit_code=EXIT_SAFETY,
        ) from exc


def _deploy_gc_unlocked(
    config_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    _config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove only verified rendered deployments not referenced by a client."""

    config = _load_local_config(config_path) if _config is None else _config
    _assert_no_incomplete_deployment_receipts(_data_root(config))
    rendered_root = _rendered_root(config)
    references = _rendered_link_references(rendered_root, detect_clients())
    references.update(_operation_receipt_references(_data_root(config), rendered_root))
    removed: list[str] = []
    candidates: list[str] = []
    skipped: list[dict[str, str]] = []
    if not rendered_root.exists():
        return {
            "rendered_root": str(rendered_root),
            "dry_run": dry_run,
            "candidates": [],
            "removed": [],
            "skipped": [],
        }

    for digest_dir in sorted(rendered_root.iterdir(), key=lambda path: path.name):
        if not _valid_rendered_digest_dir(digest_dir):
            skipped.append({"path": str(digest_dir), "reason": "unknown-layout"})
            continue
        for deployment in sorted(digest_dir.iterdir(), key=lambda path: path.name):
            verification = verify_deployment(deployment)
            if not verification.ok:
                skipped.append(
                    {"path": str(deployment), "reason": verification.state}
                )
                continue
            if deployment.resolve(strict=False) in references:
                continue
            candidates.append(str(deployment))
            if dry_run:
                continue
            # Re-scan immediately before deletion so a link created after the
            # initial inventory cannot turn this into a referenced cache.
            current_references = _rendered_link_references(
                rendered_root, detect_clients()
            )
            current_references.update(
                _operation_receipt_references(_data_root(config), rendered_root)
            )
            if deployment.resolve(strict=False) in current_references:
                skipped.append({"path": str(deployment), "reason": "became-referenced"})
                continue
            remove_verified_deployment(
                deployment,
                rendered_root,
                _data_root(config) / "trash",
            )
            removed.append(str(deployment))
    return {
        "rendered_root": str(rendered_root),
        "dry_run": dry_run,
        "candidates": candidates,
        "removed": removed,
        "skipped": skipped,
    }


def _rendered_link_references(rendered_root: Path, clients: Iterable[Any]) -> set[Path]:
    references: set[Path] = set()
    for client in clients:
        # Detection describes whether a client application appears installed;
        # an existing known Skill root can still contain a live managed link.
        if not client.skills_dir.is_dir():
            continue
        for destination in client.skills_dir.iterdir():
            target = _directory_link_target(destination)
            if target is not None and _is_inside(target, rendered_root):
                deployment = _rendered_deployment_root(target, rendered_root)
                if deployment is not None:
                    references.add(deployment)
    return references


def _operation_receipt_references(data_root: Path, rendered_root: Path) -> set[Path]:
    references: set[Path] = set()
    _assert_no_malformed_deployment_receipts(data_root)
    operations = data_root / "operations"
    if not operations.is_dir():
        return references
    receipt_paths = list(operations.glob("deploy-migrate-*.json"))
    receipt_paths.extend(operations.glob("edit-recover-*.json"))
    for receipt_path in sorted(receipt_paths):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise SkillSyncError(
                f"cannot safely read deployment receipt: {receipt_path}",
                code="deployment_recovery_required",
                exit_code=EXIT_SAFETY,
                details={"receipt": str(receipt_path), "cause": str(exc)},
            ) from exc
        reference_statuses = {"prepared", "applying", "needs-recovery"}
        if receipt.get("operation") == "edit-recover":
            reference_statuses.add("cleanup-pending")
        if receipt.get("status") not in reference_statuses:
            continue
        for item in receipt.get("links", []):
            if not isinstance(item, dict):
                continue
            for key in ("old_target", "new_target"):
                value = item.get(key)
                if not isinstance(value, str):
                    continue
                target = Path(value).resolve(strict=False)
                if _is_inside(target, rendered_root):
                    deployment = _rendered_deployment_root(target, rendered_root)
                    if deployment is not None:
                        references.add(deployment)
        if receipt.get("operation") == "edit-recover":
            for key in ("deployment_path", "quarantine_path", "recovery_path"):
                value = receipt.get(key)
                if not isinstance(value, str):
                    continue
                deployment = _rendered_deployment_root(
                    Path(value).resolve(strict=False), rendered_root
                )
                if deployment is not None:
                    references.add(deployment)
    return references


def _rendered_deployment_root(target: Path, rendered_root: Path) -> Path | None:
    """Normalize a rendered target (including descendants) to its deployment."""

    resolved_root = rendered_root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        relative = resolved_target.relative_to(resolved_root)
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    digest_dir = resolved_root / relative.parts[0]
    if not _valid_rendered_digest_dir(digest_dir):
        return None
    return (digest_dir / relative.parts[1]).resolve(strict=False)


_ACTIVE_DEPLOYMENT_RECEIPT_STATES = {"prepared", "applying", "needs-recovery"}
_TERMINAL_DEPLOYMENT_RECEIPT_STATES = {
    "completed",
    "rolled-back",
    "cleanup-pending",
}


def _deployment_receipt_health(data_root: Path) -> list[dict[str, Any]]:
    """Return receipts that cannot be proven safely completed or rolled back."""

    operations = data_root / "operations"
    if not operations.is_dir():
        return []
    recovery: list[dict[str, Any]] = []
    receipt_paths = list(operations.glob("deploy-migrate-*.json"))
    receipt_paths.extend(operations.glob("edit-recover-*.json"))
    for receipt_path in sorted(receipt_paths):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            recovery.append(
                {
                    "path": str(receipt_path),
                    "status": "malformed",
                    "error": str(exc),
                    "recovery": "Inspect this receipt and affected Agent links before moving or removing it.",
                }
            )
            continue
        if not isinstance(receipt, dict):
            recovery.append(
                {
                    "path": str(receipt_path),
                    "status": "malformed",
                    "error": "receipt root must be an object",
                    "recovery": "Inspect this receipt and affected Agent links before moving or removing it.",
                }
            )
            continue
        if receipt_path.name.startswith("edit-recover-"):
            invalid_reason = _invalid_edit_recovery_receipt_reason(
                receipt_path, receipt
            )
            if invalid_reason is not None:
                recovery.append(
                    {
                        "path": str(receipt_path),
                        "status": "malformed",
                        "error": invalid_reason,
                        "recovery": "Inspect this recovery receipt and its recorded artifacts before continuing.",
                    }
                )
                continue
        status = receipt.get("status")
        if status in _TERMINAL_DEPLOYMENT_RECEIPT_STATES:
            continue
        if status not in _ACTIVE_DEPLOYMENT_RECEIPT_STATES:
            recovery.append(
                {
                    "path": str(receipt_path),
                    "status": "malformed",
                    "error": f"unknown or missing receipt status: {status!r}",
                    "recovery": "Inspect this receipt and affected Agent links before moving or removing it.",
                }
            )
            continue
        item: dict[str, Any] = {
            "path": str(receipt_path),
            "operation_id": receipt.get("operation_id"),
            "status": status,
            "recovery": "Verify every recorded Agent link, then complete or roll back this operation.",
        }
        if isinstance(receipt.get("in_flight"), str):
            item["in_flight"] = receipt["in_flight"]
        if isinstance(receipt.get("error"), str):
            item["error"] = receipt["error"]
        if isinstance(receipt.get("rollback_errors"), list):
            item["rollback_errors"] = receipt["rollback_errors"]
        recovery.append(item)
    return recovery


def _invalid_edit_recovery_receipt_reason(
    receipt_path: Path, receipt: dict[str, Any]
) -> str | None:
    """Return why an edit-recover receipt cannot be trusted, if applicable."""

    operation_id = receipt.get("operation_id")
    try:
        parsed_id = str(uuid.UUID(operation_id)) if isinstance(operation_id, str) else None
    except ValueError:
        parsed_id = None
    expected_name = (
        None if parsed_id is None else f"edit-recover-{parsed_id}.json"
    )
    required_strings = (
        "skill",
        "client",
        "phase",
        "canonical_path",
        "canonical_hash",
        "deployment_path",
        "observed_hash",
        "tampered_authored_hash",
    )
    if receipt.get("schema_version") != 1:
        return "invalid or missing schema_version"
    if receipt.get("operation") != "edit-recover":
        return "invalid or missing operation"
    if expected_name != receipt_path.name:
        return "operation_id does not match receipt filename"
    if receipt.get("action") not in {"capture", "discard"}:
        return "invalid or missing recovery action"
    if any(not isinstance(receipt.get(key), str) or not receipt[key] for key in required_strings):
        return "missing required recovery receipt fields"
    if not all(
        receipt[key].startswith("sha256:") and len(receipt[key]) == 71
        for key in ("canonical_hash", "observed_hash", "tampered_authored_hash")
    ):
        return "invalid recovery receipt hash"
    if not all(
        Path(receipt[key]).is_absolute()
        for key in ("canonical_path", "deployment_path")
    ):
        return "recovery receipt paths must be absolute"
    if not isinstance(receipt.get("created_at"), (int, float)):
        return "invalid or missing created_at"
    status = receipt.get("status")
    allowed_statuses = (
        _ACTIVE_DEPLOYMENT_RECEIPT_STATES
        | _TERMINAL_DEPLOYMENT_RECEIPT_STATES
    )
    if status not in allowed_statuses:
        return f"unknown or missing receipt status: {status!r}"
    valid_phases = {
        "prepared": {"prepared"},
        "applying": (
            {"snapshotting"}
            if receipt["action"] == "capture"
            else {"quarantining", "rebuilding"}
        ),
        "completed": {"completed"},
        "rolled-back": {"rolled-back"},
        "needs-recovery": {"needs-recovery"},
        "cleanup-pending": {"cleanup-pending"},
    }
    if receipt["phase"] not in valid_phases[status]:
        return f"receipt phase {receipt['phase']!r} does not match status {status!r}"
    if receipt["action"] == "capture":
        if not isinstance(receipt.get("snapshot_path"), str) or not Path(
            receipt["snapshot_path"]
        ).is_absolute():
            return "capture receipt has no absolute snapshot_path"
        if status in {"completed", "cleanup-pending", "needs-recovery"} and not isinstance(
            receipt.get("session_id"), str
        ):
            return "capture receipt has no session_id"
    else:
        if not isinstance(receipt.get("quarantine_path"), str) or not Path(
            receipt["quarantine_path"]
        ).is_absolute():
            return "discard receipt has no absolute quarantine_path"
    if status in _TERMINAL_DEPLOYMENT_RECEIPT_STATES | {"needs-recovery"}:
        if not isinstance(receipt.get("completed_at"), (int, float)):
            return "terminal recovery receipt has no completed_at"
    if status == "needs-recovery" and not isinstance(
        receipt.get("recovery_path"), str
    ):
        return "needs-recovery receipt has no recovery_path"
    if status == "cleanup-pending" and not (
        isinstance(receipt.get("cleanup_pending"), list)
        and all(isinstance(item, str) for item in receipt["cleanup_pending"])
    ):
        return "cleanup-pending receipt has invalid cleanup paths"
    return None


def _assert_no_malformed_deployment_receipts(data_root: Path) -> None:
    malformed = [
        receipt
        for receipt in _deployment_receipt_health(data_root)
        if receipt["status"] == "malformed"
    ]
    if malformed:
        raise SkillSyncError(
            "deployment receipts are malformed; refusing unsafe cache cleanup",
            code="deployment_recovery_required",
            exit_code=EXIT_SAFETY,
            details={"operations": malformed},
        )


def _assert_no_incomplete_deployment_receipts(data_root: Path) -> None:
    recovery = _deployment_receipt_health(data_root)
    if recovery:
        raise SkillSyncError(
            "an incomplete deployment operation requires recovery",
            code="deployment_recovery_required",
            exit_code=EXIT_SAFETY,
            details={"operations": recovery},
        )


def _fsync_directory(directory: Path) -> None:
    """Persist an atomic rename in its parent directory where supported."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        if os.name == "nt":  # Windows does not consistently open directories.
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _valid_rendered_digest_dir(path: Path) -> bool:
    name = path.name
    digest = name.removeprefix("sha256-")
    return (
        path.is_dir()
        and not path.is_symlink()
        and name.startswith("sha256-")
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _deployment_plan(
    config: dict[str, Any],
    registry: dict[str, Any],
    skill_names: Iterable[str] | None,
    agent_names: Iterable[str] | None,
    *,
    clients: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    targets = _target_names(registry, skill_names)
    requested = set(agent_names or ())
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    known = {client.id for client in client_snapshot} | {
        client.family_id for client in client_snapshot
    }
    unknown = requested - known
    if unknown:
        raise SkillSyncError("unknown Agent or client: " + ", ".join(sorted(unknown)))
    disabled = _disabled_agents(config)
    available = [
        client
        for client in client_snapshot
        if client.detected
        and client.family_id not in disabled
        and (not requested or client.id in requested or client.family_id in requested)
    ]
    rendered_root = _rendered_root(config)
    result: list[dict[str, Any]] = []
    for name in targets:
        source = _local_skill_path_or_default(config, registry, name)
        _validate_skill_path(source)
        source_hash = hash_skill_dir(source)
        entry = registry.get("skills", {}).get(name, {})
        configured = _configured_client_targets(entry)
        rows: list[dict[str, Any]] = []
        for client in available:
            if configured and not {client.id, client.family_id}.intersection(configured):
                continue
            try:
                spec = _client_deployment_spec(
                    source,
                    rendered_root,
                    name,
                    client.id,
                    source_hash=source_hash,
                )
            except (OSError, ValueError) as exc:
                raise SkillSyncError(
                    f"could not resolve deployment for {name}/{client.id}: {exc}",
                    code="variant_resolution_invalid",
                    exit_code=EXIT_SAFETY,
                    details={"skill": name, "client": client.id},
                ) from exc
            desired = spec.path
            verification = verify_deployment(
                desired,
                expected_provenance=spec.expected_provenance,
            )
            destination = client.skills_dir / name
            if "platform" in config and _same_path_location(source, destination):
                # Legacy platform-mode installs may use the Agent directory as
                # their authored source.  They must be imported into the
                # global root before a read-only deployment can replace them.
                continue
            current_state, current_target = _deployment_link_state(
                source,
                destination,
                desired,
                rendered_root,
                name,
                client.id,
            )
            action = _deployment_action(current_state, verification.state)
            reason = (
                _deployment_block_reason(current_state, verification.state)
                if action == "blocked"
                else verification.reason
            )
            rows.append(
                {
                    "client": client.id,
                    "agent": client.family_id,
                    "destination": str(destination),
                    "deployment_path": str(desired),
                    "resolution_hash": spec.resolution_hash,
                    "resolver_version": spec.expected_provenance["resolver_version"],
                    "applied_layers": spec.expected_provenance["applied_layers"],
                    "deployment_state": verification.state,
                    "current_state": current_state,
                    "current_target": None if current_target is None else str(current_target),
                    "action": action,
                    "reason": reason,
                }
            )
        result.append(
            {
                "name": name,
                "source_path": str(source),
                "source_hash": source_hash,
                "clients": rows,
            }
        )
    return result


def _client_deployment_spec(
    source: Path,
    rendered_root: Path,
    skill_name: str,
    client_id: str,
    *,
    source_hash: str | None = None,
    variant_skill_root: Path | None = None,
) -> _ClientDeploymentSpec:
    """Resolve the deployment format without migrating Base-only clients."""

    canonical_hash = hash_skill_dir(source) if source_hash is None else source_hash
    variants = (
        source.parent.parent / "variants" / skill_name
        if variant_skill_root is None
        else variant_skill_root
    )
    layered = resolve_variant_for_client(source, variants, client_id)
    if layered.applied_variant_targets:
        expected = expected_layered_provenance(skill_name, layered)
        return _ClientDeploymentSpec(
            path=deployment_path(rendered_root, skill_name, layered.resolution_hash),
            resolution_hash=layered.resolution_hash,
            expected_provenance=expected,
            layered_resolution=layered,
        )
    resolved_hash = resolution_hash(skill_name, canonical_hash, client_id)
    return _ClientDeploymentSpec(
        path=deployment_path(rendered_root, skill_name, resolved_hash),
        resolution_hash=resolved_hash,
        expected_provenance=expected_provenance(
            skill_name,
            canonical_hash,
            client_id,
        ),
        layered_resolution=None,
    )


def _render_client_deployment(
    spec: _ClientDeploymentSpec,
    source: Path,
    rendered_root: Path,
    skill_name: str,
    client_id: str,
):
    if spec.layered_resolution is not None:
        return render_layered_deployment(
            spec.layered_resolution,
            rendered_root,
            skill_name,
        )
    return render_base_deployment(source, rendered_root, skill_name, client_id)


def _deployment_link_state(
    source: Path,
    destination: Path,
    desired: Path,
    rendered_root: Path,
    skill_name: str,
    client_id: str,
) -> tuple[str, Path | None]:
    if link_state(source, destination) == "linked":
        # Preserve the immediate symlink target. Some installations chain one
        # Agent through another (Claude -> Codex -> canonical); resolving the
        # chain would make ownership change when Codex migrates first.
        return "direct-source-link", _directory_link_target_lexical(destination) or source
    if link_state(desired, destination) == "linked":
        verification = verify_deployment(desired)
        return (
            "linked-render" if verification.ok else f"{verification.state}-render",
            desired,
        )
    if (
        not destination.exists()
        and not destination.is_symlink()
        and not is_link_or_reparse(destination)
    ):
        return "missing", None

    target = _directory_link_target(destination)
    if target is not None and _is_inside(target, rendered_root):
        verification = verify_deployment(target)
        provenance = verification.provenance or {}
        if verification.state == "missing":
            return "missing-render", target
        if not verification.ok:
            return "tampered-render", target
        if (
            provenance.get("logical_skill") == skill_name
            and provenance.get("target_client") == client_id
        ):
            return "stale-render", target
        return "wrong-link", target
    physical = link_state(source, destination)
    return physical, target


def _deployment_action(current_state: str, deployment_state: str) -> str:
    if current_state == "linked-render" and deployment_state == "valid":
        return "noop"
    if current_state in {"tampered-render", "missing-render", "wrong-link", "broken-link", "conflict"}:
        return "blocked"
    if deployment_state == "tampered":
        return "blocked"
    if current_state == "missing":
        return "create" if deployment_state == "valid" else "build-and-create"
    if current_state in {"direct-source-link", "stale-render"}:
        return "swap" if deployment_state == "valid" else "build-and-swap"
    return "blocked"


def _deployment_block_reason(current_state: str, deployment_state: str) -> str:
    current_reasons = {
        "conflict": "Agent destination contains unmanaged content.",
        "wrong-link": "Agent link points to a different location.",
        "broken-link": "Agent link target is missing.",
        "tampered-render": "The active managed deployment was modified.",
        "missing-render": "The active managed deployment is missing.",
    }
    if current_state in current_reasons:
        return current_reasons[current_state]
    if deployment_state == "tampered":
        return "The desired managed deployment was modified."
    return "Agent destination is not safely replaceable."


def _directory_link_target(destination: Path) -> Path | None:
    try:
        if destination.is_symlink():
            raw = Path(os.readlink(destination))
            return (destination.parent / raw).resolve(strict=False) if not raw.is_absolute() else raw.resolve(strict=False)
        if os.name == "nt" and destination.exists():
            return destination.resolve(strict=True)
    except OSError:
        return None
    return None


def _directory_link_target_lexical(destination: Path) -> Path | None:
    """Return a symlink's immediate absolute target without resolving chains."""
    try:
        if destination.is_symlink():
            raw = Path(os.readlink(destination))
            return raw if raw.is_absolute() else (destination.parent / raw).absolute()
    except OSError:
        return None
    return None


def _same_path_location(left: Path, right: Path) -> bool:
    left_location = left.parent.resolve(strict=False) / left.name
    right_location = right.parent.resolve(strict=False) / right.name
    return os.path.normcase(str(left_location)) == os.path.normcase(
        str(right_location)
    )


def link_skills(
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Render selected Skills and link detected Agent clients to deployments."""
    config = _load_local_config(config_path)
    clients = tuple(detect_clients())
    normalized = _resolve_link_input(
        skill_names, agent_names, config_path, clients=clients
    )
    targets = normalized["skills"]
    agents = normalized["agents"]
    registry = _load_local_registry(config)
    initial_plan = _deployment_plan(
        config, registry, targets, agents, clients=clients
    )
    conflicts, blockers = _link_safety_findings(
        config, normalized, initial_plan, clients
    )
    _raise_preflight_findings(
        "link-repair", conflicts=conflicts, blockers=blockers
    )
    deploy_migrate(
        config_path=config_path,
        skill_names=targets,
        agent_names=agents,
        _clients=clients,
    )
    plan = _deployment_plan(
        config, registry, targets, agents, clients=clients
    )
    results: list[dict[str, str]] = []
    for skill in plan:
        for client in skill["clients"]:
            results.append(
                {
                    "skill": skill["name"],
                    "agent": client["agent"],
                    "client": client["client"],
                    "state": client["current_state"],
                    "method": "rendered",
                    "path": client["destination"],
                }
            )
    return {"links": results}


def unlink_skills(
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    try:
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            return _unlink_skills_unlocked(
                skill_names, agent_names, config_path, _config=config
            )
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="deployment_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc


def _unlink_skills_unlocked(
    skill_names: Iterable[str] | None = None,
    agent_names: Iterable[str] | None = None,
    config_path: str | Path | None = None,
    *,
    _config: dict[str, Any] | None = None,
    _clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path) if _config is None else _config
    _assert_no_incomplete_deployment_receipts(_data_root(config))
    registry = _load_local_registry(config)
    targets = _target_names(registry, skill_names)
    requested = set(agent_names or ())
    clients = tuple(detect_clients() if _clients is None else _clients)
    known = {client.id for client in clients} | {client.family_id for client in clients}
    unknown = requested - known
    if unknown:
        raise SkillSyncError("unknown Agent or client: " + ", ".join(sorted(unknown)))
    removed: list[dict[str, str]] = []
    for client in clients:
        if not client.skills_dir.is_dir():
            continue
        if requested and client.id not in requested and client.family_id not in requested:
            continue
        for name in targets:
            source = _local_skill_path_or_default(config, registry, name)
            entry = registry.get("skills", {}).get(name, {})
            configured = _configured_client_targets(entry)
            if configured and not {client.id, client.family_id}.intersection(configured):
                continue
            destination = client.skills_dir / name
            owned_target = _owned_link_target(
                source,
                destination,
                _rendered_root(config),
                name,
                client.id,
            )
            if owned_target is None:
                continue
            if remove_directory_link(owned_target, destination):
                removed.append(
                    {
                        "skill": name,
                        "agent": client.family_id,
                        "client": client.id,
                        "path": str(destination),
                    }
                )
    return {"unlinked": removed}


def _owned_link_target(
    source: Path,
    destination: Path,
    rendered_root: Path,
    skill_name: str,
    client_id: str,
) -> Path | None:
    if link_state(source, destination) == "linked":
        return source
    target = _directory_link_target(destination)
    if target is None or not _is_inside(target, rendered_root):
        return None
    verification = verify_deployment(target)
    provenance = verification.provenance or {}
    if provenance.get("logical_skill") != skill_name or provenance.get(
        "target_client"
    ) != client_id:
        return None
    return target


def _imported_deployment_target(
    destination: Path,
    rendered_root: Path,
    skill_name: str,
    expected_client_ids: set[str],
) -> Path | None:
    """Return a verified rendered target installed during an import.

    Import accepts an Agent family while provenance records a concrete client
    (for example ``kimi`` versus ``kimi-code``), so rollback verifies the
    rendered deployment and logical Skill instead of guessing a client id.
    Real directories and links outside the deployment store never qualify.
    """

    target = _directory_link_target(destination)
    if target is None or not _is_inside(target, rendered_root):
        return None
    verification = verify_deployment(target)
    provenance = verification.provenance or {}
    if (
        verification.state != "valid"
        or provenance.get("logical_skill") != skill_name
        or provenance.get("target_client") not in expected_client_ids
    ):
        return None
    return target


def _import_client_ids(
    agent_name: str,
    skills_dir: Path,
    *,
    clients: Iterable[Any] | None = None,
) -> set[str]:
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    client_ids = {
        client.id
        for client in client_snapshot
        if client.family_id == agent_name
        and _same_path_location(client.skills_dir, skills_dir)
    }
    if client_ids:
        return client_ids
    fallback = {
        "claude": "claude-code",
        "codex": "codex",
        "workbuddy": "workbuddy",
    }.get(agent_name)
    return {fallback} if fallback else set()


def _path_identity(path: Path) -> tuple[int, int, int]:
    metadata = os.lstat(path)
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _quarantine_and_remove_owned_directory(
    path: Path,
    expected_identity: tuple[int, int, int],
    trash_root: Path,
) -> bool:
    """Remove an owned directory only after isolating its verified inode.

    The live canonical path is never passed to ``rmtree``.  A concurrent path
    winner therefore remains at the live location, while deletion happens
    under a private machine-local trash directory.
    """

    try:
        if is_link_or_reparse(path) or _path_identity(path) != expected_identity:
            return False
    except OSError:
        return False
    private_trash = trash_root / "owned-removals"
    if is_link_or_reparse(trash_root) or is_link_or_reparse(private_trash):
        return False
    private_trash.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_trash.chmod(0o700)
    quarantine = private_trash / f"{path.name}-{uuid.uuid4().hex}"
    try:
        rename_no_replace(path, quarantine)
    except (FileExistsError, FileNotFoundError, OSError):
        return False
    try:
        if is_link_or_reparse(quarantine) or _path_identity(
            quarantine
        ) != expected_identity:
            if not path.exists() and not is_link_or_reparse(path):
                try:
                    rename_no_replace(quarantine, path)
                except (FileExistsError, OSError):
                    pass
            return False
        shutil.rmtree(quarantine)
        return True
    except OSError:
        return False


def doctor(
    config_path: str | Path | None = None,
    *,
    clients: Iterable[Any] | None = None,
    _sync_units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    registry = _load_local_registry(config)
    if _sync_units is None:
        repo = _repo_path(config)
        branch = _branch(config)
        git_state = git.state(repo, branch, fetch_remote=False)
        remote_registry = (
            _cached_remote_registry(repo, branch)
            if git_state.behind > 0
            else registry
        )
        remote_changes = (
            _remote_sync_unit_keys(repo, branch, registry, remote_registry)
            if git_state.behind > 0
            else set()
        )
        sync_units = _sync_unit_rows(
            config,
            registry,
            sorted(_selected_names(registry)),
            remote_changes,
        )
    else:
        sync_units = _sync_units
    client_snapshot = tuple(detect_clients() if clients is None else clients)
    agents = aggregate_agent_targets(client_snapshot)
    disabled_agents = _disabled_agents(config)
    issues: list[dict[str, str]] = []
    deployment_operations = _deployment_receipt_health(_data_root(config))
    for operation in deployment_operations:
        issue = {
            "type": "deployment-recovery-required",
            "path": str(operation["path"]),
            "status": str(operation["status"]),
            "detail": str(operation["recovery"]),
        }
        if "in_flight" in operation:
            issue["in_flight"] = str(operation["in_flight"])
        issues.append(issue)
    matrix: list[dict[str, str]] = []
    client_matrix: list[dict[str, str]] = []
    deployment_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for selected_name in sorted(_selected_names(registry)):
        selected_source = _local_skill_path_or_default(
            config, registry, selected_name
        )
        if not (selected_source / "SKILL.md").is_file():
            continue
        for planned_skill in _deployment_plan(
            config, registry, [selected_name], None, clients=client_snapshot
        ):
            for row in planned_skill["clients"]:
                deployment_rows[(selected_name, row["client"])] = row
    for name in sorted(_selected_names(registry)):
        source = _local_skill_path_or_default(config, registry, name)
        if not (source / "SKILL.md").is_file():
            issues.append({"type": "missing-skill", "skill": name, "path": str(source)})
            continue
        client_states: dict[str, str] = {}
        for client in client_snapshot:
            if not client.detected:
                continue
            row = deployment_rows.get((name, client.id))
            if client.family_id in disabled_agents:
                state = "disabled"
            elif row is None:
                continue
            else:
                state = row["current_state"]
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
                for client in client_snapshot
                if client.detected
                and client.family_id == agent.name
                and client.id in client_states
            ]
            state = _combined_link_state(states)
            matrix.append({"skill": name, "agent": agent.name, "state": state})
            if state not in {"linked", "missing", "copied", "disabled"}:
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
            for client in client_snapshot
        ],
        "matrix": matrix,
        "client_matrix": client_matrix,
        "deployment_operations": deployment_operations,
        "recovery_required": bool(deployment_operations),
        "sync_units": sync_units,
        "issues": issues,
    }


def disable_agent_sync(
    agent_name: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Persistently disable an Agent target and remove its managed links."""
    clients = tuple(detect_clients())
    resolved = _resolve_agent_toggle_input(
        agent_name, False, config_path, clients=clients
    )
    agent_name = resolved["agent"]
    config_file = _config_path(config_path)
    config = load_config(config_file)
    _raise_preflight_findings(
        "agent-disable",
        blockers=_agent_safety_blockers(config, enabled=False),
    )
    try:
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            _assert_no_incomplete_deployment_receipts(_data_root(config))
            previous_disabled = list(config.get("disabled_agents", []))
            disabled_agents = _disabled_agents(config)
            disabled_agents.add(agent_name)
            config["disabled_agents"] = sorted(disabled_agents)
            save_config(config_file, config)
            try:
                removed = _unlink_skills_unlocked(
                    agent_names=[agent_name],
                    config_path=config_file,
                    _config=config,
                    _clients=clients,
                )
            except Exception as exc:
                config["disabled_agents"] = previous_disabled
                save_config(config_file, config)
                try:
                    deploy_migrate(
                        config_path=config_file,
                        agent_names=[agent_name],
                        _clients=clients,
                    )
                except Exception as rollback_exc:
                    raise SkillSyncError(
                        "Agent disable failed and requires link recovery",
                        code="disable_rollback_needs_recovery",
                        exit_code=EXIT_SAFETY,
                        details={
                            "cause": str(exc),
                            "rollback_error": str(rollback_exc),
                        },
                    ) from exc
                raise
            return {"disabled": agent_name, **removed}
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="deployment_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc


def enable_agent_sync(
    agent_name: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Re-enable an Agent target without creating links automatically."""
    clients = tuple(detect_clients())
    resolved = _resolve_agent_toggle_input(
        agent_name, True, config_path, clients=clients
    )
    agent_name = resolved["agent"]
    config_file = _config_path(config_path)
    config = load_config(config_file)
    config["disabled_agents"] = sorted(_disabled_agents(config) - {agent_name})
    save_config(config_file, config)
    return {"enabled": agent_name}


def _disabled_agents(config: dict[str, Any]) -> set[str]:
    value = config.get("disabled_agents", [])
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise SkillSyncError("configured disabled_agents must be a list of strings")
    return {_normalize_agent_name(name) for name in value}


def _configured_client_targets(entry: Any) -> set[str]:
    if not isinstance(entry, dict):
        return set()
    raw_targets = str(entry.get("targets", DEFAULT_AGENT_TARGETS)).split(",")
    return {name.strip() for name in raw_targets if name.strip()}


def _normalize_agent_name(name: str) -> str:
    if name == "kimi-code":
        return "kimi"
    return name


def _combined_link_state(states: list[str]) -> str:
    if not states:
        return "missing"
    unique = set(states)
    if len(unique) == 1:
        return "linked" if states[0] == "linked-render" else states[0]
    if unique <= {"direct-source-link", "missing"}:
        return "partial"
    if unique <= {"linked", "linked-render", "missing"}:
        return "partial"
    if unique <= {"linked", "linked-render", "copied"}:
        return "copied"
    priority = (
        "tampered-render",
        "missing-render",
        "stale-render",
        "wrong-link",
        "broken-link",
        "conflict",
        "direct-source-link",
    )
    return next((state for state in priority if state in unique), states[0])


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
            if is_link_or_reparse(source):
                continue
            if not source.is_dir() or not (source / "SKILL.md").is_file():
                continue
            _validate_skill_path(source)
            destination = global_root / source.name
            state = "importable"
            if destination.exists() or is_link_or_reparse(destination):
                _validate_skill_path(destination)
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
    clients = tuple(detect_clients())
    resolved = _resolve_import_input(
        names, agent_name, config_path, clients=clients
    )
    conflicts, blockers = _import_safety_findings(resolved)
    _raise_preflight_findings(
        "import", conflicts=conflicts, blockers=blockers
    )
    names = resolved["skills"]
    agent_name = resolved["agent"]
    config = _load_local_config(config_path)
    try:
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            return _import_agent_skills_unlocked(
                names,
                agent_name,
                config_path,
                _config=config,
                _clients=clients,
            )
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="deployment_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc


def _import_agent_skills_unlocked(
    names: Iterable[str],
    agent_name: str,
    config_path: str | Path | None = None,
    *,
    _config: dict[str, Any] | None = None,
    _clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Move Agent-local Skills into the global root and link a deployment."""
    name_list = list(names)
    if not name_list:
        raise SkillSyncError("import requires at least one Skill name")
    config = _load_local_config(config_path) if _config is None else _config
    _assert_no_incomplete_deployment_receipts(_data_root(config))
    clients = tuple(detect_clients() if _clients is None else _clients)
    matching_clients = tuple(
        client
        for client in clients
        if client.family_id == agent_name and client.detected
    )
    if not any(client.family_id == agent_name for client in clients):
        raise SkillSyncError(f"unknown Agent: {agent_name}")
    if not matching_clients:
        raise SkillSyncError(f"Agent is not detected: {agent_name}")
    if len(matching_clients) != 1:
        raise SkillSyncError(
            f"import requires one concrete source client: {agent_name}"
        )
    source_client = matching_clients[0]
    global_root = _global_skill_root(config)
    expected_client_ids = _import_client_ids(
        agent_name, source_client.skills_dir, clients=clients
    )
    registry = _load_local_registry(config)
    initially_selected = _selected_names(registry)
    imported: list[dict[str, str]] = []
    for name in name_list:
        if Path(name).name != name or name in {".", ".."}:
            raise SkillSyncError(f"invalid Skill name: {name}")
        source = source_client.skills_dir / name
        destination = global_root / name
        installed_target = _imported_deployment_target(
            source,
            _rendered_root(config),
            name,
            expected_client_ids,
        )
        if installed_target is not None:
            imported.append({"name": name, "agent": agent_name, "state": "already-linked"})
            continue
        if is_link_or_reparse(source):
            raise SkillSyncError(
                f"{name} link is not a verified deployment for {agent_name}"
            )
        _validate_skill_path(source)
        destination_existed = destination.exists() or is_link_or_reparse(destination)
        created_identity: tuple[int, int, int] | None = None
        created_hash: str | None = None
        if destination_existed:
            _validate_skill_path(destination)
            if hash_skill_dir(source) != hash_skill_dir(destination):
                raise SkillSyncError(f"global Skill has different content: {name}")
        else:
            created_hash = copy_skill_dir(source, destination)
            created_identity = _path_identity(destination)
        backup = source.parent / f".{name}.skill-sync-import-{time.time_ns()}"
        try:
            rename_no_replace(source, backup)
            select_skills([name], platform=None, config_path=config_path)
            deploy_migrate(
                config_path=config_path,
                skill_names=[name],
                agent_names=[agent_name],
                _clients=clients,
            )
            if _imported_deployment_target(
                source, _rendered_root(config), name, expected_client_ids
            ) is None:
                raise SkillSyncError(f"failed to verify imported deployment link: {name}")
        except Exception as exc:
            recovery_errors: list[str] = []
            owned_target = _imported_deployment_target(
                source, _rendered_root(config), name, expected_client_ids
            )
            if owned_target is not None:
                if not remove_directory_link(owned_target, source):
                    recovery_errors.append(f"could not remove imported link: {source}")
            if backup.exists() or is_link_or_reparse(backup):
                try:
                    rename_no_replace(backup, source)
                except (FileExistsError, OSError):
                    recovery_errors.append(
                        f"Agent path changed; original preserved at {backup}"
                    )
            if name not in initially_selected:
                try:
                    deselect_skills([name], config_path=config_path)
                except Exception as rollback_exc:
                    recovery_errors.append(
                        f"could not restore selection state: {rollback_exc}"
                    )
            if not destination_existed:
                try:
                    unchanged = (
                        created_identity is not None
                        and created_hash is not None
                        and not is_link_or_reparse(destination)
                        and destination.is_dir()
                        and _path_identity(destination) == created_identity
                        and hash_skill_dir(destination) == created_hash
                    )
                except (OSError, ValueError) as rollback_exc:
                    unchanged = False
                    recovery_errors.append(
                        f"could not verify newly created global Skill: {rollback_exc}"
                    )
                if unchanged:
                    if not _quarantine_and_remove_owned_directory(
                        destination, created_identity, _data_root(config) / "trash"
                    ):
                        recovery_errors.append(
                            f"newly created global Skill changed during cleanup and was preserved: {destination}"
                        )
                else:
                    recovery_errors.append(
                        f"global Skill changed during rollback and was preserved: {destination}"
                    )
            if recovery_errors:
                raise SkillSyncError(
                    "Skill import failed and requires recovery",
                    code="import_rollback_needs_recovery",
                    exit_code=EXIT_SAFETY,
                    details={
                        "cause": str(exc),
                        "recovery_errors": recovery_errors,
                        "backup": str(backup) if backup.exists() else None,
                    },
                ) from exc
            raise
        shutil.rmtree(backup)
        imported.append({"name": name, "agent": agent_name, "state": "imported"})
    return {"imported": imported}


def copy_global_skills_to_agents(
    names: Iterable[str],
    agent_names: Iterable[str],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    config = _load_local_config(config_path)
    try:
        with local_file_lock(_data_root(config) / "locks" / "deployment.lock"):
            return _copy_global_skills_to_agents_unlocked(
                names, agent_names, config_path, _config=config
            )
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="deployment_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc


def _copy_global_skills_to_agents_unlocked(
    names: Iterable[str],
    agent_names: Iterable[str],
    config_path: str | Path | None = None,
    *,
    _config: dict[str, Any] | None = None,
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
    config = _load_local_config(config_path) if _config is None else _config
    _assert_no_incomplete_deployment_receipts(_data_root(config))
    registry = _load_local_registry(config)
    clients = detect_clients()
    known = {client.id for client in clients} | {client.family_id for client in clients}
    unknown = requested_agents - known
    if unknown:
        raise SkillSyncError("unknown Agent: " + ", ".join(sorted(unknown)))
    selected_clients = [
        client
        for client in clients
        if client.id in requested_agents or client.family_id in requested_agents
    ]
    unavailable = [
        name
        for name in requested_agents
        if not any(
            client.detected
            and (client.id == name or client.family_id == name)
            for client in selected_clients
        )
    ]
    if unavailable:
        raise SkillSyncError("Agent is not detected: " + ", ".join(sorted(unavailable)))
    copied: list[dict[str, str]] = []
    for name in name_list:
        if Path(name).name != name or name in {".", ".."}:
            raise SkillSyncError(f"invalid Skill name: {name}")
        source = _local_skill_path_or_default(config, registry, name)
        _validate_skill_path(source)
        for client in selected_clients:
            if not client.detected:
                continue
            destination = client.skills_dir / name
            owned_target = _owned_link_target(
                source,
                destination,
                _rendered_root(config),
                name,
                client.id,
            )
            state = "linked" if owned_target is not None else link_state(source, destination)
            if owned_target is not None:
                remove_directory_link(owned_target, destination)
            elif state != "missing":
                raise SkillSyncError(
                    f"refusing to overwrite existing {state} directory: {destination}"
                )
            try:
                copy_skill_dir(source, destination)
            except Exception:
                if not destination.exists() and owned_target is not None:
                    create_directory_link(owned_target, destination)
                raise
            copied.append(
                {
                    "skill": name,
                    "agent": client.family_id,
                    "client": client.id,
                    "path": str(destination),
                    "state": "copied",
                }
            )
    return {"copied": copied}


def delete_global_skills(
    names: Iterable[str],
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    requested_names = _validate_delete_names(names)
    initial_config = _load_local_config(config_path)
    locked_data_root = _data_root(initial_config)
    try:
        with local_file_lock(locked_data_root / "locks" / "deployment.lock"):
            config = _load_local_config(config_path)
            if _data_root(config) != locked_data_root:
                raise SkillSyncError(
                    "configured data_root changed while acquiring the deployment lock",
                    code="delete_config_changed",
                    exit_code=EXIT_SAFETY,
                )
            registry = _load_local_registry(config)
            name_list = _resolve_delete_names(
                requested_names,
                global_root=_global_skill_root(config),
                config=config,
                registry=registry,
            )
            _assert_no_incomplete_deployment_receipts(locked_data_root)
            clients = tuple(detect_clients())
            resolved = _resolve_delete_input(
                name_list,
                config_path,
                clients=clients,
                config=config,
                registry=registry,
            )
            name_list = resolved["skills"]
            store = EditSessionStore(locked_data_root)
            with ExitStack() as skill_locks:
                lock_names: dict[str, str] = {}
                for name in name_list:
                    lock_path = store.skill_lock_path(name)
                    lock_names.setdefault(os.path.normcase(str(lock_path)), name)
                for _, name in sorted(lock_names.items()):
                    skill_locks.enter_context(store.skill_lock(name))
                _assert_delete_has_no_active_edits(store, name_list)
                _raise_preflight_findings(
                    "delete",
                    blockers=_delete_safety_blockers(config, name_list),
                )
                return _delete_global_skills_unlocked(
                    name_list,
                    config_path,
                    _config=config,
                    _clients=clients,
                )
    except TimeoutError as exc:
        raise SkillSyncError(
            str(exc), code="deployment_lock_timeout", exit_code=EXIT_SAFETY
        ) from exc


def _validate_delete_names(names: Iterable[str]) -> list[str]:
    if isinstance(names, (str, bytes)):
        raise SkillSyncError("delete requires a list of Skill names")
    try:
        name_list = list(dict.fromkeys(names))
    except (TypeError, ValueError) as exc:
        raise SkillSyncError("delete requires a list of Skill names") from exc
    if not name_list:
        raise SkillSyncError("delete requires at least one Skill name")
    for name in name_list:
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
        ):
            raise SkillSyncError(f"invalid Skill name: {name}")
    return name_list


def _resolve_delete_names(
    name_list: Iterable[str],
    *,
    global_root: Path,
    config: dict[str, Any],
    registry: dict[str, Any],
) -> list[str]:
    identity_names: dict[str, set[str]] = {}
    if is_link_or_reparse(global_root):
        raise SkillSyncError(
            f"global Skill root is a symlink or reparse point: {global_root}",
            code="delete_skill_root_unsafe",
            exit_code=EXIT_SAFETY,
        )
    if global_root.is_dir():
        try:
            children = list(global_root.iterdir())
        except OSError as exc:
            raise SkillSyncError(
                f"cannot safely inspect global Skill names: {exc}",
                code="delete_skill_names_unreadable",
                exit_code=EXIT_SAFETY,
            ) from exc
        for child in children:
            identity_names.setdefault(child.name.casefold(), set()).add(child.name)
    for container in (config.get("skills", {}), registry.get("skills", {})):
        if not isinstance(container, dict):
            raise SkillSyncError("Skill metadata must be a mapping")
        for name in container:
            if isinstance(name, str):
                identity_names.setdefault(name.casefold(), set()).add(name)
    normalized: list[str] = []
    seen: set[str] = set()
    for requested in name_list:
        matches = sorted(identity_names.get(requested.casefold(), set()))
        if len(matches) > 1:
            raise SkillSyncError(
                f"ambiguous case-insensitive Skill name: {requested}",
                code="delete_skill_name_ambiguous",
                exit_code=EXIT_SAFETY,
                details={"requested": requested, "matches": sorted(matches)},
            )
        resolved = matches[0] if matches else requested
        identity = resolved.casefold()
        if identity not in seen:
            seen.add(identity)
            normalized.append(resolved)
    return normalized


def _assert_delete_has_no_active_edits(
    store: EditSessionStore,
    names: Iterable[str],
) -> None:
    requested = {name.casefold() for name in names}
    blockers = [
        metadata
        for metadata in store.list_metadata()
        if metadata.logical_skill.casefold() in requested
        and metadata.status
        in {
            EditSessionStatus.ACTIVE,
            EditSessionStatus.APPLYING,
            EditSessionStatus.NEEDS_RECOVERY,
        }
    ]
    if blockers:
        raise SkillSyncError(
            "cannot delete a Skill with an unfinished edit session",
            code="delete_edit_session_blocked",
            exit_code=EXIT_SAFETY,
            details={
                "sessions": [
                    {
                        "skill": item.logical_skill,
                        "session_id": item.session_id,
                        "status": item.status.value,
                    }
                    for item in blockers
                ]
            },
        )


def _delete_global_skills_unlocked(
    names: Iterable[str],
    config_path: str | Path | None = None,
    *,
    _config: dict[str, Any] | None = None,
    _clients: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Permanently delete canonical Skills and their managed Agent links."""
    name_list = list(dict.fromkeys(names))
    if not name_list:
        raise SkillSyncError("delete requires at least one Skill name")
    config_file = _config_path(config_path)
    config = load_config(config_file) if _config is None else _config
    _assert_no_incomplete_deployment_receipts(_data_root(config))
    repo = _repo_path(config)
    registry_path = repo / REGISTRY_FILE
    registry = _read_or_empty_registry(registry_path)
    global_root = _global_skill_root(config)
    clients = tuple(detect_clients() if _clients is None else _clients)
    moved: list[tuple[str, Path, Path]] = []
    removed_links: list[tuple[Path, Path]] = []
    try:
        for name in name_list:
            if Path(name).name != name or name in {".", ".."}:
                raise SkillSyncError(f"invalid Skill name: {name}")
            source = global_root / name
            _validate_skill_path(source)
            for client in clients:
                if not client.skills_dir.is_dir():
                    continue
                destination = client.skills_dir / name
                owned_target = _owned_link_target(
                    source,
                    destination,
                    _rendered_root(config),
                    name,
                    client.id,
                )
                if owned_target is not None and remove_directory_link(
                    owned_target, destination
                ):
                    removed_links.append((owned_target, destination))
            backup = global_root / f".{name}.skill-sync-delete-{time.time_ns()}"
            rename_no_replace(source, backup)
            moved.append((name, source, backup))
        skills = registry.setdefault("skills", {})
        local_skills = config.setdefault("skills", {})
        for name in name_list:
            skills.pop(name, None)
            local_skills.pop(name, None)
        save_registry(registry_path, registry)
        save_config(config_file, config)
    except Exception as exc:
        recovery_errors: list[str] = []
        for _, source, backup in reversed(moved):
            if backup.exists() or is_link_or_reparse(backup):
                try:
                    rename_no_replace(backup, source)
                except (FileExistsError, OSError):
                    recovery_errors.append(
                        f"canonical path changed; backup preserved at {backup}"
                    )
        for link_target, destination in removed_links:
            if link_target.exists() and not destination.exists():
                try:
                    create_directory_link(link_target, destination)
                except (FileExistsError, OSError) as rollback_exc:
                    recovery_errors.append(
                        f"could not restore {destination}: {rollback_exc}"
                    )
        if recovery_errors:
            raise SkillSyncError(
                "Skill deletion failed and requires recovery",
                code="delete_rollback_needs_recovery",
                exit_code=EXIT_SAFETY,
                details={"cause": str(exc), "recovery_errors": recovery_errors},
            ) from exc
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
    _validate_skill_path(source)
    destination = _global_skill_root(config) / ".skill-sync-backups" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    copy_skill_dir(source, destination)
    return {"skill": name, "backup_path": str(destination)}
