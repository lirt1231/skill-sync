"""Read-only client resolution and Base-to-client Variant diff models."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_sync.agents import AGENT_FAMILIES
from skill_sync.errors import SkillSyncError
from skill_sync.protocol import EXIT_SAFETY
from skill_sync.variant import VARIANT_MANIFEST_FILE
from skill_sync.variant_overlay import VariantOverlayFile
from skill_sync.variant_resolution import (
    LayeredVariantResolution,
    ResolutionLayerProvenance,
    resolve_variant_for_client,
)
from skill_sync.variant_source import (
    resolve_variant_source_paths,
    verify_variant_source_paths,
)


MAX_TEXT_DIFF_INPUT_BYTES = 64 * 1024
MAX_TOTAL_TEXT_DIFF_INPUT_BYTES = 256 * 1024

_CLIENT_FAMILIES = {
    client: family.id
    for family in AGENT_FAMILIES
    for client in family.client_ids
}


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    skill: str
    resolution: LayeredVariantResolution


@dataclass(frozen=True, slots=True)
class _FileOrigin:
    role: str
    target: str | None


def resolve_variant_dry_run(
    skill: str,
    *,
    client: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Describe a client resolution without materializing or mutating state."""

    resolved = _resolve(skill, client=client, config_path=config_path)
    resolution = resolved.resolution
    origins = _resolved_origins(resolution)
    files = [
        {
            "path": entry.relative_path,
            **_file_metadata(entry),
            "source_role": origins[entry.relative_path.casefold()].role,
            "source_target": origins[entry.relative_path.casefold()].target,
        }
        for entry in resolution.overlay_plan.files
    ]
    return {
        "skill": resolved.skill,
        "client": resolution.target_client,
        "family": resolution.family,
        "mode": "dry-run",
        "resolver_version": resolution.resolver_version,
        "output_hash": resolution.output_hash,
        "resolution_hash": resolution.resolution_hash,
        "applied_variant_targets": list(resolution.applied_variant_targets),
        "file_count": len(files),
        "layers": [_layer_dict(layer) for layer in resolution.layers],
        "files": files,
    }


