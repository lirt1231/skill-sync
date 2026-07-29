"""Read-only diff and validation for active Base edit-session trees."""

from __future__ import annotations

import difflib
import hashlib
import ntpath
import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from skill_sync.hash import is_ignored_path, is_link_or_reparse


class EditTreeInspectionError(ValueError):
    """Raised when a session tree cannot be inspected safely."""


@dataclass(frozen=True)
class TreeIssue:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class FileRecord:
    path: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()

    def safe_text(self) -> str | None:
        try:
            text = self.content.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if any(
            ord(character) < 32 and character not in "\t\n\r"
            or ord(character) == 127
            for character in text
        ):
            return None
        return text


@dataclass(frozen=True)
class TreeInspection:
    files: dict[str, FileRecord]
    issues: tuple[TreeIssue, ...]

    @property
    def hash(self) -> str | None:
        if self.issues:
            return None
        digest = hashlib.sha256()
        for path in sorted(self.files):
            record = self.files[path]
            path_bytes = path.encode("utf-8")
            digest.update(b"file\0")
            digest.update(struct.pack(">Q", len(path_bytes)))
            digest.update(path_bytes)
            digest.update(struct.pack(">Q", len(record.content)))
            digest.update(record.content)
        return f"sha256:{digest.hexdigest()}"


def inspect_tree(root: str | Path) -> TreeInspection:
    """Read a real directory without following links or special files."""

    root_path = Path(root)
    if is_link_or_reparse(root_path) or not root_path.is_dir():
        raise EditTreeInspectionError(
            f"edit session tree must be a real directory: {root_path}"
        )

    files: dict[str, FileRecord] = {}
    issues: list[TreeIssue] = []
    try:
        _inspect_directory(root_path, root_path, files, issues)
    except EditTreeInspectionError:
        raise
    except OSError as exc:
        raise EditTreeInspectionError(
            f"cannot safely inspect edit session tree: {root_path}: {exc}"
        ) from exc
    return TreeInspection(files=files, issues=tuple(issues))


def build_diff(
    baseline: TreeInspection,
    workspace: TreeInspection,
) -> dict[str, Any]:
    """Return deterministic file classifications and safe unified text diffs."""

    if baseline.issues or workspace.issues:
        issue = (baseline.issues + workspace.issues)[0]
        raise EditTreeInspectionError(
            f"cannot diff unsafe edit session path {issue.path}: {issue.message}"
        )

    changes: list[dict[str, Any]] = []
    paths = sorted(set(baseline.files) | set(workspace.files))
    for path in paths:
        old = baseline.files.get(path)
        new = workspace.files.get(path)
        if old is not None and new is not None and old.content == new.content:
            continue
        if old is None:
            change = "added"
        elif new is None:
            change = "deleted"
        else:
            change = "modified"

        old_text = old.safe_text() if old is not None else ""
        new_text = new.safe_text() if new is not None else ""
        kind = "text" if old_text is not None and new_text is not None else "binary"
        item: dict[str, Any] = {
            "path": path,
            "change": change,
            "kind": kind,
            "old_hash": old.hash if old is not None else None,
            "new_hash": new.hash if new is not None else None,
            "old_size": old.size if old is not None else None,
            "new_size": new.size if new is not None else None,
        }
        if kind == "text":
            item["diff"] = _unified_diff(path, old_text or "", new_text or "")
        changes.append(item)

    summary = {
        "added": sum(item["change"] == "added" for item in changes),
        "modified": sum(item["change"] == "modified" for item in changes),
        "deleted": sum(item["change"] == "deleted" for item in changes),
        "total": len(changes),
    }
    return {"changed": bool(changes), "summary": summary, "files": changes}


def validate_workspace(
    inspection: TreeInspection,
    *,
    logical_skill: str,
) -> list[TreeIssue]:
    """Validate safe paths plus the minimum portable SKILL.md frontmatter."""

    issues = list(inspection.issues)
    skill_file = inspection.files.get("SKILL.md")
    if skill_file is None:
        issues.append(
            TreeIssue("missing_skill_file", "SKILL.md", "workspace must contain SKILL.md")
        )
        return sorted(issues, key=_issue_sort_key)

    text = skill_file.safe_text()
    if text is None:
        issues.append(
            TreeIssue(
                "invalid_skill_file",
                "SKILL.md",
                "SKILL.md must be safe UTF-8 text",
            )
        )
        return sorted(issues, key=_issue_sort_key)

    issues.extend(_validate_frontmatter(text, logical_skill=logical_skill))
    return sorted(issues, key=_issue_sort_key)


