"""Deterministic, file-level overlays for validated Skill variants."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from skill_sync.copying import rename_no_replace
from skill_sync.hash import (
    is_ignored_path,
    is_link_or_reparse,
    portable_skill_file_mode,
)
from skill_sync.variant import (
    VARIANT_MANIFEST_FILE,
    VariantManifest,
    parse_variant_manifest_bytes,
    validate_portable_relative_path,
)


@dataclass(frozen=True, slots=True)
class VariantOverlayFile:
    """One immutable authored-file snapshot in a resolved overlay plan."""

    relative_path: str
    content: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class VariantOverlayLayer:
    """One exact immutable source snapshot used by an overlay plan."""

    source_root: Path
    source_identity: tuple[int, int, int, int]
    files: tuple[VariantOverlayFile, ...]
    manifest: VariantManifest | None


@dataclass(frozen=True, slots=True)
class _SourceTreeSnapshot:
    source_identity: tuple[int, int, int, int]
    files: tuple[VariantOverlayFile, ...]


@dataclass(frozen=True, slots=True)
class VariantOverlayPlan:
    """A read-only, deterministic snapshot ready for materialization."""

    base_root: Path
    variant_roots: tuple[Path, ...]
    files: tuple[VariantOverlayFile, ...]
    layers: tuple[VariantOverlayLayer, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterializedVariantOverlay:
    """The atomically published result of one overlay plan."""

    path: Path
    file_count: int


def plan_variant_overlay(
    base_root: str | Path,
    variant_roots: Iterable[str | Path] = (),
    *,
    variant_target_names: Iterable[str] | None = None,
) -> VariantOverlayPlan:
    """Snapshot Base plus caller-ordered variant layers without writing state.

    The caller owns layer selection and precedence. Passing family before exact
    client implements the platform's Base -> family -> client ordering without
    coupling this pure engine to the Agent registry.
    """

    base = Path(base_root)
    variants = tuple(Path(root) for root in variant_roots)
    targets = (
        tuple(root.name for root in variants)
        if variant_target_names is None
        else tuple(variant_target_names)
    )
    if len(targets) != len(variants):
        raise ValueError("variant target names must match the Variant root count")
    resolved: dict[str, VariantOverlayFile] = {}

    base_snapshot = _stable_source_snapshot(base)
    base_files = tuple(
        source_file
        for source_file in base_snapshot.files
        if source_file.relative_path != VARIANT_MANIFEST_FILE
    )
    layers: list[VariantOverlayLayer] = [
        VariantOverlayLayer(
            source_root=base.absolute(),
            source_identity=base_snapshot.source_identity,
            files=base_files,
            manifest=None,
        )
    ]
    for source_file in base_files:
        resolved[source_file.relative_path.casefold()] = source_file
    _validate_resolved_paths(resolved.values())

    for variant, target in zip(variants, targets):
        variant_snapshot = _stable_source_snapshot(variant)
        manifest_file = next(
            (
                entry
                for entry in variant_snapshot.files
                if entry.relative_path == VARIANT_MANIFEST_FILE
            ),
            None,
        )
        if manifest_file is None:
            raise ValueError(f"Variant source is missing {VARIANT_MANIFEST_FILE}: {variant}")
        manifest = parse_variant_manifest_bytes(
            manifest_file.content,
            directory_target=target,
            expected_target=target,
        )
        for deleted_path in manifest.delete:
            deleted_identity = deleted_path.casefold()
            prefix = deleted_identity + "/"
            resolved = {
                identity: entry
                for identity, entry in resolved.items()
                if identity != deleted_identity and not identity.startswith(prefix)
            }

        layers.append(
            VariantOverlayLayer(
                source_root=variant.absolute(),
                source_identity=variant_snapshot.source_identity,
                files=variant_snapshot.files,
                manifest=manifest,
            )
        )
        for source_file in variant_snapshot.files:
            if source_file.relative_path == VARIANT_MANIFEST_FILE:
                continue
            identity = source_file.relative_path.casefold()
            previous = resolved.get(identity)
            if previous is not None and previous.relative_path != source_file.relative_path:
                raise ValueError(
                    "Overlay contains a case-insensitive path duplicate: "
                    f"{previous.relative_path!r}, {source_file.relative_path!r}"
                )
            resolved[identity] = source_file
        _validate_resolved_paths(resolved.values())

    skill_entry = resolved.get("skill.md")
    if skill_entry is None or skill_entry.relative_path != "SKILL.md":
        raise ValueError("Resolved Skill must contain root SKILL.md")

    files = tuple(
        sorted(
            resolved.values(),
            key=lambda entry: entry.relative_path,
        )
    )
    return VariantOverlayPlan(
        base_root=base.absolute(),
        variant_roots=tuple(root.absolute() for root in variants),
        files=files,
        layers=tuple(layers),
    )


def materialize_variant_overlay(
    plan: VariantOverlayPlan,
    destination: str | Path,
) -> MaterializedVariantOverlay:
    """Stage and atomically publish one immutable overlay plan.

    Existing destinations and concurrent winners are preserved. The plan owns
    byte snapshots, so later source edits cannot create a mixed-layer output.
    """

    if type(plan) is not VariantOverlayPlan:
        raise TypeError("plan must be a VariantOverlayPlan")
    _validate_materialization_plan(plan)
    destination_path = Path(destination)
    if destination_path.exists() or is_link_or_reparse(destination_path):
        raise FileExistsError(f"overlay destination already exists: {destination_path}")
    _reject_destination_inside_sources(plan, destination_path)

    destination_parent = destination_path.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(destination_parent) or not destination_parent.is_dir():
        raise ValueError(
            f"overlay destination parent must be a real directory: {destination_parent}"
        )

    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.tmp-",
            dir=destination_parent,
        )
    )
    staged = temp_root / destination_path.name
    try:
        staged.mkdir()
        for entry in plan.files:
            _write_planned_file(staged, entry)
        _verify_staged_plan(staged, plan)
        rename_no_replace(staged, destination_path)
        return MaterializedVariantOverlay(destination_path, len(plan.files))
    finally:
        if temp_root.exists() and not is_link_or_reparse(temp_root):
            shutil.rmtree(temp_root)


def _stable_source_snapshot(root: Path) -> _SourceTreeSnapshot:
    first = _snapshot_source_tree(root)
    second = _snapshot_source_tree(root)
    if first != second:
        raise ValueError(f"Overlay source changed while planning: {root}")
    return second


def _snapshot_source_tree(root: Path) -> _SourceTreeSnapshot:
    if is_link_or_reparse(root) or not root.is_dir():
        raise ValueError(f"Overlay source must be a real directory, not a link or reparse point: {root}")

    root_before = _path_identity(root)
    if root_before is None:
        raise ValueError(f"Cannot identify overlay source root: {root}")

    files: list[VariantOverlayFile] = []
    identities: dict[str, tuple[str, str]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: (entry.name.casefold(), entry.name),
                reverse=True,
            )
        except OSError as exc:
            raise ValueError(f"Cannot safely inspect overlay source {directory}: {exc}") from exc

        for entry in entries:
            path = Path(entry.path)
            relative_path = path.relative_to(root).as_posix()
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError(
                    f"Overlay source must not contain links or reparse points: {relative_path}"
                )

            is_directory = entry.is_dir(follow_symlinks=False)
            if is_ignored_path(relative_path, is_dir=is_directory):
                continue
            validate_portable_relative_path(relative_path, allow_manifest=True)
            kind = "directory" if is_directory else "file"
            identity = relative_path.casefold()
            previous = identities.get(identity)
            if previous is not None:
                raise ValueError(
                    "Overlay source contains a case-insensitive path duplicate: "
                    f"{previous[0]!r}, {relative_path!r}"
                )
            identities[identity] = (relative_path, kind)

            if is_directory:
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                content, mode = _read_regular_file_snapshot(path)
                files.append(VariantOverlayFile(relative_path, content, mode))
            else:
                raise ValueError(
                    f"Overlay source contains an unsupported filesystem entry: {relative_path}"
                )

    root_after = _path_identity(root)
    if root_after != root_before:
        raise ValueError(f"Overlay source root changed while planning: {root}")
    files.sort(key=lambda item: item.relative_path)
    return _SourceTreeSnapshot(root_before, tuple(files))


def _read_regular_file_snapshot(path: Path) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Cannot safely read overlay file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Overlay source is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    identity_before = _file_snapshot_identity(before)
    identity_after = _file_snapshot_identity(after)
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"Overlay file changed while reading: {path}") from exc
    identity_current = _file_snapshot_identity(current)
    if identity_before != identity_after or identity_after != identity_current:
        raise ValueError(f"Overlay file changed while reading: {path}")
    return content, portable_skill_file_mode(content)


def _file_snapshot_identity(metadata: os.stat_result) -> tuple[int, ...]:
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    if os.name == "nt":
        return identity
    return identity + (
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode) & 0o777,
    )


def _validate_resolved_paths(files: Iterable[VariantOverlayFile]) -> None:
    nodes: dict[str, tuple[str, str]] = {}
    for entry in sorted(files, key=lambda item: item.relative_path):
        parts = entry.relative_path.split("/")
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            _register_resolved_node(nodes, directory, "directory")
        _register_resolved_node(nodes, entry.relative_path, "file")


def _validate_materialization_plan(plan: VariantOverlayPlan) -> None:
    if not isinstance(plan.base_root, Path) or type(plan.variant_roots) is not tuple:
        raise ValueError("Overlay plan source roots have an invalid shape")
    if any(not isinstance(root, Path) for root in plan.variant_roots):
        raise ValueError("Overlay plan variant roots have an invalid shape")
    if type(plan.files) is not tuple:
        raise ValueError("Overlay plan files must be an immutable tuple")

    identities: set[str] = set()
    for entry in plan.files:
        if type(entry) is not VariantOverlayFile:
            raise ValueError("Overlay plan contains an invalid file entry")
        relative_path = validate_portable_relative_path(
            entry.relative_path,
            allow_manifest=True,
        )
        if relative_path == VARIANT_MANIFEST_FILE:
            raise ValueError("Resolved overlay must not contain root variant.yaml")
        identity = relative_path.casefold()
        if identity in identities:
            raise ValueError(
                f"Overlay plan contains a case-insensitive path duplicate: {relative_path!r}"
            )
        identities.add(identity)
        if type(entry.content) is not bytes:
            raise ValueError(f"Overlay plan content must be immutable bytes: {relative_path}")
        if (
            type(entry.mode) is not int
            or not 0 <= entry.mode <= 0o777
            or portable_skill_file_mode(entry.content) != entry.mode
        ):
            raise ValueError(f"Overlay plan mode is invalid: {relative_path}")

    if tuple(sorted(plan.files, key=lambda item: item.relative_path)) != plan.files:
        raise ValueError("Overlay plan files must use deterministic POSIX path order")
    _validate_resolved_paths(plan.files)
    skill_entries = [entry for entry in plan.files if entry.relative_path == "SKILL.md"]
    if len(skill_entries) != 1:
        raise ValueError("Overlay plan must contain exactly one root SKILL.md")


def _register_resolved_node(
    nodes: dict[str, tuple[str, str]],
    path: str,
    kind: str,
) -> None:
    identity = path.casefold()
    previous = nodes.get(identity)
    if previous is None:
        nodes[identity] = (path, kind)
        return
    previous_path, previous_kind = previous
    if previous_kind != kind:
        raise ValueError(
            f"Overlay creates a file/directory collision at {path!r}"
        )
    if previous_path != path:
        raise ValueError(
            "Overlay contains a case-insensitive path duplicate: "
            f"{previous_path!r}, {path!r}"
        )


def _write_planned_file(root: Path, entry: VariantOverlayFile) -> None:
    destination = root.joinpath(*entry.relative_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(entry.content)
    destination.chmod(entry.mode)


def _verify_staged_plan(root: Path, plan: VariantOverlayPlan) -> None:
    expected = {
        entry.relative_path: (entry.content, entry.mode)
        for entry in plan.files
    }
    actual: dict[str, tuple[bytes, int]] = {}
    for path in root.rglob("*"):
        if is_link_or_reparse(path):
            raise ValueError(f"Staged overlay contains a link or reparse point: {path}")
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = (
                (content := path.read_bytes()),
                _materialized_file_mode(path.stat().st_mode, content),
            )
        elif not path.is_dir():
            raise ValueError(f"Staged overlay contains an unsupported entry: {path}")
    if actual != expected:
        raise ValueError("Staged overlay does not match the immutable resolution plan")


def _materialized_file_mode(
    st_mode: int,
    content: bytes,
    *,
    platform: str | None = None,
) -> int:
    platform_name = os.name if platform is None else platform
    permission_bits = stat.S_IMODE(st_mode) & 0o777
    if platform_name == "nt":
        return portable_skill_file_mode(content)
    return permission_bits


def _reject_destination_inside_sources(
    plan: VariantOverlayPlan,
    destination: Path,
) -> None:
    resolved_destination = destination.resolve(strict=False)
    for source in (plan.base_root, *plan.variant_roots):
        resolved_source = source.resolve(strict=False)
        if resolved_destination == resolved_source or resolved_destination.is_relative_to(
            resolved_source
        ):
            raise ValueError(
                f"overlay destination must not be a source or inside a source: {destination}"
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
