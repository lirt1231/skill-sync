"""Read-only ownership inspection for canonical and Agent Skill paths.

The inspector deliberately accepts all machine-specific inputs explicitly.  It
does not load configuration, mutate links, or contact a Git remote, which makes
it safe for CLI, doctor, and future deployment code to share.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from skill_sync.deployment import (
    expected_layered_provenance,
    expected_provenance,
    verify_deployment,
)
from skill_sync.hash import hash_skill_dir
from skill_sync.variant_resolution import (
    resolve_variant_for_client,
)


@dataclass(frozen=True)
class OwnershipResult:
    """Stable result returned by :func:`inspect_ownership`."""

    managed: bool
    healthy: bool
    state: str
    role: str
    skill: str | None
    input_path: str
    source_path: str | None
    client: str | None
    migration_required: bool
    deployment_path: str | None = None
    resolution_hash: str | None = None
    source_hash: str | None = None
    rendered_hash: str | None = None
    referenced: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation without filesystem objects."""

        return asdict(self)


@dataclass(frozen=True)
class _Endpoint:
    client_id: str
    family_id: str
    skills_root: Path


@dataclass(frozen=True)
class _Intent:
    selected: frozenset[str]
    targets: Mapping[str, frozenset[str] | None]


def inspect_ownership(
    input_path: str | Path,
    *,
    skills_root: str | Path,
    selected_skills: Iterable[str] | Mapping[str, Any] | None = None,
    registry: Mapping[str, Any] | None = None,
    clients: Iterable[Any] = (),
    targets: Iterable[Any] = (),
    client: str | None = None,
    rendered_root: str | Path | None = None,
) -> OwnershipResult:
    """Inspect whether one path is owned by the current Skill Sync model.

    ``selected_skills`` is convenient for callers that already resolved the
    registry.  ``registry`` may instead be the complete registry mapping.  If
    both are supplied, explicitly selected names are added while existing
    registry entries retain their configured targets.

    Agent endpoints may be concrete ``AgentClient`` objects or legacy
    ``AgentTarget`` objects.  Only their public path and identity attributes are
    used, keeping this module independent from detection and CLI workflows.
    """

    raw_input = os.fspath(input_path)
    canonical_root = _absolute_path(skills_root)
    intent = _ownership_intent(selected_skills, registry)
    endpoints = _endpoints(clients, targets)
    deployment_root = (
        None if rendered_root is None else _absolute_path(rendered_root)
    )

    if _is_name(raw_input):
        name = raw_input
        if name not in intent.selected:
            return _ambiguous(raw_input)
        path = canonical_root / name
        display_input = raw_input
    else:
        path = _absolute_path(input_path)
        display_input = str(path)

    if client is not None:
        endpoints = tuple(
            endpoint
            for endpoint in endpoints
            if client in {endpoint.client_id, endpoint.family_id}
        )
        if not endpoints:
            return _ambiguous(display_input)

    source_match = _skill_beneath(path, canonical_root)
    endpoint_matches = _matching_endpoints(path, endpoints)
    if source_match is not None and endpoint_matches:
        return _ambiguous(display_input, skill=source_match)

    if source_match is not None:
        source = canonical_root / source_match
        if not path.exists():
            return _ambiguous(
                display_input,
                skill=source_match,
                source_path=source,
            )
        if source_match not in intent.selected:
            return _unmanaged(
                display_input,
                skill=source_match,
                source_path=source,
            )
        return OwnershipResult(
            managed=True,
            healthy=True,
            state="managed-source",
            role="source",
            skill=source_match,
            input_path=display_input,
            source_path=_resolved_text(source),
            client=None,
            migration_required=True,
        )

    deployment = (
        None
        if deployment_root is None
        else _deployment_for_path(path, deployment_root)
    )
    if deployment is not None:
        return _inspect_deployment_path(
            display_input,
            deployment,
            canonical_root,
            intent,
            endpoints,
        )

    if len(endpoint_matches) > 1:
        names = {endpoint.client_id for endpoint, _ in endpoint_matches}
        if len(names) > 1:
            return _ambiguous(display_input)

    if endpoint_matches:
        endpoint, skill_name = endpoint_matches[0]
        source = canonical_root / skill_name
        destination = endpoint.skills_root / skill_name
        if skill_name not in intent.selected or not _targets_client(
            intent.targets.get(skill_name), endpoint
        ):
            return _unmanaged(
                display_input,
                skill=skill_name,
                source_path=source,
                client=endpoint.client_id,
            )

        link_health = _direct_link_health(source, destination)
        if link_health == "linked":
            return OwnershipResult(
                managed=True,
                healthy=True,
                state="managed-source",
                role="direct-source-link",
                skill=skill_name,
                input_path=display_input,
                source_path=_resolved_text(source),
                client=endpoint.client_id,
                migration_required=True,
            )
        if deployment_root is not None:
            deployment_target = _directory_link_target(destination)
            if (
                deployment_target is not None
                and _is_beneath(deployment_target, deployment_root)
            ):
                return _inspect_endpoint_deployment(
                    display_input,
                    skill_name,
                    source,
                    endpoint,
                    deployment_target,
                )
        if link_health in {"wrong-link", "broken-link"}:
            return OwnershipResult(
                managed=True,
                healthy=False,
                state=link_health,
                role="direct-source-link",
                skill=skill_name,
                input_path=display_input,
                source_path=_resolved_text(source),
                client=endpoint.client_id,
                migration_required=True,
            )
        if link_health == "real-directory":
            return _unmanaged(
                display_input,
                skill=skill_name,
                source_path=source,
                client=endpoint.client_id,
            )
        return _ambiguous(
            display_input,
            skill=skill_name,
            source_path=source,
            client=endpoint.client_id,
        )

    if path.exists() or path.is_symlink():
        return _unmanaged(display_input)
    return _ambiguous(display_input)


