"""Fail-closed filesystem primitives for Base edit apply transactions."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_sync.copying import copy_skill_dir, rename_no_replace
from skill_sync.hash import hash_skill_dir, is_link_or_reparse


class CanonicalSwapError(OSError):
    """A canonical swap failed but the previous source was restored."""


class CanonicalSwapRecoveryRequired(CanonicalSwapError):
    """A canonical swap could not safely restore the previous source."""

    def __init__(self, message: str, *, recovery_path: Path) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path


class ReceiptRecoveryRequired(OSError):
    """A receipt endpoint changed and must not be overwritten."""


@dataclass
class PrivateJsonReceipt:
    """An identity-checked private JSON receipt updated with no-replace renames."""

    path: Path
    identity: tuple[int, int, int]
    digest: str

    @classmethod
    def create(cls, path: str | Path, value: dict[str, Any]) -> "PrivateJsonReceipt":
        destination = Path(path)
        prepare_private_directory(destination.parent)
        if destination.exists() or is_link_or_reparse(destination):
            raise ReceiptRecoveryRequired(f"receipt already exists: {destination}")
        temporary, temporary_identity, digest = _prepare_json_file(destination, value)
        try:
            rename_no_replace(temporary, destination)
            fsync_directory(destination.parent)
        except FileExistsError as exc:
            _remove_owned_file(temporary, temporary_identity)
            raise ReceiptRecoveryRequired(
                f"receipt publication preserved an external winner: {destination}"
            ) from exc
        except Exception:
            _remove_owned_file(temporary, temporary_identity)
            raise
        identity = _require_file_identity(destination, "published receipt")
        if identity != temporary_identity or _file_digest(destination) != digest:
            raise ReceiptRecoveryRequired(
                f"published receipt failed verification: {destination}"
            )
        return cls(destination, identity, digest)

    def update(self, value: dict[str, Any]) -> None:
        prepare_private_directory(self.path.parent)
        old_identity = self.identity
        temporary, temporary_identity, digest = _prepare_json_file(self.path, value)
        previous = self.path.with_name(
            f".{self.path.name}.previous-{time.time_ns()}"
        )
        moved = False
        try:
            if (
                _path_identity(self.path) != self.identity
                or _file_digest(self.path) != self.digest
            ):
                raise ReceiptRecoveryRequired(
                    f"receipt changed before update: {self.path}"
                )
            rename_no_replace(self.path, previous)
            moved = True
            fsync_directory(self.path.parent)
            if _path_identity(previous) != self.identity:
                _restore_moved_file(previous, self.path, _path_identity(previous))
                raise ReceiptRecoveryRequired(
                    f"moved receipt failed identity verification: {self.path}"
                )
            try:
                rename_no_replace(temporary, self.path)
            except Exception as exc:
                if not _restore_moved_file(previous, self.path, self.identity):
                    raise ReceiptRecoveryRequired(
                        f"receipt update preserved an external winner: {self.path}"
                    ) from exc
                moved = False
                raise
            new_identity = _require_file_identity(self.path, "updated receipt")
            if new_identity != temporary_identity or _file_digest(self.path) != digest:
                raise ReceiptRecoveryRequired(
                    f"updated receipt failed verification: {self.path}"
                )
            self.identity = new_identity
            self.digest = digest
            if not _remove_owned_file(previous, old_identity):
                raise ReceiptRecoveryRequired(
                    f"could not remove previous receipt: {previous}"
                )
            moved = False
            fsync_directory(self.path.parent)
        finally:
            _remove_owned_file(temporary, temporary_identity)
            if moved and not self.path.exists():
                _restore_moved_file(previous, self.path, _path_identity(previous))


@dataclass
class CanonicalSwap:
    """A prepared same-parent directory swap that preserves external winners."""

    source: Path
    candidate: Path
    previous: Path
    expected_old_hash: str
    expected_new_hash: str
    old_identity: tuple[int, int, int]
    candidate_identity: tuple[int, int, int]
    installed_identity: tuple[int, int, int] | None = None
    previous_moved: bool = False

    @classmethod
    def prepare(
        cls,
        source: str | Path,
        replacement: str | Path,
        *,
        expected_old_hash: str,
        expected_new_hash: str,
        token: str,
    ) -> "CanonicalSwap":
        source_path = Path(source)
        replacement_path = Path(replacement)
        _assert_real_directory(source_path, "canonical source")
        _assert_real_directory(replacement_path, "edit workspace")
        _assert_real_directory(source_path.parent, "canonical parent")
        old_identity = _require_identity(source_path, "canonical source")
        if hash_skill_dir(source_path) != expected_old_hash:
            raise FileExistsError("canonical source changed before staging")

        candidate = source_path.parent / f".{source_path.name}.apply-stage-{token}"
        previous = source_path.parent / f".{source_path.name}.apply-previous-{token}"
        for path in (candidate, previous):
            if path.exists() or is_link_or_reparse(path):
                raise FileExistsError(f"edit apply path already exists: {path}")

        candidate_identity: tuple[int, int, int] | None = None
        try:
            candidate_hash = copy_skill_dir(replacement_path, candidate)
            if candidate_hash != expected_new_hash:
                raise FileExistsError("edit workspace changed while staging apply")
            candidate_identity = _require_identity(candidate, "staged replacement")
            fsync_tree(candidate)
            if hash_skill_dir(replacement_path) != expected_new_hash:
                raise FileExistsError("edit workspace changed after staging apply")
            if (
                _path_identity(source_path) != old_identity
                or hash_skill_dir(source_path) != expected_old_hash
            ):
                raise FileExistsError("canonical source changed while staging apply")
            return cls(
                source=source_path,
                candidate=candidate,
                previous=previous,
                expected_old_hash=expected_old_hash,
                expected_new_hash=expected_new_hash,
                old_identity=old_identity,
                candidate_identity=candidate_identity,
            )
        except Exception:
            _remove_owned_tree(candidate, candidate_identity)
            raise

    def apply(self) -> None:
        """Publish the candidate with two atomic no-replace renames."""

        if (
            _path_identity(self.source) != self.old_identity
            or hash_skill_dir(self.source) != self.expected_old_hash
        ):
            raise FileExistsError("canonical source changed before replacement")
        if (
            _path_identity(self.candidate) != self.candidate_identity
            or hash_skill_dir(self.candidate) != self.expected_new_hash
        ):
            raise FileExistsError("staged replacement changed before publication")

        rename_no_replace(self.source, self.previous)
        self.previous_moved = True
        fsync_directory(self.source.parent)
        moved_identity = _path_identity(self.previous)
        if moved_identity != self.old_identity:
            if moved_identity is not None and self._restore_identity(moved_identity):
                self.previous_moved = False
                raise CanonicalSwapError(
                    "a concurrent canonical winner was preserved"
                )
            raise CanonicalSwapRecoveryRequired(
                "a moved concurrent canonical winner could not be restored",
                recovery_path=self.previous,
            )
        if hash_skill_dir(self.previous) != self.expected_old_hash:
            if self._restore_previous():
                raise CanonicalSwapError(
                    "moved canonical source failed hash verification"
                )
            raise CanonicalSwapRecoveryRequired(
                "moved canonical source failed verification and could not be restored",
                recovery_path=self.previous,
            )

        try:
            rename_no_replace(self.candidate, self.source)
            self.installed_identity = _require_identity(
                self.source, "installed canonical source"
            )
            if self.installed_identity != self.candidate_identity:
                raise CanonicalSwapError(
                    "installed canonical source failed identity verification"
                )
            if hash_skill_dir(self.source) != self.expected_new_hash:
                raise CanonicalSwapError(
                    "installed canonical source failed hash verification"
                )
            fsync_directory(self.source.parent)
        except Exception as exc:
            if self.rollback():
                if isinstance(exc, CanonicalSwapError):
                    raise
                raise CanonicalSwapError("canonical replacement was rolled back") from exc
            raise CanonicalSwapRecoveryRequired(
                "canonical replacement failed and the previous source could not be restored",
                recovery_path=self.previous,
            ) from exc

    def rollback(self) -> bool:
        """Restore the previous directory only when every owned identity matches."""

        if not self.previous_moved:
            return True
        failed: Path | None = None
        failed_identity: tuple[int, int, int] | None = None
        if self.source.exists() or is_link_or_reparse(self.source):
            if (
                self.installed_identity is None
                or _path_identity(self.source) != self.installed_identity
            ):
                return False
            failed = self.source.parent / (
                f".{self.source.name}.apply-failed-{time.time_ns()}"
            )
            try:
                rename_no_replace(self.source, failed)
            except (OSError, FileExistsError):
                return False
            failed_identity = _path_identity(failed)
            fsync_directory(self.source.parent)
            if failed_identity != self.installed_identity:
                if failed_identity is not None and not self.source.exists():
                    self._restore_moved_path(failed, failed_identity)
                return False

        if not self._restore_previous():
            if failed is not None and not self.source.exists():
                try:
                    rename_no_replace(failed, self.source)
                except (OSError, FileExistsError):
                    pass
            return False
        if failed is not None:
            _remove_owned_tree(failed, failed_identity)
        return (
            _path_identity(self.source) == self.old_identity
            and hash_skill_dir(self.source) == self.expected_old_hash
        )

    def finalize(self) -> None:
        """Remove transaction-owned same-parent artifacts after durable completion."""

        if self.previous_moved and not _remove_owned_tree(
            self.previous, self.old_identity
        ):
            raise CanonicalSwapRecoveryRequired(
                "could not remove the verified previous canonical directory",
                recovery_path=self.previous,
            )
        if (self.candidate.exists() or is_link_or_reparse(self.candidate)) and not (
            _remove_owned_tree(self.candidate, self.candidate_identity)
        ):
            raise CanonicalSwapRecoveryRequired(
                "could not remove the verified staged canonical directory",
                recovery_path=self.candidate,
            )
        fsync_directory(self.source.parent)

    def _restore_previous(self) -> bool:
        return self._restore_identity(self.old_identity)

    def _restore_identity(self, identity: tuple[int, int, int]) -> bool:
        if _path_identity(self.previous) != identity:
            return False
        return self._restore_moved_path(self.previous, identity)

    def _restore_moved_path(
        self, moved: Path, identity: tuple[int, int, int]
    ) -> bool:
        if self.source.exists() or is_link_or_reparse(self.source):
            return False
        try:
            rename_no_replace(moved, self.source)
        except (OSError, FileExistsError):
            return False
        if moved == self.previous:
            self.previous_moved = False
        fsync_directory(self.source.parent)
        return _path_identity(self.source) == identity


@dataclass
class AbsentCanonicalSwap:
    """Publish a previously absent authored layer with rollback ownership checks."""

    source: Path
    candidate: Path
    previous: Path
    expected_new_hash: str
    candidate_identity: tuple[int, int, int]
    created_parents: tuple[tuple[Path, tuple[int, int, int]], ...]
    installed_identity: tuple[int, int, int] | None = None
    previous_moved: bool = False

    @classmethod
    def prepare(
        cls,
        source: str | Path,
        replacement: str | Path,
        *,
        expected_new_hash: str,
        token: str,
        allowed_root: str | Path,
    ) -> "AbsentCanonicalSwap":
        source_path = Path(source)
        replacement_path = Path(replacement)
        root = Path(allowed_root)
        _assert_real_directory(root, "authored source root")
        _assert_real_directory(replacement_path, "edit workspace")
        if source_path.exists() or is_link_or_reparse(source_path):
            raise FileExistsError("absent authored layer appeared before staging")
        try:
            source_path.parent.relative_to(root)
        except ValueError as exc:
            raise ValueError("authored layer is outside its portable source root") from exc

        created = _prepare_missing_parents(root, source_path.parent)
        candidate = source_path.parent / f".{source_path.name}.apply-stage-{token}"
        if candidate.exists() or is_link_or_reparse(candidate):
            _remove_created_parents(created)
            raise FileExistsError(f"edit apply path already exists: {candidate}")
        candidate_identity: tuple[int, int, int] | None = None
        try:
            candidate_hash = copy_skill_dir(replacement_path, candidate)
            if candidate_hash != expected_new_hash:
                raise FileExistsError("edit workspace changed while staging apply")
            candidate_identity = _require_identity(candidate, "staged authored layer")
            fsync_tree(candidate)
            if hash_skill_dir(replacement_path) != expected_new_hash:
                raise FileExistsError("edit workspace changed after staging apply")
            if source_path.exists() or is_link_or_reparse(source_path):
                raise FileExistsError("absent authored layer appeared while staging apply")
            return cls(
                source=source_path,
                candidate=candidate,
                previous=source_path,
                expected_new_hash=expected_new_hash,
                candidate_identity=candidate_identity,
                created_parents=created,
            )
        except Exception:
            _remove_owned_tree(candidate, candidate_identity)
            _remove_created_parents(created)
            raise

    def apply(self) -> None:
        if self.source.exists() or is_link_or_reparse(self.source):
            raise FileExistsError("absent authored layer appeared before publication")
        if (
            _path_identity(self.candidate) != self.candidate_identity
            or hash_skill_dir(self.candidate) != self.expected_new_hash
        ):
            raise FileExistsError("staged authored layer changed before publication")
        rename_no_replace(self.candidate, self.source)
        self.installed_identity = _require_identity(
            self.source,
            "installed authored layer",
        )
        self.previous_moved = True
        if self.installed_identity != self.candidate_identity:
            raise CanonicalSwapRecoveryRequired(
                "installed authored layer failed identity verification",
                recovery_path=self.source,
            )
        if hash_skill_dir(self.source) != self.expected_new_hash:
            if self.rollback():
                raise CanonicalSwapError(
                    "installed authored layer failed hash verification"
                )
            raise CanonicalSwapRecoveryRequired(
                "installed authored layer failed verification and could not be removed",
                recovery_path=self.source,
            )
        fsync_directory(self.source.parent)

    def rollback(self) -> bool:
        if not self.previous_moved:
            return not self.source.exists() and not is_link_or_reparse(self.source)
        if (
            self.installed_identity is None
            or _path_identity(self.source) != self.installed_identity
        ):
            return False
        failed = self.source.parent / (
            f".{self.source.name}.apply-failed-{time.time_ns()}"
        )
        try:
            rename_no_replace(self.source, failed)
        except (OSError, FileExistsError):
            return False
        failed_identity = _path_identity(failed)
        fsync_directory(self.source.parent)
        if failed_identity != self.installed_identity:
            if failed_identity is not None and not self.source.exists():
                try:
                    rename_no_replace(failed, self.source)
                except (OSError, FileExistsError):
                    pass
            return False
        if not _remove_owned_tree(failed, failed_identity):
            return False
        self.previous_moved = False
        self.installed_identity = None
        _remove_created_parents(self.created_parents)
        return not self.source.exists() and not is_link_or_reparse(self.source)

    def finalize(self) -> None:
        if self.candidate.exists() or is_link_or_reparse(self.candidate):
            if not _remove_owned_tree(self.candidate, self.candidate_identity):
                raise CanonicalSwapRecoveryRequired(
                    "could not remove staged authored layer",
                    recovery_path=self.candidate,
                )
        self.previous_moved = False
        fsync_directory(self.source.parent)


def _prepare_missing_parents(
    root: Path,
    destination: Path,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    relative = destination.relative_to(root)
    current = root
    created: list[tuple[Path, tuple[int, int, int]]] = []
    try:
        for part in relative.parts:
            child = current / part
            if child.exists() or is_link_or_reparse(child):
                _assert_real_directory(child, "authored source parent")
            else:
                os.mkdir(child, mode=0o700)
                identity = _require_identity(child, "created authored source parent")
                created.append((child, identity))
                fsync_directory(current)
            current = child
        return tuple(created)
    except Exception:
        _remove_created_parents(tuple(created))
        raise


def _remove_created_parents(
    created: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> None:
    for path, identity in reversed(created):
        if _path_identity(path) != identity:
            continue
        try:
            path.rmdir()
        except OSError:
            continue
        fsync_directory(path.parent)


def write_private_json_atomic(path: str | Path, value: dict[str, Any]) -> None:
    """Create a new private receipt without replacing any existing path."""

    PrivateJsonReceipt.create(path, value)


def fsync_tree(root: str | Path) -> None:
    """Flush regular files and directories in a prepared candidate tree."""

    root_path = Path(root)
    _assert_real_directory(root_path, "fsync tree")
    directories = [root_path]
    for path in sorted(root_path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if is_link_or_reparse(path):
            raise OSError(f"refusing to fsync linked path: {path}")
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise OSError(f"refusing to fsync non-regular file: {path}")
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise OSError(f"file identity changed during fsync: {path}")
            finally:
                os.close(descriptor)
        else:
            raise OSError(f"refusing to fsync non-regular path: {path}")
    for directory in sorted(
        set(directories), key=lambda item: len(item.parts), reverse=True
    ):
        fsync_directory(directory)
    fsync_directory(root_path.parent)


def fsync_directory(directory: str | Path) -> None:
    directory_path = Path(directory)
    if is_link_or_reparse(directory_path) or not directory_path.is_dir():
        raise OSError(f"fsync directory must be a real directory: {directory_path}")
    identity = _require_identity(directory_path, "fsync directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory_path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if _path_identity(directory_path) != identity:
        raise OSError(f"directory identity changed during fsync: {directory_path}")


def prepare_private_directory(path: str | Path) -> Path:
    """Create and verify an owner-private real directory hierarchy."""

    path = Path(path)
    current = path
    missing: list[Path] = []
    while not current.exists():
        if is_link_or_reparse(current):
            raise OSError(f"private data path must not be linked: {current}")
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    if is_link_or_reparse(current) or not current.is_dir():
        raise OSError(f"private data parent must be a real directory: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        fsync_directory(directory.parent)
    if is_link_or_reparse(path) or not path.is_dir():
        raise OSError(f"private data directory must be real: {path}")
    os.chmod(path, 0o700)
    fsync_directory(path)
    return path


def _assert_real_directory(path: Path, label: str) -> None:
    if is_link_or_reparse(path) or not path.is_dir():
        raise OSError(f"{label} must be a real directory: {path}")


def _require_identity(path: Path, label: str) -> tuple[int, int, int]:
    identity = _path_identity(path)
    if identity is None or identity[2] != stat.S_IFDIR:
        raise OSError(f"{label} must have a stable directory identity: {path}")
    return identity


def _require_file_identity(path: Path, label: str) -> tuple[int, int, int]:
    identity = _path_identity(path)
    if identity is None or identity[2] != stat.S_IFREG:
        raise OSError(f"{label} must have a stable file identity: {path}")
    return identity


def _path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _remove_owned_tree(
    path: Path, expected_identity: tuple[int, int, int] | None
) -> bool:
    if expected_identity is None or _path_identity(path) != expected_identity:
        return False
    quarantine = path.parent / f".{path.name}.discard-{time.time_ns()}"
    try:
        rename_no_replace(path, quarantine)
    except (OSError, FileExistsError):
        return False
    if _path_identity(quarantine) != expected_identity:
        return False
    shutil.rmtree(quarantine)
    fsync_directory(path.parent)
    return True


def _prepare_json_file(
    destination: Path, value: dict[str, Any]
) -> tuple[Path, tuple[int, int, int], str]:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        identity = _require_file_identity(temporary, "prepared receipt")
        return temporary, identity, "sha256:" + hashlib.sha256(payload).hexdigest()
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _remove_owned_file(temporary, _path_identity(temporary))
        raise


def _file_digest(path: Path) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"receipt is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as input_file:
            content = input_file.read()
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError(f"receipt changed while reading: {path}")
        if _path_identity(path) != (
            after.st_dev,
            after.st_ino,
            stat.S_IFMT(after.st_mode),
        ):
            raise OSError(f"receipt was replaced while reading: {path}")
        return "sha256:" + hashlib.sha256(content).hexdigest()
    finally:
        os.close(descriptor)


def _remove_owned_file(
    path: Path, expected_identity: tuple[int, int, int] | None
) -> bool:
    if expected_identity is None or _path_identity(path) != expected_identity:
        return False
    quarantine = path.parent / f".{path.name}.discard-{time.time_ns()}"
    try:
        rename_no_replace(path, quarantine)
    except (OSError, FileExistsError):
        return False
    if _path_identity(quarantine) != expected_identity:
        return False
    quarantine.unlink()
    fsync_directory(path.parent)
    return True


def _restore_moved_file(
    moved: Path,
    destination: Path,
    identity: tuple[int, int, int] | None,
) -> bool:
    if identity is None or _path_identity(moved) != identity:
        return False
    if destination.exists() or is_link_or_reparse(destination):
        return False
    try:
        rename_no_replace(moved, destination)
    except (OSError, FileExistsError):
        return False
    fsync_directory(destination.parent)
    return _path_identity(destination) == identity
