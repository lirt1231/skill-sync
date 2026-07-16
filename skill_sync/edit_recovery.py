"""Safe primitives for recovering a tampered rendered deployment."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from skill_sync.copying import copy_skill_dir, rename_no_replace
from skill_sync.deployment import PROVENANCE_FILE
from skill_sync.edit_apply import fsync_directory
from skill_sync.edit_validation import EditTreeInspectionError, TreeInspection, inspect_tree
from skill_sync.hash import hash_skill_dir, is_link_or_reparse


class DeploymentQuarantineRecoveryRequired(OSError):
    """A tampered deployment could not be restored or cleaned safely."""

    def __init__(self, message: str, *, recovery_path: Path) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path


@dataclass
class CapturedSnapshot:
    """Identity-checked cleanup for a private recovery snapshot."""

    path: Path
    identity: tuple[int, int, int]
    expected_hash: str

    @classmethod
    def prepare(cls, path: str | Path, *, expected_hash: str) -> "CapturedSnapshot":
        snapshot = Path(path)
        identity = _path_identity(snapshot)
        if (
            identity is None
            or is_link_or_reparse(snapshot)
            or not snapshot.is_dir()
            or hash_skill_dir(snapshot) != expected_hash
        ):
            raise ValueError("captured recovery snapshot failed ownership verification")
        return cls(snapshot, identity, expected_hash)

    def finalize(self) -> None:
        if (
            _path_identity(self.path) != self.identity
            or hash_skill_dir(self.path) != self.expected_hash
        ):
            raise DeploymentQuarantineRecoveryRequired(
                "captured recovery snapshot changed before cleanup",
                recovery_path=self.path,
            )
        discard = self.path.with_name(
            f".{self.path.name}.discard-{uuid.uuid4().hex}"
        )
        rename_no_replace(self.path, discard)
        if _path_identity(discard) != self.identity:
            raise DeploymentQuarantineRecoveryRequired(
                "moved captured snapshot changed identity",
                recovery_path=discard,
            )
        try:
            _make_writable(discard)
            shutil.rmtree(discard)
            fsync_directory(self.path.parent)
        except Exception as exc:
            raise DeploymentQuarantineRecoveryRequired(
                "captured recovery snapshot cleanup failed",
                recovery_path=discard,
            ) from exc


def inspect_authored_deployment(path: str | Path) -> TreeInspection:
    """Inspect authored files while excluding only root deployment provenance."""

    inspection = inspect_tree(path)
    return TreeInspection(
        files={
            name: record
            for name, record in inspection.files.items()
            if name != PROVENANCE_FILE
        },
        issues=inspection.issues,
    )


def copy_authored_deployment(
    source: str | Path,
    destination: str | Path,
    *,
    expected_hash: str,
) -> str:
    """Copy a stable, link-free authored deployment snapshot into a workspace."""

    source_path = Path(source)
    destination_path = Path(destination)
    before = inspect_authored_deployment(source_path)
    if before.issues or before.hash != expected_hash:
        raise EditTreeInspectionError("tampered deployment changed before capture")

    staging_root = Path(
        tempfile.mkdtemp(prefix=".recover-capture-", dir=destination_path.parent)
    )
    staged = staging_root / destination_path.name
    try:
        def ignore(directory: str, names: list[str]) -> set[str]:
            if Path(directory) == source_path and PROVENANCE_FILE in names:
                return {PROVENANCE_FILE}
            return set()

        shutil.copytree(source_path, staged, symlinks=True, ignore=ignore)
        staged_inspection = inspect_tree(staged)
        if staged_inspection.issues or staged_inspection.hash != expected_hash:
            raise EditTreeInspectionError(
                "tampered deployment changed or became unsafe during capture"
            )
        copied_hash = copy_skill_dir(staged, destination_path)
        if copied_hash != expected_hash:
            raise EditTreeInspectionError("captured workspace hash mismatch")
        _make_writable(destination_path)
        final = inspect_tree(destination_path)
        if final.issues or final.hash != expected_hash:
            raise EditTreeInspectionError("captured workspace failed verification")
        return expected_hash
    finally:
        if staging_root.exists() and not is_link_or_reparse(staging_root):
            _make_writable(staging_root)
            shutil.rmtree(staging_root)


@dataclass
class DeploymentQuarantine:
    """Move a tampered deployment aside without overwriting any path winner."""

    deployment: Path
    quarantine: Path
    expected_identity: tuple[int, int, int]
    expected_hash: str
    moved: bool = False

    @classmethod
    def prepare(
        cls,
        deployment: str | Path,
        *,
        expected_hash: str,
        token: str,
    ) -> "DeploymentQuarantine":
        path = Path(deployment)
        identity = _path_identity(path)
        if identity is None or is_link_or_reparse(path) or not path.is_dir():
            raise ValueError(f"tampered deployment is not a real directory: {path}")
        if hash_skill_dir(path) != expected_hash:
            raise FileExistsError("tampered deployment changed before quarantine")
        quarantine = path.with_name(f".{path.name}.recover-tampered-{token}")
        if quarantine.exists() or is_link_or_reparse(quarantine):
            raise FileExistsError(f"recovery quarantine already exists: {quarantine}")
        return cls(path, quarantine, identity, expected_hash)

    def apply(self) -> None:
        if (
            _path_identity(self.deployment) != self.expected_identity
            or hash_skill_dir(self.deployment) != self.expected_hash
        ):
            raise FileExistsError("tampered deployment changed before quarantine")
        try:
            rename_no_replace(self.deployment, self.quarantine)
            self.moved = True
            fsync_directory(self.deployment.parent)
            if (
                _path_identity(self.quarantine) != self.expected_identity
                or hash_skill_dir(self.quarantine) != self.expected_hash
            ):
                raise OSError("quarantined deployment failed verification")
        except Exception as exc:
            if self.rollback():
                raise OSError("deployment quarantine was rolled back") from exc
            raise DeploymentQuarantineRecoveryRequired(
                "tampered deployment quarantine requires recovery",
                recovery_path=self.quarantine,
            ) from exc

    def rollback(self) -> bool:
        if not self.moved:
            return True
        if self.deployment.exists() or is_link_or_reparse(self.deployment):
            return False
        if (
            _path_identity(self.quarantine) != self.expected_identity
            or hash_skill_dir(self.quarantine) != self.expected_hash
        ):
            return False
        try:
            rename_no_replace(self.quarantine, self.deployment)
            fsync_directory(self.deployment.parent)
        except (OSError, FileExistsError):
            return False
        restored = (
            _path_identity(self.deployment) == self.expected_identity
            and hash_skill_dir(self.deployment) == self.expected_hash
        )
        if restored:
            self.moved = False
        return restored

    def finalize(self) -> None:
        if not self.moved:
            return
        if _path_identity(self.quarantine) != self.expected_identity:
            raise DeploymentQuarantineRecoveryRequired(
                "recovery quarantine identity changed",
                recovery_path=self.quarantine,
            )
        discard = self.quarantine.with_name(
            f".{self.quarantine.name}.discard-{uuid.uuid4().hex}"
        )
        rename_no_replace(self.quarantine, discard)
        if _path_identity(discard) != self.expected_identity:
            raise DeploymentQuarantineRecoveryRequired(
                "moved recovery quarantine changed identity",
                recovery_path=discard,
            )
        self.moved = False
        try:
            _make_writable(discard)
            shutil.rmtree(discard)
            fsync_directory(self.deployment.parent)
        except Exception as exc:
            raise DeploymentQuarantineRecoveryRequired(
                "committed recovery quarantine cleanup failed",
                recovery_path=discard,
            ) from exc


def _path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _make_writable(root: Path) -> None:
    if not root.exists() or is_link_or_reparse(root):
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if is_link_or_reparse(path):
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    mode = stat.S_IMODE(root.stat().st_mode)
    root.chmod(mode | stat.S_IWUSR | stat.S_IXUSR)
