"""Deterministic hashing for Agent Skill directories."""

from __future__ import annotations

import hashlib
import struct
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


def hash_skill_dir(path: str | Path) -> str:
    """Return the deterministic SHA-256 content hash for a Skill directory.

    The digest is computed over regular files in sorted POSIX relative path
    order. Each file is framed with a literal tag and explicit path/content
    byte lengths so binary content cannot create ambiguous boundaries.
    """
    root = Path(path)
    if root.is_symlink():
        raise ValueError(f"Cannot hash skill directory symlink: {root}")
    if not root.is_dir():
        raise ValueError(f"Skill path is not a directory: {root}")

    digest = hashlib.sha256()
    for relative_path, file_path in _iter_hashable_files(root):
        _update_file_hash(digest, relative_path, file_path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _update_file_hash(digest, relative_path: str, content: bytes) -> None:
    path_bytes = relative_path.encode("utf-8")
    digest.update(b"file\0")
    digest.update(struct.pack(">Q", len(path_bytes)))
    digest.update(path_bytes)
    digest.update(struct.pack(">Q", len(content)))
    digest.update(content)


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

        if child.is_symlink():
            raise ValueError(f"Cannot hash symlink in skill directory: {relative_path}")
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
