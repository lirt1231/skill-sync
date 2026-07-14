"""Safe Skill directory copying."""

from __future__ import annotations

import os
import errno
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from skill_sync.hash import hash_skill_dir, is_ignored_path, is_link_or_reparse


def copy_skill_dir(source: str | Path, destination: str | Path) -> str:
    """Safely copy a Skill directory and return its final deterministic hash.

    The copy is staged in a temporary directory under the destination parent so
    the final install avoids cross-device directory moves. If an existing
    destination is displaced and the install fails, the previous destination is
    restored when possible.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    if is_link_or_reparse(source_path) or not source_path.is_dir():
        raise ValueError(f"source is not a directory: {source_path}")
    _reject_destination_inside_source(source_path, destination_path)
    destination_exists = destination_path.exists() or is_link_or_reparse(
        destination_path
    )
    original_identity: tuple[int, int, int] | None = None
    if destination_exists:
        if is_link_or_reparse(destination_path) or not destination_path.is_dir():
            raise ValueError(f"destination is not a directory: {destination_path}")
        original_identity = _path_identity(destination_path)

    expected_hash = hash_skill_dir(source_path)
    destination_parent = destination_path.parent
    destination_parent.mkdir(parents=True, exist_ok=True)

    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.tmp-",
            dir=destination_parent,
        )
    )
    staged_destination = temp_root / destination_path.name
    backup_path: Path | None = None
    installed_identity: tuple[int, int, int] | None = None

    try:
        shutil.copytree(
            source_path,
            staged_destination,
            ignore=_copy_ignore(source_path),
        )
        staged_hash = hash_skill_dir(staged_destination)
        if staged_hash != expected_hash:
            raise ValueError(
                f"staged copy hash mismatch: expected {expected_hash}, got {staged_hash}"
            )

        if original_identity is not None:
            if (
                _path_identity(destination_path) != original_identity
                or is_link_or_reparse(destination_path)
                or not destination_path.is_dir()
            ):
                raise FileExistsError(
                    f"destination changed before replacement: {destination_path}"
                )
            backup_path = _unique_backup_path(destination_path)
            _rename_no_replace(destination_path, backup_path)
            moved_identity = _path_identity(backup_path)
            if moved_identity != original_identity:
                _restore_moved_path(destination_path, backup_path, moved_identity)
                raise FileExistsError(
                    f"moved destination failed identity verification: {destination_path}"
                )

        try:
            _rename_no_replace(staged_destination, destination_path)
            installed_identity = _path_identity(destination_path)
            if installed_identity is None:
                raise OSError(f"installed destination is missing: {destination_path}")
            final_hash = hash_skill_dir(destination_path)
            if _path_identity(destination_path) != installed_identity:
                raise FileExistsError(
                    f"destination changed during verification: {destination_path}"
                )
            if final_hash != expected_hash:
                raise ValueError(
                    f"final copy hash mismatch: expected {expected_hash}, got {final_hash}"
                )
        except Exception:
            _rollback_install(
                destination_path,
                installed_identity,
                backup_path,
                original_identity,
            )
            raise

        if backup_path is not None:
            _remove_owned_directory(backup_path, original_identity)
        return final_hash
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


def rename_no_replace(source: str | Path, destination: str | Path) -> None:
    """Atomically move a path while preserving every destination winner."""
    _rename_no_replace(Path(source), Path(destination))


def _copy_ignore(source_root: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        directory_path = Path(directory)
        ignored: set[str] = set()
        for name in names:
            child = directory_path / name
            relative_path = child.relative_to(source_root).as_posix()
            if is_ignored_path(relative_path, is_dir=child.is_dir()):
                ignored.add(name)
        return ignored

    return ignore


def _reject_destination_inside_source(source: Path, destination: Path) -> None:
    resolved_source = source.resolve(strict=True)
    resolved_destination = destination.resolve(strict=False)
    if resolved_destination == resolved_source or resolved_destination.is_relative_to(
        resolved_source
    ):
        raise ValueError(
            f"destination must not be the source or inside source: {destination}"
        )


def _unique_backup_path(destination: Path) -> Path:
    parent = destination.parent
    for attempt in range(100):
        candidate = parent / (
            f".{destination.name}.backup-{time.time_ns()}-{os.getpid()}-{attempt}"
        )
        if not candidate.exists() and not is_link_or_reparse(candidate):
            return candidate
    raise FileExistsError(f"could not allocate backup path for {destination}")


def _rollback_install(
    destination: Path,
    installed_identity: tuple[int, int, int] | None,
    backup: Path | None,
    original_identity: tuple[int, int, int] | None,
) -> None:
    if installed_identity is not None:
        _remove_owned_directory(destination, installed_identity)
    _restore_original(destination, backup, original_identity)


def _restore_original(
    destination: Path,
    backup: Path | None,
    original_identity: tuple[int, int, int] | None,
) -> bool:
    if backup is None or original_identity is None:
        return False
    return _restore_moved_path(destination, backup, original_identity)


def _restore_moved_path(
    destination: Path,
    backup: Path,
    moved_identity: tuple[int, int, int] | None,
) -> bool:
    if moved_identity is None or _path_identity(backup) != moved_identity:
        return False
    if destination.exists() or is_link_or_reparse(destination):
        return False
    try:
        _rename_no_replace(backup, destination)
    except FileExistsError:
        return False
    return _path_identity(destination) == moved_identity


def _remove_owned_directory(
    path: Path,
    expected_identity: tuple[int, int, int] | None,
) -> bool:
    """Remove a directory only after moving and rechecking the owned inode."""
    if expected_identity is None or _path_identity(path) != expected_identity:
        return False
    quarantine = _unique_discard_path(path)
    try:
        _rename_no_replace(path, quarantine)
    except (FileExistsError, FileNotFoundError):
        return False
    if _path_identity(quarantine) != expected_identity:
        if not path.exists() and not is_link_or_reparse(path):
            try:
                _rename_no_replace(quarantine, path)
            except FileExistsError:
                pass
        return False
    shutil.rmtree(quarantine)
    return True


def _unique_discard_path(path: Path) -> Path:
    for attempt in range(100):
        candidate = path.parent / (
            f".{path.name}.discard-{time.time_ns()}-{os.getpid()}-{attempt}"
        )
        if not candidate.exists() and not is_link_or_reparse(candidate):
            return candidate
    raise FileExistsError(f"could not allocate discard path for {path}")


def _path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename ``source`` while refusing every existing winner."""
    if os.name == "nt":
        os.rename(source, destination)
        return

    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 1)
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-overwrite directory rename is unsupported on this platform",
            str(destination),
        )
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(error, os.strerror(error), str(destination))
        raise OSError(error, os.strerror(error), str(destination))
