"""Create and inspect portable Variant source directories.

This module manages only ``<skills_root>/../variants`` source data.  It does
not resolve overlays, update registries, deploy Skills, or open edit sessions.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

from skill_sync.agents import AGENT_FAMILIES
from skill_sync.config import default_config_path, load_config
from skill_sync.copying import rename_no_replace
from skill_sync.errors import SkillSyncError
from skill_sync.hash import is_link_or_reparse
from skill_sync.protocol import EXIT_SAFETY
from skill_sync.variant import VARIANT_MANIFEST_FILE, load_variant_manifest
from skill_sync.variant_overlay import plan_variant_overlay


_FAMILIES = {family.id: family for family in AGENT_FAMILIES}
_CLIENT_FAMILIES = {
    client_id: family.id
    for family in AGENT_FAMILIES
    for client_id in family.client_ids
}
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"/\\|?*')


def variants_root(config_path: str | Path | None = None) -> Path:
    """Return the portable Variant root derived from the canonical Skill root."""

    config = _load_config(config_path)
    raw_skills_root = config.get("skills_root") or Path.home() / ".agents" / "skills"
    skills_root = Path(raw_skills_root).expanduser()
    if not skills_root.is_absolute():
        raise SkillSyncError(
            "configured skills_root must be an absolute path",
            code="variant_config_invalid",
            exit_code=EXIT_SAFETY,
        )
    return skills_root.absolute().parent / "variants"


def create_variant(
    skill: str,
    *,
    scope: str,
    target: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically create the smallest valid overlay manifest for one target."""

    _validate_skill_name(skill)
    family, affected_clients, resolution_order = _target_metadata(scope, target)
    config = _load_config(config_path)
    skills_root, root = _configured_roots(config)
    resolved_skill = _resolve_base_skill(skill, skills_root, root)
    variant_skill_root = root / resolved_skill
    destination = variant_skill_root / target

    try:
        _prepare_real_directory(root, label="Variant root")
        _reject_case_conflict(root, resolved_skill, label="Variant Skill name")
        _prepare_real_directory(variant_skill_root, label="Variant Skill root")
        _reject_case_conflict(variant_skill_root, target, label="Variant target")
        if destination.exists() or is_link_or_reparse(destination):
            raise SkillSyncError(
                f"Variant target already exists: {destination}",
                code="variant_target_exists",
                details={"skill": resolved_skill, "target": target, "path": str(destination)},
            )

        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{target}.create-", dir=variant_skill_root)
        )
        staging = staging_root / target
        staging.mkdir()
        try:
            manifest = staging / VARIANT_MANIFEST_FILE
            with manifest.open("x", encoding="utf-8") as output:
                output.write(f"version: 1\ntarget: {target}\nmode: overlay\n")
                output.flush()
                os.fsync(output.fileno())
            load_variant_manifest(manifest, expected_target=target)
            _fsync_directory(staging)
            try:
                rename_no_replace(staging, destination)
            except FileExistsError:
                raise SkillSyncError(
                    f"Variant target already exists: {destination}",
                    code="variant_target_exists",
                    details={
                        "skill": resolved_skill,
                        "target": target,
                        "path": str(destination),
                    },
                ) from None
            _fsync_directory(variant_skill_root)
        finally:
            if staging_root.exists() or staging_root.is_symlink():
                shutil.rmtree(staging_root, ignore_errors=True)
    except SkillSyncError:
        raise
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"cannot create Variant source: {exc}",
            code="variant_create_failed",
            details={"skill": resolved_skill, "scope": scope, "target": target},
        ) from exc

    return {
        "skill": resolved_skill,
        "scope": scope,
        "target": target,
        "family": family,
        "affected_clients": list(affected_clients),
        "path": str(destination),
        "manifest_path": str(destination / VARIANT_MANIFEST_FILE),
        "created": True,
        "resolution_order": list(resolution_order),
    }


