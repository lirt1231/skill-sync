import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import skill_sync.core as core_module
    from skill_sync.core import (
        deselect_skills,
        init_sync,
        pull,
        push,
        scan_skills,
        select_skills,
        status,
        sync,
    )
    from skill_sync.errors import SkillSyncError
except ImportError as exc:  # pragma: no cover - exercised by initial TDD red run
    if "skill_sync.core" not in str(exc) and "skill_sync.errors" not in str(exc):
        raise
    core_module = None
    init_sync = None
    scan_skills = None
    select_skills = None
    deselect_skills = None
    status = None
    pull = None
    push = None
    sync = None
    SkillSyncError = Exception

from skill_sync.config import load_config, save_config
from skill_sync.git import init_repo, run_git
from skill_sync.hash import hash_skill_dir
from skill_sync.registry import load_registry, save_registry


def require_git():
    if shutil.which("git") is None:
        raise unittest.SkipTest("git executable is not available")


def configure_identity(repo: Path) -> None:
    run_git(repo, ["config", "user.name", "Skill Sync Tests"])
    run_git(repo, ["config", "user.email", "skill-sync-tests@example.invalid"])


def write_file(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_file(root: Path, relative_path: str) -> str:
    return (root / relative_path).read_text(encoding="utf-8")


def make_skill(root: Path, name: str, text: str | None = None) -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    write_file(skill, "SKILL.md", text or f"# {name}\n")
    return skill


def make_commit(repo: Path, relative_path: str, text: str, message: str) -> None:
    write_file(repo, relative_path, text)
    run_git(repo, ["add", "."])
    run_git(repo, ["commit", "-m", message])


def create_remote_with_registry(work: Path) -> tuple[Path, Path]:
    source = work / "source"
    remote = work / "remote.git"
    init_repo(source)
    configure_identity(source)
    save_registry(source / "registry.yaml", {"version": 1, "skills": {}})
    run_git(source, ["add", "."])
    run_git(source, ["commit", "-m", "initial registry"])
    run_git(work, ["init", "--bare", str(remote)])
    run_git(source, ["remote", "add", "origin", str(remote)])
    run_git(source, ["push", "origin", "HEAD:main"])
    return source, remote


def add_remote_skill(source: Path, name: str, text: str, message: str | None = None) -> None:
    registry = load_registry(source / "registry.yaml")
    registry.setdefault("skills", {})[name] = {
        "selected": True,
        "source_platform": "codex",
        "display_name": name,
    }
    save_registry(source / "registry.yaml", registry)
    make_skill(source / "skills", name, text)
    run_git(source, ["add", "."])
    run_git(source, ["commit", "-m", message or f"add {name}"])
    run_git(source, ["push", "origin", "HEAD:main"])


@unittest.skipIf(shutil.which("git") is None, "git executable is not available")
class CoreWorkflowTest(unittest.TestCase):
    def setUp(self):
        if core_module is None:
            self.fail("skill_sync.core module is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.config_path = self.work / "config.json"
        self.skill_root = self.work / "codex-home" / "skills"
        self.skill_root.mkdir(parents=True)
        self.env_patch = mock.patch.dict(
            "os.environ", {"CODEX_HOME": str(self.work / "codex-home")}, clear=False
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def init_from_remote(self) -> tuple[Path, Path, Path]:
        _, remote = create_remote_with_registry(self.work)
        sync_repo = self.work / "sync"
        result = init_sync(
            str(remote),
            sync_dir=sync_repo,
            config_path=self.config_path,
        )
        configure_identity(sync_repo)
        self.assertEqual(Path(result["sync_repo_path"]), sync_repo)
        return remote, sync_repo, self.config_path

    def select_default_skill(self, name: str, text: str | None = None) -> Path:
        skill = make_skill(self.skill_root, name, text)
        select_skills(
            [name],
            config_path=self.config_path,
            skill_dir=self.skill_root,
        )
        return skill

    def test_init_creates_missing_local_repo_as_non_bare_with_registry_and_defaults(self):
        repo = self.work / "new-local-sync-repo"

        init_sync(str(repo), config_path=self.config_path)

        self.assertTrue((repo / ".git").is_dir())
        self.assertEqual(run_git(repo, ["rev-parse", "--is-bare-repository"]), "false")
        self.assertEqual(load_registry(repo / "registry.yaml"), {"version": 1, "skills": {}})
        config = load_config(self.config_path)
        self.assertEqual(config["sync_repo_path"], str(repo))
        self.assertEqual(config["branch"], "main")
        self.assertEqual(config["platform"], "codex")

    def test_init_uses_default_sync_dir_and_branch(self):
        _, remote = create_remote_with_registry(self.work / "remote-work")
        data_home = self.work / "xdg-data"

        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(data_home)}, clear=False):
            init_sync(str(remote), config_path=self.config_path)

        config = load_config(self.config_path)
        self.assertEqual(
            config["sync_repo_path"],
            str(data_home / "skill-sync" / "repo"),
        )
        self.assertEqual(config["branch"], "main")

    def test_init_rejects_missing_local_repo_with_explicit_sync_dir(self):
        with self.assertRaisesRegex(SkillSyncError, "sync_dir|missing local"):
            init_sync(
                str(self.work / "missing-local-repo"),
                sync_dir=self.work / "explicit-sync-dir",
                config_path=self.config_path,
            )

    def test_init_reports_git_availability_failure(self):
        with mock.patch.object(core_module.git, "ensure_git_available", side_effect=core_module.git.GitError("git missing")):
            with self.assertRaisesRegex(SkillSyncError, "git missing"):
                init_sync(str(self.work / "repo"), config_path=self.config_path)

    def test_init_reports_git_auth_or_subprocess_failure(self):
        with mock.patch.object(core_module.git, "clone_repo", side_effect=core_module.git.GitError("authentication failed")):
            with self.assertRaisesRegex(SkillSyncError, "authentication failed"):
                init_sync(
                    "https://example.invalid/private.git",
                    sync_dir=self.work / "sync",
                    config_path=self.config_path,
                )

    def test_scan_lists_selected_and_external_candidates_without_mutating_state(self):
        self.init_from_remote()
        make_skill(self.skill_root, "selected")
        make_skill(self.skill_root, "unselected")
        select_skills(["selected"], config_path=self.config_path, skill_dir=self.skill_root)
        before = read_file(self.work / "sync", "registry.yaml")

        candidates = scan_skills(config_path=self.config_path, skill_dir=self.skill_root)
        external_candidates = scan_skills(
            config_path=self.config_path,
            skill_dir=self.work / "other-root",
        )

        self.assertEqual(
            [(item["name"], item["selected"], item["external"]) for item in candidates],
            [("selected", True, False), ("unselected", False, False)],
        )
        self.assertEqual(external_candidates, [])
        self.assertEqual(read_file(self.work / "sync", "registry.yaml"), before)

    def test_select_and_deselect_update_registry_and_local_config_without_commit(self):
        self.init_from_remote()
        skill = make_skill(self.skill_root, "alpha")

        select_skills([str(skill)], config_path=self.config_path, skill_dir=self.skill_root)

        registry = load_registry(self.work / "sync" / "registry.yaml")
        self.assertEqual(
            registry["skills"]["alpha"],
            {"selected": True, "source_platform": "codex", "display_name": "alpha"},
        )
        self.assertNotIn(str(skill), read_file(self.work / "sync", "registry.yaml"))
        self.assertEqual(
            load_config(self.config_path)["skills"]["alpha"]["local_path"],
            str(skill.resolve()),
        )
        self.assertNotEqual(run_git(self.work / "sync", ["status", "--porcelain"]), "")

        deselect_skills(["alpha"], config_path=self.config_path)

        self.assertNotIn("alpha", load_registry(self.work / "sync" / "registry.yaml")["skills"])
        self.assertNotIn("alpha", load_config(self.config_path)["skills"])

    def test_select_rejects_external_without_flag_invalid_missing_and_non_skill_paths(self):
        self.init_from_remote()
        external = make_skill(self.work / "external", "outside")
        missing = self.work / "missing"
        non_skill = self.work / "not-skill"
        non_skill.mkdir()

        with self.assertRaisesRegex(SkillSyncError, "external"):
            select_skills([str(external)], config_path=self.config_path, skill_dir=self.skill_root)
        with self.assertRaisesRegex(SkillSyncError, "does not exist"):
            select_skills([str(missing)], config_path=self.config_path, skill_dir=self.skill_root)
        with self.assertRaisesRegex(SkillSyncError, "SKILL.md"):
            select_skills([str(non_skill)], config_path=self.config_path, skill_dir=self.skill_root)

        select_skills(
            [str(external)],
            config_path=self.config_path,
            skill_dir=self.skill_root,
            allow_external=True,
        )
        self.assertIn("outside", load_config(self.config_path)["skills"])

    def test_status_reports_hashes_repo_state_and_repeated_skill_filtering(self):
        self.init_from_remote()
        self.select_default_skill("alpha")
        self.select_default_skill("beta")
        push(config_path=self.config_path, skill_names=["alpha", "beta"])
        write_file(self.skill_root, "alpha/extra.txt", "changed\n")

        result = status(config_path=self.config_path, skill_names=["alpha"])

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual([item["name"] for item in result["skills"]], ["alpha"])
        self.assertTrue(result["skills"][0]["changed_local"])
        self.assertIn("local_path", result["skills"][0])
        self.assertNotIn("beta", [item["name"] for item in result["skills"]])
        self.assertTrue(result["repo"]["clean"])

    def test_push_allows_expected_registry_dirty_changes_updates_baseline_and_omits_local_paths(self):
        self.init_from_remote()
        skill = self.select_default_skill("alpha")

        push(config_path=self.config_path, skill_names=["alpha"], message="sync alpha")

        self.assertEqual(read_file(self.work / "sync", "skills/alpha/SKILL.md"), "# alpha\n")
        self.assertTrue(run_git(self.work / "sync", ["status", "--porcelain"]) == "")
        registry_text = read_file(self.work / "sync", "registry.yaml")
        self.assertNotIn(str(skill), registry_text)
        self.assertEqual(
            load_config(self.config_path)["skills"]["alpha"]["last_installed_hash"],
            hash_skill_dir(skill),
        )

    def test_push_rejects_unrelated_dirty_sync_repo_changes(self):
        self.init_from_remote()
        self.select_default_skill("alpha")
        write_file(self.work / "sync", "unexpected.txt", "dirty\n")

        with self.assertRaisesRegex(SkillSyncError, "dirty|unexpected"):
            push(config_path=self.config_path, skill_names=["alpha"])

    def test_push_skill_filter_does_not_touch_unfiltered_selected_skills(self):
        self.init_from_remote()
        self.select_default_skill("alpha", "# alpha v1\n")
        self.select_default_skill("beta", "# beta v1\n")

        push(config_path=self.config_path, skill_names=["alpha"])

        self.assertTrue((self.work / "sync" / "skills" / "alpha" / "SKILL.md").exists())
        self.assertFalse((self.work / "sync" / "skills" / "beta").exists())
        config = load_config(self.config_path)
        self.assertIn("last_installed_hash", config["skills"]["alpha"])
        self.assertNotIn("last_installed_hash", config["skills"]["beta"])

    def test_pull_refuses_to_overwrite_changed_local_destination(self):
        self.init_from_remote()
        skill = self.select_default_skill("alpha", "# alpha v1\n")
        push(config_path=self.config_path, skill_names=["alpha"])
        write_file(skill, "SKILL.md", "# local changed\n")

        with self.assertRaisesRegex(SkillSyncError, "overwrite|local"):
            pull(config_path=self.config_path, skill_names=["alpha"])

    def test_pull_fresh_machine_installs_remote_skill_without_local_path(self):
        source, remote = create_remote_with_registry(self.work)
        add_remote_skill(source, "alpha", "# alpha remote\n")
        sync_repo = self.work / "sync"
        init_sync(str(remote), sync_dir=sync_repo, config_path=self.config_path)
        configure_identity(sync_repo)

        result = pull(config_path=self.config_path)

        destination = self.skill_root / "alpha"
        config = load_config(self.config_path)
        self.assertEqual(result["pulled"], ["alpha"])
        self.assertEqual(read_file(destination, "SKILL.md"), "# alpha remote\n")
        self.assertEqual(config["skills"]["alpha"]["local_path"], str(destination))
        self.assertEqual(
            config["skills"]["alpha"]["last_installed_hash"],
            hash_skill_dir(destination),
        )

    def test_pull_default_targets_use_post_merge_registry_for_new_remote_selection(self):
        source, remote = create_remote_with_registry(self.work)
        sync_repo = self.work / "sync"
        init_sync(str(remote), sync_dir=sync_repo, config_path=self.config_path)
        configure_identity(sync_repo)
        add_remote_skill(source, "new-remote", "# new remote\n")

        result = pull(config_path=self.config_path)

        self.assertEqual(result["pulled"], ["new-remote"])
        self.assertEqual(
            read_file(self.skill_root / "new-remote", "SKILL.md"),
            "# new remote\n",
        )

    def test_pull_remote_deselect_allows_reselecting_same_name_from_new_path(self):
        source, remote = create_remote_with_registry(self.work)
        add_remote_skill(source, "alpha", "# alpha remote\n")
        sync_repo = self.work / "sync"
        init_sync(str(remote), sync_dir=sync_repo, config_path=self.config_path)
        configure_identity(sync_repo)
        pull(config_path=self.config_path)

        run_git(source, ["pull", "--ff-only", "origin", "main"])
        registry = load_registry(source / "registry.yaml")
        registry["skills"].pop("alpha")
        save_registry(source / "registry.yaml", registry)
        run_git(source, ["add", "."])
        run_git(source, ["commit", "-m", "deselect alpha"])
        run_git(source, ["push", "origin", "HEAD:main"])

        pull(config_path=self.config_path)
        new_root = self.work / "new-codex-home" / "skills"
        new_skill = make_skill(new_root, "alpha", "# alpha new local\n")

        select_skills(
            [str(new_skill)],
            config_path=self.config_path,
            skill_dir=new_root,
            allow_external=True,
        )

        self.assertEqual(
            load_config(self.config_path)["skills"]["alpha"]["local_path"],
            str(new_skill.resolve()),
        )

    def test_sync_remote_changed_installs_newly_selected_remote_skill(self):
        source, remote = create_remote_with_registry(self.work)
        sync_repo = self.work / "sync"
        init_sync(str(remote), sync_dir=sync_repo, config_path=self.config_path)
        configure_identity(sync_repo)
        add_remote_skill(source, "new-remote", "# new remote\n")

        result = sync(config_path=self.config_path)

        self.assertEqual(result["pulled"], ["new-remote"])
        self.assertEqual(
            read_file(self.skill_root / "new-remote", "SKILL.md"),
            "# new remote\n",
        )

    def test_sync_fresh_machine_installs_existing_remote_selection_without_local_path(self):
        source, remote = create_remote_with_registry(self.work)
        add_remote_skill(source, "alpha", "# alpha remote\n")
        sync_repo = self.work / "sync"
        init_sync(str(remote), sync_dir=sync_repo, config_path=self.config_path)
        configure_identity(sync_repo)

        result = sync(config_path=self.config_path)

        destination = self.skill_root / "alpha"
        config = load_config(self.config_path)
        self.assertEqual(result["pulled"], ["alpha"])
        self.assertEqual(read_file(destination, "SKILL.md"), "# alpha remote\n")
        self.assertEqual(config["skills"]["alpha"]["local_path"], str(destination))
        self.assertEqual(
            config["skills"]["alpha"]["last_installed_hash"],
            hash_skill_dir(destination),
        )

    def test_pull_refuses_overwrite_when_baseline_missing_and_destination_differs_from_repo_copy(self):
        self.init_from_remote()
        skill = self.select_default_skill("alpha", "# alpha v1\n")
        push(config_path=self.config_path, skill_names=["alpha"])
        config = load_config(self.config_path)
        del config["skills"]["alpha"]["last_installed_hash"]
        save_config(self.config_path, config)
        write_file(skill, "SKILL.md", "# alpha local divergent\n")

        with self.assertRaisesRegex(SkillSyncError, "overwrite|local"):
            pull(config_path=self.config_path, skill_names=["alpha"])

    def test_pull_skill_filter_installs_only_filtered_remote_skill(self):
        self.init_from_remote()
        alpha = self.select_default_skill("alpha", "# alpha local\n")
        beta = self.select_default_skill("beta", "# beta local\n")
        push(config_path=self.config_path, skill_names=["alpha", "beta"])
        write_file(self.work / "sync", "skills/alpha/SKILL.md", "# alpha remote\n")
        write_file(self.work / "sync", "skills/beta/SKILL.md", "# beta remote\n")
        run_git(self.work / "sync", ["add", "."])
        run_git(self.work / "sync", ["commit", "-m", "remote updates"])

        pull(config_path=self.config_path, skill_names=["alpha"])

        self.assertEqual(read_file(alpha, "SKILL.md"), "# alpha remote\n")
        self.assertEqual(read_file(beta, "SKILL.md"), "# beta local\n")

    def test_sync_stops_when_remote_ahead_and_selected_local_changed(self):
        source, remote = create_remote_with_registry(self.work)
        sync_repo = self.work / "sync"
        init_sync(str(remote), sync_dir=sync_repo, config_path=self.config_path)
        configure_identity(sync_repo)
        skill = self.select_default_skill("alpha", "# alpha v1\n")
        push(config_path=self.config_path, skill_names=["alpha"])

        run_git(source, ["pull", "--ff-only", "origin", "main"])
        make_commit(source, "remote.txt", "remote\n", "remote change")
        run_git(source, ["push", "origin", "HEAD:main"])
        write_file(skill, "local.txt", "local changed\n")

        with self.assertRaisesRegex(SkillSyncError, "both|remote.*local|local.*remote"):
            sync(config_path=self.config_path, skill_names=["alpha"])

    def test_sync_explicit_unknown_skill_filter_raises(self):
        self.init_from_remote()

        with self.assertRaisesRegex(SkillSyncError, "not selected|typo"):
            sync(config_path=self.config_path, skill_names=["typo"])

    def test_sync_rejects_unrelated_dirty_sync_repo_changes_on_noop_path(self):
        self.init_from_remote()
        self.select_default_skill("alpha", "# alpha v1\n")
        push(config_path=self.config_path, skill_names=["alpha"])
        write_file(self.work / "sync", "unexpected.txt", "dirty\n")

        with self.assertRaisesRegex(SkillSyncError, "dirty|unexpected"):
            sync(config_path=self.config_path, skill_names=["alpha"])

    def test_sync_rejects_dirty_registry_even_though_push_allows_it(self):
        self.init_from_remote()
        self.select_default_skill("alpha", "# alpha v1\n")

        with self.assertRaisesRegex(SkillSyncError, "dirty|clean"):
            sync(config_path=self.config_path, skill_names=["alpha"])

        result = push(config_path=self.config_path, skill_names=["alpha"])
        self.assertEqual(result["pushed"], ["alpha"])

    def test_core_reports_git_fail_closed_divergence_and_missing_remote_branch(self):
        self.init_from_remote()
        self.select_default_skill("alpha")
        with mock.patch.object(core_module.git, "state", return_value=core_module.git.GitState(clean=True, ahead=1, behind=1, diverged=True)):
            with self.assertRaisesRegex(SkillSyncError, "diverged"):
                push(config_path=self.config_path, skill_names=["alpha"])

        push(config_path=self.config_path, skill_names=["alpha"])
        with mock.patch.object(core_module.git, "fetch", side_effect=core_module.git.GitError("missing remote branch origin/main")):
            with self.assertRaisesRegex(SkillSyncError, "missing remote branch"):
                pull(config_path=self.config_path, skill_names=["alpha"])

    def test_core_reports_unrelated_histories_force_push_divergence_and_push_rejection(self):
        self.init_from_remote()
        self.select_default_skill("alpha")
        push(config_path=self.config_path, skill_names=["alpha"])

        with mock.patch.object(core_module.git, "merge_ff_only", side_effect=core_module.git.GitError("unrelated histories")):
            with self.assertRaisesRegex(SkillSyncError, "unrelated histories"):
                pull(config_path=self.config_path, skill_names=["alpha"])

        with mock.patch.object(core_module.git, "push", side_effect=core_module.git.GitError("local and remote branches diverged")):
            with self.assertRaisesRegex(SkillSyncError, "diverged"):
                push(config_path=self.config_path, skill_names=["alpha"])

        with mock.patch.object(core_module.git, "push", side_effect=core_module.git.GitError("push rejected: non-fast-forward")):
            with self.assertRaisesRegex(SkillSyncError, "push rejected"):
                push(config_path=self.config_path, skill_names=["alpha"])


if __name__ == "__main__":
    unittest.main()
