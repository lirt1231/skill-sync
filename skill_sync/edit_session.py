"""Machine-local metadata and locking for managed Skill edit sessions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from skill_sync.copying import copy_skill_dir, rename_no_replace
from skill_sync.hash import is_link_or_reparse
from skill_sync.local_lock import local_file_lock


EDIT_SESSION_SCHEMA_VERSION = 1
EDIT_SESSIONS_DIRECTORY = "edit-sessions"
EDIT_SESSION_METADATA_FILE = "session.json"


class EditSessionMetadataError(ValueError):
    """Raised when edit-session metadata cannot be trusted."""


class InvalidEditSessionTransition(EditSessionMetadataError):
    """Raised when an edit session attempts an invalid state transition."""


class ActiveEditSessionError(EditSessionMetadataError):
    """Raised when a logical Skill already has an unfinished edit session."""

    def __init__(self, logical_skill: str, session_id: str) -> None:
        super().__init__(
            f"Skill already has an active edit session: {logical_skill} ({session_id})"
        )
        self.logical_skill = logical_skill
        self.session_id = session_id


class CanonicalSkillChangedError(EditSessionMetadataError):
    """Raised when canonical content changes while its snapshot is created."""


class EditSessionStatus(str, Enum):
    """Durable states for a managed edit transaction."""

    ACTIVE = "active"
    APPLYING = "applying"
    APPLIED = "applied"
    ABORTED = "aborted"
    NEEDS_RECOVERY = "needs-recovery"


_ALLOWED_TRANSITIONS = {
    EditSessionStatus.ACTIVE: frozenset(
        {EditSessionStatus.APPLYING, EditSessionStatus.ABORTED}
    ),
    EditSessionStatus.APPLYING: frozenset(
        {
            EditSessionStatus.ACTIVE,
            EditSessionStatus.APPLIED,
            EditSessionStatus.NEEDS_RECOVERY,
        }
    ),
    EditSessionStatus.NEEDS_RECOVERY: frozenset(
        {
            EditSessionStatus.ACTIVE,
            EditSessionStatus.APPLIED,
            EditSessionStatus.ABORTED,
        }
    ),
    EditSessionStatus.APPLIED: frozenset(),
    EditSessionStatus.ABORTED: frozenset(),
}

_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "logical_skill",
        "status",
        "actor",
        "baseline_hash",
        "created_at",
        "updated_at",
    }
)


@dataclass(frozen=True)
class EditSessionMetadata:
    """Strict, credential-free metadata for one Base edit session."""

    schema_version: int
    session_id: str
    logical_skill: str
    status: EditSessionStatus
    actor: str | None
    baseline_hash: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        try:
            _validate_metadata(self)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, EditSessionMetadataError):
                raise
            raise EditSessionMetadataError(str(exc)) from exc

    @classmethod
    def new(
        cls,
        *,
        logical_skill: str,
        baseline_hash: str,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> "EditSessionMetadata":
        """Create active Base-session metadata without creating a workspace."""

        timestamp = _utc_timestamp(now)
        return cls(
            schema_version=EDIT_SESSION_SCHEMA_VERSION,
            session_id=str(uuid.uuid4()),
            logical_skill=logical_skill,
            status=EditSessionStatus.ACTIVE,
            actor=actor,
            baseline_hash=baseline_hash,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "EditSessionMetadata":
        """Parse strict JSON metadata, rejecting missing and unknown fields."""

        if not isinstance(value, dict):
            raise EditSessionMetadataError("edit session metadata must be a JSON object")
        fields = frozenset(value)
        if fields != _METADATA_FIELDS:
            missing = sorted(_METADATA_FIELDS - fields)
            unknown = sorted(fields - _METADATA_FIELDS)
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown fields: {', '.join(unknown)}")
            raise EditSessionMetadataError(
                "invalid edit session metadata fields (" + "; ".join(details) + ")"
            )
        try:
            status = EditSessionStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise EditSessionMetadataError("invalid edit session status") from exc
        try:
            return cls(
                schema_version=value["schema_version"],
                session_id=value["session_id"],
                logical_skill=value["logical_skill"],
                status=status,
                actor=value["actor"],
                baseline_hash=value["baseline_hash"],
                created_at=value["created_at"],
                updated_at=value["updated_at"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, EditSessionMetadataError):
                raise
            raise EditSessionMetadataError(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON representation."""

        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "logical_skill": self.logical_skill,
            "status": self.status.value,
            "actor": self.actor,
            "baseline_hash": self.baseline_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def transitioned(
        self,
        status: EditSessionStatus,
        *,
        now: datetime | None = None,
    ) -> "EditSessionMetadata":
        """Return metadata in an allowed next state."""

        try:
            target = EditSessionStatus(status)
        except (TypeError, ValueError) as exc:
            raise InvalidEditSessionTransition(f"unknown edit session status: {status}") from exc
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidEditSessionTransition(
                f"edit session cannot transition from {self.status.value} to {target.value}"
            )
        next_timestamp = _utc_timestamp(now)
        if _parse_utc_timestamp(
            next_timestamp, "updated_at"
        ) < _parse_utc_timestamp(self.updated_at, "updated_at"):
            # A local clock adjustment must not make persisted session history
            # move backwards.  Keeping the previous value is deterministic and
            # still records the state change atomically.
            next_timestamp = self.updated_at
        return replace(self, status=target, updated_at=next_timestamp)


