import dataclasses
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_sync.variant_overlay as variant_overlay_module
import skill_sync.variant_resolution as variant_resolution_module
from skill_sync.hash import hash_portable_skill_dir, hash_skill_files_with_modes
from skill_sync.variant_overlay import materialize_variant_overlay
from skill_sync.variant_resolution import (
    VARIANT_RESOLVER_VERSION,
    LayeredVariantResolution,
    resolve_variant_for_client,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "variant-overlay"


class VariantResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.base = self.root / "skills" / "demo"
        self.variants = self.root / "variants" / "demo"
        shutil.copytree(FIXTURE_ROOT / "base", self.base)
        shutil.copytree(FIXTURE_ROOT / "variants", self.variants)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_selects_base_family_and_exact_client_in_priority_order(self) -> None:
        resolution = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-desktop",
        )

        self.assertIsInstance(resolution, LayeredVariantResolution)
        self.assertEqual(resolution.resolver_version, VARIANT_RESOLVER_VERSION)
        self.assertEqual(resolution.target_client, "kimi-desktop")
        self.assertEqual(resolution.family, "kimi")
        self.assertEqual(
            [(layer.role, layer.target) for layer in resolution.layers],
            [
                ("base", None),
                ("family", "kimi"),
                ("client", "kimi-desktop"),
            ],
        )
        self.assertEqual(
            resolution.applied_variant_targets,
            ("kimi", "kimi-desktop"),
        )
        self.assertEqual(resolution.layers[1].delete, ("references/remove.md",))
        self.assertEqual(resolution.layers[2].delete, ("references/family.md",))
        files = {
            entry.relative_path: entry.content
            for entry in resolution.overlay_plan.files
        }
        self.assertEqual(files["SKILL.md"], b"# Kimi Desktop\n")
        self.assertNotIn("references/family.md", files)
        self.assertNotIn("references/remove.md", files)

    def test_base_only_resolution_is_valid_when_variant_root_is_absent(self) -> None:
        missing = self.root / "variants" / "missing"
        resolution = resolve_variant_for_client(self.base, missing, "claude-code")

        self.assertEqual(
            [(layer.role, layer.target) for layer in resolution.layers],
            [("base", None)],
        )
        self.assertEqual(resolution.applied_variant_targets, ())
        self.assertEqual(
            resolution.layers[0].content_hash,
            hash_skill_files_with_modes(
                (entry.relative_path, entry.content, entry.mode)
                for entry in resolution.overlay_plan.layers[0].files
            ),
        )

    def test_family_and_client_with_same_id_are_applied_once(self) -> None:
        codex = self.variants / "codex"
        codex.mkdir()
        (codex / "variant.yaml").write_text(
            "version: 1\ntarget: codex\nmode: overlay\n",
            encoding="utf-8",
        )
        (codex / "SKILL.md").write_text("# Codex\n", encoding="utf-8")

        resolution = resolve_variant_for_client(self.base, self.variants, "codex")

        self.assertEqual(
            [(layer.role, layer.target) for layer in resolution.layers],
            [("base", None), ("family-client", "codex")],
        )
        self.assertEqual(resolution.applied_variant_targets, ("codex",))
        self.assertEqual(
            dict(
                (entry.relative_path, entry.content)
                for entry in resolution.overlay_plan.files
            )["SKILL.md"],
            b"# Codex\n",
        )

    def test_hashes_are_deterministic_and_machine_paths_are_not_an_input(self) -> None:
        first = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")
        copied_root = self.root / "other-machine"
        copied_base = copied_root / "skills" / "demo"
        copied_variants = copied_root / "variants" / "demo"
        shutil.copytree(self.base, copied_base)
        shutil.copytree(self.variants, copied_variants)

        second = resolve_variant_for_client(
            copied_base,
            copied_variants,
            "kimi-desktop",
        )

        self.assertEqual(
            [layer.content_hash for layer in first.layers],
            [layer.content_hash for layer in second.layers],
        )
        self.assertEqual(first.output_hash, second.output_hash)
        self.assertEqual(first.resolution_hash, second.resolution_hash)
        self.assertNotEqual(first.layers[0].source_path, second.layers[0].source_path)

    def test_changing_one_client_variant_changes_only_that_client_resolution(self) -> None:
        desktop_before = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-desktop",
        )
        code_before = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-code",
        )

        desktop_skill = self.variants / "kimi-desktop" / "SKILL.md"
        desktop_skill.write_text("# Kimi Desktop changed\n", encoding="utf-8")

        desktop_after = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-desktop",
        )
        code_after = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-code",
        )

        self.assertNotEqual(
            desktop_before.resolution_hash,
            desktop_after.resolution_hash,
        )
        self.assertNotEqual(desktop_before.output_hash, desktop_after.output_hash)
        self.assertEqual(code_before.resolution_hash, code_after.resolution_hash)
        self.assertEqual(code_before.output_hash, code_after.output_hash)
        self.assertEqual(
            [layer.content_hash for layer in desktop_before.layers[:2]],
            [layer.content_hash for layer in desktop_after.layers[:2]],
        )
        self.assertNotEqual(
            desktop_before.layers[2].content_hash,
            desktop_after.layers[2].content_hash,
        )

    def test_target_client_is_part_of_resolution_hash(self) -> None:
        shutil.rmtree(self.variants / "kimi-desktop")

        code = resolve_variant_for_client(self.base, self.variants, "kimi-code")
        desktop = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-desktop",
        )

        self.assertEqual(
            [layer.content_hash for layer in code.layers],
            [layer.content_hash for layer in desktop.layers],
        )
        self.assertEqual(code.output_hash, desktop.output_hash)
        self.assertNotEqual(code.resolution_hash, desktop.resolution_hash)

    def test_ignored_noise_does_not_change_layer_or_resolution_hash(self) -> None:
        before = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")
        (self.variants / "kimi-desktop" / ".DS_Store").write_bytes(b"noise")
        cache = self.variants / "kimi-desktop" / "__pycache__"
        cache.mkdir()
        (cache / "cache.pyc").write_bytes(b"noise")

        after = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

        self.assertEqual(before.layers, after.layers)
        self.assertEqual(before.output_hash, after.output_hash)
        self.assertEqual(before.resolution_hash, after.resolution_hash)

    def test_reserved_base_manifest_is_not_a_layer_hash_input(self) -> None:
        before = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")
        (self.base / "variant.yaml").write_text("not a Base input\n", encoding="utf-8")

        after = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

        self.assertNotIn(
            "variant.yaml",
            {entry.relative_path for entry in after.overlay_plan.layers[0].files},
        )
        self.assertEqual(before.layers[0].content_hash, after.layers[0].content_hash)
        self.assertEqual(before.output_hash, after.output_hash)
        self.assertEqual(before.resolution_hash, after.resolution_hash)

    def test_output_hash_matches_materialized_skill_hash(self) -> None:
        resolution = resolve_variant_for_client(
            self.base,
            self.variants,
            "kimi-desktop",
        )
        destination = self.root / "rendered" / "demo"

        materialize_variant_overlay(resolution.overlay_plan, destination)

        self.assertEqual(resolution.output_hash, hash_portable_skill_dir(destination))

    @unittest.skipUnless(os.name == "posix", "POSIX mode transitions required")
    def test_host_chmod_does_not_change_portable_resolution_identity(self) -> None:
        script = self.base / "scripts" / "shared.sh"
        script.chmod(0o644)
        before = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

        script.chmod(0o755)
        after = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

        before_file = next(
            entry
            for entry in before.overlay_plan.files
            if entry.relative_path == "scripts/shared.sh"
        )
        after_file = next(
            entry
            for entry in after.overlay_plan.files
            if entry.relative_path == "scripts/shared.sh"
        )
        self.assertEqual(before_file.content, after_file.content)
        self.assertEqual((before_file.mode, after_file.mode), (0o644, 0o644))
        self.assertEqual(before.layers[0].content_hash, after.layers[0].content_hash)
        self.assertEqual(before.output_hash, after.output_hash)
        self.assertEqual(before.resolution_hash, after.resolution_hash)

    def test_layer_hashes_follow_exact_plan_snapshot_during_forced_aba(self) -> None:
        skill_file = self.base / "SKILL.md"
        original_content = skill_file.read_bytes()
        snapshot_content = b"# ABA snapshot\n"
        original_snapshot = variant_overlay_module._snapshot_source_tree
        base_reads = 0

        def snapshot_with_aba(root: Path):
            nonlocal base_reads
            if root == self.base:
                if base_reads == 0:
                    skill_file.write_bytes(snapshot_content)
                result = original_snapshot(root)
                base_reads += 1
                if base_reads == 2:
                    skill_file.write_bytes(original_content)
                return result
            return original_snapshot(root)

        with mock.patch(
            "skill_sync.variant_overlay._snapshot_source_tree",
            side_effect=snapshot_with_aba,
        ):
            resolution = resolve_variant_for_client(
                self.base,
                self.variants,
                "kimi-desktop",
            )

        self.assertEqual(skill_file.read_bytes(), original_content)
        base_layer = resolution.overlay_plan.layers[0]
        base_skill = next(
            entry for entry in base_layer.files if entry.relative_path == "SKILL.md"
        )
        self.assertEqual(base_skill.content, snapshot_content)
        self.assertEqual(
            resolution.layers[0].content_hash,
            hash_skill_files_with_modes(
                (entry.relative_path, entry.content, entry.mode)
                for entry in base_layer.files
            ),
        )

    def test_manifest_semantics_follow_exact_snapshot_during_forced_aba(self) -> None:
        manifest_file = self.variants / "kimi-desktop" / "variant.yaml"
        original_content = manifest_file.read_bytes()
        snapshot_content = (
            b"version: 1\n"
            b"target: kimi-desktop\n"
            b"mode: overlay\n"
            b"delete: references/base.md\n"
        )
        original_snapshot = variant_overlay_module._snapshot_source_tree
        client_reads = 0

        def snapshot_with_manifest_aba(root: Path):
            nonlocal client_reads
            if root == self.variants / "kimi-desktop":
                if client_reads == 0:
                    manifest_file.write_bytes(snapshot_content)
                result = original_snapshot(root)
                client_reads += 1
                if client_reads == 2:
                    manifest_file.write_bytes(original_content)
                return result
            return original_snapshot(root)

        with mock.patch(
            "skill_sync.variant_overlay._snapshot_source_tree",
            side_effect=snapshot_with_manifest_aba,
        ):
            resolution = resolve_variant_for_client(
                self.base,
                self.variants,
                "kimi-desktop",
            )

        self.assertEqual(manifest_file.read_bytes(), original_content)
        client_layer = resolution.overlay_plan.layers[2]
        captured_manifest = next(
            entry
            for entry in client_layer.files
            if entry.relative_path == "variant.yaml"
        )
        self.assertEqual(captured_manifest.content, snapshot_content)
        self.assertEqual(client_layer.manifest.delete, ("references/base.md",))
        self.assertEqual(resolution.layers[2].delete, ("references/base.md",))
        output_paths = {entry.relative_path for entry in resolution.overlay_plan.files}
        self.assertNotIn("references/base.md", output_paths)
        self.assertIn("references/family.md", output_paths)

    def test_rejects_client_variant_appearing_during_resolution(self) -> None:
        original_plan = variant_resolution_module.plan_variant_overlay

        def plan_then_add_client(*args, **kwargs):
            plan = original_plan(*args, **kwargs)
            client = self.variants / "kimi-code"
            client.mkdir()
            (client / "variant.yaml").write_text(
                "version: 1\ntarget: kimi-code\nmode: overlay\n",
                encoding="utf-8",
            )
            return plan

        with mock.patch(
            "skill_sync.variant_resolution.plan_variant_overlay",
            side_effect=plan_then_add_client,
        ):
            with self.assertRaisesRegex(ValueError, "selection changed"):
                resolve_variant_for_client(self.base, self.variants, "kimi-code")

    def test_rejects_selected_variant_replaced_during_resolution(self) -> None:
        original_plan = variant_resolution_module.plan_variant_overlay

        def plan_then_replace_client(*args, **kwargs):
            plan = original_plan(*args, **kwargs)
            client = self.variants / "kimi-desktop"
            replacement = self.root / "replacement"
            shutil.copytree(client, replacement)
            shutil.rmtree(client)
            shutil.copytree(replacement, client)
            return plan

        with mock.patch(
            "skill_sync.variant_resolution.plan_variant_overlay",
            side_effect=plan_then_replace_client,
        ):
            with self.assertRaisesRegex(ValueError, "selection changed"):
                resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

    def test_rejects_case_ambiguity_observed_by_final_selection_scan(self) -> None:
        initial_scan = variant_resolution_module._scan_variant_targets(self.variants)
        ambiguous_scan = dataclasses.replace(
            initial_scan,
            targets=initial_scan.targets
            + (
                dataclasses.replace(
                    next(item for item in initial_scan.targets if item.name == "kimi"),
                    name="KIMI",
                ),
            ),
        )

        with mock.patch(
            "skill_sync.variant_resolution._scan_variant_targets",
            side_effect=(initial_scan, ambiguous_scan),
        ):
            with self.assertRaisesRegex(ValueError, "case-insensitive"):
                resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

    def test_resolution_read_model_is_immutable(self) -> None:
        resolution = resolve_variant_for_client(self.base, self.variants, "kimi-desktop")

        with self.assertRaises(dataclasses.FrozenInstanceError):
            resolution.provenance.target_client = "kimi-code"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            resolution.layers[0].content_hash = "changed"  # type: ignore[misc]

    def test_rejects_family_id_in_place_of_concrete_client(self) -> None:
        with self.assertRaisesRegex(ValueError, "concrete Agent client"):
            resolve_variant_for_client(self.base, self.variants, "kimi")

    def test_rejects_unknown_client(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown Agent client"):
            resolve_variant_for_client(self.base, self.variants, "unknown")

    def test_rejects_case_mismatched_selected_variant(self) -> None:
        (self.variants / "kimi").rename(self.variants / "KIMI")

        with self.assertRaisesRegex(ValueError, "case-insensitive"):
            resolve_variant_for_client(self.base, self.variants, "kimi-desktop")


if __name__ == "__main__":
    unittest.main()