def _inspect_directory(
    root: Path,
    directory: Path,
    files: dict[str, FileRecord],
    issues: list[TreeIssue],
) -> None:
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise EditTreeInspectionError(
            f"directory became unsafe while being inspected: {directory}"
        )
    with os.scandir(directory) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix()
        path_issue = validate_relative_path(relative)
        if path_issue is not None:
            issues.append(path_issue)

        if is_link_or_reparse(path) or entry.is_symlink():
            issues.append(
                TreeIssue(
                    "linked_path",
                    relative,
                    "links and reparse points are not allowed in an edit workspace",
                )
            )
            continue

        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if is_ignored_path(relative, is_dir=True):
                continue
            _inspect_directory(root, path, files, issues)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            issues.append(
                TreeIssue(
                    "non_regular_path",
                    relative,
                    "only regular files and directories are allowed",
                )
            )
            continue
        if is_ignored_path(relative):
            continue
        try:
            content = _read_regular_file(path)
        except EditTreeInspectionError as exc:
            issues.append(TreeIssue("unsafe_file", relative, str(exc)))
            continue
        files[relative] = FileRecord(path=relative, content=content)


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EditTreeInspectionError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise EditTreeInspectionError(f"file changed while being inspected: {path}")
        if is_link_or_reparse(path):
            raise EditTreeInspectionError(f"path became a link while being inspected: {path}")
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or any(
            getattr(after, field) != getattr(current, field) for field in stable_fields
        ):
            raise EditTreeInspectionError(f"file was replaced while being inspected: {path}")
        return content
    finally:
        os.close(descriptor)


def validate_relative_path(path: str) -> TreeIssue | None:
    """Return an issue when a relative path is unsafe on supported platforms."""

    parts = path.split("/")
    invalid = (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any("\\" in part or ":" in part for part in parts)
        or any(any(ord(character) < 32 for character in part) for part in parts)
        or any(
            part.endswith((" ", ".")) or _is_reserved_windows_path(part)
            for part in parts
        )
        or PureWindowsPath(path).is_absolute()
    )
    if not invalid:
        return None
    return TreeIssue(
        "invalid_path",
        path,
        "path is not a safe portable relative path",
    )


def _is_reserved_windows_path(path: str) -> bool:
    checker = getattr(ntpath, "isreserved", None)
    if checker is not None:
        return checker(path)
    return PureWindowsPath(path).is_reserved()


def _validate_frontmatter(text: str, *, logical_skill: str) -> list[TreeIssue]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return [
            TreeIssue(
                "invalid_frontmatter",
                "SKILL.md",
                "SKILL.md must start with YAML frontmatter",
            )
        ]
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        return [
            TreeIssue(
                "invalid_frontmatter",
                "SKILL.md",
                "SKILL.md frontmatter is missing its closing delimiter",
            )
        ]

    values: dict[str, str] = {}
    issues: list[TreeIssue] = []
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            continue
        if ":" not in line:
            issues.append(
                TreeIssue(
                    "invalid_frontmatter",
                    "SKILL.md",
                    f"frontmatter entry is not a key/value pair: {line}",
                )
            )
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key in values:
            issues.append(
                TreeIssue(
                    "invalid_frontmatter",
                    "SKILL.md",
                    f"frontmatter key is empty or duplicated: {key or '<empty>'}",
                )
            )
            continue
        values[key] = value.strip().strip("'\"")

    for required in ("name", "description"):
        if not values.get(required):
            issues.append(
                TreeIssue(
                    "invalid_frontmatter",
                    "SKILL.md",
                    f"frontmatter requires a non-empty {required} field",
                )
            )
    if values.get("name") and values["name"] != logical_skill:
        issues.append(
            TreeIssue(
                "skill_name_mismatch",
                "SKILL.md",
                f"frontmatter name must match logical Skill {logical_skill}",
            )
        )
    return issues


def _unified_diff(path: str, old: str, new: str) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"baseline/{path}",
            tofile=f"workspace/{path}",
            lineterm="",
        )
    )
    return "\n".join(lines) + ("\n" if lines else "")


def _issue_sort_key(issue: TreeIssue) -> tuple[str, str, str]:
    return issue.path, issue.code, issue.message
