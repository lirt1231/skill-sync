"""Strict, dependency-free parsing for portable Skill variant manifests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from skill_sync.agents import AGENT_FAMILIES
from skill_sync.hash import is_link_or_reparse
from skill_sync.registry import parse_registry_text


VARIANT_MANIFEST_FILE = "variant.yaml"
VARIANT_MANIFEST_VERSION = 1
VARIANT_MODE_OVERLAY = "overlay"
_ALLOWED_FIELDS = frozenset({"version", "target", "mode", "delete"})
_REQUIRED_FIELDS = frozenset({"version", "target", "mode"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"\\|?*')


@dataclass(frozen=True, slots=True)
class VariantManifest:
    """Validated manifest metadata without any resolved Skill content."""

    version: int
    target: str
    mode: str
    delete: tuple[str, ...] = ()


def known_variant_targets() -> frozenset[str]:
    """Return every registered Agent family and concrete client ID."""

    return frozenset(
        target
        for family in AGENT_FAMILIES
        for target in (family.id, *family.client_ids)
    )


def load_variant_manifest(
    path: str | Path,
    *,
    expected_target: str | None = None,
) -> VariantManifest:
    """Read and validate one ``variant.yaml`` without changing local state.

    The manifest uses the same small mapping-only YAML subset as the portable
    registry. ``delete`` accepts either one relative path string or a mapping
    of ``path: true`` entries for multiple deterministic deletions.
    """

    manifest_path = Path(path)
    if manifest_path.name != VARIANT_MANIFEST_FILE:
        raise ValueError(
            f"Variant manifest must be named {VARIANT_MANIFEST_FILE}: {manifest_path}"
        )
    if is_link_or_reparse(manifest_path):
        raise ValueError(f"Variant manifest must not be a link or reparse point: {manifest_path}")
    if not manifest_path.is_file():
        raise ValueError(f"Variant manifest must be a regular file: {manifest_path}")

    variant_root = manifest_path.parent
    _validate_variant_tree(variant_root)
    try:
        content = manifest_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read Variant manifest: {manifest_path}") from exc
    return parse_variant_manifest_bytes(
        content,
        directory_target=variant_root.name,
        expected_target=expected_target,
    )


def parse_variant_manifest_bytes(
    content: bytes,
    *,
    directory_target: str,
    expected_target: str | None = None,
) -> VariantManifest:
    """Parse manifest semantics from the exact immutable authored bytes."""

    if type(content) is not bytes:
        raise ValueError("Variant manifest snapshot must be immutable bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Variant manifest must be valid UTF-8") from exc
    raw = parse_registry_text(text)
    if not isinstance(raw, dict):
        raise ValueError("Variant manifest root must be a mapping")

    unknown = set(raw) - _ALLOWED_FIELDS
    missing = _REQUIRED_FIELDS - set(raw)
    if unknown:
        raise ValueError("Unknown variant manifest field(s): " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("Missing variant manifest field(s): " + ", ".join(sorted(missing)))

    version = raw["version"]
    if type(version) is not int or version != VARIANT_MANIFEST_VERSION:
        raise ValueError(f"Variant manifest version must be {VARIANT_MANIFEST_VERSION}")

    target = raw["target"]
    if not isinstance(target, str) or target not in known_variant_targets():
        raise ValueError(f"Unknown variant target: {target!r}")
    if target != directory_target:
        raise ValueError(
            f"Variant target {target!r} does not match directory {directory_target!r}"
        )
    if expected_target is not None and target != expected_target:
        raise ValueError(
            f"Variant target {target!r} does not match expected target {expected_target!r}"
        )

    mode = raw["mode"]
    if mode != VARIANT_MODE_OVERLAY:
        raise ValueError(f"Variant mode must be {VARIANT_MODE_OVERLAY!r}")

    delete = _parse_delete_paths(raw.get("delete"))
    return VariantManifest(version=version, target=target, mode=mode, delete=delete)


def _parse_delete_paths(value: Any) -> tuple[str, ...]:
    if value is None:
        values: Iterable[str] = ()
    elif isinstance(value, str):
        values = (value,)
    elif isinstance(value, dict):
        for path, enabled in value.items():
            if not isinstance(path, str) or enabled is not True:
                raise ValueError("Variant delete mapping must contain only path: true entries")
        values = value.keys()
    else:
        raise ValueError("Variant delete must be a relative path or path: true mapping")

    normalized: list[str] = []
    identities: dict[str, str] = {}
    for raw_path in values:
        path = validate_portable_relative_path(raw_path)
        identity = path.casefold()
        previous = identities.get(identity)
        if previous is not None:
            raise ValueError(
                f"Variant delete paths contain a case-insensitive duplicate: {previous!r}, {path!r}"
            )
        identities[identity] = path
        normalized.append(path)
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def validate_portable_relative_path(
    value: str,
    *,
    allow_manifest: bool = False,
) -> str:
    """Validate one normalized, cross-platform relative Variant path."""

    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("Variant paths must be non-empty unpadded strings")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Variant paths must not contain control characters")
    if any(character in value for character in _WINDOWS_RESERVED_CHARACTERS):
        raise ValueError(f"Variant path is not portable: {value!r}")

    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"Variant path must be relative: {value!r}")
    if value.endswith("/") or posix_path.as_posix() != value:
        raise ValueError(f"Variant path must be normalized POSIX syntax: {value!r}")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError(f"Variant path escapes or aliases the Skill root: {value!r}")
    if value == VARIANT_MANIFEST_FILE and not allow_manifest:
        raise ValueError("Variant manifest cannot delete itself")

    for part in posix_path.parts:
        if part.endswith((" ", ".")):
            raise ValueError(f"Variant path is not portable to Windows: {value!r}")
        device_name = part.split(".", 1)[0].casefold()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Variant path uses a reserved Windows name: {value!r}")
    return value


def _validate_variant_tree(root: Path) -> None:
    if is_link_or_reparse(root) or not root.is_dir():
        raise ValueError(f"Variant root must be a real directory: {root}")

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ValueError(f"Cannot safely inspect variant directory {directory}: {exc}") from exc
        for entry in entries:
            child = Path(entry.path)
            if entry.is_symlink() or is_link_or_reparse(child):
                raise ValueError(f"Variant source must not contain links or reparse points: {child}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)
            elif not entry.is_file(follow_symlinks=False):
                raise ValueError(f"Variant source contains an unsupported filesystem entry: {child}")
