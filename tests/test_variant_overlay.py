import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.variant_overlay import (
    VariantOverlayFile,
    VariantOverlayPlan,
    materialize_variant_overlay,
    plan_variant_overlay,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "variant-overlay"


def write(root: Path, relative_path: str, content: bytes) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def tree_contents(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class VariantOverlayGoldenFixtureTest(unittest.TestCase):
    def assert_fixture_resolution(
        self,
        expected_name: str,
        *variant_names: str,
    ) -> None:
        base = FIXTURE_ROOT / "base"
        variants = tuple(FIXTURE_ROOT / "variants" / name for name in variant_names)
        expected = FIXTURE_ROOT / "expected" / expected_name

        plan = plan_variant_overlay(base, variants)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "resolved"
            materialize_variant_overlay(plan, destination)
            self.assertEqual(tree_contents(destination), tree_contents(expected))
            self.assertFalse((destination / "variant.yaml").exists())

    def test_base_only(self):
        self.assert_fixture_resolution("base-only")

    def test_base_plus_family(self):
        self.assert_fixture_resolution("base-family", "kimi")

    def test_base_plus_exact_client(self):
        self.assert_fixture_resolution("base-client", "kimi-desktop")

    def test_base_plus_family_plus_client(self):
        self.assert_fixture_resolution(
            "base-family-client",
            "kimi",
            "kimi-desktop",
        )

    def test_exact_client_overrides_family_and_shared_script_stays_unchanged(self):
        plan = plan_variant_overlay(
            FIXTURE_ROOT / "base",
            (
                FIXTURE_ROOT / "variants" / "kimi",
                FIXTURE_ROOT / "variants" / "kimi-desktop",
            ),
        )

        entries = {entry.relative_path: entry for entry in plan.files}
        self.assertEqual(entries["SKILL.md"].content, b"# Kimi Desktop\n")
        self.assertEqual(entries["scripts/shared.sh"].content, b"printf 'shared\\n'\n")
        self.assertNotIn("references/remove.md", entries)
        self.assertNotIn("references/family.md", entries)


class VariantOverlaySafetyTest(unittest.TestCase):
    def make_base(self, work: Path) -> Path:
        base = work / "base"
        write(base, "SKILL.md", b"# Base\n")
        return base

    def make_variant(
        self,
        work: Path,
        target: str = "codex",
        *,
        delete: str | None = None,
    ) -> Path:
        variant = work / "variants" / target
        manifest = f"version: 1\ntarget: {target}\nmode: overlay\n"
        if delete is not None:
            manifest += f"delete: {delete}\n"
        write(variant, "variant.yaml", manifest.encode())
        return variant

    def test_plan_is_deterministic_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, "z.txt", b"z")
            write(base, "a.txt", b"base-a")
            variant = self.make_variant(work)
            write(variant, "a.txt", b"variant-a")
            write(variant, "m.txt", b"m")
            before = tree_contents(work)

            first = plan_variant_overlay(base, (variant,))
            second = plan_variant_overlay(base, (variant,))

            self.assertEqual(first, second)
            self.assertEqual(
                [entry.relative_path for entry in first.files],
                ["SKILL.md", "a.txt", "m.txt", "z.txt"],
            )
            self.assertEqual(tree_contents(work), before)

    def test_ignored_noise_is_not_resolved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, ".DS_Store", b"finder")
            write(base, ".git/config", b"git")
            write(base, "__pycache__/cache.pyc", b"cache")
            variant = self.make_variant(work)
            write(variant, "nested/.DS_Store", b"finder")

            plan = plan_variant_overlay(base, (variant,))

            self.assertEqual([entry.relative_path for entry in plan.files], ["SKILL.md"])

    def test_root_variant_manifest_is_excluded_from_base_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, "variant.yaml", b"authored but reserved")

            plan = plan_variant_overlay(base, ())

            self.assertEqual([entry.relative_path for entry in plan.files], ["SKILL.md"])

    def test_plan_fails_if_source_changes_between_validation_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            import skill_sync.variant_overlay as variant_overlay

            original_snapshot = variant_overlay._snapshot_source_tree
            calls = 0

            def mutate_after_first_snapshot(root):
                nonlocal calls
                snapshot = original_snapshot(root)
                calls += 1
                if calls == 1:
                    write(base, "late.md", b"late")
                return snapshot

            with mock.patch.object(
                variant_overlay,
                "_snapshot_source_tree",
                side_effect=mutate_after_first_snapshot,
            ):
                with self.assertRaisesRegex(ValueError, "changed while planning"):
                    plan_variant_overlay(base, ())

    def test_delete_removes_a_file_or_directory_subtree_before_layer_files_apply(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, "references/a.md", b"a")
            write(base, "references/nested/b.md", b"b")
            variant = self.make_variant(work, delete="references")
            write(variant, "references/replacement.md", b"replacement")

            plan = plan_variant_overlay(base, (variant,))

            self.assertEqual(
                [entry.relative_path for entry in plan.files],
                ["SKILL.md", "references/replacement.md"],
            )

    def test_rejects_missing_skill_markdown_after_overlay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            variant = self.make_variant(work, delete="SKILL.md")

            with self.assertRaisesRegex(ValueError, "SKILL.md"):
                plan_variant_overlay(base, (variant,))

    def test_rejects_case_insensitive_duplicates_and_file_directory_collisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, "Guide.md", b"guide")
            variant = self.make_variant(work)
            write(variant, "guide.md", b"different case")
            with self.assertRaisesRegex(ValueError, "case-insensitive"):
                plan_variant_overlay(base, (variant,))

        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, "references/a.md", b"a")
            variant = self.make_variant(work)
            write(variant, "references", b"file over directory")
            with self.assertRaisesRegex(ValueError, "file/directory collision"):
                plan_variant_overlay(base, (variant,))

    def test_rejects_nonportable_content_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            variant = self.make_variant(work)
            write(variant, "bad:name.md", b"bad")

            with self.assertRaisesRegex(ValueError, "portable"):
                plan_variant_overlay(base, (variant,))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unsupported")
    def test_rejects_links_in_base_and_variant_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            (base / "linked.md").symlink_to("SKILL.md")
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                plan_variant_overlay(base, ())

        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            variant = self.make_variant(work)
            (variant / "linked.md").symlink_to("variant.yaml")
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                plan_variant_overlay(base, (variant,))

    def test_rejects_roots_reported_as_reparse_points(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            with mock.patch(
                "skill_sync.variant_overlay.is_link_or_reparse",
                return_value=True,
            ):
                with self.assertRaisesRegex(ValueError, "link|reparse"):
                    plan_variant_overlay(base, ())

    def test_materialization_is_atomic_and_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            plan = plan_variant_overlay(self.make_base(work), ())
            destination = work / "resolved"
            write(destination, "winner.txt", b"winner")

            with self.assertRaises(FileExistsError):
                materialize_variant_overlay(plan, destination)

            self.assertEqual(tree_contents(destination), {"winner.txt": b"winner"})
            self.assertFalse(any(path.name.startswith(".resolved.tmp-") for path in work.iterdir()))

    def test_materialization_preserves_a_concurrent_destination_winner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            plan = plan_variant_overlay(self.make_base(work), ())
            destination = work / "resolved"
            import skill_sync.variant_overlay as variant_overlay

            original_rename = variant_overlay.rename_no_replace

            def install_winner_then_publish(source, target):
                if Path(target) == destination:
                    write(destination, "winner.txt", b"winner")
                return original_rename(source, target)

            with mock.patch.object(
                variant_overlay,
                "rename_no_replace",
                side_effect=install_winner_then_publish,
            ):
                with self.assertRaises(FileExistsError):
                    materialize_variant_overlay(plan, destination)

            self.assertEqual(tree_contents(destination), {"winner.txt": b"winner"})
            self.assertFalse(any(path.name.startswith(".resolved.tmp-") for path in work.iterdir()))

    def test_materialization_failure_leaves_no_partial_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            write(base, "references/a.md", b"a")
            plan = plan_variant_overlay(base, ())
            destination = work / "resolved"

            with mock.patch(
                "skill_sync.variant_overlay._write_planned_file",
                side_effect=OSError("forced write failure"),
            ):
                with self.assertRaisesRegex(OSError, "forced write failure"):
                    materialize_variant_overlay(plan, destination)

            self.assertFalse(destination.exists())
            self.assertFalse(any(path.name.startswith(".resolved.tmp-") for path in work.iterdir()))

    @unittest.skipIf(os.name == "nt", "POSIX executable bits are unavailable")
    def test_materialization_rejects_staged_mode_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            plan = plan_variant_overlay(self.make_base(work), ())
            destination = work / "resolved"
            import skill_sync.variant_overlay as variant_overlay

            original_write = variant_overlay._write_planned_file

            def change_mode_after_write(root, entry):
                original_write(root, entry)
                root.joinpath(*entry.relative_path.split("/")).chmod(0o777)

            with mock.patch.object(
                variant_overlay,
                "_write_planned_file",
                side_effect=change_mode_after_write,
            ):
                with self.assertRaisesRegex(ValueError, "immutable resolution plan"):
                    materialize_variant_overlay(plan, destination)

            self.assertFalse(destination.exists())
            self.assertFalse(any(path.name.startswith(".resolved.tmp-") for path in work.iterdir()))

    def test_materialization_revalidates_public_plan_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work).resolve()
            destination = work / "output" / "resolved"
            escaped = work / "escaped.txt"
            malicious = VariantOverlayPlan(
                base_root=base,
                variant_roots=(),
                files=(
                    VariantOverlayFile("../../escaped.txt", b"escaped", 0o644),
                    VariantOverlayFile("SKILL.md", b"# Skill\n", 0o644),
                ),
            )

            with self.assertRaises(ValueError):
                materialize_variant_overlay(malicious, destination)

            self.assertFalse(destination.parent.exists())
            self.assertFalse(escaped.exists())

    def test_materialization_rejects_invalid_public_plan_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work).resolve()
            valid_skill = VariantOverlayFile("SKILL.md", b"# Skill\n", 0o644)
            cases = {
                "mutable files": VariantOverlayPlan(base, (), [valid_skill]),
                "manifest output": VariantOverlayPlan(
                    base,
                    (),
                    (
                        valid_skill,
                        VariantOverlayFile("variant.yaml", b"reserved", 0o644),
                    ),
                ),
                "invalid mode": VariantOverlayPlan(
                    base,
                    (),
                    (VariantOverlayFile("SKILL.md", b"# Skill\n", 0o1000),),
                ),
                "non-portable mode": VariantOverlayPlan(
                    base,
                    (),
                    (VariantOverlayFile("SKILL.md", b"# Skill\n", 0o751),),
                ),
                "collision": VariantOverlayPlan(
                    base,
                    (),
                    (
                        valid_skill,
                        VariantOverlayFile("references", b"file", 0o644),
                        VariantOverlayFile("references/a.md", b"nested", 0o644),
                    ),
                ),
            }

            for name, plan in cases.items():
                with self.subTest(name=name):
                    destination = work / name.replace(" ", "-")
                    with self.assertRaises(ValueError):
                        materialize_variant_overlay(plan, destination)
                    self.assertFalse(destination.exists())

    def test_materialization_uses_snapshot_even_if_sources_change_after_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            plan = plan_variant_overlay(base, ())
            write(base, "SKILL.md", b"# Changed later\n")
            destination = work / "resolved"

            materialize_variant_overlay(plan, destination)

            self.assertEqual((destination / "SKILL.md").read_bytes(), b"# Base\n")

    def test_materialization_rejects_destination_inside_any_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            variant = self.make_variant(work)
            plan = plan_variant_overlay(base, (variant,))

            for destination in (base / "resolved", variant / "resolved"):
                with self.subTest(destination=destination):
                    with self.assertRaisesRegex(ValueError, "inside a source"):
                        materialize_variant_overlay(plan, destination)
                    self.assertFalse(destination.exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable bits are unavailable")
    def test_materialization_derives_executable_mode_from_shebang_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            script = write(base, "scripts/run.sh", b"#!/bin/sh\n")
            script.chmod(0o751)
            plan = plan_variant_overlay(base, ())
            destination = work / "resolved"

            materialize_variant_overlay(plan, destination)

            self.assertEqual((destination / "scripts/run.sh").stat().st_mode & 0o777, 0o755)

    def test_windows_source_and_materialization_modes_use_content_semantics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            base = self.make_base(work)
            script = write(base, "scripts/run.sh", b"#!/bin/sh\n")
            script.chmod(0o644)
            plan = plan_variant_overlay(base, ())
            planned_script = next(
                entry
                for entry in plan.files
                if entry.relative_path == "scripts/run.sh"
            )
            destination = work / "resolved"
            import skill_sync.variant_overlay as variant_overlay

            original_mode = variant_overlay._materialized_file_mode
            with mock.patch.object(
                variant_overlay,
                "_materialized_file_mode",
                side_effect=lambda st_mode, content: original_mode(
                    st_mode,
                    content,
                    platform="nt",
                ),
            ):
                materialize_variant_overlay(plan, destination)

            self.assertEqual(planned_script.mode, 0o755)
            self.assertEqual((destination / "scripts/run.sh").read_bytes(), b"#!/bin/sh\n")


if __name__ == "__main__":
    unittest.main()
