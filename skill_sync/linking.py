"""Safe cross-platform directory link management."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from collections.abc import Iterable

from skill_sync.copying import rename_no_replace


def link_state(source: Path, destination: Path) -> str:
    if destination.is_symlink():
        try:
            return "linked" if destination.resolve() == source.resolve() else "wrong-link"
        except OSError:
            return "broken-link"
    if destination.exists():
        if os.name == "nt" and _same_file(source, destination):
            return "linked"
        return "conflict"
    return "missing"


def create_directory_link(source: Path, destination: Path) -> str:
    state = link_state(source, destination)
    if state == "linked":
        return "linked"
    if state != "missing":
        raise FileExistsError(f"refusing to replace {state}: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.symlink_to(source, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        _validate_windows_mklink_path(destination)
        _validate_windows_mklink_path(source)
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return "junction"


def remove_directory_link(source: Path, destination: Path) -> bool:
    if link_state(source, destination) != "linked":
        return False
    if destination.is_symlink():
        destination.unlink()
    elif os.name == "nt":
        identity = _link_identity(destination)
        if not _remove_windows_reparse_directory(destination, identity):
            return False
    else:
        return False
    return True


def replace_directory_link(
    source: Path,
    destination: Path,
    *,
    allowed_current_sources: Iterable[Path] = (),
) -> str:
    """Safely point an owned directory link at ``source`` with rollback.

    Existing real directories and links not owned by one of
    ``allowed_current_sources`` are never deleted.  Existing links are moved to
    a unique backup and revalidated before the replacement is created directly
    at the final path with no-overwrite semantics.  This introduces a very
    short missing-link window, but any path that wins that window is preserved.
    """

    if link_state(source, destination) == "linked":
        return "linked"

    destination_exists = destination.exists() or destination.is_symlink()
    allowed = tuple(Path(item) for item in allowed_current_sources)
    if not destination_exists:
        # Creating the final link directly is an atomic no-overwrite operation:
        # symlink/mklink fails if another path wins the race.
        return create_directory_link(source, destination)

    if not _is_directory_link(destination):
        raise FileExistsError(f"refusing to replace a real directory: {destination}")
    current_source = next(
        (
            current
            for current in allowed
            if link_state(current, destination) == "linked"
        ),
        None,
    )
    if current_source is None:
        raise FileExistsError(f"refusing to replace unowned link: {destination}")
    original_identity = _link_identity(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    backup = destination.with_name(f".{destination.name}.previous-{token}")
    backup_moved = False
    installed_identity: tuple[int, int, int] | None = None

    try:
        if (
            _link_identity(destination) != original_identity
            or link_state(current_source, destination) != "linked"
        ):
            raise FileExistsError(f"link changed before replacement: {destination}")
        rename_no_replace(destination, backup)
        backup_moved = True
        if (
            _link_identity(backup) != original_identity
            or link_state(current_source, backup) != "linked"
        ):
            raise FileExistsError(f"moved link failed identity verification: {destination}")
        try:
            # Never rename/replace a prepared link onto the final endpoint:
            # create_directory_link fails if a file, link, or directory wins
            # the missing-path window.
            method = create_directory_link(source, destination)
            installed_identity = _link_identity(destination)
        except Exception:
            if _restore_verified_backup_link(
                backup,
                destination,
                current_source,
                original_identity,
            ):
                backup_moved = False
            raise
        if link_state(source, destination) != "linked":
            _remove_verified_link_path(
                destination, source, identity=installed_identity
            )
            if _restore_verified_backup_link(
                backup,
                destination,
                current_source,
                original_identity,
            ):
                backup_moved = False
            raise OSError(f"replacement link failed verification: {destination}")
        if not _remove_verified_link_path(
            backup, current_source, identity=original_identity
        ):
            raise OSError(f"refusing to delete unverified link backup: {backup}")
        backup_moved = False
        return method
    finally:
        if backup_moved and (backup.exists() or backup.is_symlink()):
            # Best-effort rollback uses the same no-overwrite creation path.
            # If another path won, keep both that winner and the recoverable
            # backup and let the caller fail closed.
            _restore_verified_backup_link(
                backup,
                destination,
                current_source,
                original_identity,
            )


def _restore_verified_backup_link(
    backup: Path,
    destination: Path,
    source: Path,
    identity: tuple[int, int, int] | None,
) -> bool:
    """Restore an owned backup without ever replacing an endpoint winner."""

    if identity is None or _link_identity(backup) != identity:
        return False
    if link_state(source, backup) != "linked":
        return False
    try:
        create_directory_link(source, destination)
    except (OSError, ValueError):
        return False
    if link_state(source, destination) != "linked":
        return False
    return _remove_verified_link_path(backup, source, identity=identity)


def _is_directory_link(path: Path) -> bool:
    return path.is_symlink() or (
        os.name == "nt" and _is_windows_reparse_point(path)
    )


def _link_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _remove_verified_link_path(
    path: Path,
    source: Path,
    *,
    identity: tuple[int, int, int] | None = None,
) -> bool:
    if not _is_directory_link(path):
        return False
    if identity is not None and _link_identity(path) != identity:
        return False
    if link_state(source, path) != "linked":
        return False
    if path.is_symlink():
        path.unlink()
    elif os.name == "nt" and _is_windows_reparse_point(path):
        if not _remove_windows_reparse_directory(path, identity):
            return False
    else:
        return False
    return True


def _same_file(source: Path, destination: Path) -> bool:
    try:
        return os.path.samefile(source, destination)
    except OSError:
        return False


def _is_windows_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _remove_windows_reparse_directory(
    path: Path,
    identity: tuple[int, int, int] | None,
) -> bool:
    """Remove a verified Windows directory reparse point, never a real dir.

    ``Path.rmdir`` is required for junctions, but it would also remove an empty
    real directory.  Revalidate both the filesystem identity and the reparse
    bit immediately before calling it so a reparse-to-directory replacement is
    preserved instead of deleted.
    """

    if identity is None or _link_identity(path) != identity:
        return False
    if not _is_windows_reparse_point(path):
        return False
    if _link_identity(path) != identity or not _is_windows_reparse_point(path):
        return False
    path.rmdir()
    return True


_CMD_META_PATTERN = re.compile(r'[&|<>()@^%!"\r\n]')


def _validate_windows_mklink_path(path: Path) -> None:
    """Reject paths that ``cmd /c`` can interpret rather than pass literally."""

    value = str(path)
    match = _CMD_META_PATTERN.search(value)
    if match is not None:
        raise ValueError(
            f"refusing unsafe Windows link path containing {match.group(0)!r}: {path}"
        )
