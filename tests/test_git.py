import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import skill_sync.git as git_module
    from skill_sync.git import (
        GitError,
        clone_or_use_existing,
        clone_repo,
        commit_all_if_changed,
        fetch,
        init_repo,
        is_clean,
        merge_ff_only,
        push,
        run_git,
        state,
    )
except ImportError as exc:  # pragma: no cover - exercised by initial TDD red run
    if "skill_sync.git" not in str(exc):
        raise
    git_module = None
    GitError = None
    clone_or_use_existing = None
    clone_repo = None
    commit_all_if_changed = None
    fetch = None
    init_repo = None
    is_clean = None
    merge_ff_only = None
    push = None
    run_git = None
    state = None


def require_git():
    if shutil.which("git") is None:
        raise unittest.SkipTest("git executable is not available")


def configure_identity(repo: Path) -> None:
    run_git(repo, ["config", "user.name", "Skill Sync Tests"])
    run_git(repo, ["config", "user.email", "skill-sync-tests@example.invalid"])


def write_file(repo: Path, relative_path: str, text: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_file(repo: Path, relative_path: str) -> str:
    return (repo / relative_path).read_text(encoding="utf-8")


def make_commit(repo: Path, relative_path: str, text: str, message: str) -> None:
    write_file(repo, relative_path, text)
    run_git(repo, ["add", "."])
    run_git(repo, ["commit", "-m", message])


def create_remote_with_initial_commit(work: Path) -> tuple[Path, Path]:
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source"
    remote = work / "remote.git"
    init_repo(source)
    configure_identity(source)
    make_commit(source, "README.md", "initial\n", "initial")
    run_git(work, ["init", "--bare", str(remote)])
    run_git(remote, ["config", "receive.denyDeleteCurrent", "ignore"])
    run_git(source, ["remote", "add", "origin", str(remote)])
    run_git(source, ["push", "origin", "HEAD:main"])
    return source, remote


@unittest.skipIf(shutil.which("git") is None, "git executable is not available")
class GitWrapperTest(unittest.TestCase):
    def setUp(self):
        if run_git is None:
            self.fail("skill_sync.git module is missing")
        if clone_or_use_existing is None:
            self.fail("clone_or_use_existing is missing")

    def assert_git_subprocess_hardened(self, kwargs):
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], git_module.GIT_TIMEOUT_SECONDS)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GIT_ASKPASS"], "true")

    def test_run_git_uses_non_interactive_bounded_subprocess(self):
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        with mock.patch.object(git_module.shutil, "which", return_value="/usr/bin/git"):
            with mock.patch.object(git_module.subprocess, "run", return_value=completed) as run:
                self.assertEqual(run_git(Path("/tmp/repo"), ["status"]), "ok")

        args, kwargs = run.call_args
        self.assertEqual(args[0], ["git", "status"])
        self.assertEqual(kwargs["cwd"], Path("/tmp/repo"))
        self.assert_git_subprocess_hardened(kwargs)

    def test_clone_repo_uses_non_interactive_bounded_subprocess(self):
        completed = subprocess.CompletedProcess(
            args=["git", "clone"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "clone"
            with mock.patch.object(git_module.shutil, "which", return_value="/usr/bin/git"):
                with mock.patch.object(git_module.subprocess, "run", return_value=completed) as run:
                    clone_repo("https://example.invalid/repo.git", dest)

        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                "git",
                "clone",
                "--branch",
                "main",
                "https://example.invalid/repo.git",
                str(dest),
            ],
        )
        self.assertEqual(kwargs["cwd"], dest.parent)
        self.assert_git_subprocess_hardened(kwargs)

    def test_init_repo_creates_main_branch_and_clean_worktree(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"

            init_repo(repo)

            self.assertEqual(run_git(repo, ["branch", "--show-current"]), "main")
            self.assertTrue(is_clean(repo))

    def test_clone_repo_checks_out_existing_remote_branch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"

            clone_repo(str(remote), clone)

            self.assertEqual(read_file(clone, "README.md"), "initial\n")
            self.assertEqual(run_git(clone, ["branch", "--show-current"]), "main")
            self.assertTrue(is_clean(clone))

    def test_clone_or_use_existing_reuses_matching_clone_and_checks_out_branch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            source, remote = create_remote_with_initial_commit(work)
            existing = work / "existing"
            clone_repo(str(remote), existing)
            run_git(existing, ["checkout", "-b", "topic"])

            make_commit(source, "remote.txt", "remote\n", "remote change")
            push(source)

            clone_or_use_existing(str(remote), existing)

            self.assertEqual(run_git(existing, ["branch", "--show-current"]), "main")
            self.assertEqual(
                run_git(existing, ["rev-parse", "origin/main"]),
                run_git(source, ["rev-parse", "HEAD"]),
            )

    def test_clone_or_use_existing_clones_when_destination_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            dest = work / "missing"

            clone_or_use_existing(str(remote), dest)

            self.assertEqual(read_file(dest, "README.md"), "initial\n")
            self.assertEqual(run_git(dest, ["branch", "--show-current"]), "main")

    def test_clone_or_use_existing_fails_for_wrong_existing_origin(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, expected_remote = create_remote_with_initial_commit(work / "expected")
            _, wrong_remote = create_remote_with_initial_commit(work / "wrong")
            existing = work / "existing"
            clone_repo(str(wrong_remote), existing)

            with self.assertRaisesRegex(GitError, "origin URL"):
                clone_or_use_existing(str(expected_remote), existing)

    def test_clone_or_use_existing_fails_for_existing_non_git_destination(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            dest = work / "dest"
            dest.mkdir()
            write_file(dest, "not-git.txt", "not git\n")

            with self.assertRaisesRegex(GitError, "not.*git"):
                clone_or_use_existing(str(remote), dest)

    def test_clone_or_use_existing_fails_for_dirty_existing_checkout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            existing = work / "existing"
            clone_repo(str(remote), existing)
            write_file(existing, "dirty.txt", "dirty\n")

            with self.assertRaisesRegex(GitError, "dirty"):
                clone_or_use_existing(str(remote), existing)

    def test_is_clean_detects_untracked_and_modified_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            init_repo(repo)
            configure_identity(repo)

            write_file(repo, "new.txt", "new\n")
            self.assertFalse(is_clean(repo))
            self.assertTrue(commit_all_if_changed(repo, "add new file"))
            self.assertTrue(is_clean(repo))

            write_file(repo, "new.txt", "changed\n")
            self.assertFalse(is_clean(repo))

    def test_state_reports_clean_ahead_and_behind_counts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            source, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            configure_identity(clone)

            make_commit(source, "remote.txt", "remote\n", "remote change")
            push(source)
            make_commit(clone, "local.txt", "local\n", "local change")

            result = state(clone)

            self.assertTrue(result.clean)
            self.assertEqual(result.ahead, 1)
            self.assertEqual(result.behind, 1)
            self.assertTrue(result.diverged)

    def test_merge_ff_only_pulls_remote_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            source, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)

            make_commit(source, "remote.txt", "remote\n", "remote change")
            push(source)

            merge_ff_only(clone)

            self.assertEqual(read_file(clone, "remote.txt"), "remote\n")
            self.assertEqual(state(clone).behind, 0)

    def test_commit_all_if_changed_commits_only_when_changes_exist(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            init_repo(repo)
            configure_identity(repo)

            self.assertFalse(commit_all_if_changed(repo, "nothing"))

            write_file(repo, "skill.md", "# Skill\n")
            self.assertTrue(commit_all_if_changed(repo, "add skill"))

            self.assertEqual(run_git(repo, ["log", "-1", "--pretty=%s"]), "add skill")
            self.assertTrue(is_clean(repo))
            self.assertFalse(commit_all_if_changed(repo, "nothing again"))

    def test_push_updates_local_remote(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            configure_identity(clone)

            make_commit(clone, "pushed.txt", "pushed\n", "pushed change")
            push(clone)

            verify = work / "verify"
            clone_repo(str(remote), verify)
            self.assertEqual(read_file(verify, "pushed.txt"), "pushed\n")

    def test_merge_ff_only_fails_closed_on_dirty_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            write_file(clone, "dirty.txt", "dirty\n")

            with self.assertRaisesRegex(GitError, "dirty"):
                merge_ff_only(clone)

    def test_merge_ff_only_fails_closed_without_origin(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            init_repo(repo)
            configure_identity(repo)
            make_commit(repo, "local.txt", "local\n", "local root")

            with self.assertRaisesRegex(GitError, "origin"):
                merge_ff_only(repo)

    def test_push_fails_closed_on_dirty_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            write_file(clone, "dirty.txt", "dirty\n")

            with self.assertRaisesRegex(GitError, "dirty"):
                push(clone)

    def test_push_fails_closed_without_origin(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            init_repo(repo)
            configure_identity(repo)
            make_commit(repo, "local.txt", "local\n", "local root")

            with self.assertRaisesRegex(GitError, "origin"):
                push(repo)

    def test_merge_ff_only_fails_closed_on_diverged_branch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            source, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            configure_identity(clone)

            make_commit(source, "remote.txt", "remote\n", "remote change")
            push(source)
            make_commit(clone, "local.txt", "local\n", "local change")

            with self.assertRaisesRegex(GitError, "diverged"):
                merge_ff_only(clone)

    def test_state_fails_closed_when_remote_branch_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            init_repo(clone)
            run_git(clone, ["remote", "add", "origin", str(remote)])
            run_git(clone, ["config", "--unset-all", "remote.origin.fetch"])

            with self.assertRaisesRegex(GitError, "missing remote branch"):
                state(clone)

    def test_merge_ff_only_fails_closed_on_unrelated_histories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            repo = work / "unrelated"
            init_repo(repo)
            configure_identity(repo)
            make_commit(repo, "local.txt", "local\n", "local root")
            run_git(repo, ["remote", "add", "origin", str(remote)])

            with self.assertRaisesRegex(GitError, "unrelated histories"):
                merge_ff_only(repo)

    def test_push_fails_closed_on_force_push_divergence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            source, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            configure_identity(clone)

            make_commit(clone, "local.txt", "local\n", "local change")
            run_git(source, ["checkout", "--orphan", "rewritten-main"])
            run_git(source, ["rm", "-rf", "."])
            make_commit(source, "rewritten.txt", "rewritten\n", "rewritten root")
            run_git(source, ["push", "--force", "origin", "HEAD:main"])

            with self.assertRaisesRegex(GitError, "diverged"):
                push(clone)

    def test_push_fails_closed_when_remote_rejects_update_due_to_hook(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            clone = work / "clone"
            clone_repo(str(remote), clone)
            configure_identity(clone)
            hook = remote / "hooks" / "pre-receive"
            hook.write_text("#!/bin/sh\necho hook rejection >&2\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)

            make_commit(clone, "local.txt", "local\n", "local change")

            with self.assertRaisesRegex(GitError, "push rejected|hook rejection"):
                push(clone)

    def test_fetch_fails_closed_when_remote_branch_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            _, remote = create_remote_with_initial_commit(work)
            repo = work / "repo"
            init_repo(repo)
            run_git(repo, ["remote", "add", "origin", str(remote)])
            run_git(repo, ["config", "--unset-all", "remote.origin.fetch"])

            with self.assertRaisesRegex(GitError, "missing remote branch"):
                fetch(repo)

    def test_fetch_preserves_inaccessible_origin_failure_context(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            repo = work / "repo"
            not_git_remote = work / "not-git"
            not_git_remote.mkdir()
            init_repo(repo)
            run_git(repo, ["remote", "add", "origin", str(not_git_remote)])

            with self.assertRaises(GitError) as context:
                fetch(repo)

            message = str(context.exception)
            self.assertIn("git fetch", message)
            self.assertNotIn("missing remote branch", message)

    def test_fetch_fails_closed_without_origin(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            init_repo(repo)

            with self.assertRaisesRegex(GitError, "origin"):
                fetch(repo)


if __name__ == "__main__":
    unittest.main()