def _ownership_intent(
    selected_skills: Iterable[str] | Mapping[str, Any] | None,
    registry: Mapping[str, Any] | None,
) -> _Intent:
    selected: set[str] = set()
    configured_targets: dict[str, frozenset[str] | None] = {}

    if registry is not None:
        _add_registry_intent(registry, selected, configured_targets)

    if selected_skills is not None:
        if isinstance(selected_skills, Mapping):
            _add_registry_intent(selected_skills, selected, configured_targets)
        else:
            for name in selected_skills:
                if not isinstance(name, str) or not name:
                    raise ValueError("selected Skill names must be non-empty strings")
                selected.add(name)
                configured_targets.setdefault(name, None)

    return _Intent(frozenset(selected), configured_targets)


def _add_registry_intent(
    registry: Mapping[str, Any],
    selected: set[str],
    configured_targets: dict[str, frozenset[str] | None],
) -> None:
    skills = registry.get("skills", registry)
    if not isinstance(skills, Mapping):
        raise ValueError("registry skills must be a mapping")

    for name, entry in skills.items():
        if not isinstance(name, str) or not name:
            raise ValueError("registry Skill names must be non-empty strings")
        if not isinstance(entry, Mapping):
            continue
        if not entry.get("selected", True):
            continue
        selected.add(name)
        raw_targets = entry.get("targets")
        if raw_targets is None:
            configured_targets[name] = None
        elif isinstance(raw_targets, str):
            configured_targets[name] = frozenset(
                item.strip() for item in raw_targets.split(",") if item.strip()
            )
        elif isinstance(raw_targets, Iterable):
            values = tuple(raw_targets)
            if not all(isinstance(item, str) and item for item in values):
                raise ValueError("registry Skill targets must be strings")
            configured_targets[name] = frozenset(values)
        else:
            raise ValueError("registry Skill targets must be a string or iterable")


