"""Deterministic hashing for Agent Skill directories."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from collections.abc import Iterable
from pathlib import Path

IGNORED_DIR_NAMES = frozenset({"__pycache__", ".git"})
IGNORED_FILE_NAMES = frozenset({".DS_Store"})


def is_ignored_path(relative_path: str, *, is_dir: bool = False) -> bool:
    """Return whether a POSIX relative path is excluded from Skill copies/hashes."""
    parts = tuple(part for part in relative_path.split("/") if part)
    if any(part in IGNORED_DIR_NAMES for part in parts[:-1]):
        return True
    if not parts:
        return False
    name = parts[-1]
    if is_dir:
        return name in IGNORED_DIR_NAMES
    return name in IGNORED_FILE_NAMES


def is_link_or_reparse(path: str | Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""

    candidate = Path(path)
    if candidate.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(candidate), "st_file_attributes", 0)
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def hash_skill_dir(path: str | Path) -> str:
    """Return the deterministic SHA-256 content hash for a Skill directory.

    The digest is computed over regular files in sorted POSIX relative path
    order. Each file is framed with a literal tag and explicit path/content
    byte lengths so binary content cannot create ambiguous boundaries.
    """
    root = Path(path)
    if is_link_or_reparse(root):
        raise ValueError(f"Cannot hash skill directory symlink or reparse point: {root}")
    if not root.is_dir():
        raise ValueError(f"Skill path is not a directory: {root}")

    return hash_skill_files(
        (relative_path, file_path.read_bytes())
        for relative_path, file_path in _iter_hashable_files(root)
    )


def hash_skill_files(files: Iterable[tuple[str, bytes]]) -> str:
    """Hash an immutable Skill file snapshot with directory-hash framing.

    Paths are sorted before hashing so callers such as the Variant resolver can
    hash an in-memory overlay plan without materializing it first. File modes
    remain intentionally excluded, matching :func:`hash_skill_dir`.
    """

    entries = tuple(files)
    for relative_path, content in entries:
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("Skill hash paths must be non-empty strings")
        if type(content) is not bytes:
            raise ValueError(f"Skill hash content must be immutable bytes: {relative_path}")
    paths = [relative_path for relative_path, _ in entries]
    if len(set(paths)) != len(paths):
        raise ValueError("Skill hash snapshot contains duplicate paths")

    digest = hashlib.sha256()
    for relative_path, content in sorted(entries, key=lambda item: item[0]):
        _update_file_hash(digest, relative_path, content)
    return f"sha256:{digest.hexdigest()}"


def portable_skill_file_mode(content: bytes) -> int:
    """Return a host-independent planned mode derived only from file bytes.

    A file whose immutable content starts with a shebang is executable
    (``0755``); every other regular file is ``0644``. Host ``st_mode`` is never
    an authored input, so the same source bytes resolve identically on POSIX
    and Windows filesystems.
    """

    if type(content) is not bytes:
        raise ValueError("Skill mode content must be immutable bytes")
    return 0o755 if content.startswith(b"#!") else 0o644


def hash_skill_files_with_modes(
    files: Iterable[tuple[str, bytes, int]],
) -> str:
    """Hash immutable Skill files including normalized executable semantics."""

    entries = tuple(files)
    paths: list[str] = []
    normalized: list[tuple[str, bytes, int]] = []
    for relative_path, content, mode in entries:
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("Skill hash paths must be non-empty strings")
        if type(content) is not bytes:
            raise ValueError(f"Skill hash content must be immutable bytes: {relative_path}")
        paths.append(relative_path)
        expected_mode = portable_skill_file_mode(content)
        if mode != expected_mode:
            raise ValueError(
                f"Skill hash mode does not match portable content mode: {relative_path}"
            )
        normalized.append((relative_path, content, expected_mode))
    if len(set(paths)) != len(paths):
        raise ValueError("Skill hash snapshot contains duplicate paths")

    digest = hashlib.sha256()
    for relative_path, content, mode in sorted(normalized, key=lambda item: item[0]):
        digest.update(b"file-mode-v1\0")
        _update_length_prefixed(digest, relative_path.encode("utf-8"))
        _update_length_prefixed(digest, f"{mode:04o}".encode("ascii"))
        _update_length_prefixed(digest, content)
    return f"sha256:{digest.hexdigest()}"


def hash_portable_skill_dir(path: str | Path) -> str:
    """Hash an actual materialized Skill and verify its portable file modes."""

    root = Path(path)
    if is_link_or_reparse(root):
        raise ValueError(f"Cannot hash skill directory symlink or reparse point: {root}")
    if not root.is_dir():
        raise ValueError(f"Skill path is not a directory: {root}")

    entries: list[tuple[str, bytes, int]] = []
    for relative_path, file_path in _iter_hashable_files(root):
        content = file_path.read_bytes()
        mode = (
            portable_skill_file_mode(content)
            if os.name == "nt"
            else stat.S_IMODE(file_path.stat().st_mode) & 0o777
        )
        entries.append((relative_path, content, mode))
    return hash_skill_files_with_modes(entries)


def _update_file_hash(digest, relative_path: str, content: bytes) -> None:
    path_bytes = relative_path.encode("utf-8")
    digest.update(b"file\0")
    digest.update(struct.pack(">Q", len(path_bytes)))
    digest.update(path_bytes)
    digest.update(struct.pack(">Q", len(content)))
    digest.update(content)


def _update_length_prefixed(digest, value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _iter_hashable_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    _collect_hashable_files(root, root, files)
    files.sort(key=lambda item: item[0])
    return files


def _collect_hashable_files(
    root: Path, directory: Path, files: list[tuple[str, Path]]
) -> None:
    for child in sorted(directory.iterdir(), key=lambda entry: entry.name):
        relative_path = child.relative_to(root).as_posix()

        if is_link_or_reparse(child):
            raise ValueError(
                f"Cannot hash symlink or reparse point in skill directory: {relative_path}"
            )
        if child.is_dir():
            if is_ignored_path(relative_path, is_dir=True):
                continue
            _collect_hashable_files(root, child, files)
            continue
        if child.is_file():
            if is_ignored_path(relative_path):
                continue
            files.append((relative_path, child))
            continue

        raise ValueError(f"Cannot hash non-regular file in skill directory: {relative_path}")
