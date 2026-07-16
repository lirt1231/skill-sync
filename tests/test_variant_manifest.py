import os
import tempfile
import unittest
from pathlib import Path

from skill_sync.variant import VariantManifest, load_variant_manifest


class VariantManifestTest(unittest.TestCase):
    def write_manifest(self, root: Path, text: str, target: str = "codex") -> Path:
        variant_root = root / target
        variant_root.mkdir(parents=True)
        manifest = variant_root / "variant.yaml"
        manifest.write_text(text, encoding="utf-8")
        return manifest

    def test_loads_overlay_with_one_delete_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self.write_manifest(
                Path(tmp_dir),
                "version: 1\ntarget: codex\nmode: overlay\ndelete: references/claude-tools.md\n",
            )

            manifest = load_variant_manifest(manifest_path)

        self.assertEqual(
            manifest,
            VariantManifest(
                version=1,
                target="codex",
                mode="overlay",
                delete=("references/claude-tools.md",),
            ),
        )

    def test_loads_multiple_delete_paths_in_deterministic_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self.write_manifest(
                Path(tmp_dir),
                "\n".join(
                    [
                        "version: 1",
                        "target: codex",
                        "mode: overlay",
                        "delete:",
                        "  scripts/z.py: true",
                        "  references/a.md: true",
                        "",
                    ]
                ),
            )

            manifest = load_variant_manifest(manifest_path)

        self.assertEqual(
            manifest.delete,
            ("references/a.md", "scripts/z.py"),
        )

    def test_requires_exact_schema_and_supported_values(self):
        cases = {
            "missing version": "target: codex\nmode: overlay\n",
            "missing target": "version: 1\nmode: overlay\n",
            "missing mode": "version: 1\ntarget: codex\n",
            "unknown field": "version: 1\ntarget: codex\nmode: overlay\nscript: run.sh\n",
            "future version": "version: 2\ntarget: codex\nmode: overlay\n",
            "boolean version": "version: true\ntarget: codex\nmode: overlay\n",
            "replace mode": "version: 1\ntarget: codex\nmode: replace\n",
            "unknown target": "version: 1\ntarget: mystery\nmode: overlay\n",
            "delete false": "version: 1\ntarget: codex\nmode: overlay\ndelete:\n  a.md: false\n",
            "nested delete": "version: 1\ntarget: codex\nmode: overlay\ndelete:\n  references:\n    a.md: true\n",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            for index, (name, text) in enumerate(cases.items()):
                with self.subTest(name=name):
                    manifest_path = self.write_manifest(root / str(index), text)
                    with self.assertRaises(ValueError):
                        load_variant_manifest(manifest_path)

    def test_target_must_match_variant_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self.write_manifest(
                Path(tmp_dir),
                "version: 1\ntarget: kimi\nmode: overlay\n",
                target="kimi-desktop",
            )

            with self.assertRaisesRegex(ValueError, "directory"):
                load_variant_manifest(manifest_path)

    def test_rejects_unsafe_or_nonportable_delete_paths(self):
        paths = [
            "../outside.md",
            "references/../outside.md",
            "/absolute.md",
            "C:/absolute.md",
            r"C:\absolute.md",
            r"\\server\share\file.md",
            r"references\windows.md",
            "./references/a.md",
            "references//a.md",
            "references/a.md/",
            "variant.yaml",
            "references/name:stream.md",
            "references/escape\x1b.md",
            "references/tab\tname.md",
            "references/delete\x7f.md",
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            for index, value in enumerate(paths):
                with self.subTest(value=value):
                    manifest_path = self.write_manifest(
                        root / str(index),
                        f"version: 1\ntarget: codex\nmode: overlay\ndelete: {value}\n",
                    )
                    with self.assertRaises(ValueError):
                        load_variant_manifest(manifest_path)

    def test_rejects_casefold_duplicate_delete_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self.write_manifest(
                Path(tmp_dir),
                "version: 1\ntarget: codex\nmode: overlay\ndelete:\n  A.md: true\n  a.md: true\n",
            )

            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_variant_manifest(manifest_path)

    def test_rejects_manifest_with_wrong_name_or_non_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "codex"
            root.mkdir()
            wrong = root / "other.yaml"
            wrong.write_text("version: 1\ntarget: codex\nmode: overlay\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_variant_manifest(wrong)

            directory = root / "variant.yaml"
            directory.mkdir()
            with self.assertRaises(ValueError):
                load_variant_manifest(directory)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_manifest_and_variant_content_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            variant_root = root / "manifest-link" / "codex"
            variant_root.mkdir(parents=True)
            real = variant_root / "real.yaml"
            real.write_text("version: 1\ntarget: codex\nmode: overlay\n", encoding="utf-8")
            linked = variant_root / "variant.yaml"
            try:
                linked.symlink_to(real.name)
            except OSError as exc:
                self.skipTest(f"cannot create symlink: {exc}")
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                load_variant_manifest(linked)

            content_manifest = self.write_manifest(
                root / "content-link",
                "version: 1\ntarget: codex\nmode: overlay\n",
            )
            target = content_manifest.parent / "real.md"
            target.write_text("real", encoding="utf-8")
            (content_manifest.parent / "linked.md").symlink_to(target.name)
            with self.assertRaisesRegex(ValueError, "link|reparse"):
                load_variant_manifest(content_manifest)

    def test_read_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self.write_manifest(
                Path(tmp_dir),
                "version: 1\ntarget: codex\nmode: overlay\n",
            )
            before = {
                path.relative_to(manifest_path.parent): (
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in manifest_path.parent.rglob("*")
            }

            load_variant_manifest(manifest_path)

            after = {
                path.relative_to(manifest_path.parent): (
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in manifest_path.parent.rglob("*")
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
