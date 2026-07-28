from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitState:
    clean: bool
    ahead: int
    behind: int
    diverged: bool


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise GitError("git executable is not available")


def _run_git_subprocess(
    cwd: Path,
    args: list[str],
    *,
    read_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    ensure_git_available()
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    if read_only:
        # Status and graph inspection must not refresh the index or create
        # optional lock files. Mutation commands retain Git's normal locking.
        env["GIT_OPTIONAL_LOCKS"] = "0"
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"{' '.join(command)} timed out after {GIT_TIMEOUT_SECONDS} seconds"
        ) from exc


def run_git(repo: Path, args: list[str], *, read_only: bool = False) -> str:
    result = _run_git_subprocess(repo, args, read_only=read_only)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        command = "git " + " ".join(args)
        raise GitError(f"{command} failed: {detail}")
    return result.stdout.strip()


def init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, ["init", "-b", branch])


def clone_repo(repo_url: str, dest: Path, branch: str = "main") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git_subprocess(
        dest.parent,
        ["clone", "--branch", branch, repo_url, str(dest)],
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"git clone failed: {detail}")


def clone_or_use_existing(repo_url: str, dest: Path, branch: str = "main") -> None:
    if not dest.exists():
        clone_repo(repo_url, dest, branch)
        return
    if not (dest / ".git").exists():
        raise GitError(f"{dest} exists but is not the expected git repo")

    try:
        inside_work_tree = run_git(dest, ["rev-parse", "--is-inside-work-tree"])
    except GitError as exc:
        raise GitError(f"{dest} exists but is not a usable git repo") from exc
    if inside_work_tree != "true":
        raise GitError(f"{dest} exists but is not a usable git repo")

    origin_url = run_git(dest, ["remote", "get-url", "origin"])
    if origin_url != repo_url:
        raise GitError(f"origin URL mismatch: expected {repo_url}, found {origin_url}")

    _ensure_clean(dest)
    fetch(dest, branch)
    try:
        run_git(dest, ["checkout", branch])
    except GitError:
        run_git(dest, ["checkout", "-b", branch, "--track", f"origin/{branch}"])


def is_clean(repo: Path) -> bool:
    return run_git(repo, ["status", "--porcelain"], read_only=True) == ""


def _has_origin(repo: Path) -> bool:
    try:
        run_git(repo, ["remote", "get-url", "origin"], read_only=True)
    except GitError:
        return False
    return True


def _require_origin(repo: Path) -> None:
    if not _has_origin(repo):
        raise GitError("missing origin remote")


def fetch(repo: Path, branch: str = "main") -> None:
    _require_origin(repo)
    try:
        run_git(repo, ["fetch", "origin", branch])
    except GitError as exc:
        raise GitError(f"git fetch failed: {exc}") from exc
    try:
        run_git(repo, ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"])
    except GitError as exc:
        raise GitError(f"missing remote branch origin/{branch}") from exc


def state(repo: Path, branch: str = "main", *, fetch_remote: bool = True) -> GitState:
    clean = is_clean(repo)
    if not _has_origin(repo):
        return GitState(clean=clean, ahead=0, behind=0, diverged=False)

    if fetch_remote:
        fetch(repo, branch)
    counts = run_git(
        repo,
        ["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"],
        read_only=True,
    )
    ahead_text, behind_text = counts.split()
    ahead = int(ahead_text)
    behind = int(behind_text)
    return GitState(clean=clean, ahead=ahead, behind=behind, diverged=ahead > 0 and behind > 0)


def remote_changed_paths(repo: Path, branch: str = "main") -> tuple[str, ...]:
    """Return paths changed between local HEAD and the cached remote branch."""

    _require_origin(repo)
    output = run_git(
        repo,
        [
            "diff",
            "--name-only",
            "--no-renames",
            f"HEAD..origin/{branch}",
            "--",
        ],
        read_only=True,
    )
    return tuple(path for path in output.splitlines() if path)


def read_remote_file(repo: Path, path: str, branch: str = "main") -> str:
    """Read one UTF-8 text file from the cached remote branch."""

    _require_origin(repo)
    return run_git(
        repo,
        ["show", f"origin/{branch}:{path}"],
        read_only=True,
    ) + "\n"


def _ensure_clean(repo: Path) -> None:
    if not is_clean(repo):
        raise GitError("sync repository is dirty")


def _ensure_related(repo: Path, branch: str) -> None:
    try:
        run_git(repo, ["merge-base", "HEAD", f"origin/{branch}"])
    except GitError as exc:
        raise GitError("unrelated histories") from exc


def merge_ff_only(repo: Path, branch: str = "main") -> None:
    _ensure_clean(repo)
    _require_origin(repo)
    current = state(repo, branch)
    if current.behind > 0:
        _ensure_related(repo, branch)
    if current.diverged:
        raise GitError("local and remote branches diverged")
    if current.behind == 0:
        return
    try:
        run_git(repo, ["merge", "--ff-only", f"origin/{branch}"])
    except GitError as exc:
        message = str(exc)
        if "refusing to merge unrelated histories" in message or "no merge base" in message:
            raise GitError("unrelated histories") from exc
        raise GitError(f"fast-forward merge failed: {message}") from exc


def commit_all_if_changed(repo: Path, message: str) -> bool:
    if is_clean(repo):
        return False
    run_git(repo, ["add", "-A"])
    run_git(repo, ["commit", "-m", message])
    return True


def push(repo: Path, branch: str = "main") -> None:
    _ensure_clean(repo)
    _require_origin(repo)
    current = state(repo, branch)
    if current.diverged or current.behind > 0:
        raise GitError("local and remote branches diverged")
    try:
        run_git(repo, ["push", "origin", f"HEAD:{branch}"])
    except GitError as exc:
        message = str(exc)
        if "rejected" in message or "non-fast-forward" in message:
            raise GitError(f"push rejected: {message}") from exc
        raise
