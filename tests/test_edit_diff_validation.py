import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync import cli
from skill_sync.config import empty_config, save_config
from skill_sync.core import edit_begin, edit_diff, edit_validate
from skill_sync.edit_session import EditSessionStatus, EditSessionStore
from skill_sync.edit_validation import validate_relative_path
from skill_sync.errors import SkillSyncError
from skill_sync.registry import save_registry


def run_cli(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class EditDiffValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.skill = self.skills_root / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Test Skill\n---\n\n# Alpha\n",
            encoding="utf-8",
        )
        (self.skill / ".hidden").write_text("old hidden\n", encoding="utf-8")
        (self.skill / "asset.bin").write_bytes(b"\x00BINARY_OLD\xff")

        config = empty_config()
        config["sync_repo_path"] = str(self.repo)
        config["skills_root"] = str(self.skills_root)
        config["data_root"] = str(self.root / "data")
        config["skills"] = {"alpha": {"local_path": str(self.skill)}}
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    "alpha": {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": "alpha",
                    }
                },
            },
        )
        self.session = edit_begin("alpha", actor="codex", config_path=self.config_path)
        self.session_id = self.session["session_id"]
        self.baseline = Path(self.session["baseline_path"])
        self.workspace = Path(self.session["workspace_path"])

    def test_empty_diff_and_valid_workspace_have_stable_text_and_json(self):
        diff = edit_diff(self.session_id, config_path=self.config_path)
        self.assertEqual(
            diff,
            {
                "session_id": self.session_id,
                "skill": "alpha",
                "scope": "base",
                "status": "active",
                "changed": False,
                "summary": {"added": 0, "modified": 0, "deleted": 0, "total": 0},
                "files": [],
            },
        )

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "diff", self.session_id]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            f"Edit diff: {self.session_id} (alpha, Base)\nNo changes.\n",
        )

        validation = edit_validate(self.session_id, config_path=self.config_path)
        self.assertTrue(validation["valid"])
        self.assertFalse(validation["changed"])
        self.assertEqual(validation["issues"], [])
        self.assertEqual(validation["workspace_hash"], validation["baseline_hash"])

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "edit",
                "validate",
                self.session_id,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "edit validate")
        self.assertEqual(envelope["result"], validation)

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "validate", self.session_id]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "\n".join(
                (
                    f"Validation: valid ({self.session_id}, alpha, Base)",
                    f"Workspace: {validation['workspace_hash']}",
                    "Changes: no",
                    "Issues: 0",
                    "",
                )
            ),
        )

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "edit",
                "diff",
                self.session_id,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        envelope = json.loads(stdout)
        self.assertEqual(envelope["command"], "edit diff")
        self.assertEqual(envelope["result"], diff)

    def test_diff_classifies_text_hidden_binary_added_and_deleted_files(self):
        (self.workspace / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Updated Skill\n---\n\n# Alpha\n",
            encoding="utf-8",
        )
        (self.workspace / ".hidden").unlink()
        (self.workspace / "asset.bin").write_bytes(b"\x00BINARY_SECRET\xfe")
        (self.workspace / "notes.txt").write_text("new note\n", encoding="utf-8")

        result = edit_diff(self.session_id, config_path=self.config_path)

        self.assertEqual(
            result["summary"],
            {"added": 1, "modified": 2, "deleted": 1, "total": 4},
        )
        files = {item["path"]: item for item in result["files"]}
        self.assertEqual(files[".hidden"]["change"], "deleted")
        self.assertEqual(files[".hidden"]["kind"], "text")
        self.assertEqual(files["notes.txt"]["change"], "added")
        self.assertIn("+new note", files["notes.txt"]["diff"])
        self.assertEqual(files["SKILL.md"]["change"], "modified")
        self.assertIn("-description: Test Skill", files["SKILL.md"]["diff"])
        binary = files["asset.bin"]
        self.assertEqual(binary["kind"], "binary")
        self.assertNotIn("diff", binary)
        self.assertRegex(binary["old_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(binary["new_hash"], r"^sha256:[0-9a-f]{64}$")

        code, stdout, stderr = run_cli(
            ["--config", str(self.config_path), "edit", "diff", self.session_id]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("- modified binary: asset.bin", stdout)
        self.assertNotIn("BINARY_SECRET", stdout)
        self.assertNotIn("BINARY_OLD", stdout)

    def test_validate_reports_frontmatter_and_skill_file_errors(self):
        (self.workspace / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
        result = edit_validate(self.session_id, config_path=self.config_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["issues"][0]["code"], "invalid_frontmatter")

        (self.workspace / "SKILL.md").write_text(
            "---\nname: beta\nname: alpha\ndescription:\n---\n",
            encoding="utf-8",
        )
        result = edit_validate(self.session_id, config_path=self.config_path)
        codes = [issue["code"] for issue in result["issues"]]
        self.assertIn("invalid_frontmatter", codes)
        self.assertIn("skill_name_mismatch", codes)

        (self.workspace / "SKILL.md").unlink()
        result = edit_validate(self.session_id, config_path=self.config_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["issues"][0]["code"], "missing_skill_file")

    def test_symlink_is_reported_by_validate_and_blocks_diff(self):
        target = self.root / "outside.txt"
        target.write_text("outside secret\n", encoding="utf-8")
        (self.workspace / "linked.txt").symlink_to(target)

        result = edit_validate(self.session_id, config_path=self.config_path)
        self.assertFalse(result["valid"])
        self.assertIn("linked_path", [issue["code"] for issue in result["issues"]])

        with self.assertRaises(SkillSyncError) as raised:
            edit_diff(self.session_id, config_path=self.config_path)
        self.assertEqual(raised.exception.code, "invalid_edit_workspace")
        self.assertEqual(raised.exception.exit_code, 4)

    @unittest.skipIf(os.name == "nt", "portable-invalid filename is POSIX-only")
    def test_invalid_portable_path_is_reported_and_blocks_diff(self):
        (self.workspace / "bad\\name.txt").write_text("unsafe\n", encoding="utf-8")

        result = edit_validate(self.session_id, config_path=self.config_path)
        self.assertIn("invalid_path", [issue["code"] for issue in result["issues"]])
        with self.assertRaises(SkillSyncError) as raised:
            edit_diff(self.session_id, config_path=self.config_path)
        self.assertEqual(raised.exception.code, "invalid_edit_workspace")

    def test_traversal_and_absolute_paths_are_rejected_by_path_contract(self):
        for path in (
            "../outside",
            "nested/../outside",
            "/absolute",
            "C:\\outside",
            "CON",
            "folder/trailing.",
        ):
            with self.subTest(path=path):
                issue = validate_relative_path(path)
                self.assertIsNotNone(issue)
                self.assertEqual(issue.code, "invalid_path")

    @unittest.skipIf(os.name == "nt", "FIFO fixture is POSIX-only")
    def test_non_regular_path_is_reported_without_reading_it(self):
        os.mkfifo(self.workspace / "pipe")

        result = edit_validate(self.session_id, config_path=self.config_path)
        self.assertIn("non_regular_path", [issue["code"] for issue in result["issues"]])
        with self.assertRaises(SkillSyncError) as raised:
            edit_diff(self.session_id, config_path=self.config_path)
        self.assertEqual(raised.exception.exit_code, 4)

    def test_missing_or_damaged_session_components_fail_closed(self):
        metadata_path = Path(self.session["workspace_path"]).parent / "session.json"
        original_metadata = metadata_path.read_bytes()
        metadata_path.write_bytes(b"{broken")
        with self.assertRaises(SkillSyncError) as raised:
            edit_validate(self.session_id, config_path=self.config_path)
        self.assertEqual(raised.exception.code, "invalid_edit_session_metadata")
        self.assertEqual(raised.exception.exit_code, 4)

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "edit",
                "diff",
                self.session_id,
                "--json",
            ]
        )
        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        envelope = json.loads(stderr)
        self.assertEqual(envelope["command"], "edit diff")
        self.assertEqual(
            envelope["errors"][0]["code"], "invalid_edit_session_metadata"
        )
        metadata_path.write_bytes(original_metadata)

        (self.workspace / "SKILL.md").write_text("changed\n", encoding="utf-8")
        for missing in (self.workspace, self.baseline):
            temporary = missing.with_name(missing.name + "-missing")
            missing.rename(temporary)
            try:
                with self.assertRaises(SkillSyncError) as raised:
                    edit_diff(self.session_id, config_path=self.config_path)
                self.assertEqual(raised.exception.code, "edit_session_incomplete")
                self.assertEqual(raised.exception.exit_code, 4)
            finally:
                temporary.rename(missing)

        (self.baseline / "SKILL.md").chmod(0o600)
        (self.baseline / "SKILL.md").write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(SkillSyncError) as raised:
            edit_validate(self.session_id, config_path=self.config_path)
        self.assertEqual(raised.exception.code, "unsafe_edit_baseline")
        self.assertEqual(raised.exception.exit_code, 4)

    def test_unknown_session_uses_shared_not_found_contract(self):
        unknown = "12345678-1234-4234-9234-123456789abc"
        for action in ("diff", "validate"):
            code, stdout, stderr = run_cli(
                [
                    "--config",
                    str(self.config_path),
                    "edit",
                    action,
                    unknown,
                    "--json",
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            envelope = json.loads(stderr)
            self.assertEqual(envelope["command"], "edit " + action)
            self.assertEqual(envelope["errors"][0]["code"], "edit_session_not_found")

    def test_terminal_and_in_flight_sessions_are_not_diffable_or_validatable(self):
        store = EditSessionStore(self.root / "data")
        store.transition(self.session_id, EditSessionStatus.ABORTED)
        for operation in (edit_diff, edit_validate):
            with self.assertRaises(SkillSyncError) as raised:
                operation(self.session_id, config_path=self.config_path)
            self.assertEqual(raised.exception.code, "edit_session_not_active")
            self.assertEqual(raised.exception.exit_code, 4)

        second = edit_begin("alpha", config_path=self.config_path)
        store.transition(second["session_id"], EditSessionStatus.APPLYING)
        for operation in (edit_diff, edit_validate):
            with self.assertRaises(SkillSyncError) as raised:
                operation(second["session_id"], config_path=self.config_path)
            self.assertEqual(raised.exception.code, "edit_session_not_active")

    def test_diff_and_validate_are_read_only_and_do_not_invoke_git(self):
        before = self._tree_snapshot(Path(self.session["baseline_path"]).parent)
        canonical_before = self._tree_snapshot(self.skill)
        with mock.patch.object(
            cli.core.git,
            "run_git",
            side_effect=AssertionError("inspection must remain local-only"),
        ):
            edit_diff(self.session_id, config_path=self.config_path)
            edit_validate(self.session_id, config_path=self.config_path)
        self.assertEqual(self._tree_snapshot(Path(self.session["baseline_path"]).parent), before)
        self.assertEqual(self._tree_snapshot(self.skill), canonical_before)

    @staticmethod
    def _tree_snapshot(root: Path):
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes(),
                path.stat().st_mode,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }


if __name__ == "__main__":
    unittest.main()