def diff_base_to_client(
    skill: str,
    *,
    client: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare immutable Base bytes with one resolved client output."""

    resolved = _resolve(skill, client=client, config_path=config_path)
    resolution = resolved.resolution
    base_files = {
        entry.relative_path.casefold(): entry
        for entry in resolution.overlay_plan.layers[0].files
        if entry.relative_path != VARIANT_MANIFEST_FILE
    }
    client_files = {
        entry.relative_path.casefold(): entry
        for entry in resolution.overlay_plan.files
    }
    changes: list[dict[str, Any]] = []
    unchanged = 0
    text_input_bytes = 0
    total_budget_exhausted = False
    for identity in sorted(
        set(base_files) | set(client_files),
        key=lambda value: _display_path(value, base_files, client_files),
    ):
        base = base_files.get(identity)
        client_file = client_files.get(identity)
        if base is not None and client_file is not None and _same_file(base, client_file):
            unchanged += 1
            continue
        if base is None:
            change = "added"
        elif client_file is None:
            change = "deleted"
        else:
            change = "modified"
        changed_file = client_file if client_file is not None else base
        if changed_file is None:  # pragma: no cover - guaranteed by the path union
            raise ValueError("diff path has neither Base nor client content")
        path = changed_file.relative_path
        input_size = _diff_input_size(base, client_file)
        safe_texts = None
        omit_reason = None
        if input_size <= MAX_TEXT_DIFF_INPUT_BYTES:
            safe_texts = _safe_diff_texts(base, client_file)
            if safe_texts is not None:
                if (
                    total_budget_exhausted
                    or text_input_bytes + input_size > MAX_TOTAL_TEXT_DIFF_INPUT_BYTES
                ):
                    total_budget_exhausted = True
                    omit_reason = "total_size_limit"
                else:
                    text_input_bytes += input_size
        changes.append(
            _file_diff(
                path,
                change,
                base,
                client_file,
                safe_texts=safe_texts,
                omit_reason=omit_reason,
            )
        )

    summary = {
        "added": sum(item["change"] == "added" for item in changes),
        "modified": sum(item["change"] == "modified" for item in changes),
        "deleted": sum(item["change"] == "deleted" for item in changes),
        "total": len(changes),
    }
    base_layer = resolution.layers[0]
    return {
        "skill": resolved.skill,
        "comparison": "base-to-client",
        "client": resolution.target_client,
        "family": resolution.family,
        "resolver_version": resolution.resolver_version,
        "base_hash": base_layer.content_hash,
        "output_hash": resolution.output_hash,
        "resolution_hash": resolution.resolution_hash,
        "applied_variant_targets": list(resolution.applied_variant_targets),
        "changed": bool(changes),
        "unchanged_file_count": unchanged,
        "summary": summary,
        "files": changes,
    }


def _resolve(
    skill: str,
    *,
    client: str,
    config_path: str | Path | None,
) -> _ResolvedSource:
    if client not in _CLIENT_FAMILIES:
        raise SkillSyncError(
            f"unknown concrete Agent client ID: {client}",
            code="variant_client_unknown",
            details={"client": client, "known": sorted(_CLIENT_FAMILIES)},
        )
    sources = resolve_variant_source_paths(skill, config_path=config_path)
    verify_variant_source_paths(sources)
    try:
        resolution = resolve_variant_for_client(
            sources.base_root,
            sources.variant_skill_root,
            client,
        )
    except (OSError, ValueError) as exc:
        verify_variant_source_paths(sources)
        raise SkillSyncError(
            f"Variant resolution is invalid: {exc}",
            code="variant_resolution_invalid",
            exit_code=EXIT_SAFETY,
            details={"skill": sources.skill, "client": client},
        ) from exc
    verify_variant_source_paths(sources, resolution=resolution)
    return _ResolvedSource(sources.skill, resolution)


def _resolved_origins(
    resolution: LayeredVariantResolution,
) -> dict[str, _FileOrigin]:
    origins: dict[str, _FileOrigin] = {}
    for layer_index, (provenance, overlay_layer) in enumerate(
        zip(resolution.layers, resolution.overlay_plan.layers)
    ):
        if layer_index:
            for deleted in provenance.delete:
                identity = deleted.casefold()
                prefix = identity + "/"
                origins = {
                    key: value
                    for key, value in origins.items()
                    if key != identity and not key.startswith(prefix)
                }
        for entry in overlay_layer.files:
            if entry.relative_path == VARIANT_MANIFEST_FILE:
                continue
            origins[entry.relative_path.casefold()] = _FileOrigin(
                provenance.role,
                provenance.target,
            )
    final_identities = {
        entry.relative_path.casefold() for entry in resolution.overlay_plan.files
    }
    if set(origins) != final_identities:
        raise ValueError("resolved file origins do not match the immutable overlay plan")
    return origins


def _layer_dict(layer: ResolutionLayerProvenance) -> dict[str, Any]:
    return {
        "role": layer.role,
        "target": layer.target,
        "source_path": str(layer.source_path),
        "content_hash": layer.content_hash,
        "mode": layer.mode,
        "delete": list(layer.delete),
    }


def _file_metadata(entry: VariantOverlayFile) -> dict[str, Any]:
    return {
        "size": len(entry.content),
        "hash": "sha256:" + hashlib.sha256(entry.content).hexdigest(),
        "mode": f"{entry.mode:04o}",
    }


def _same_file(first: VariantOverlayFile, second: VariantOverlayFile) -> bool:
    return first.content == second.content and first.mode == second.mode


def _display_path(
    identity: str,
    base: dict[str, VariantOverlayFile],
    client: dict[str, VariantOverlayFile],
) -> str:
    entry = client.get(identity) or base[identity]
    return entry.relative_path


def _file_diff(
    path: str,
    change: str,
    base: VariantOverlayFile | None,
    client: VariantOverlayFile | None,
    *,
    safe_texts: tuple[str, str] | None,
    omit_reason: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path,
        "change": change,
        "base": None if base is None else _file_metadata(base),
        "client": None if client is None else _file_metadata(client),
    }
    if _diff_input_size(base, client) > MAX_TEXT_DIFF_INPUT_BYTES:
        item.update({"kind": "large", "diff_omitted": "size_limit"})
        return item
    if safe_texts is None:
        item["kind"] = "binary"
        return item
    if omit_reason is not None:
        item.update({"kind": "text", "diff_omitted": omit_reason})
        return item
    old_text, new_text = safe_texts
    item["kind"] = "text"
    item["diff"] = _unified_diff(path, old_text, new_text)
    return item


def _safe_diff_texts(
    base: VariantOverlayFile | None,
    client: VariantOverlayFile | None,
) -> tuple[str, str] | None:
    old_text = "" if base is None else _safe_text(base.content)
    new_text = "" if client is None else _safe_text(client.content)
    if old_text is None or new_text is None:
        return None
    return old_text, new_text


def _diff_input_size(
    base: VariantOverlayFile | None,
    client: VariantOverlayFile | None,
) -> int:
    return sum(
        len(entry.content)
        for entry in (base, client)
        if entry is not None
    )


def _safe_text(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in text
    ):
        return None
    return text


def _unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
