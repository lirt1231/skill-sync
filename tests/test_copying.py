import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.copying import copy_skill_dir
from skill_sync.hash import hash_skill_dir


def write_file(root: Path, relative_path: str, data: bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_file(root: Path, relative_path: str) -> bytes:
    return (root / relative_path).read_bytes()


class CopySkillDirTest(unittest.TestCase):
    def test_copies_hidden_files_and_matches_source_hash(self):
        with tempfile.TemporaryDirectory() as work_dir:
            work = Path(work_dir)
            source = work / "source"
            destination = work / "destination"
            write_file(source, "SKILL.md", b"# Skill\n")
            write_file(source, ".skill-metadata", b"hidden top-level file\n")
            write_file(source, "references/.hidden-guide.md", b"hidden nested file\n")

            result_hash = copy_skill_dir(source, destination)

            self.assertEqual(read_file(destination, ".skill-metadata"), b"hidden top-level file\n")
            self.assertEqual(
                read_file(destination, "references/.hidden-guide.md"),
                b"hidden nested file\n",
            )
            self.assertEqual(result_hash, hash_skill_dir(source))
            self.assertEqual(hash_skill_dir(destination), hash_skill_dir(source))

    def test_excludes_generated_noise_when_copying(self):
        with tempfile.TemporaryDirectory() as work_dir:
            work = Path(work_dir)
            source = work / "source"
            destination = work / "destination"
            write_file(source, "SKILL.md", b"# Skill\n")
            write_file(source, ".DS_Store", b"finder")
            write_file(source, "nested/.DS_Store", b"nested finder")
            write_file(source, "__pycache__/module.pyc", b"compiled")
            write_file(source, "nested/__pycache__/module.pyc", b"nested compiled")
            write_file(source, ".git/config", b"[core]\n")

            copy_skill_dir(source, destination)

            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertFalse((destination / ".DS_Store").exists())
            self.assertFalse((destination / "nested" / ".DS_Store").exists())
            self.assertFalse((destination / "__pycache__").exists())
            self.assertFalse((destination / "nested" / "__pycache__").exists())
            self.assertFalse((destination / ".git").exists())

    def test_replaces_existing_destination_and_removes_backup_after_hash_match(self):
        with tempfile.TemporaryDirectory() as work_dir:
            work = Path(work_dir)
            source = work / "source"
            destination = work / "destination"
            write_file(source, "SKILL.md", b"# New Skill\n")
            write_file(source, "references/guide.md", b"new guide\n")
            write_file(destination, "SKILL.md", b"# Old Skill\n")
            write_file(destination, "old-only.md", b"remove me\n")

            result_hash = copy_skill_dir(source, destination)

            self.assertEqual(read_file(destination, "SKILL.md"), b"# New Skill\n")
            self.assertEqual(read_file(destination, "references/guide.md"), b"new guide\n")
            self.assertFalse((destination / "old-only.md").exists())
            self.assertEqual(result_hash, hash_skill_dir(source))
            self.assertEqual(hash_skill_dir(destination), hash_skill_dir(source))
            self.assertEqual(
                [],
                [
                    path
                    for path in work.iterdir()
                    if path.name.startswith(".destination.backup-")
                ],
            )

    def test_restores_existing_destination_when_replacement_fails(self):
        with tempfile.TemporaryDirectory() as work_dir:
            work = Path(work_dir)
            source = work / "source"
            destination = work / "destination"
            write_file(source, "SKILL.md", b"# New Skill\n")
            write_file(destination, "SKILL.md", b"# Old Skill\n")
            write_file(destination, "old-only.md", b"still here\n")

            import skill_sync.copying as copying

            original_replace = copying.os.replace

            def fail_install_temp(src, dst):
                src_path = Path(src)
                dst_path = Path(dst)
                if (
                    dst_path == destination
                    and src_path.name == destination.name
                    and src_path.parent.name.startswith(".destination.tmp-")
                ):
                    raise OSError("forced replacement failure")
                return original_replace(src, dst)

            with mock.patch.object(copying.os, "replace", side_effect=fail_install_temp):
                with self.assertRaisesRegex(OSError, "forced replacement failure"):
                    copy_skill_dir(source, destination)

            self.assertEqual(read_file(destination, "SKILL.md"), b"# Old Skill\n")
            self.assertEqual(read_file(destination, "old-only.md"), b"still here\n")
            self.assertFalse(any(path.name.startswith(".destination.backup-") for path in work.iterdir()))

    def test_restores_existing_destination_when_final_hash_mismatches(self):
        with tempfile.TemporaryDirectory() as work_dir:
            work = Path(work_dir)
            source = work / "source"
            destination = work / "destination"
            write_file(source, "SKILL.md", b"# New Skill\n")
            write_file(destination, "SKILL.md", b"# Old Skill\n")
            write_file(destination, "old-only.md", b"still here\n")

            import skill_sync.copying as copying

            expected_hash = "sha256:expected"
            bad_hash = "sha256:bad"
            with mock.patch.object(
                copying,
                "hash_skill_dir",
                side_effect=[expected_hash, expected_hash, bad_hash],
            ):
                with self.assertRaisesRegex(ValueError, "final copy hash mismatch"):
                    copy_skill_dir(source, destination)

            self.assertEqual(read_file(destination, "SKILL.md"), b"# Old Skill\n")
            self.assertEqual(read_file(destination, "old-only.md"), b"still here\n")
            self.assertFalse(any(path.name.startswith(".destination.backup-") for path in work.iterdir()))

    def test_removes_new_destination_when_final_hash_mismatches_without_backup(self):
        with tempfile.TemporaryDirectory() as work_dir:
            source = Path(work_dir) / "source"
            destination = Path(work_dir) / "destination"
            write_file(source, "SKILL.md", b"# New Skill\n")

            import skill_sync.copying as copying

            expected_hash = "sha256:expected"
            bad_hash = "sha256:bad"
            with mock.patch.object(
                copying,
                "hash_skill_dir",
                side_effect=[expected_hash, expected_hash, bad_hash],
            ):
                with self.assertRaisesRegex(ValueError, "final copy hash mismatch"):
                    copy_skill_dir(source, destination)

            self.assertFalse(destination.exists())

    def test_rejects_destination_equal_to_source_before_creating_temp_dir(self):
        with tempfile.TemporaryDirectory() as work_dir:
            source = Path(work_dir) / "source"
            write_file(source, "SKILL.md", b"# Skill\n")

            with self.assertRaisesRegex(ValueError, "destination.*source"):
                copy_skill_dir(source, source)

            self.assertEqual(["SKILL.md"], [path.name for path in source.iterdir()])

    def test_rejects_destination_inside_source_before_creating_temp_dir(self):
        with tempfile.TemporaryDirectory() as work_dir:
            source = Path(work_dir) / "source"
            destination = source / "nested" / "destination"
            write_file(source, "SKILL.md", b"# Skill\n")

            with self.assertRaisesRegex(ValueError, "destination.*source"):
                copy_skill_dir(source, destination)

            self.assertFalse((source / "nested").exists())

    def test_rejects_invalid_source(self):
        with tempfile.TemporaryDirectory() as work_dir:
            work = Path(work_dir)

            with self.assertRaisesRegex(ValueError, "source.*directory"):
                copy_skill_dir(work / "missing", work / "destination")


if __name__ == "__main__":
    unittest.main()
