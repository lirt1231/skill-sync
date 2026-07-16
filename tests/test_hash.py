import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.hash import (
    hash_skill_dir,
    hash_skill_files_with_modes,
    portable_skill_file_mode,
)


def write_file(root: Path, relative_path: str, data: bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def expected_hash(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(entries):
        path_bytes = relative_path.encode("utf-8")
        content = entries[relative_path]
        digest.update(b"file\0")
        digest.update(struct.pack(">Q", len(path_bytes)))
        digest.update(path_bytes)
        digest.update(struct.pack(">Q", len(content)))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def old_delimiter_hash(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(entries):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entries[relative_path])
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class HashSkillDirTest(unittest.TestCase):
    def test_mode_aware_hash_uses_portable_content_semantics(self):
        content = (("SKILL.md", b"# Example\n", 0o644),)

        self.assertEqual(portable_skill_file_mode(b"# Example\n"), 0o644)
        self.assertEqual(portable_skill_file_mode(b"#!/bin/sh\nexit 0\n"), 0o755)
        self.assertEqual(hash_skill_files_with_modes(content), hash_skill_files_with_modes(content))
        with self.assertRaisesRegex(ValueError, "portable content mode"):
            hash_skill_files_with_modes((("SKILL.md", b"# Example\n", 0o755),))

    def test_hash_is_deterministic_regardless_of_file_creation_order(self):
        entries = {
            "SKILL.md": b"# Example\n",
            "references/guide.md": b"Use carefully.\n",
            "scripts/tool.py": b"print('hello')\n",
        }

        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)

            for relative_path, data in entries.items():
                write_file(first, relative_path, data)
            for relative_path, data in reversed(entries.items()):
                write_file(second, relative_path, data)

            self.assertEqual(hash_skill_dir(first), hash_skill_dir(second))
            self.assertEqual(hash_skill_dir(first), expected_hash(entries))

    def test_hash_ignores_generated_noise_and_empty_directories(self):
        entries = {
            "SKILL.md": b"# Example\n",
            "nested/keep.txt": b"keep me\n",
        }

        with tempfile.TemporaryDirectory() as clean_dir, tempfile.TemporaryDirectory() as noisy_dir:
            clean = Path(clean_dir)
            noisy = Path(noisy_dir)

            for relative_path, data in entries.items():
                write_file(clean, relative_path, data)
                write_file(noisy, relative_path, data)

            write_file(noisy, ".DS_Store", b"finder metadata")
            write_file(noisy, "nested/.DS_Store", b"nested finder metadata")
            write_file(noisy, "__pycache__/module.cpython-312.pyc", b"compiled")
            write_file(noisy, "nested/__pycache__/module.pyc", b"compiled")
            write_file(noisy, ".git/config", b"[core]\nrepositoryformatversion = 0\n")
            (noisy / "empty").mkdir()

            self.assertEqual(hash_skill_dir(clean), hash_skill_dir(noisy))

    def test_hash_includes_regular_files_named_like_ignored_directories(self):
        entries = {
            "SKILL.md": b"# Example\n",
            ".git": b"not a directory\n",
            "__pycache__": b"also not a directory\n",
            "nested/.git": b"nested file\n",
            "nested/__pycache__": b"nested cache-named file\n",
        }

        with tempfile.TemporaryDirectory() as skill_dir:
            root = Path(skill_dir)
            for relative_path, data in entries.items():
                write_file(root, relative_path, data)

            self.assertEqual(hash_skill_dir(root), expected_hash(entries))

    def test_hash_includes_binary_file_bytes_exactly(self):
        entries = {
            "SKILL.md": b"# Example\n",
            "data/blob.bin": bytes([0, 1, 2, 10, 13, 255]),
        }

        with tempfile.TemporaryDirectory() as skill_dir:
            root = Path(skill_dir)
            for relative_path, data in entries.items():
                write_file(root, relative_path, data)

            self.assertEqual(hash_skill_dir(root), expected_hash(entries))

            write_file(root, "data/blob.bin", bytes([0, 1, 2, 10, 13, 254]))
            self.assertNotEqual(hash_skill_dir(root), expected_hash(entries))

    def test_hash_uses_length_prefixes_to_avoid_delimiter_framing_collisions(self):
        first_entries = {
            "SKILL.md": b"# Example\n",
            "a": b"x\0b\0",
        }
        second_entries = {
            "SKILL.md": b"# Example\n",
            "a": b"x",
            "b": b"",
        }
        self.assertEqual(old_delimiter_hash(first_entries), old_delimiter_hash(second_entries))

        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            for relative_path, data in first_entries.items():
                write_file(first, relative_path, data)
            for relative_path, data in second_entries.items():
                write_file(second, relative_path, data)

            self.assertEqual(hash_skill_dir(first), expected_hash(first_entries))
            self.assertEqual(hash_skill_dir(second), expected_hash(second_entries))
            self.assertNotEqual(hash_skill_dir(first), hash_skill_dir(second))

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks are unsupported on this platform")
    def test_hash_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as skill_dir:
            root = Path(skill_dir)
            write_file(root, "SKILL.md", b"# Example\n")
            os.symlink(root / "SKILL.md", root / "linked.md")

            with self.assertRaisesRegex(ValueError, "symlink.*linked.md"):
                hash_skill_dir(root)

    def test_hash_rejects_reparse_point_at_skill_root(self):
        with tempfile.TemporaryDirectory() as skill_dir:
            root = Path(skill_dir)
            write_file(root, "SKILL.md", b"# Example\n")
            with mock.patch(
                "skill_sync.hash.is_link_or_reparse", return_value=True
            ):
                with self.assertRaisesRegex(ValueError, "reparse point"):
                    hash_skill_dir(root)


if __name__ == "__main__":
    unittest.main()
