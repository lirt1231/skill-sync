"""Select Variant layers and describe one deterministic client resolution."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from skill_sync.agents import AGENT_FAMILIES
from skill_sync.hash import hash_skill_files_with_modes, is_link_or_reparse
from skill_sync.variant_overlay import (
    VariantOverlayLayer,
    VariantOverlayPlan,
    plan_variant_overlay,
)


VARIANT_RESOLVER_VERSION = "variant-overlay-v2"


@dataclass(frozen=True, slots=True)
class ResolutionLayerProvenance:
    """Immutable explanation of one authored source applied to a resolution."""

    role: str
    target: str | None
    source_path: Path
    content_hash: str
    mode: str | None = None
    delete: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VariantResolutionProvenance:
    """Portable hash inputs plus local source evidence for one client output."""

    resolver_version: str
    target_client: str
    family: str
    layers: tuple[ResolutionLayerProvenance, ...]
    output_hash: str
    resolution_hash: str

    @property
    def applied_variant_targets(self) -> tuple[str, ...]:
        return tuple(layer.target for layer in self.layers if layer.target is not None)


@dataclass(frozen=True, slots=True)
class LayeredVariantResolution:
    """An immutable overlay plan paired with its resolution provenance."""

    provenance: VariantResolutionProvenance
    overlay_plan: VariantOverlayPlan

    @property
    def resolver_version(self) -> str:
        return self.provenance.resolver_version

    @property
    def target_client(self) -> str:
        return self.provenance.target_client

    @property
    def family(self) -> str:
        return self.provenance.family

    @property
    def layers(self) -> tuple[ResolutionLayerProvenance, ...]:
        return self.provenance.layers

    @property
    def output_hash(self) -> str:
        return self.provenance.output_hash

    @property
    def resolution_hash(self) -> str:
        return self.provenance.resolution_hash

    @property
    def applied_variant_targets(self) -> tuple[str, ...]:
        return self.provenance.applied_variant_targets


@dataclass(frozen=True, slots=True)
class _VariantTargetObservation:
    name: str
    path: Path
    identity: tuple[int, int, int, int]
    is_real_directory: bool


@dataclass(frozen=True, slots=True)
class _VariantTargetScan:
    root_identity: tuple[int, int, int, int] | None
    targets: tuple[_VariantTargetObservation, ...]


@dataclass(frozen=True, slots=True)
class _ApplicableVariant:
    role: str
    target: str
    path: Path
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ApplicableSelection:
    root_identity: tuple[int, int, int, int] | None
    variants: tuple[_ApplicableVariant, ...]


def resolve_variant_for_client(
    base_root: str | Path,
    variant_skill_root: str | Path,
    target_client: str,
    *,
    override_target: str | None = None,
    override_root: str | Path | None = None,
) -> LayeredVariantResolution:
    """Resolve Base -> family -> exact-client layers for one registered client.

    ``variant_skill_root`` is the logical Skill's directory below the portable
    Variant root, for example ``~/.agents/variants/meeting-note``. Missing
    family or client layers are valid. A family whose ID is also its sole
    client ID (Codex and WorkBuddy) applies that shared source once.

    This function is read-only. Layer and output hashes are computed only from
    the exact immutable snapshots owned by the 7.2 overlay plan. Applicable
    targets and directory identities are scanned before planning and again
    before return so concurrent source selection changes fail closed.
    """

    family = _family_for_client(target_client)
    base = Path(base_root)
    variant_root = Path(variant_skill_root)

    if (override_target is None) != (override_root is None):
        raise ValueError("override target and root must be provided together")

    selection = _capture_applicable_selection(variant_root, family, target_client)
    plan_selection = selection
    if override_target is not None and override_root is not None:
        plan_selection = _selection_with_override(
            selection,
            family=family,
            target_client=target_client,
            override_target=override_target,
            override_root=Path(override_root),
        )
    overlay_plan = plan_variant_overlay(
        base,
        (variant.path for variant in plan_selection.variants),
        variant_target_names=(
            variant.target for variant in plan_selection.variants
        ),
    )
    _verify_plan_selection(overlay_plan, plan_selection)
    final_selection = _capture_applicable_selection(
        variant_root,
        family,
        target_client,
    )
    if final_selection != selection:
        raise ValueError("Variant selection changed while resolving")
    _verify_selection_identities(plan_selection)

    base_layer = overlay_plan.layers[0]
    layers: list[ResolutionLayerProvenance] = [
        _layer_provenance(base_layer, role="base", target=None)
    ]
    for selected, plan_layer in zip(plan_selection.variants, overlay_plan.layers[1:]):
        layers.append(
            _layer_provenance(
                plan_layer,
                role=selected.role,
                target=selected.target,
            )
        )

    immutable_layers = tuple(layers)
    output_hash = hash_skill_files_with_modes(
        (entry.relative_path, entry.content, entry.mode)
        for entry in overlay_plan.files
    )
    resolution_hash = _resolution_hash(
        target_client=target_client,
        family=family,
        layers=immutable_layers,
    )
    provenance = VariantResolutionProvenance(
        resolver_version=VARIANT_RESOLVER_VERSION,
        target_client=target_client,
        family=family,
        layers=immutable_layers,
        output_hash=output_hash,
        resolution_hash=resolution_hash,
    )
    return LayeredVariantResolution(provenance, overlay_plan)


def _selection_with_override(
    selection: _ApplicableSelection,
    *,
    family: str,
    target_client: str,
    override_target: str,
    override_root: Path,
) -> _ApplicableSelection:
    requested = _requested_variant_roles(family, target_client)
    requested_roles = {target: role for role, target in requested}
    if override_target not in requested_roles:
        raise ValueError(
            f"Variant override {override_target!r} does not affect client {target_client!r}"
        )
    identity = _path_identity(override_root)
    if identity is None or is_link_or_reparse(override_root) or not override_root.is_dir():
        raise ValueError(f"Variant override must be a real directory: {override_root}")
    canonical = {variant.target: variant for variant in selection.variants}
    variants = tuple(
        _ApplicableVariant(
            role=role,
            target=target,
            path=override_root.absolute(),
            identity=identity,
        )
        if target == override_target
        else canonical[target]
        for role, target in requested
        if target == override_target or target in canonical
    )
    return _ApplicableSelection(selection.root_identity, variants)


def _family_for_client(target_client: str) -> str:
    for family in AGENT_FAMILIES:
        if target_client in family.client_ids:
            return family.id
    if any(target_client == family.id for family in AGENT_FAMILIES):
        raise ValueError(
            f"target must be a concrete Agent client ID, not family {target_client!r}"
        )
    raise ValueError(f"unknown Agent client: {target_client!r}")


def _scan_variant_targets(root: Path) -> _VariantTargetScan:
    if not root.exists() and not is_link_or_reparse(root):
        return _VariantTargetScan(None, ())
    if is_link_or_reparse(root) or not root.is_dir():
        raise ValueError(f"Variant Skill root must be a real directory: {root}")
    root_before = _path_identity(root)
    if root_before is None:
        raise ValueError(f"Cannot identify Variant Skill root: {root}")
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise ValueError(f"Cannot inspect Variant Skill root {root}: {exc}") from exc
    targets: list[_VariantTargetObservation] = []
    for entry in entries:
        path = Path(entry.path)
        identity = _path_identity(path)
        if identity is None:
            raise ValueError(f"Variant target changed while scanning: {path}")
        targets.append(
            _VariantTargetObservation(
                name=entry.name,
                path=path.absolute(),
                identity=identity,
                is_real_directory=(
                    not entry.is_symlink()
                    and not is_link_or_reparse(path)
                    and entry.is_dir(follow_symlinks=False)
                ),
            )
        )
    for observation in targets:
        if _path_identity(observation.path) != observation.identity:
            raise ValueError(
                f"Variant target changed while scanning: {observation.path}"
            )
    root_after = _path_identity(root)
    if root_after != root_before:
        raise ValueError(f"Variant Skill root changed while scanning: {root}")
    return _VariantTargetScan(
        root_identity=root_before,
        targets=tuple(
            sorted(targets, key=lambda item: (item.name.casefold(), item.name))
        ),
    )


def _capture_applicable_selection(
    root: Path,
    family: str,
    target_client: str,
) -> _ApplicableSelection:
    scan = _scan_variant_targets(root)
    requested = _requested_variant_roles(family, target_client)

    selected: list[_ApplicableVariant] = []
    for role, target in requested:
        observation = _select_variant_target(scan, target)
        if observation is not None:
            selected.append(
                _ApplicableVariant(
                    role=role,
                    target=target,
                    path=observation.path,
                    identity=observation.identity,
                )
            )
    return _ApplicableSelection(scan.root_identity, tuple(selected))


def _requested_variant_roles(
    family: str,
    target_client: str,
) -> tuple[tuple[str, str], ...]:
    if family == target_client:
        return (("family-client", family),)
    return (("family", family), ("client", target_client))


def _select_variant_target(
    scan: _VariantTargetScan,
    target: str,
) -> _VariantTargetObservation | None:
    matches = [entry for entry in scan.targets if entry.name.casefold() == target.casefold()]
    if len(matches) > 1 or (matches and matches[0].name != target):
        names = sorted(entry.name for entry in matches)
        raise ValueError(
            f"Variant target has a case-insensitive ambiguity for {target!r}: {names}"
        )
    if not matches:
        return None
    observation = matches[0]
    if not observation.is_real_directory:
        raise ValueError(f"Variant target must be a real directory: {observation.path}")
    return observation


def _verify_plan_selection(
    plan: VariantOverlayPlan,
    selection: _ApplicableSelection,
) -> None:
    if len(plan.layers) != 1 + len(selection.variants):
        raise ValueError("Overlay plan does not expose the selected source layers")
    for selected, layer in zip(selection.variants, plan.layers[1:]):
        if (
            layer.source_root != selected.path
            or layer.source_identity != selected.identity
        ):
            raise ValueError("Variant selection changed while planning overlay")


def _verify_selection_identities(selection: _ApplicableSelection) -> None:
    for selected in selection.variants:
        if _path_identity(selected.path) != selected.identity:
            raise ValueError("Variant selection changed while resolving")


def _layer_provenance(
    layer: VariantOverlayLayer,
    *,
    role: str,
    target: str | None,
) -> ResolutionLayerProvenance:
    manifest = layer.manifest
    if target is None and manifest is not None:
        raise ValueError("Base overlay layer must not contain a Variant manifest")
    if target is not None and (manifest is None or manifest.target != target):
        raise ValueError("Variant overlay layer manifest does not match selection")
    return ResolutionLayerProvenance(
        role=role,
        target=target,
        source_path=layer.source_root,
        content_hash=hash_skill_files_with_modes(
            (entry.relative_path, entry.content, entry.mode)
            for entry in layer.files
        ),
        mode=None if manifest is None else manifest.mode,
        delete=() if manifest is None else manifest.delete,
    )


def _path_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_mtime_ns if os.name == "nt" else metadata.st_ctime_ns,
    )


def _resolution_hash(
    *,
    target_client: str,
    family: str,
    layers: tuple[ResolutionLayerProvenance, ...],
) -> str:
    return resolution_hash_for_layers(
        target_client=target_client,
        family=family,
        layers=tuple(
            (layer.role, layer.target, layer.content_hash) for layer in layers
        ),
    )


def resolution_hash_for_layers(
    *,
    target_client: str,
    family: str,
    layers: tuple[tuple[str, str | None, str], ...],
) -> str:
    """Recompute a portable resolution hash from persisted layer evidence."""

    digest = hashlib.sha256()
    digest.update(b"skill-sync-variant-resolution\0")
    _update_framed_field(digest, "resolver-version", VARIANT_RESOLVER_VERSION)
    _update_framed_field(digest, "target-client", target_client)
    _update_framed_field(digest, "family", family)
    _update_framed_field(digest, "layer-count", str(len(layers)))
    for role, target, content_hash in layers:
        _update_framed_field(digest, "layer-role", role)
        _update_framed_field(digest, "layer-target", target or "")
        _update_framed_field(digest, "layer-hash", content_hash)
    return f"sha256:{digest.hexdigest()}"


def _update_framed_field(digest: object, name: str, value: str) -> None:
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    digest.update(struct.pack(">Q", len(name_bytes)))
    digest.update(name_bytes)
    digest.update(struct.pack(">Q", len(value_bytes)))
    digest.update(value_bytes)
