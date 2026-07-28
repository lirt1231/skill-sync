import json
import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.deployment import (
    PROVENANCE_FILE,
    deployment_path,
    expected_layered_provenance,
    render_base_deployment,
    render_layered_deployment,
    remove_verified_deployment,
    resolution_hash,
    verify_deployment,
)
from skill_sync.hash import hash_skill_dir
from skill_sync.variant_resolution import resolve_variant_for_client


def write(root: Path, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class DeploymentTest(unittest.TestCase):
    def make_source(self, work: Path) -> Path:
        source = work / "source"
        write(source, "SKILL.md", b"---\nname: alpha\n---\n# Alpha\n")
        return source

    def test_content_address_and_provenance_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            first = render_base_deployment(source, work / "rendered", "alpha", "codex")
            second = render_base_deployment(source, work / "rendered", "alpha", "codex")

            source_hash = hash_skill_dir(source)
            expected_resolution = resolution_hash("alpha", source_hash, "codex")
            self.assertEqual(
                first.path,
                deployment_path(work / "rendered", "alpha", expected_resolution),
            )
            self.assertEqual(first.path, second.path)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.provenance, second.provenance)
            self.assertEqual(
                first.provenance,
                {
                    "schema_version": 1,
                    "logical_skill": "alpha",
                    "source_hash": source_hash,
                    "resolution_hash": expected_resolution,
                    "resolver_version": "base-v1",
                    "rendered_hash": source_hash,
                    "target_client": "codex",
                    "applied_layers": ["base"],
                },
            )
            self.assertTrue(verify_deployment(first.path).ok)

    def test_target_client_changes_resolution_and_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            codex = render_base_deployment(source, work / "rendered", "alpha", "codex")
            workbuddy = render_base_deployment(
                source, work / "rendered", "alpha", "workbuddy"
            )
            self.assertNotEqual(codex.path, workbuddy.path)
            self.assertNotEqual(
                codex.provenance["resolution_hash"],
                workbuddy.provenance["resolution_hash"],
            )

    def test_layered_deployment_persists_portable_provenance_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            variants = work / "variants" / "alpha" / "kimi"
            write(
                variants,
                "variant.yaml",
                b"version: 1\ntarget: kimi\nmode: overlay\n",
            )
            write(variants, "family.txt", b"Kimi family\n")
            resolution = resolve_variant_for_client(
                source,
                variants.parent,
                "kimi-code",
            )

            first = render_layered_deployment(
                resolution,
                work / "rendered",
                "alpha",
            )
            second = render_layered_deployment(
                resolution,
                work / "rendered",
                "alpha",
            )

            expected = expected_layered_provenance("alpha", resolution)
            self.assertEqual(first.provenance, expected)
            self.assertEqual(second.provenance, expected)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.path, second.path)
            self.assertEqual((first.path / "family.txt").read_bytes(), b"Kimi family\n")
            self.assertTrue(verify_deployment(first.path).ok)
            self.assertEqual(expected["schema_version"], 2)
            self.assertEqual(expected["resolver_version"], "variant-overlay-v2")
            self.assertEqual(
                expected["applied_layers"],
                ["base", "family:kimi"],
            )
            self.assertNotIn(str(source), json.dumps(expected))
            self.assertNotIn(str(variants), json.dumps(expected))

    def test_layered_deployment_rejects_tampered_layer_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            resolution = resolve_variant_for_client(
                source,
                work / "missing-variants",
                "codex",
            )
            deployed = render_layered_deployment(
                resolution,
                work / "rendered",
                "alpha",
            )
            manifest = deployed.path / PROVENANCE_FILE
            manifest.chmod(0o644)
            provenance = json.loads(manifest.read_text(encoding="utf-8"))
            provenance["layers"][0]["content_hash"] = "sha256:" + "0" * 64
            manifest.write_text(json.dumps(provenance), encoding="utf-8")

            verification = verify_deployment(deployed.path)
            self.assertEqual(verification.state, "tampered")
            self.assertIn("resolution hash", verification.reason or "")

    def test_layered_deployment_rejects_malformed_schema_v2_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            variants = work / "variants" / "alpha" / "codex"
            write(
                variants,
                "variant.yaml",
                b"version: 1\ntarget: codex\nmode: overlay\n",
            )
            resolution = resolve_variant_for_client(
                source,
                variants.parent,
                "codex",
            )
            deployed = render_layered_deployment(
                resolution,
                work / "rendered",
                "alpha",
            )
            manifest = deployed.path / PROVENANCE_FILE
            manifest.chmod(0o644)
            provenance = json.loads(manifest.read_text(encoding="utf-8"))
            provenance["layers"][1]["role"] = "base"
            manifest.write_text(json.dumps(provenance), encoding="utf-8")

            verification = verify_deployment(deployed.path)
            self.assertEqual(verification.state, "tampered")
            self.assertEqual(verification.reason, "invalid provenance fields")

    def test_copies_hidden_and_binary_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            write(source, ".hidden", b"hidden\x00data")
            write(source, "assets/image.bin", bytes(range(256)))

            deployed = render_base_deployment(source, work / "rendered", "alpha", "codex")

            self.assertEqual((deployed.path / ".hidden").read_bytes(), b"hidden\x00data")
            self.assertEqual((deployed.path / "assets/image.bin").read_bytes(), bytes(range(256)))

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks are unsupported")
    def test_rejects_source_root_and_nested_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            source_link = work / "source-link"
            source_link.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                render_base_deployment(source_link, work / "rendered", "alpha", "codex")

            (source / "linked.md").symlink_to(source / "SKILL.md")
            with self.assertRaisesRegex(ValueError, "symlink.*linked.md"):
                render_base_deployment(source, work / "rendered", "alpha", "codex")

    def test_rejects_symlinked_deployment_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            real_store = work / "real-store"
            real_store.mkdir()
            linked_store = work / "rendered"
            linked_store.symlink_to(real_store, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "deployment store"):
                render_base_deployment(source, linked_store, "alpha", "codex")

    def test_output_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            deployed = render_base_deployment(
                self.make_source(work), work / "rendered", "alpha", "codex"
            )
            for path in [deployed.path, *deployed.path.rglob("*")]:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o222, 0, path)

    def test_verifies_missing_stale_and_tampered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            missing = verify_deployment(work / "missing")
            self.assertEqual(missing.state, "missing")

            deployed = render_base_deployment(source, work / "rendered", "alpha", "codex")
            stale_expected = {**deployed.provenance, "source_hash": "sha256:" + "0" * 64}
            stale = verify_deployment(deployed.path, expected_provenance=stale_expected)
            self.assertEqual(stale.state, "stale")

            skill_file = deployed.path / "SKILL.md"
            skill_file.chmod(0o644)
            skill_file.write_text("tampered", encoding="utf-8")
            tampered = verify_deployment(deployed.path)
            self.assertEqual(tampered.state, "tampered")
            self.assertIn("rendered hash mismatch", tampered.reason or "")

    def test_tampered_provenance_is_detected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            deployed = render_base_deployment(
                self.make_source(work), work / "rendered", "alpha", "codex"
            )
            provenance = deployed.path / PROVENANCE_FILE
            provenance.chmod(0o644)
            provenance.write_text("not json", encoding="utf-8")
            self.assertEqual(verify_deployment(deployed.path).state, "tampered")

    def test_tampered_resolution_in_provenance_is_detected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            deployed = render_base_deployment(
                self.make_source(work), work / "rendered", "alpha", "codex"
            )
            manifest = deployed.path / PROVENANCE_FILE
            manifest.chmod(0o644)
            provenance = json.loads(manifest.read_text(encoding="utf-8"))
            provenance["resolution_hash"] = "sha256:" + "0" * 64
            manifest.write_text(json.dumps(provenance), encoding="utf-8")
            result = verify_deployment(deployed.path)
            self.assertEqual(result.state, "tampered")
            self.assertIn("resolution hash", result.reason or "")

    def test_atomic_install_failure_leaves_no_partial_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            import skill_sync.deployment as deployment

            original_rename = deployment.os.rename

            def fail_staged_install(src, dst):
                if Path(dst).name == "alpha" and Path(src).parent.name.startswith(".alpha.tmp-"):
                    raise OSError("forced atomic rename failure")
                return original_rename(src, dst)

            with mock.patch.object(deployment.os, "rename", side_effect=fail_staged_install):
                with self.assertRaisesRegex(OSError, "forced atomic rename failure"):
                    render_base_deployment(source, work / "rendered", "alpha", "codex")

            source_hash = hash_skill_dir(source)
            expected = deployment_path(
                work / "rendered",
                "alpha",
                resolution_hash("alpha", source_hash, "codex"),
            )
            self.assertFalse(expected.exists())
            self.assertFalse(any(expected.parent.glob(".alpha.tmp-*")))

    def test_concurrent_winner_is_never_deleted_after_install_race(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            store = work / "rendered"
            source_hash = hash_skill_dir(source)
            destination = deployment_path(
                store,
                "alpha",
                resolution_hash("alpha", source_hash, "codex"),
            )
            import skill_sync.deployment as deployment

            def install_foreign_winner(_src, dst):
                write(Path(dst), "foreign", b"must survive")
                raise OSError(errno.ENOTEMPTY, "concurrent winner")

            with mock.patch.object(
                deployment.os, "rename", side_effect=install_foreign_winner
            ):
                with self.assertRaisesRegex(ValueError, "concurrent deployment winner"):
                    render_base_deployment(source, store, "alpha", "codex")

            self.assertEqual((destination / "foreign").read_bytes(), b"must survive")

    def test_refuses_to_overwrite_existing_tampered_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            source_hash = hash_skill_dir(source)
            destination = deployment_path(
                work / "rendered",
                "alpha",
                resolution_hash("alpha", source_hash, "codex"),
            )
            write(destination, "old-marker", b"preserve this cache")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                render_base_deployment(source, work / "rendered", "alpha", "codex")

            self.assertEqual((destination / "old-marker").read_bytes(), b"preserve this cache")
            self.assertFalse((destination / "SKILL.md").exists())

    def test_provenance_bytes_are_stable_across_fresh_stores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            one = render_base_deployment(source, work / "one", "alpha", "codex")
            two = render_base_deployment(source, work / "two", "alpha", "codex")
            self.assertEqual(
                (one.path / PROVENANCE_FILE).read_bytes(),
                (two.path / PROVENANCE_FILE).read_bytes(),
            )
            self.assertEqual(json.loads((one.path / PROVENANCE_FILE).read_text()), one.provenance)

    def test_rejects_path_traversal_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            with self.assertRaisesRegex(ValueError, "safe path component"):
                render_base_deployment(source, work / "rendered", "../alpha", "codex")

    def test_rejects_reserved_provenance_in_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            write(source, PROVENANCE_FILE, b"authored content")
            with self.assertRaisesRegex(ValueError, "reserved file"):
                render_base_deployment(source, work / "rendered", "alpha", "codex")

    def test_nested_provenance_named_file_is_hashed_as_authored_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            source = self.make_source(work)
            write(source, f"references/{PROVENANCE_FILE}", b"nested authored content")
            deployed = render_base_deployment(
                source, work / "rendered", "alpha", "codex"
            )
            self.assertTrue(verify_deployment(deployed.path).ok)
            nested = deployed.path / "references" / PROVENANCE_FILE
            nested.chmod(0o644)
            nested.write_bytes(b"tampered nested content")
            self.assertEqual(verify_deployment(deployed.path).state, "tampered")

    def test_remove_verified_deployment_uses_trash_and_refuses_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            store = work / "rendered"
            first = render_base_deployment(
                self.make_source(work), store, "alpha", "codex"
            )
            displaced = remove_verified_deployment(
                first.path, store, work / "trash"
            )
            self.assertFalse(first.path.exists())
            self.assertFalse(displaced.exists())

            second = render_base_deployment(
                self.make_source(work), store, "alpha", "codex"
            )
            skill_file = second.path / "SKILL.md"
            skill_file.chmod(0o644)
            skill_file.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unverified"):
                remove_verified_deployment(second.path, store, work / "trash")
            self.assertTrue(second.path.exists())


if __name__ == "__main__":
    unittest.main()
