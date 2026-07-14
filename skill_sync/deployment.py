"""Deterministic, content-addressed base Skill deployments."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import stat
import string
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_sync.hash import hash_skill_dir, is_ignored_path, is_link_or_reparse
from skill_sync.local_lock import local_file_lock


PROVENANCE_FILE = ".skill-sync-provenance.json"
RESOLVER_VERSION = "base-v1"
APPLIED_LAYERS = ("base",)


@dataclass(frozen=True)
class Deployment:
    path: Path
    provenance: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class DeploymentVerification:
    state: str
    path: Path
    provenance: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "valid"


def resolution_hash(
    logical_skill: str,
    source_hash: str,
    target_client: str,
    *,
    resolver_version: str = RESOLVER_VERSION,
) -> str:
    """Hash all inputs that can affect a base-only deployment."""
    _validate_identifier(logical_skill, "logical Skill name")
    _validate_identifier(target_client, "target client")
    _validate_sha256(source_hash, "source hash")
    if not isinstance(resolver_version, str) or not resolver_version:
        raise ValueError("resolver version must be a non-empty string")
    payload = {
        "applied_layers": list(APPLIED_LAYERS),
        "logical_skill": logical_skill,
        "resolver_version": resolver_version,
        "source_hash": source_hash,
        "target_client": target_client,
    }
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return f"sha256:{digest}"


def deployment_path(
    store: str | Path,
    logical_skill: str,
    resolution: str,
) -> Path:
    """Return the content-addressed path for a resolved Skill."""
    _validate_identifier(logical_skill, "logical Skill name")
    _validate_sha256(resolution, "resolution hash")
    return Path(store) / f"sha256-{resolution.removeprefix('sha256:')}" / logical_skill


def render_base_deployment(
    source: str | Path,
    store: str | Path,
    logical_skill: str,
    target_client: str,
    *,
    resolver_version: str = RESOLVER_VERSION,
) -> Deployment:
    """Render a verified, read-only base snapshot into the deployment store."""
    source_path = Path(source)
    store_path = Path(store)
    if is_link_or_reparse(store_path) or is_link_or_reparse(store_path.parent):
        raise ValueError(
            f"deployment store must not be a symlink or reparse point: {store_path}"
        )
    if is_link_or_reparse(source_path):
        raise ValueError(f"source Skill must not be a symlink or reparse point: {source_path}")
    if not source_path.is_dir():
        raise ValueError(f"source Skill is not a directory: {source_path}")
    if not (source_path / "SKILL.md").is_file():
        raise ValueError(f"source Skill has no SKILL.md: {source_path}")
    if (source_path / PROVENANCE_FILE).exists() or (
        source_path / PROVENANCE_FILE
    ).is_symlink():
        raise ValueError(f"source Skill contains reserved file: {PROVENANCE_FILE}")

    source_hash = hash_skill_dir(source_path)
    resolved_hash = resolution_hash(
        logical_skill,
        source_hash,
        target_client,
        resolver_version=resolver_version,
    )
    destination = deployment_path(store_path, logical_skill, resolved_hash)
    expected = expected_provenance(
        logical_skill,
        source_hash,
        target_client,
        resolver_version=resolver_version,
    )

    lock_path = store_path / ".locks" / f"{resolved_hash.removeprefix('sha256:')}.lock"
    with local_file_lock(lock_path):
        existing = verify_deployment(destination, expected_provenance=expected)
        if existing.ok:
            return Deployment(destination, existing.provenance or expected, False)
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                "refusing to overwrite an invalid content-addressed deployment: "
                f"{destination} ({existing.state}: {existing.reason or 'verification failed'})"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_root = Path(
            tempfile.mkdtemp(prefix=f".{logical_skill}.tmp-", dir=destination.parent)
        )
        staged = temp_root / logical_skill
        try:
            shutil.copytree(source_path, staged, ignore=_copy_ignore(source_path))
            rendered_hash = hash_skill_dir(staged)
            if rendered_hash != source_hash:
                raise ValueError(
                    f"rendered hash mismatch: expected {source_hash}, got {rendered_hash}"
                )
            provenance = {**expected, "rendered_hash": rendered_hash}
            (staged / PROVENANCE_FILE).write_bytes(_pretty_json(provenance))
            _make_read_only(staged)
            # macOS requires the moved directory itself to remain writable for
            # rename(2), even when both parents are writable.  Keep only the
            # staging root writable during publication; all authored content
            # and nested directories are already read-only.  The per-resolution
            # lock prevents another skill-sync process from observing this as
            # a completed deployment before the final chmod and verification.
            staged.chmod(stat.S_IMODE(staged.stat().st_mode) | stat.S_IWUSR)

            try:
                os.rename(staged, destination)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                winner = verify_deployment(
                    destination, expected_provenance=provenance
                )
                if winner.ok:
                    return Deployment(
                        destination, winner.provenance or provenance, False
                    )
                raise ValueError(
                    "a concurrent deployment winner failed verification: "
                    f"{winner.state}: {winner.reason or 'verification failed'}"
                ) from exc

            destination.chmod(
                stat.S_IMODE(destination.stat().st_mode) & ~0o222
            )
            verified = verify_deployment(destination, expected_provenance=provenance)
            if not verified.ok:
                # Leave the content-addressed output intact for diagnosis.  It
                # may already be referenced; never delete an installed path on
                # an uncertain post-install failure.
                raise ValueError(
                    f"installed deployment failed verification: {verified.reason or verified.state}"
                )
            return Deployment(destination, provenance, True)
        finally:
            if temp_root.exists():
                _remove_tree(temp_root)


def verify_deployment(
    path: str | Path,
    *,
    expected_provenance: dict[str, Any] | None = None,
) -> DeploymentVerification:
    """Classify a deployment as valid, missing, stale, or tampered."""
    deployment = Path(path)
    if is_link_or_reparse(deployment) or is_link_or_reparse(deployment.parent):
        return DeploymentVerification(
            "tampered", deployment, reason="deployment path contains a link or reparse point"
        )
    if not deployment.exists() and not deployment.is_symlink():
        return DeploymentVerification("missing", deployment, reason="deployment is missing")
    if deployment.is_symlink() or not deployment.is_dir():
        return DeploymentVerification(
            "tampered", deployment, reason="deployment is not a real directory"
        )

    manifest_path = deployment / PROVENANCE_FILE
    try:
        provenance = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return DeploymentVerification(
            "tampered", deployment, reason=f"invalid provenance: {exc}"
        )
    if not isinstance(provenance, dict) or not _valid_provenance_shape(provenance):
        return DeploymentVerification(
            "tampered", deployment, provenance if isinstance(provenance, dict) else None,
            "invalid provenance fields",
        )

    recomputed_resolution = resolution_hash(
        provenance["logical_skill"],
        provenance["source_hash"],
        provenance["target_client"],
        resolver_version=provenance["resolver_version"],
    )
    if recomputed_resolution != provenance["resolution_hash"]:
        return DeploymentVerification(
            "tampered", deployment, provenance, "resolution hash does not match provenance"
        )
    expected_location = deployment_path(
        deployment.parent.parent,
        provenance["logical_skill"],
        provenance["resolution_hash"],
    )
    if deployment != expected_location:
        return DeploymentVerification(
            "tampered", deployment, provenance, "deployment path does not match provenance"
        )

    try:
        actual_hash = _hash_rendered_content(deployment)
    except (OSError, ValueError) as exc:
        return DeploymentVerification(
            "tampered", deployment, provenance, f"cannot hash deployment: {exc}"
        )
    if actual_hash != provenance["rendered_hash"]:
        return DeploymentVerification(
            "tampered",
            deployment,
            provenance,
            f"rendered hash mismatch: expected {provenance['rendered_hash']}, got {actual_hash}",
        )
    if expected_provenance is not None:
        for key in (
            "logical_skill",
            "source_hash",
            "resolution_hash",
            "resolver_version",
            "target_client",
            "applied_layers",
        ):
            if provenance.get(key) != expected_provenance.get(key):
                return DeploymentVerification(
                    "stale", deployment, provenance, f"provenance mismatch for {key}"
                )
    return DeploymentVerification("valid", deployment, provenance)


def remove_verified_deployment(
    path: str | Path,
    store: str | Path,
    trash_root: str | Path,
) -> Path:
    """Move one verified unreferenced deployment to trash and delete it."""

    deployment = Path(path)
    rendered_store = Path(store)
    if any(
        is_link_or_reparse(component)
        for component in (rendered_store, deployment.parent, deployment)
    ):
        raise ValueError("refusing to remove deployment through a link or reparse point")
    try:
        relative = deployment.relative_to(rendered_store)
    except ValueError as exc:
        raise ValueError(f"deployment is outside rendered store: {deployment}") from exc
    if len(relative.parts) != 2 or not relative.parts[0].startswith("sha256-"):
        raise ValueError(f"deployment path has an unsafe layout: {deployment}")
    verification = verify_deployment(deployment)
    if not verification.ok:
        raise ValueError(
            f"refusing to remove unverified deployment: {verification.state}"
        )

    trash = Path(trash_root)
    trash.mkdir(parents=True, exist_ok=True)
    displaced = trash / f"{relative.parts[0]}-{relative.parts[1]}-{uuid.uuid4().hex}"
    _make_writable(deployment)
    try:
        os.replace(deployment, displaced)
    except Exception:
        _make_read_only(deployment)
        raise
    _remove_tree(displaced)
    digest_dir = deployment.parent
    try:
        digest_dir.rmdir()
    except OSError:
        pass
    return displaced


def expected_provenance(
    logical_skill: str,
    source_hash: str,
    target_client: str,
    *,
    resolver_version: str = RESOLVER_VERSION,
) -> dict[str, Any]:
    """Return the deterministic provenance expected for one deployment."""

    resolved_hash = resolution_hash(
        logical_skill,
        source_hash,
        target_client,
        resolver_version=resolver_version,
    )
    return {
        "schema_version": 1,
        "logical_skill": logical_skill,
        "source_hash": source_hash,
        "resolution_hash": resolved_hash,
        "resolver_version": resolver_version,
        "rendered_hash": source_hash,
        "target_client": target_client,
        "applied_layers": list(APPLIED_LAYERS),
    }


def _hash_rendered_content(deployment: Path) -> str:
    _assert_no_reparse_tree(deployment)
    manifest = deployment / PROVENANCE_FILE
    if manifest.is_symlink():
        raise ValueError(f"provenance must not be a symlink: {manifest}")
    with tempfile.TemporaryDirectory(prefix="skill-sync-verify-") as temp_dir:
        snapshot = Path(temp_dir) / deployment.name
        def ignore(directory: str, names: list[str]) -> set[str]:
            if Path(directory) == deployment and PROVENANCE_FILE in names:
                return {PROVENANCE_FILE}
            return set()

        shutil.copytree(deployment, snapshot, ignore=ignore)
        return hash_skill_dir(snapshot)


def _assert_no_reparse_tree(root: Path) -> None:
    if is_link_or_reparse(root):
        raise ValueError(f"deployment contains link or reparse point: {root}")
    for directory, directories, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in directories + files:
            child = directory_path / name
            if is_link_or_reparse(child):
                raise ValueError(
                    f"deployment contains link or reparse point: {child}"
                )


def _valid_provenance_shape(value: dict[str, Any]) -> bool:
    string_fields = (
        "logical_skill",
        "source_hash",
        "resolution_hash",
        "resolver_version",
        "rendered_hash",
        "target_client",
    )
    if not all(isinstance(value.get(key), str) for key in string_fields):
        return False
    if value.get("schema_version") != 1:
        return False
    try:
        _validate_identifier(value["logical_skill"], "logical Skill name")
        _validate_identifier(value["target_client"], "target client")
        _validate_sha256(value["source_hash"], "source hash")
        _validate_sha256(value["resolution_hash"], "resolution hash")
        _validate_sha256(value["rendered_hash"], "rendered hash")
    except ValueError:
        return False
    return bool(value["resolver_version"]) and value.get("applied_layers") == list(
        APPLIED_LAYERS
    )


def _copy_ignore(source_root: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        directory_path = Path(directory)
        ignored: set[str] = set()
        for name in names:
            child = directory_path / name
            relative = child.relative_to(source_root).as_posix()
            if is_ignored_path(relative, is_dir=child.is_dir()):
                ignored.add(name)
        return ignored

    return ignore


def _make_read_only(root: Path) -> None:
    for directory, directories, files in os.walk(root):
        directory_path = Path(directory)
        for name in files:
            path = directory_path / name
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        for name in directories:
            path = directory_path / name
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def _make_writable(root: Path) -> None:
    if root.is_symlink() or not root.exists():
        return
    for directory, directories, files in os.walk(root):
        directory_path = Path(directory)
        directory_path.chmod(stat.S_IMODE(directory_path.stat().st_mode) | 0o700)
        for name in directories + files:
            path = directory_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)


def _remove_tree(path: Path) -> None:
    _make_writable(path)
    shutil.rmtree(path)


def _validate_identifier(value: str, label: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise ValueError(f"{label} must be a single safe path component")


def _validate_sha256(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in string.hexdigits for character in value[7:])
    ):
        raise ValueError(f"{label} must be a full sha256: string")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