def _endpoints(clients: Iterable[Any], targets: Iterable[Any]) -> tuple[_Endpoint, ...]:
    results: list[_Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for item in (*tuple(clients), *tuple(targets)):
        client_id = getattr(item, "id", None) or getattr(item, "name", None)
        family_id = getattr(item, "family_id", None) or getattr(
            item, "family", None
        )
        family_id = family_id or client_id
        skill_dirs = getattr(item, "skill_dirs", None)
        if skill_dirs is None:
            skill_dir = getattr(item, "skills_dir", None)
            skill_dirs = () if skill_dir is None else (skill_dir,)
        if not isinstance(client_id, str) or not isinstance(family_id, str):
            raise ValueError("Agent endpoints must expose string identity fields")
        for skill_dir in skill_dirs:
            root = _absolute_path(skill_dir)
            key = (client_id, str(root))
            if key in seen:
                continue
            seen.add(key)
            results.append(_Endpoint(client_id, family_id, root))
    return tuple(results)


def _matching_endpoints(
    path: Path, endpoints: tuple[_Endpoint, ...]
) -> list[tuple[_Endpoint, str]]:
    matches: list[tuple[_Endpoint, str]] = []
    for endpoint in endpoints:
        skill = _skill_beneath(path, endpoint.skills_root)
        if skill is not None:
            matches.append((endpoint, skill))
    return matches


def _skill_beneath(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if name in {".", ".."}:
        return None
    return name


def _targets_client(targets: frozenset[str] | None, endpoint: _Endpoint) -> bool:
    return targets is None or bool(
        targets.intersection({endpoint.client_id, endpoint.family_id})
    )


def _direct_link_health(source: Path, destination: Path) -> str:
    if destination.is_symlink():
        if not destination.exists():
            return "broken-link"
        if _same_file(source, destination):
            return "linked"
        try:
            return (
                "linked"
                if destination.resolve(strict=True) == source.resolve(strict=True)
                else "wrong-link"
            )
        except OSError:
            return "broken-link"
    if destination.exists():
        return "linked" if _same_file(source, destination) else "real-directory"
    return "missing"


def _expected_current_provenance(
    skill_name: str,
    source: Path,
    client_id: str,
    current_source_hash: str | None,
) -> dict[str, Any] | None:
    if current_source_hash is None:
        return None
    try:
        resolution = resolve_variant_for_client(
            source,
            source.parent.parent / "variants" / skill_name,
            client_id,
        )
        if resolution.applied_variant_targets:
            return expected_layered_provenance(skill_name, resolution)
        return expected_provenance(skill_name, current_source_hash, client_id)
    except (OSError, ValueError):
        # An invalid current source chain must classify an otherwise intact
        # rendered artifact as stale rather than silently trusting old output.
        return {}


def _inspect_endpoint_deployment(
    input_path: str,
    skill_name: str,
    source: Path,
    endpoint: _Endpoint,
    deployment: Path,
) -> OwnershipResult:
    try:
        current_source_hash = hash_skill_dir(source)
    except (OSError, ValueError):
        current_source_hash = None
    initial = verify_deployment(deployment)
    expected = _expected_current_provenance(
        skill_name,
        source,
        endpoint.client_id,
        current_source_hash,
    )
    verification = verify_deployment(deployment, expected_provenance=expected)
    provenance = verification.provenance or {}
    common = {
        "managed": True,
        "role": "rendered-deployment-link",
        "skill": skill_name,
        "input_path": input_path,
        "source_path": _resolved_text(source),
        "client": endpoint.client_id,
        "deployment_path": str(deployment),
        "resolution_hash": provenance.get("resolution_hash"),
        "source_hash": provenance.get("source_hash"),
        "rendered_hash": provenance.get("rendered_hash"),
        "referenced": True,
    }
    if verification.state == "missing":
        return OwnershipResult(
            healthy=False,
            state="missing-render",
            migration_required=True,
            **common,
        )
    if provenance.get("logical_skill") != skill_name or provenance.get(
        "target_client"
    ) != endpoint.client_id:
        return OwnershipResult(
            healthy=False,
            state="wrong-link",
            migration_required=True,
            **common,
        )
    if verification.state == "stale":
        return OwnershipResult(
            healthy=False,
            state="stale-render",
            migration_required=True,
            **common,
        )
    if not verification.ok:
        return OwnershipResult(
            healthy=False,
            state="tampered-render",
            migration_required=True,
            **common,
        )
    if current_source_hash != provenance.get("source_hash"):
        return OwnershipResult(
            healthy=False,
            state="stale-render",
            migration_required=True,
            **common,
        )
    return OwnershipResult(
        healthy=True,
        state="managed-deployment",
        migration_required=False,
        **common,
    )


def _inspect_deployment_path(
    input_path: str,
    deployment: Path,
    canonical_root: Path,
    intent: _Intent,
    endpoints: tuple[_Endpoint, ...],
) -> OwnershipResult:
    verification = verify_deployment(deployment)
    provenance = verification.provenance or {}
    skill_name = provenance.get("logical_skill")
    client_id = provenance.get("target_client")
    if not isinstance(skill_name, str) or not isinstance(client_id, str):
        return _ambiguous(input_path)
    source = canonical_root / skill_name
    if skill_name not in intent.selected:
        return _unmanaged(input_path, skill=skill_name, source_path=source)
    referenced = any(
        endpoint.client_id == client_id
        and _same_file(deployment, endpoint.skills_root / skill_name)
        for endpoint in endpoints
    )
    try:
        current_source_hash = hash_skill_dir(source)
    except (OSError, ValueError):
        current_source_hash = None
    expected = _expected_current_provenance(
        skill_name,
        source,
        client_id,
        current_source_hash,
    )
    if expected is not None:
        verification = verify_deployment(
            deployment, expected_provenance=expected
        )
        provenance = verification.provenance or provenance
    common = {
        "managed": True,
        "role": "deployment",
        "skill": skill_name,
        "input_path": input_path,
        "source_path": _resolved_text(source),
        "client": client_id,
        "deployment_path": str(deployment),
        "resolution_hash": provenance.get("resolution_hash"),
        "source_hash": provenance.get("source_hash"),
        "rendered_hash": provenance.get("rendered_hash"),
        "referenced": referenced,
    }
    if not verification.ok:
        if verification.state == "stale":
            return OwnershipResult(
                healthy=False,
                state="stale-render",
                migration_required=True,
                **common,
            )
        return OwnershipResult(
            healthy=False,
            state="tampered-render",
            migration_required=True,
            **common,
        )
    if current_source_hash != provenance.get("source_hash"):
        return OwnershipResult(
            healthy=False,
            state="stale-render",
            migration_required=True,
            **common,
        )
    return OwnershipResult(
        healthy=True,
        state="managed-deployment" if referenced else "unreferenced-render",
        migration_required=False,
        **common,
    )


def _deployment_for_path(path: Path, rendered_root: Path) -> Path | None:
    try:
        relative = path.relative_to(rendered_root)
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    digest_dir, skill_name = relative.parts[:2]
    if (
        not digest_dir.startswith("sha256-")
        or len(digest_dir) != 71
        or skill_name in {".", ".."}
    ):
        return None
    return rendered_root / digest_dir / skill_name


def _directory_link_target(destination: Path) -> Path | None:
    try:
        if destination.is_symlink():
            raw = Path(os.readlink(destination))
            if not raw.is_absolute():
                raw = destination.parent / raw
            return _absolute_path(raw)
        if os.name == "nt" and destination.exists():
            return destination.resolve(strict=True)
    except OSError:
        return None
    return None


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _same_file(source: Path, destination: Path) -> bool:
    try:
        return os.path.samefile(source, destination)
    except OSError:
        return False


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _resolved_text(path: Path) -> str:
    """Return a stable canonical-source identity without resolving input links."""
    return str(path.resolve(strict=False))


def _is_name(value: str) -> bool:
    return bool(value) and not Path(value).is_absolute() and len(Path(value).parts) == 1


def _unmanaged(
    input_path: str,
    *,
    skill: str | None = None,
    source_path: Path | None = None,
    client: str | None = None,
) -> OwnershipResult:
    return OwnershipResult(
        managed=False,
        healthy=True,
        state="unmanaged",
        role="unmanaged",
        skill=skill,
        input_path=input_path,
        source_path=None if source_path is None else _resolved_text(source_path),
        client=client,
        migration_required=False,
    )


def _ambiguous(
    input_path: str,
    *,
    skill: str | None = None,
    source_path: Path | None = None,
    client: str | None = None,
) -> OwnershipResult:
    return OwnershipResult(
        managed=False,
        healthy=False,
        state="ambiguous",
        role="unknown",
        skill=skill,
        input_path=input_path,
        source_path=None if source_path is None else _resolved_text(source_path),
        client=client,
        migration_required=False,
    )
