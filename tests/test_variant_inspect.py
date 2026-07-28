import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync import cli, variant_overlay, variant_source
from skill_sync.config import empty_config, save_config
from skill_sync.errors import SkillSyncError

try:
    from skill_sync import variant_inspect
except ImportError:  # pragma: no cover - initial TDD red state
    variant_inspect = None


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class VariantInspectTest(unittest.TestCase):
    def setUp(self):
        if variant_inspect is None:
            self.fail("skill_sync.variant_inspect module is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skills_root = self.root / "portable" / "skills"
        self.variants_root = self.root / "portable" / "variants"
        self.config_path = self.root / "config.json"
        config = empty_config()
        config["skills_root"] = str(self.skills_root)
        save_config(self.config_path, config)
        self.base = self.skills_root / "alpha"
        self.base.mkdir(parents=True)
        (self.base / "SKILL.md").write_text("base\n", encoding="utf-8")
        (self.base / "shared.txt").write_text("shared\n", encoding="utf-8")

    def make_variant(
        self,
        target: str,
        *,
        files: dict[str, bytes | str] | None = None,
        deleted: tuple[str, ...] = (),
    ) -> Path:
        root = self.variants_root / "alpha" / target
        root.mkdir(parents=True)
        manifest = f"version: 1\ntarget: {target}\nmode: overlay\n"
        if len(deleted) == 1:
            manifest += f"delete: {deleted[0]}\n"
        elif deleted:
            manifest += "delete:\n" + "".join(f"  {path}: true\n" for path in deleted)
        (root / "variant.yaml").write_text(manifest, encoding="utf-8")
        for relative, content in (files or {}).items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                destination.write_bytes(content)
            else:
                destination.write_text(content, encoding="utf-8")
        return root

    def snapshot(self) -> dict[str, tuple[str, int]]:
        result: dict[str, tuple[str, int]] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_file():
                result[relative] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mode,
                )
            elif path.is_dir():
                result[relative] = ("directory", path.stat().st_mode)
            else:
                result[relative] = ("other", path.lstat().st_mode)
        return result

    def test_resolve_reports_kimi_family_then_exact_client_priority(self):
        self.make_variant(
            "kimi",
            files={"SKILL.md": "family\n", "family.txt": "family\n"},
            deleted=("shared.txt",),
        )
        self.make_variant(
            "kimi-code",
            files={"SKILL.md": "code\n", "client.txt": "client\n"},
        )

        result = variant_inspect.resolve_variant_dry_run(
            "alpha", client="kimi-code", config_path=self.config_path
        )

        self.assertEqual(result["skill"], "alpha")
        self.assertEqual(result["client"], "kimi-code")
        self.assertEqual(result["family"], "kimi")
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(
            [(item["role"], item["target"]) for item in result["layers"]],
            [("base", None), ("family", "kimi"), ("client", "kimi-code")],
        )
        files = {item["path"]: item for item in result["files"]}
        self.assertEqual(list(files), ["SKILL.md", "client.txt", "family.txt"])
        self.assertEqual(files["SKILL.md"]["source_role"], "client")
        self.assertEqual(files["SKILL.md"]["source_target"], "kimi-code")
        self.assertRegex(result["output_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result["resolution_hash"], r"^sha256:[0-9a-f]{64}$")

    def test_resolve_base_only_is_deterministic_and_uses_one_layer(self):
        first = variant_inspect.resolve_variant_dry_run(
            "alpha", client="codex", config_path=self.config_path
        )
        second = variant_inspect.resolve_variant_dry_run(
            "alpha", client="codex", config_path=self.config_path
        )

        self.assertEqual(first, second)
        self.assertEqual(first["family"], "codex")
        self.assertEqual(first["applied_variant_targets"], [])
        self.assertEqual(len(first["layers"]), 1)
        self.assertEqual(
            [item["path"] for item in first["files"]],
            ["SKILL.md", "shared.txt"],
        )

    def test_missing_and_invalid_sources_fail_closed_with_structured_errors(self):
        with self.assertRaises(SkillSyncError) as missing:
            variant_inspect.resolve_variant_dry_run(
                "missing", client="codex", config_path=self.config_path
            )
        self.assertEqual(missing.exception.code, "variant_base_missing")

        target = self.make_variant("kimi")
        (target / "variant.yaml").write_text(
            "version: 1\ntarget: kimi\nmode: replace\n", encoding="utf-8"
        )
        before = self.snapshot()
        with self.assertRaises(SkillSyncError) as invalid:
            variant_inspect.resolve_variant_dry_run(
                "alpha", client="kimi-code", config_path=self.config_path
            )
        self.assertEqual(invalid.exception.code, "variant_resolution_invalid")
        self.assertIn("overlay", str(invalid.exception))
        self.assertEqual(self.snapshot(), before)

        with self.assertRaises(SkillSyncError) as family_only:
            variant_inspect.resolve_variant_dry_run(
                "alpha", client="kimi", config_path=self.config_path
            )
        self.assertEqual(family_only.exception.code, "variant_client_unknown")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_invalid_base_path_is_rejected_before_resolution(self):
        external = self.root / "external.txt"
        external.write_text("outside\n", encoding="utf-8")
        linked = self.base / "linked.txt"
        try:
            linked.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        with self.assertRaises(SkillSyncError) as unsafe:
            variant_inspect.diff_base_to_client(
                "alpha", client="codex", config_path=self.config_path
            )

        self.assertEqual(unsafe.exception.code, "variant_base_unsafe")
        self.assertEqual(unsafe.exception.exit_code, 4)

    def test_diff_uses_one_immutable_plan_without_rereading_live_base(self):
        self.make_variant("kimi", files={"SKILL.md": "family\n"})
        original_resolver = variant_inspect.resolve_variant_for_client

        def resolve_then_change(*args, **kwargs):
            resolution = original_resolver(*args, **kwargs)
            (self.base / "SKILL.md").write_text(
                "changed live after snapshot\n", encoding="utf-8"
            )
            return resolution

        with mock.patch.object(
            variant_inspect,
            "resolve_variant_for_client",
            side_effect=resolve_then_change,
        ):
            result = variant_inspect.diff_base_to_client(
                "alpha", client="kimi-code", config_path=self.config_path
            )

        skill_diff = next(
            item for item in result["files"] if item["path"] == "SKILL.md"
        )
        self.assertIn("-base", skill_diff["diff"])
        self.assertNotIn("changed live after snapshot", skill_diff["diff"])

    def test_diff_is_ordered_and_keeps_binary_and_large_files_metadata_only(self):
        binary = b"\x00\xffprivate-binary\n"
        large = "x" * (variant_inspect.MAX_TEXT_DIFF_INPUT_BYTES + 1)
        (self.base / "binary.bin").write_bytes(b"\x00old")
        (self.base / "deleted.txt").write_text("delete me\n", encoding="utf-8")
        (self.base / "large.txt").write_text("old\n", encoding="utf-8")
        self.make_variant(
            "claude-code",
            files={
                "binary.bin": binary,
                "added.txt": "added\n",
                "large.txt": large,
            },
            deleted=("deleted.txt",),
        )

        result = variant_inspect.diff_base_to_client(
            "alpha", client="claude-code", config_path=self.config_path
        )

        self.assertEqual(result["comparison"], "base-to-client")
        self.assertEqual(
            result["summary"],
            {"added": 1, "modified": 2, "deleted": 1, "total": 4},
        )
        self.assertEqual(
            [item["path"] for item in result["files"]],
            ["added.txt", "binary.bin", "deleted.txt", "large.txt"],
        )
        files = {item["path"]: item for item in result["files"]}
        self.assertEqual(files["binary.bin"]["kind"], "binary")
        self.assertNotIn("diff", files["binary.bin"])
        self.assertEqual(files["binary.bin"]["client"]["size"], len(binary))
        self.assertRegex(
            files["binary.bin"]["client"]["hash"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertEqual(files["large.txt"]["kind"], "large")
        self.assertEqual(files["large.txt"]["diff_omitted"], "size_limit")
        self.assertNotIn("diff", files["large.txt"])
        self.assertIn("+++ b/added.txt", files["added.txt"]["diff"])
        self.assertIn("--- a/deleted.txt", files["deleted.txt"]["diff"])

    def test_diff_applies_one_deterministic_aggregate_text_input_budget(self):
        payload = "x" * (60 * 1024)
        self.make_variant(
            "claude-code",
            files={
                "00-binary.bin": b"\x00" + b"x" * (60 * 1024 - 1),
                **{f"file-{index}.txt": payload for index in range(6)},
            },
        )

        result = variant_inspect.diff_base_to_client(
            "alpha", client="claude-code", config_path=self.config_path
        )

        files = {
            item["path"]: item
            for item in result["files"]
            if item["path"].startswith("file-")
        }
        self.assertEqual(list(files), [f"file-{index}.txt" for index in range(6)])
        self.assertTrue(all("diff" in files[f"file-{index}.txt"] for index in range(4)))
        binary = next(item for item in result["files"] if item["path"] == "00-binary.bin")
        self.assertEqual(binary["kind"], "binary")
        self.assertNotIn("diff", binary)
        for index in (4, 5):
            item = files[f"file-{index}.txt"]
            self.assertNotIn("diff", item)
            self.assertEqual(item["kind"], "text")
            self.assertEqual(item["diff_omitted"], "total_size_limit")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_configured_skills_root_replaced_by_symlink_during_resolution(self):
        external_skills = self.root / "external-skills"
        external_base = external_skills / "alpha"
        external_base.mkdir(parents=True)
        (external_base / "SKILL.md").write_text("external secret\n", encoding="utf-8")
        original_resolver = variant_inspect.resolve_variant_for_client

        def replace_then_resolve(*args, **kwargs):
            self.skills_root.rename(self.root / "saved-skills")
            self.skills_root.symlink_to(external_skills, target_is_directory=True)
            return original_resolver(*args, **kwargs)

        with mock.patch.object(
            variant_inspect,
            "resolve_variant_for_client",
            side_effect=replace_then_resolve,
        ):
            with self.assertRaises(SkillSyncError) as changed:
                variant_inspect.diff_base_to_client(
                    "alpha", client="codex", config_path=self.config_path
                )

        self.assertEqual(changed.exception.code, "variant_source_changed")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_real_variants_root_replaced_by_symlink_during_resolution(self):
        self.make_variant("kimi", files={"SKILL.md": "local family\n"})
        external_root = self.root / "external-variants"
        external_target = external_root / "alpha" / "kimi"
        external_target.mkdir(parents=True)
        (external_target / "variant.yaml").write_text(
            "version: 1\ntarget: kimi\nmode: overlay\n", encoding="utf-8"
        )
        (external_target / "SKILL.md").write_text(
            "external secret\n", encoding="utf-8"
        )
        original_resolver = variant_inspect.resolve_variant_for_client

        def replace_then_resolve(*args, **kwargs):
            self.variants_root.rename(self.root / "saved-variants")
            self.variants_root.symlink_to(external_root, target_is_directory=True)
            return original_resolver(*args, **kwargs)

        with mock.patch.object(
            variant_inspect,
            "resolve_variant_for_client",
            side_effect=replace_then_resolve,
        ):
            with self.assertRaises(SkillSyncError) as changed:
                variant_inspect.diff_base_to_client(
                    "alpha", client="kimi-code", config_path=self.config_path
                )

        self.assertEqual(changed.exception.code, "variant_source_changed")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_missing_variants_root_becoming_symlink_during_resolution(self):
        external_root = self.root / "external-variants"
        external_target = external_root / "alpha" / "kimi"
        external_target.mkdir(parents=True)
        (external_target / "variant.yaml").write_text(
            "version: 1\ntarget: kimi\nmode: overlay\n", encoding="utf-8"
        )
        (external_target / "SKILL.md").write_text(
            "external secret\n", encoding="utf-8"
        )
        original_resolver = variant_inspect.resolve_variant_for_client

        def replace_then_resolve(*args, **kwargs):
            self.variants_root.symlink_to(external_root, target_is_directory=True)
            return original_resolver(*args, **kwargs)

        with mock.patch.object(
            variant_inspect,
            "resolve_variant_for_client",
            side_effect=replace_then_resolve,
        ):
            with self.assertRaises(SkillSyncError) as changed:
                variant_inspect.resolve_variant_dry_run(
                    "alpha", client="kimi-code", config_path=self.config_path
                )

        self.assertEqual(changed.exception.code, "variant_source_changed")

    def test_rejects_real_skills_root_replacement_during_initial_validation(self):
        external_skills = self.root / "external-skills"
        external_base = external_skills / "alpha"
        external_base.mkdir(parents=True)
        (external_base / "SKILL.md").write_text(
            "external real directory secret\n", encoding="utf-8"
        )
        original_resolve_base = variant_source._resolve_base_skill

        def validate_then_replace(*args, **kwargs):
            resolved = original_resolve_base(*args, **kwargs)
            self.skills_root.rename(self.root / "saved-skills")
            external_skills.rename(self.skills_root)
            return resolved

        with mock.patch.object(
            variant_source,
            "_resolve_base_skill",
            side_effect=validate_then_replace,
        ):
            with self.assertRaises(SkillSyncError) as changed:
                variant_inspect.diff_base_to_client(
                    "alpha", client="codex", config_path=self.config_path
                )

        self.assertEqual(changed.exception.code, "variant_source_changed")

    def test_rejects_real_variants_root_replacement_during_initial_validation(self):
        self.make_variant("kimi", files={"SKILL.md": "local family\n"})
        external_root = self.root / "external-variants"
        external_target = external_root / "alpha" / "kimi"
        external_target.mkdir(parents=True)
        (external_target / "variant.yaml").write_text(
            "version: 1\ntarget: kimi\nmode: overlay\n", encoding="utf-8"
        )
        (external_target / "SKILL.md").write_text(
            "external real directory secret\n", encoding="utf-8"
        )
        original_resolve_base = variant_source._resolve_base_skill

        def validate_then_replace(*args, **kwargs):
            resolved = original_resolve_base(*args, **kwargs)
            self.variants_root.rename(self.root / "saved-variants")
            external_root.rename(self.variants_root)
            return resolved

        with mock.patch.object(
            variant_source,
            "_resolve_base_skill",
            side_effect=validate_then_replace,
        ):
            with self.assertRaises(SkillSyncError) as changed:
                variant_inspect.diff_base_to_client(
                    "alpha", client="kimi-code", config_path=self.config_path
                )

        self.assertEqual(changed.exception.code, "variant_source_changed")

    def test_resolve_and_diff_are_read_only_and_never_materialize_or_run_git(self):
        self.make_variant("kimi", files={"SKILL.md": "family\n"})
        before = self.snapshot()

        with (
            mock.patch.object(
                variant_overlay, "materialize_variant_overlay"
            ) as materialize,
            mock.patch.object(subprocess, "run") as run,
            mock.patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")),
            mock.patch.object(
                Path, "write_bytes", side_effect=AssertionError("unexpected write")
            ),
            mock.patch.object(
                Path, "write_text", side_effect=AssertionError("unexpected write")
            ),
            mock.patch.object(os, "replace", side_effect=AssertionError("unexpected replace")),
        ):
            variant_inspect.resolve_variant_dry_run(
                "alpha", client="kimi-code", config_path=self.config_path
            )
            variant_inspect.diff_base_to_client(
                "alpha", client="kimi-code", config_path=self.config_path
            )

        materialize.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_cli_uses_v1_json_envelopes_and_concise_text(self):
        self.make_variant("kimi", files={"SKILL.md": "family\n"})

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "resolve",
                "alpha",
                "--client",
                "kimi-code",
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        envelope = json.loads(stdout)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["command"], "resolve")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"]["applied_variant_targets"], ["kimi"])

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "diff",
                "alpha",
                "--base",
                "--client",
                "kimi-code",
                "--json",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        envelope = json.loads(stdout)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["command"], "diff")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"]["comparison"], "base-to-client")

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "diff",
                "alpha",
                "--base",
                "--client",
                "kimi-code",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Base -> kimi-code", stdout)
        self.assertIn("modified", stdout)

    def test_cli_requires_explicit_dry_run_and_json_error_is_stable(self):
        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "resolve",
                "alpha",
                "--client",
                "codex",
                "--json",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        envelope = json.loads(stderr)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["command"], "resolve")
        self.assertEqual(envelope["errors"][0]["code"], "variant_dry_run_required")

    def test_cli_reports_aggregate_omission_metadata_without_payload(self):
        result = {
            "skill": "alpha",
            "comparison": "base-to-client",
            "client": "claude-code",
            "family": "claude",
            "resolver_version": "variant-overlay-v2",
            "base_hash": "sha256:" + "a" * 64,
            "output_hash": "sha256:" + "b" * 64,
            "resolution_hash": "sha256:" + "c" * 64,
            "applied_variant_targets": ["claude-code"],
            "changed": True,
            "unchanged_file_count": 0,
            "summary": {"added": 1, "modified": 0, "deleted": 0, "total": 1},
            "files": [
                {
                    "path": "later.txt",
                    "change": "added",
                    "base": None,
                    "client": {
                        "size": 1024,
                        "hash": "sha256:" + "d" * 64,
                        "mode": "0644",
                    },
                    "kind": "text",
                    "diff_omitted": "total_size_limit",
                }
            ],
        }
        with mock.patch.object(
            variant_inspect,
            "diff_base_to_client",
            return_value=result,
        ):
            code, stdout, stderr = run_cli(
                ["diff", "alpha", "--base", "--client", "claude-code"]
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("metadata: none -> sha256:", stdout)
        self.assertIn("diff omitted: total_size_limit", stdout)
        self.assertNotIn("+++ b/later.txt", stdout)

    @unittest.skipIf(os.name == "nt", "Windows cannot create control-character paths")
    def test_cli_rejects_control_character_paths_without_raw_terminal_escape(self):
        (self.base / "escape\x1bname.md").write_text("unsafe\n", encoding="utf-8")

        code, stdout, stderr = run_cli(
            [
                "--config",
                str(self.config_path),
                "resolve",
                "alpha",
                "--client",
                "codex",
                "--dry-run",
            ]
        )

        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        self.assertNotIn("\x1b", stderr)
        self.assertIn("control characters", stderr)


if __name__ == "__main__":
    unittest.main()