def list_variants(
    *,
    skill: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """List Variant sources deterministically without changing local state."""

    config = _load_config(config_path)
    skills_root, root = _configured_roots(config)
    if skill is not None:
        _validate_skill_name(skill)
    skill_names = _variant_skill_names(root, requested=skill)
    variants: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    inspection_names = skill_names or ([skill] if skill is not None else [])
    for skill_name in inspection_names:
        skill_issues = _inspect_logical_skill_name(skill_name)
        base_issues = _inspect_base_skill(skills_root, skill_name)
        if skill_name in skill_names:
            rows, row_issues = _inspect_variant_skill(root, skill_name)
        else:
            rows, row_issues = [], []
        for row in rows:
            manifest_valid = bool(row["valid"])
            row["manifest_valid"] = manifest_valid
            row["base_valid"] = not base_issues
            row["skill_name_valid"] = not skill_issues
            row["valid"] = manifest_valid and not base_issues and not skill_issues
        variants.extend(rows)
        issues.extend(skill_issues)
        issues.extend(base_issues)
        issues.extend(row_issues)
    return {
        "variants_root": str(root),
        "skill": skill_names[0] if skill is not None and skill_names else skill,
        "variant_count": len(variants),
        "valid": not issues,
        "variants": variants,
        "issues": issues,
    }


def validate_variants(
    skill: str,
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate every Variant source for one logical Skill without writes."""

    result = list_variants(skill=skill, config_path=config_path)
    return {
        "skill": result["skill"],
        "variants_root": result["variants_root"],
        "variant_count": result["variant_count"],
        "valid": result["valid"],
        "variants": result["variants"],
        "issues": result["issues"],
    }


def _load_config(config_path: str | Path | None) -> dict[str, Any]:
    path = default_config_path() if config_path is None else Path(config_path)
    try:
        return load_config(path)
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"cannot load skill-sync config: {exc}",
            code="variant_config_invalid",
            exit_code=EXIT_SAFETY,
        ) from exc


def _configured_roots(config: dict[str, Any]) -> tuple[Path, Path]:
    raw = config.get("skills_root") or Path.home() / ".agents" / "skills"
    skills_root = Path(raw).expanduser()
    if not skills_root.is_absolute():
        raise SkillSyncError(
            "configured skills_root must be an absolute path",
            code="variant_config_invalid",
            exit_code=EXIT_SAFETY,
        )
    skills_root = skills_root.absolute()
    return skills_root, skills_root.parent / "variants"


def _target_metadata(
    scope: str, target: str
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if scope == "family":
        family = _FAMILIES.get(target)
        if family is None:
            raise SkillSyncError(
                f"unknown Agent family ID: {target}",
                code="variant_family_unknown",
                details={"target": target, "known": sorted(_FAMILIES)},
            )
        return (
            family.id,
            family.client_ids,
            ("base", f"family:{family.id}", "client-specific"),
        )
    if scope == "client":
        family_id = _CLIENT_FAMILIES.get(target)
        if family_id is None:
            raise SkillSyncError(
                f"unknown Agent client ID: {target}",
                code="variant_client_unknown",
                details={"target": target, "known": sorted(_CLIENT_FAMILIES)},
            )
        return (
            family_id,
            (target,),
            ("base", f"family:{family_id}", f"client:{target}"),
        )
    raise SkillSyncError(
        "Variant scope must be exactly one of family or client",
        code="variant_scope_invalid",
    )


def _resolve_base_skill(skill: str, skills_root: Path, root: Path) -> str:
    if is_link_or_reparse(skills_root):
        raise SkillSyncError(
            f"global Skill root is a link or reparse point: {skills_root}",
            code="variant_skill_root_unsafe",
            exit_code=EXIT_SAFETY,
        )
    matches = _casefold_matches(skills_root, skill, label="global Skill root")
    if len(matches) > 1:
        _raise_ambiguous("Skill name", skill, matches)
    if not matches:
        raise SkillSyncError(
            f"Base Skill does not exist: {skills_root / skill}",
            code="variant_base_missing",
            details={"skill": skill},
        )
    resolved = matches[0]
    base = skills_root / resolved
    if is_link_or_reparse(base):
        raise SkillSyncError(
            f"Base Skill is a link or reparse point: {base}",
            code="variant_base_unsafe",
            exit_code=EXIT_SAFETY,
        )
    if not base.is_dir() or not (base / "SKILL.md").is_file():
        raise SkillSyncError(
            f"Base Skill is invalid or does not contain SKILL.md: {base}",
            code="variant_base_invalid",
        )
    if is_link_or_reparse(base / "SKILL.md"):
        raise SkillSyncError(
            f"Base Skill SKILL.md is a link or reparse point: {base / 'SKILL.md'}",
            code="variant_base_unsafe",
            exit_code=EXIT_SAFETY,
        )
    try:
        plan_variant_overlay(base)
    except (OSError, ValueError) as exc:
        raise SkillSyncError(
            f"Base Skill is unsafe: {exc}",
            code="variant_base_unsafe",
            exit_code=EXIT_SAFETY,
            details={"skill": resolved, "path": str(base)},
        ) from exc
    if root.exists() or is_link_or_reparse(root):
        root_matches = _casefold_matches(root, resolved, label="Variant root")
        if len(root_matches) > 1:
            _raise_ambiguous("Variant Skill name", resolved, root_matches)
        if root_matches and root_matches[0] != resolved:
            _raise_ambiguous("Variant Skill name", resolved, root_matches + [resolved])
    return resolved


def _variant_skill_names(root: Path, *, requested: str | None) -> list[str]:
    if not root.exists() and not is_link_or_reparse(root):
        return []
    names = _real_child_directories(root, label="Variant root")
    _reject_casefold_duplicates(names, label="Variant Skill name")
    if requested is None:
        return sorted(names, key=lambda value: (value.casefold(), value))
    matches = [name for name in names if name.casefold() == requested.casefold()]
    if len(matches) > 1:
        _raise_ambiguous("Variant Skill name", requested, matches)
    return matches


def _inspect_variant_skill(
    root: Path, skill: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    skill_root = root / skill
    targets = _real_child_directories(skill_root, label=f"Variant Skill {skill}")
    _reject_casefold_duplicates(targets, label=f"Variant target for {skill}")
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for target in sorted(targets, key=lambda value: (value.casefold(), value)):
        target_root = skill_root / target
        manifest_path = target_root / VARIANT_MANIFEST_FILE
        kinds = [
            kind
            for kind, known in (("family", _FAMILIES), ("client", _CLIENT_FAMILIES))
            if target in known
        ]
        row: dict[str, Any] = {
            "skill": skill,
            "target": target,
            "target_kinds": kinds,
            "path": str(target_root),
            "manifest_path": str(manifest_path),
        }
        try:
            manifest = load_variant_manifest(manifest_path, expected_target=target)
            overlay_files = _overlay_file_count(target_root)
        except (OSError, ValueError) as exc:
            message = str(exc)
            row.update({"valid": False, "error": message, "overlay_file_count": None})
            issues.append(
                {
                    "code": "invalid_variant",
                    "skill": skill,
                    "target": target,
                    "path": str(target_root),
                    "message": message,
                }
            )
        else:
            row.update(
                {
                    "valid": True,
                    "mode": manifest.mode,
                    "delete": list(manifest.delete),
                    "overlay_file_count": overlay_files,
                }
            )
        rows.append(row)
    return rows, issues


def _overlay_file_count(root: Path) -> int:
    return sum(
        1
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix() != VARIANT_MANIFEST_FILE
    )


def _inspect_logical_skill_name(skill: str) -> list[dict[str, str]]:
    try:
        _validate_skill_name(skill)
    except SkillSyncError as exc:
        return [
            {
                "code": "variant_skill_name_invalid",
                "skill": str(skill),
                "path": str(skill),
                "message": str(exc),
            }
        ]
    return []


def _inspect_base_skill(skills_root: Path, skill: str) -> list[dict[str, str]]:
    """Return structured canonical Base issues without changing source state."""

    if is_link_or_reparse(skills_root) or not skills_root.is_dir():
        return _source_issue(
            "variant_base_root_unsafe",
            skill,
            skills_root,
            f"canonical Base root must be a real directory: {skills_root}",
        )
    try:
        entries = list(os.scandir(skills_root))
    except OSError as exc:
        return _source_issue(
            "variant_base_root_unreadable",
            skill,
            skills_root,
            f"cannot inspect canonical Base root: {exc}",
        )

    matches = sorted(
        (entry.name for entry in entries if entry.name.casefold() == skill.casefold()),
        key=lambda value: (value.casefold(), value),
    )
    if len(matches) > 1:
        return _source_issue(
            "variant_base_ambiguous",
            skill,
            skills_root,
            f"ambiguous case-insensitive canonical Base name {skill!r}: {matches}",
        )
    if not matches:
        return _source_issue(
            "variant_base_missing",
            skill,
            skills_root / skill,
            f"canonical Base Skill does not exist: {skills_root / skill}",
        )
    if matches[0] != skill:
        return _source_issue(
            "variant_base_name_mismatch",
            skill,
            skills_root / matches[0],
            f"Variant Skill name {skill!r} does not exactly match canonical Base {matches[0]!r}",
        )

    base = skills_root / skill
    if is_link_or_reparse(base) or not base.is_dir():
        return _source_issue(
            "variant_base_unsafe",
            skill,
            base,
            f"canonical Base Skill must be a real directory: {base}",
        )
    manifest = base / "SKILL.md"
    if is_link_or_reparse(manifest) or not manifest.is_file():
        return _source_issue(
            "variant_base_invalid",
            skill,
            base,
            f"canonical Base Skill must contain a real SKILL.md: {base}",
        )
    try:
        plan_variant_overlay(base)
    except (OSError, ValueError) as exc:
        return _source_issue(
            "variant_base_unsafe",
            skill,
            base,
            f"canonical Base Skill is unsafe: {exc}",
        )
    return []


def _source_issue(
    code: str,
    skill: str,
    path: str | Path,
    message: str,
) -> list[dict[str, str]]:
    return [
        {
            "code": code,
            "skill": skill,
            "path": str(path),
            "message": message,
        }
    ]


def _prepare_real_directory(path: Path, *, label: str) -> None:
    if is_link_or_reparse(path):
        raise SkillSyncError(
            f"{label} is a link or reparse point: {path}",
            code="variant_source_unsafe",
            exit_code=EXIT_SAFETY,
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillSyncError(f"cannot create {label}: {exc}") from exc
    if is_link_or_reparse(path) or not path.is_dir():
        raise SkillSyncError(
            f"{label} must be a real directory: {path}",
            code="variant_source_unsafe",
            exit_code=EXIT_SAFETY,
        )


def _real_child_directories(root: Path, *, label: str) -> list[str]:
    if is_link_or_reparse(root) or not root.is_dir():
        raise SkillSyncError(
            f"{label} must be a real directory: {root}",
            code="variant_source_unsafe",
            exit_code=EXIT_SAFETY,
        )
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise SkillSyncError(
            f"cannot inspect {label}: {exc}",
            code="variant_source_unreadable",
            exit_code=EXIT_SAFETY,
        ) from exc
    names: list[str] = []
    for entry in entries:
        path = Path(entry.path)
        if entry.is_symlink() or is_link_or_reparse(path) or not entry.is_dir(follow_symlinks=False):
            raise SkillSyncError(
                f"{label} contains an unsafe non-directory entry: {path}",
                code="variant_source_unsafe",
                exit_code=EXIT_SAFETY,
            )
        names.append(entry.name)
    return names


def _casefold_matches(root: Path, name: str, *, label: str) -> list[str]:
    names = _real_child_directories(root, label=label)
    return sorted(
        (candidate for candidate in names if candidate.casefold() == name.casefold()),
        key=lambda value: (value.casefold(), value),
    )


def _reject_case_conflict(root: Path, name: str, *, label: str) -> None:
    matches = _casefold_matches(root, name, label=str(root))
    if matches and matches != [name]:
        _raise_ambiguous(label, name, matches)


def _reject_casefold_duplicates(names: list[str], *, label: str) -> None:
    identities: dict[str, list[str]] = {}
    for name in names:
        identities.setdefault(name.casefold(), []).append(name)
    for matches in identities.values():
        if len(matches) > 1:
            _raise_ambiguous(label, matches[0], matches)


def _raise_ambiguous(label: str, requested: str, matches: list[str]) -> None:
    raise SkillSyncError(
        f"ambiguous case-insensitive {label}: {requested}",
        code="variant_name_ambiguous",
        exit_code=EXIT_SAFETY,
        details={"requested": requested, "matches": sorted(set(matches))},
    )


def _validate_skill_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or value in {".", ".."}
        or any(ord(character) < 32 for character in value)
        or any(character in _WINDOWS_RESERVED_CHARACTERS for character in value)
        or Path(value).name != value
        or PureWindowsPath(value).drive
        or value.endswith((" ", "."))
        or value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
    ):
        raise SkillSyncError(
            f"invalid portable Skill name: {value!r}",
            code="variant_skill_name_invalid",
        )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
