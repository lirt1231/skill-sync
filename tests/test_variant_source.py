import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.config import empty_config, save_config
from skill_sync.errors import SkillSyncError

try:
    from skill_sync import variant_source
except ImportError:  # pragma: no cover - initial TDD red state
    variant_source = None


class VariantSourceTest(unittest.TestCase):
    def setUp(self):
        if variant_source is None:
            self.fail("skill_sync.variant_source module is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skills_root = self.root / "portable" / "skills"
        self.variants_root = self.root / "portable" / "variants"
        self.config_path = self.root / "config.json"
        config = empty_config()
        config["skills_root"] = str(self.skills_root)
        save_config(self.config_path, config)
        self.make_skill("alpha")

    def make_skill(self, name: str) -> Path:
        skill = self.skills_root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        (skill / "base-only.txt").write_text("base\n", encoding="utf-8")
        return skill

    def make_variant(self, skill: str, target: str = "kimi") -> Path:
        variant = self.variants_root / skill / target
        variant.mkdir(parents=True)
        (variant / "variant.yaml").write_text(
            f"version: 1\ntarget: {target}\nmode: overlay\n",
            encoding="utf-8",
        )
        return variant

    def snapshot(self, root: Path) -> dict[str, tuple[int, int, int]]:
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): (
                path.stat().st_mode,
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
        }

    def test_create_family_writes_only_minimal_atomic_overlay_manifest(self):
        config_before = self.config_path.read_bytes()
        base_before = self.snapshot(self.skills_root / "alpha")

        result = variant_source.create_variant(
            "alpha", scope="family", target="kimi", config_path=self.config_path
        )

        target = self.variants_root / "alpha" / "kimi"
        self.assertEqual(
            (target / "variant.yaml").read_text(encoding="utf-8"),
            "version: 1\ntarget: kimi\nmode: overlay\n",
        )
        self.assertEqual([path.name for path in target.iterdir()], ["variant.yaml"])
        self.assertEqual(result["skill"], "alpha")
        self.assertEqual(result["scope"], "family")
        self.assertEqual(result["target"], "kimi")
        self.assertEqual(result["affected_clients"], ["kimi-code", "kimi-desktop"])
        self.assertEqual(result["resolution_order"], ["base", "family:kimi", "client-specific"])
        self.assertEqual(self.snapshot(self.skills_root / "alpha"), base_before)
        self.assertEqual(self.config_path.read_bytes(), config_before)

    def test_create_client_uses_registered_static_client_and_family(self):
        result = variant_source.create_variant(
            "alpha",
            scope="client",
            target="kimi-desktop",
            config_path=self.config_path,
        )

        self.assertEqual(result["scope"], "client")
        self.assertEqual(result["family"], "kimi")
        self.assertEqual(result["affected_clients"], ["kimi-desktop"])
        self.assertEqual(
            result["resolution_order"],
            ["base", "family:kimi", "client:kimi-desktop"],
        )

    def test_family_and_client_names_are_validated_against_their_own_registry(self):
        cases = (
            ("family", "kimi-desktop"),
            ("client", "kimi"),
            ("family", "mystery"),
            ("client", "mystery"),
        )
        for scope, target in cases:
            with self.subTest(scope=scope, target=target):
                with self.assertRaisesRegex(SkillSyncError, "unknown"):
                    variant_source.create_variant(
                        "alpha", scope=scope, target=target, config_path=self.config_path
                    )
        self.assertFalse(self.variants_root.exists())

    def test_same_id_family_and_client_scopes_share_one_unambiguous_disk_target(self):
        created = variant_source.create_variant(
            "alpha", scope="client", target="codex", config_path=self.config_path
        )
        self.assertEqual(created["scope"], "client")
        with self.assertRaisesRegex(SkillSyncError, "already exists"):
            variant_source.create_variant(
                "alpha", scope="family", target="codex", config_path=self.config_path
            )

        listed = variant_source.list_variants(config_path=self.config_path)
        self.assertEqual(listed["variants"][0]["target_kinds"], ["family", "client"])

    def test_create_rejects_existing_target_and_cleans_failed_atomic_stage(self):
        variant_source.create_variant(
            "alpha", scope="family", target="kimi", config_path=self.config_path
        )
        manifest = self.variants_root / "alpha" / "kimi" / "variant.yaml"
        before = manifest.read_bytes()
        with self.assertRaisesRegex(SkillSyncError, "already exists"):
            variant_source.create_variant(
                "alpha", scope="family", target="kimi", config_path=self.config_path
            )
        self.assertEqual(manifest.read_bytes(), before)

        self.make_skill("beta")
        with mock.patch.object(
            variant_source, "rename_no_replace", side_effect=OSError("forced publish failure")
        ):
            with self.assertRaisesRegex(SkillSyncError, "forced publish failure"):
                variant_source.create_variant(
                    "beta", scope="family", target="kimi", config_path=self.config_path
                )
        beta_root = self.variants_root / "beta"
        self.assertFalse((beta_root / "kimi").exists())
        self.assertEqual(list(beta_root.glob(".kimi.create-*")), [])

    def test_create_rejects_illegal_missing_linked_and_ambiguous_skill_names(self):
        invalid_names = ("", ".", "..", "a/b", r"a\b", " alpha", "alpha ", "NUL")
        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaises(SkillSyncError):
                    variant_source.create_variant(
                        name, scope="family", target="kimi", config_path=self.config_path
                    )

        with self.assertRaisesRegex(SkillSyncError, "does not exist"):
            variant_source.create_variant(
                "missing", scope="family", target="kimi", config_path=self.config_path
            )

        if hasattr(os, "symlink"):
            linked = self.skills_root / "linked"
            try:
                linked.symlink_to(self.skills_root / "alpha", target_is_directory=True)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(SkillSyncError, "link|reparse"):
                    variant_source.create_variant(
                        "linked", scope="family", target="kimi", config_path=self.config_path
                    )

        real_matches = variant_source._casefold_matches
        with mock.patch.object(variant_source, "_casefold_matches") as matches:
            matches.side_effect = lambda root, name, label: (
                ["Alpha", "alpha"]
                if root == self.skills_root
                else real_matches(root, name, label=label)
            )
            with self.assertRaisesRegex(SkillSyncError, "ambiguous"):
                variant_source.create_variant(
                    "alpha", scope="family", target="kimi", config_path=self.config_path
                )

    def test_create_rejects_nonportable_paths_inside_canonical_base(self):
        unsafe_paths = ("references/NUL.txt", "references/bad:name.txt")
        for index, relative_path in enumerate(unsafe_paths):
            with self.subTest(relative_path=relative_path):
                skill_name = f"unsafe-{index}"
                base = self.make_skill(skill_name)
                unsafe = base / relative_path
                unsafe.parent.mkdir(exist_ok=True)
                unsafe.write_text("unsafe\n", encoding="utf-8")

                with self.assertRaisesRegex(SkillSyncError, "portable|reserved"):
                    variant_source.create_variant(
                        skill_name,
                        scope="family",
                        target="kimi",
                        config_path=self.config_path,
                    )
                self.assertFalse((self.variants_root / skill_name / "kimi").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_create_rejects_nested_link_inside_canonical_base(self):
        external = self.root / "external-create.txt"
        external.write_text("external\n", encoding="utf-8")
        linked = self.skills_root / "alpha" / "references" / "linked.txt"
        linked.parent.mkdir()
        try:
            linked.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        with self.assertRaisesRegex(SkillSyncError, "link|reparse"):
            variant_source.create_variant(
                "alpha", scope="family", target="kimi", config_path=self.config_path
            )
        self.assertFalse((self.variants_root / "alpha" / "kimi").exists())

    def test_list_and_validate_are_deterministic_read_only_and_accept_empty_overlay(self):
        variant_source.create_variant(
            "alpha", scope="client", target="kimi-desktop", config_path=self.config_path
        )
        variant_source.create_variant(
            "alpha", scope="family", target="kimi", config_path=self.config_path
        )
        before = self.snapshot(self.root)

        listed = variant_source.list_variants(skill="alpha", config_path=self.config_path)
        validated = variant_source.validate_variants("alpha", config_path=self.config_path)

        self.assertEqual([item["target"] for item in listed["variants"]], ["kimi", "kimi-desktop"])
        self.assertTrue(validated["valid"])
        self.assertEqual(validated["variant_count"], 2)
        self.assertEqual(validated["issues"], [])
        self.assertTrue(all(item["overlay_file_count"] == 0 for item in validated["variants"]))
        self.assertEqual(self.snapshot(self.root), before)

    def test_validate_reports_invalid_manifest_without_mutating_it(self):
        target = self.variants_root / "alpha" / "kimi"
        target.mkdir(parents=True)
        manifest = target / "variant.yaml"
        manifest.write_text("version: 1\ntarget: mystery\nmode: overlay\n", encoding="utf-8")
        before = self.snapshot(self.root)

        result = variant_source.validate_variants("alpha", config_path=self.config_path)

        self.assertFalse(result["valid"])
        self.assertEqual(result["variant_count"], 1)
        self.assertEqual(result["variants"][0]["valid"], False)
        self.assertIn("Unknown variant target", result["issues"][0]["message"])
        self.assertEqual(self.snapshot(self.root), before)

    def test_list_keeps_inspectable_rows_but_marks_orphan_base_invalid(self):
        self.make_variant("alpha")
        self.make_variant("orphan")
        before = self.snapshot(self.root)

        result = variant_source.list_variants(config_path=self.config_path)

        self.assertFalse(result["valid"])
        rows = {item["skill"]: item for item in result["variants"]}
        self.assertTrue(rows["alpha"]["valid"])
        self.assertTrue(rows["alpha"]["base_valid"])
        self.assertFalse(rows["orphan"]["valid"])
        self.assertFalse(rows["orphan"]["base_valid"])
        self.assertTrue(rows["orphan"]["manifest_valid"])
        self.assertIn("variant_base_missing", [issue["code"] for issue in result["issues"]])
        self.assertEqual(self.snapshot(self.root), before)

    def test_validate_missing_requested_base_is_invalid_even_without_variants(self):
        result = variant_source.validate_variants(
            "missing", config_path=self.config_path
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["variant_count"], 0)
        self.assertEqual(result["issues"][0]["code"], "variant_base_missing")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_validate_marks_unsafe_base_tree_invalid_without_hiding_variant(self):
        self.make_variant("alpha")
        external = self.root / "external.txt"
        external.write_text("external\n", encoding="utf-8")
        linked = self.skills_root / "alpha" / "linked.txt"
        try:
            linked.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")
        before = self.snapshot(self.root)

        result = variant_source.validate_variants(
            "alpha", config_path=self.config_path
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["variant_count"], 1)
        self.assertFalse(result["variants"][0]["valid"])
        self.assertFalse(result["variants"][0]["base_valid"])
        self.assertTrue(result["variants"][0]["manifest_valid"])
        self.assertEqual(result["issues"][0]["code"], "variant_base_unsafe")
        self.assertEqual(self.snapshot(self.root), before)

    def test_validate_uses_resolver_portability_rules_for_canonical_base(self):
        self.make_variant("alpha")
        unsafe = self.skills_root / "alpha" / "references" / "NUL.txt"
        unsafe.parent.mkdir()
        unsafe.write_text("unsafe\n", encoding="utf-8")

        result = variant_source.validate_variants(
            "alpha", config_path=self.config_path
        )

        self.assertFalse(result["valid"])
        self.assertFalse(result["variants"][0]["base_valid"])
        self.assertTrue(result["variants"][0]["manifest_valid"])
        self.assertEqual(result["issues"][0]["code"], "variant_base_unsafe")
        self.assertIn("reserved Windows name", result["issues"][0]["message"])

    def test_validate_rejects_case_insensitive_base_path_collision_when_supported(self):
        self.make_variant("alpha")
        references = self.skills_root / "alpha" / "references"
        references.mkdir()
        upper = references / "Case.md"
        lower = references / "case.md"
        upper.write_text("upper\n", encoding="utf-8")
        lower.write_text("lower\n", encoding="utf-8")
        if upper.samefile(lower):
            self.skipTest("filesystem is case-insensitive")

        result = variant_source.validate_variants(
            "alpha", config_path=self.config_path
        )

        self.assertFalse(result["valid"])
        self.assertFalse(result["variants"][0]["base_valid"])
        self.assertIn("case-insensitive", result["issues"][0]["message"])

    def test_list_reports_nonportable_discovered_skill_name(self):
        self.make_skill("NUL")
        self.make_variant("NUL")

        result = variant_source.list_variants(config_path=self.config_path)

        self.assertFalse(result["valid"])
        self.assertEqual(result["variants"][0]["skill"], "NUL")
        self.assertFalse(result["variants"][0]["valid"])
        self.assertEqual(result["issues"][0]["code"], "variant_skill_name_invalid")

    def test_nested_variant_yaml_counts_as_overlay_content(self):
        target = self.make_variant("alpha")
        nested = target / "references" / "variant.yaml"
        nested.parent.mkdir()
        nested.write_text("overlay content\n", encoding="utf-8")

        result = variant_source.validate_variants(
            "alpha", config_path=self.config_path
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["variants"][0]["overlay_file_count"], 1)

    def test_list_fails_closed_for_case_ambiguous_skill_or_target_directories(self):
        self.variants_root.mkdir(parents=True)
        with mock.patch.object(
            variant_source,
            "_real_child_directories",
            side_effect=[["alpha"], ["kimi", "KIMI"]],
        ):
            with self.assertRaisesRegex(SkillSyncError, "ambiguous"):
                variant_source.list_variants(config_path=self.config_path)

        with mock.patch.object(
            variant_source,
            "_real_child_directories",
            return_value=["alpha", "Alpha"],
        ):
            with self.assertRaisesRegex(SkillSyncError, "ambiguous"):
                variant_source.list_variants(config_path=self.config_path)


if __name__ == "__main__":
    unittest.main()
