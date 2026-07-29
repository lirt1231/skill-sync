"""Portable Variant packaging for Git-backed Skill repositories."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from skill_sync.copying import copy_skill_dir
from skill_sync.hash import hash_skill_dir, hash_skill_files, is_link_or_reparse
from skill_sync.variant_overlay import plan_variant_overlay


@dataclass(frozen=True, slots=True)
class PortableVariantUnit:
    """One validated, immutable Variant source unit."""

    skill: str
    target: str
    source: Path
    source_identity: tuple[int, int, int, int]
    content_hash: str


def inspect_variant_units(
    *,
    skill: str,
    base_root: str | Path,
    variant_skill_root: str | Path,
    targets: Iterable[str],
) -> tuple[PortableVariantUnit, ...]:
    """Validate and snapshot exactly the registry-declared Variant targets."""

    base = Path(base_root)
    variant_root = Path(variant_skill_root)
    ordered_targets = tuple(sorted(targets))
    if len(set(ordered_targets)) != len(ordered_targets):
        raise ValueError(f"duplicate portable Variant target for {skill}")
    _validate_target_names(ordered_targets)
    if ordered_targets:
        _require_real_directory(variant_root, label="Variant Skill root")

    units: list[PortableVariantUnit] = []
    for target in ordered_targets:
        source = variant_root / target
        plan = plan_variant_overlay(
            base,
            (source,),
            variant_target_names=(target,),
        )
        if len(plan.layers) != 2:
            raise ValueError(f"Variant plan does not contain one target layer: {skill}/{target}")
        layer = plan.layers[1]
        units.append(
            PortableVariantUnit(
                skill=skill,
                target=target,
                source=source.absolute(),
                source_identity=layer.source_identity,
                content_hash=hash_skill_files(
                    (entry.relative_path, entry.content) for entry in layer.files
                ),
            )
        )
    return tuple(units)


def replace_variant_set(
    units: Iterable[PortableVariantUnit],
    destination_skill_root: str | Path,
) -> dict[str, str]:
    """Atomically replace one Skill's target set from validated source units."""

    ordered = tuple(sorted(units, key=lambda unit: unit.target))
    destination = Path(destination_skill_root)
    if len({unit.target for unit in ordered}) != len(ordered):
        raise ValueError("portable Variant set contains duplicate targets")
    if ordered and len({unit.skill for unit in ordered}) != 1:
        raise ValueError("portable Variant set contains multiple Skills")
    _validate_target_names(unit.target for unit in ordered)
    _prepare_real_directory(destination.parent, label="Variant root")

    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.portable-",
            dir=destination.parent,
        )
    )
    staged = temp_root / destination.name
    staged.mkdir()
    hashes: dict[str, str] = {}
    try:
        for unit in ordered:
            if _path_identity(unit.source) != unit.source_identity:
                raise ValueError(
                    f"portable Variant source changed before packaging: {unit.skill}/{unit.target}"
                )
            copied_hash = copy_skill_dir(unit.source, staged / unit.target)
            if copied_hash != unit.content_hash:
                raise ValueError(
                    f"portable Variant source changed while packaging: {unit.skill}/{unit.target}"
                )
            if _path_identity(unit.source) != unit.source_identity:
                raise ValueError(
                    f"portable Variant source changed after packaging: {unit.skill}/{unit.target}"
                )
            hashes[unit.target] = copied_hash

        copied_set_hash = copy_skill_dir(staged, destination)
        if copied_set_hash != hash_skill_dir(staged):
            raise ValueError("portable Variant target set changed during publication")
        return hashes
    finally:
        if temp_root.exists() and not is_link_or_reparse(temp_root):
            shutil.rmtree(temp_root)


def inspect_materialized_targets(
    variant_skill_root: str | Path,
) -> dict[str, str]:
    """Return deterministic hashes for every real target under a local root."""

    root = Path(variant_skill_root)
    if not root.exists() and not is_link_or_reparse(root):
        return {}
    _require_real_directory(root, label="Variant Skill root")
    targets: dict[str, str] = {}
    names: dict[str, str] = {}
    for child in sorted(root.iterdir(), key=lambda path: (path.name.casefold(), path.name)):
        if is_link_or_reparse(child) or not child.is_dir():
            raise ValueError(f"Variant target must be a real directory: {child}")
        _validate_target_names((child.name,))
        identity = child.name.casefold()
        if identity in names:
            raise ValueError(
                "Variant targets contain a case-insensitive duplicate: "
                f"{names[identity]!r}, {child.name!r}"
            )
        names[identity] = child.name
        targets[child.name] = hash_skill_dir(child)
    return targets


def _validate_target_names(targets: Iterable[str]) -> None:
    for target in targets:
        if (
            not isinstance(target, str)
            or not target
            or target in {".", ".."}
            or "/" in target
            or "\\" in target
            or target != target.lower()
        ):
            raise ValueError(f"invalid portable Variant target: {target!r}")


def _prepare_real_directory(path: Path, *, label: str) -> None:
    if path.exists() or is_link_or_reparse(path):
        _require_real_directory(path, label=label)
        return
    path.mkdir(parents=True)
    _require_real_directory(path, label=label)


def _require_real_directory(path: Path, *, label: str) -> None:
    if is_link_or_reparse(path) or not path.is_dir():
        raise ValueError(f"{label} must be a real directory: {path}")


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
