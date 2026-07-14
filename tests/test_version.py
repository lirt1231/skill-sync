import tomllib
import unittest
from pathlib import Path

import skill_sync
from skill_sync.version import __version__


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class VersionTest(unittest.TestCase):
    def test_package_reexports_canonical_runtime_version(self):
        self.assertEqual(skill_sync.__version__, __version__)

    def test_build_metadata_reads_canonical_runtime_version(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)

        self.assertNotIn("version", project["project"])
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "skill_sync.version.__version__",
        )

    def test_version_is_pep_440_compatible_for_current_release_scheme(self):
        parts = __version__.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.isdigit() for part in parts))


if __name__ == "__main__":
    unittest.main()
