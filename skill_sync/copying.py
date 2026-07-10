"""Safe Skill directory copying."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from skill_sync.hash import hash_skill_dir, is_ignored_path


def copy_skill_dir(source: str | Path, destination: str | Path) -> str:
    """Safely copy a Skill directory and return its final deterministic hash.

    The copy is staged in a temporary directory under the destination parent so
    the final install avoids cross-device directory moves. If an existing
    destination is displaced and the install fails, the previous destination is
    restored when possible.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.is_symlink() or not source_path.is_dir():
        raise ValueError(f"source is not a directory: {source_path}")
    _reject_destination_inside_source(source_path, destination_path)
    if destination_path.exists() or destination_path.is_symlink():
        if destination_path.is_symlink() or not destination_path.is_dir():
            raise ValueError(f"destination is not a directory: {destination_path}")

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

        if destination_path.exists():
            backup_path = _unique_backup_path(destination_path)
            os.replace(destination_path, backup_path)

        try:
            os.replace(staged_destination, destination_path)
            final_hash = hash_skill_dir(destination_path)
            if final_hash != expected_hash:
                raise ValueError(
                    f"final copy hash mismatch: expected {expected_hash}, got {final_hash}"
                )
        except Exception:
            _restore_backup(destination_path, backup_path)
            raise

        if backup_path is not None and backup_path.exists():
            shutil.rmtree(backup_path)
        return final_hash
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


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
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not allocate backup path for {destination}")


def _restore_backup(destination: Path, backup: Path | None) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    if backup is not None and backup.exists():
        os.replace(backup, destination)