@dataclass(frozen=True)
class EditSessionPaths:
    """Stable machine-local paths reserved for one edit session."""

    root: Path
    metadata: Path
    baseline: Path
    workspace: Path


class EditSessionStore:
    """Persist strict edit-session metadata below one machine-local data root."""

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root).expanduser()
        self.root = self.data_root / EDIT_SESSIONS_DIRECTORY

    def paths(self, session_id: str) -> EditSessionPaths:
        """Return reserved paths without creating baseline or workspace directories."""

        _validate_session_id(session_id)
        root = self.root / session_id
        return EditSessionPaths(
            root=root,
            metadata=root / EDIT_SESSION_METADATA_FILE,
            baseline=root / "baseline",
            workspace=root / "workspace",
        )

    def skill_lock_path(self, logical_skill: str) -> Path:
        """Return a case-normalized, path-safe lock filename for one logical Skill."""

        _validate_identifier(logical_skill, "logical Skill name")
        digest = hashlib.sha256(logical_skill.casefold().encode("utf-8")).hexdigest()
        return self.root / ".locks" / f"{digest}.lock"

    @contextmanager
    def skill_lock(
        self,
        logical_skill: str,
        *,
        timeout: float = 10.0,
    ) -> Iterator[None]:
        """Serialize edit-session mutations for one logical Skill."""

        self._prepare_root()
        with local_file_lock(self.skill_lock_path(logical_skill), timeout=timeout):
            yield

    def create(self, metadata: EditSessionMetadata) -> EditSessionPaths:
        """Create only the session directory and its metadata file."""

        if not isinstance(metadata, EditSessionMetadata):
            raise TypeError("metadata must be EditSessionMetadata")
        paths = self.paths(metadata.session_id)
        with self.skill_lock(metadata.logical_skill):
            try:
                paths.root.mkdir(mode=0o700)
            except FileExistsError:
                raise FileExistsError(
                    f"edit session already exists: {metadata.session_id}"
                ) from None
            try:
                _write_json_atomic(paths.metadata, metadata.to_dict())
            except Exception:
                try:
                    paths.root.rmdir()
                except OSError:
                    pass
                raise
        return paths

    def begin(
        self,
        *,
        logical_skill: str,
        source: str | Path,
        baseline_hash: str,
        actor: str | None = None,
    ) -> tuple[EditSessionMetadata, EditSessionPaths]:
        """Atomically create a Base snapshot and writable workspace.

        The caller hashes the canonical source before entering this method.  A
        different copied hash means the source moved during preparation, so no
        session is published.  The per-Skill lock makes the unfinished-session
        check and final directory publication one operation.
        """

        source_path = Path(source)
        metadata = EditSessionMetadata.new(
            logical_skill=logical_skill,
            baseline_hash=baseline_hash,
            actor=actor,
        )
        paths = self.paths(metadata.session_id)

        with self.skill_lock(logical_skill):
            active = self._unfinished_session(logical_skill)
            if active is not None:
                raise ActiveEditSessionError(logical_skill, active.session_id)

            staging = Path(
                tempfile.mkdtemp(prefix=".begin-", dir=self.root)
            )
            try:
                os.chmod(staging, 0o700)
                staged_baseline = staging / "baseline"
                staged_workspace = staging / "workspace"
                copied_hash = copy_skill_dir(source_path, staged_baseline)
                if copied_hash != baseline_hash:
                    raise CanonicalSkillChangedError(
                        "canonical Skill changed while creating the edit baseline"
                    )
                workspace_hash = copy_skill_dir(staged_baseline, staged_workspace)
                if workspace_hash != baseline_hash:
                    raise EditSessionMetadataError(
                        "edit workspace does not match its baseline snapshot"
                    )
                _set_snapshot_permissions(staged_baseline, writable=False)
                _set_snapshot_permissions(staged_workspace, writable=True)
                _write_json_atomic(
                    staging / EDIT_SESSION_METADATA_FILE,
                    metadata.to_dict(),
                )
                rename_no_replace(staging, paths.root)
                _fsync_directory(self.root)
            except Exception:
                if staging.exists() and not is_link_or_reparse(staging):
                    _remove_real_tree(staging)
                raise

        return metadata, paths

    def abort(self, session_id: str) -> EditSessionMetadata:
        """End an active session and remove only its machine-local work trees."""

        initial = self.load(session_id)
        with self.skill_lock(initial.logical_skill):
            current = self.load(session_id)
            if current.status is not EditSessionStatus.ACTIVE:
                raise InvalidEditSessionTransition(
                    f"edit session cannot transition from {current.status.value} "
                    "to aborted"
                )
            paths = self.paths(session_id)
            _assert_real_directory(paths.baseline, "edit baseline")
            _assert_real_directory(paths.workspace, "edit workspace")
            displaced: list[tuple[Path, Path]] = []
            try:
                for original in (paths.baseline, paths.workspace):
                    temporary = paths.root / f".{original.name}.aborting"
                    if temporary.exists() or is_link_or_reparse(temporary):
                        raise EditSessionMetadataError(
                            f"unexpected abort cleanup path: {temporary}"
                        )
                    original.rename(temporary)
                    displaced.append((original, temporary))
                aborted = current.transitioned(EditSessionStatus.ABORTED)
                _write_json_atomic(paths.metadata, aborted.to_dict())
            except Exception:
                for original, temporary in reversed(displaced):
                    if temporary.exists() and not original.exists():
                        temporary.rename(original)
                raise

            for _, temporary in displaced:
                _remove_real_tree(temporary)
            _fsync_directory(paths.root)
            return aborted

    def load(self, session_id: str) -> EditSessionMetadata:
        """Load one session, failing closed on malformed or displaced metadata."""

        paths = self.paths(session_id)
        self._assert_safe_session_paths(paths)
        try:
            value = json.loads(paths.metadata.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EditSessionMetadataError(
                f"cannot safely read edit session metadata: {paths.metadata}: {exc}"
            ) from exc
        metadata = EditSessionMetadata.from_dict(value)
        if metadata.session_id != session_id:
            raise EditSessionMetadataError(
                "edit session metadata ID does not match its directory"
            )
        return metadata

    def list_metadata(self) -> list[EditSessionMetadata]:
        """Return every session in deterministic order without changing local state.

        Enumeration is deliberately strict.  An unexpected entry or one broken
        metadata document makes the whole inspection fail closed instead of
        presenting a partial, incorrectly healthy view.
        """

        self._assert_safe_root_for_read()
        if not self.root.exists():
            return []

        metadata: list[EditSessionMetadata] = []
        try:
            entries = sorted(self.root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise EditSessionMetadataError(
                f"cannot safely enumerate edit sessions: {self.root}: {exc}"
            ) from exc
        for entry in entries:
            if entry.name == ".locks":
                if is_link_or_reparse(entry) or not entry.is_dir():
                    raise EditSessionMetadataError(
                        f"edit session lock path must be a real directory: {entry}"
                    )
                continue
            try:
                _validate_session_id(entry.name)
            except EditSessionMetadataError as exc:
                raise EditSessionMetadataError(
                    f"unexpected entry in edit session root: {entry}"
                ) from exc
            metadata.append(self.load(entry.name))
        return sorted(
            metadata,
            key=lambda item: (item.created_at, item.session_id),
        )

    def transition(
        self,
        session_id: str,
        status: EditSessionStatus,
        *,
        now: datetime | None = None,
    ) -> EditSessionMetadata:
        """Atomically persist one valid state transition."""

        initial = self.load(session_id)
        with self.skill_lock(initial.logical_skill):
            return self.transition_locked(session_id, status, now=now)

    def transition_locked(
        self,
        session_id: str,
        status: EditSessionStatus,
        *,
        now: datetime | None = None,
    ) -> EditSessionMetadata:
        """Persist a transition while the caller holds this Skill's lock.

        Transactional workflows use this form to keep one uninterrupted lock
        across metadata, receipt, and canonical filesystem changes.
        """

        current = self.load(session_id)
        updated = current.transitioned(status, now=now)
        _write_json_atomic(self.paths(session_id).metadata, updated.to_dict())
        return updated

    def _unfinished_session(
        self, logical_skill: str
    ) -> EditSessionMetadata | None:
        """Return an unfinished published session for one logical Skill.

        Unlike the public strict inspection API, this mutation-internal scan
        ignores verified begin staging directories.  Different logical Skills
        use different locks and may legitimately have overlapping staging
        copies; no staging directory is ever interpreted as a healthy session.
        """

        self._assert_safe_root_for_read()
        if not self.root.exists():
            return None
        try:
            entries = sorted(self.root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise EditSessionMetadataError(
                f"cannot safely enumerate edit sessions: {self.root}: {exc}"
            ) from exc
        published: list[EditSessionMetadata] = []
        for entry in entries:
            if entry.name == ".locks" or entry.name.startswith(".begin-"):
                if is_link_or_reparse(entry) or not entry.is_dir():
                    raise EditSessionMetadataError(
                        f"edit session internal path must be a real directory: {entry}"
                    )
                continue
            try:
                _validate_session_id(entry.name)
            except EditSessionMetadataError as exc:
                raise EditSessionMetadataError(
                    f"unexpected entry in edit session root: {entry}"
                ) from exc
            published.append(self.load(entry.name))

        for metadata in published:
            if (
                metadata.logical_skill.casefold() == logical_skill.casefold()
                and metadata.status
                in {
                    EditSessionStatus.ACTIVE,
                    EditSessionStatus.APPLYING,
                    EditSessionStatus.NEEDS_RECOVERY,
                }
            ):
                return metadata
        return None

    def _prepare_root(self) -> None:
        if self.data_root.exists() and is_link_or_reparse(self.data_root):
            raise EditSessionMetadataError(
                f"edit session data root must not be a link or reparse point: {self.data_root}"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_link_or_reparse(self.root) or not self.root.is_dir():
            raise EditSessionMetadataError(
                f"edit session root must be a real directory: {self.root}"
            )

    def _assert_safe_root_for_read(self) -> None:
        for path in (self.data_root, self.root):
            if is_link_or_reparse(path):
                raise EditSessionMetadataError(
                    f"edit session root must not contain a link or reparse point: {path}"
                )
        if self.data_root.exists() and not self.data_root.is_dir():
            raise EditSessionMetadataError(
                f"edit session data root must be a real directory: {self.data_root}"
            )
        if self.root.exists() and not self.root.is_dir():
            raise EditSessionMetadataError(
                f"edit session root must be a real directory: {self.root}"
            )

    def _assert_safe_session_paths(self, paths: EditSessionPaths) -> None:
        for path in (self.data_root, self.root, paths.root, paths.metadata):
            if is_link_or_reparse(path):
                raise EditSessionMetadataError(
                    f"edit session metadata path must not contain a link or reparse point: {path}"
                )
        if not paths.root.is_dir():
            if not paths.root.exists():
                raise FileNotFoundError(f"edit session does not exist: {paths.root.name}")
            raise EditSessionMetadataError(
                f"edit session path is not a directory: {paths.root}"
            )


def _validate_metadata(metadata: EditSessionMetadata) -> None:
    if (
        type(metadata.schema_version) is not int
        or metadata.schema_version != EDIT_SESSION_SCHEMA_VERSION
    ):
        raise EditSessionMetadataError(
            f"unsupported edit session schema version: {metadata.schema_version}"
        )
    _validate_session_id(metadata.session_id)
    _validate_identifier(metadata.logical_skill, "logical Skill name")
    if not isinstance(metadata.status, EditSessionStatus):
        raise EditSessionMetadataError("edit session status must be an EditSessionStatus")
    if metadata.actor is not None:
        _validate_identifier(metadata.actor, "actor")
    _validate_sha256(metadata.baseline_hash, "baseline hash")
    created = _parse_utc_timestamp(metadata.created_at, "created_at")
    updated = _parse_utc_timestamp(metadata.updated_at, "updated_at")
    if updated < created:
        raise EditSessionMetadataError("updated_at must not precede created_at")


def _validate_session_id(value: str) -> None:
    if not isinstance(value, str):
        raise EditSessionMetadataError("session ID must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise EditSessionMetadataError("session ID must be a canonical UUID") from exc
    if str(parsed) != value:
        raise EditSessionMetadataError("session ID must be a canonical UUID")


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
        or any(ord(character) < 32 for character in value)
    ):
        raise EditSessionMetadataError(f"{label} must be a single safe path component")


def _validate_sha256(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise EditSessionMetadataError(f"{label} must be a full lowercase sha256: string")


def _utc_timestamp(value: datetime | None) -> str:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise EditSessionMetadataError("edit session timestamps must be timezone-aware")
    normalized = current.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EditSessionMetadataError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise EditSessionMetadataError(f"{label} must be a canonical UTC timestamp") from exc
    if _utc_timestamp(parsed) != value:
        raise EditSessionMetadataError(f"{label} must be a canonical UTC timestamp")
    return parsed


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if is_link_or_reparse(path):
        raise EditSessionMetadataError(
            f"refusing to replace linked edit session metadata: {path}"
        )
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as metadata_file:
            metadata_file.write(payload)
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_real_directory(path: Path, label: str) -> None:
    if is_link_or_reparse(path) or not path.is_dir():
        raise EditSessionMetadataError(f"{label} must be a real directory: {path}")
    for child in path.rglob("*"):
        if is_link_or_reparse(child):
            raise EditSessionMetadataError(
                f"{label} must not contain a link or reparse point: {child}"
            )


def _set_snapshot_permissions(root: Path, *, writable: bool) -> None:
    """Keep session copies private; only the workspace is owner-writable."""

    _assert_real_directory(root, "edit session snapshot")
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            os.chmod(path, 0o700 if writable else 0o500)
        elif path.is_file():
            executable = bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            os.chmod(path, (0o700 if executable else 0o600) if writable else (0o500 if executable else 0o400))
        else:
            raise EditSessionMetadataError(
                f"edit session snapshot contains a non-regular path: {path}"
            )
    os.chmod(root, 0o700 if writable else 0o500)


def _remove_real_tree(root: Path) -> None:
    _assert_real_directory(root, "edit session cleanup tree")
    for path in (root, *root.rglob("*")):
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.is_file():
            os.chmod(path, 0o600)
    shutil.rmtree(root)
