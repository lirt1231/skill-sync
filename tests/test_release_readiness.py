import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseReadinessTest(unittest.TestCase):
    def test_public_repository_metadata_and_community_files_exist(self):
        for relative in (
            "README.md",
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CHANGELOG.md",
            "docs/RELEASING.md",
            ".github/workflows/ci.yml",
            ".github/dependabot.yml",
            ".github/ISSUE_TEMPLATE/bug.yml",
            ".github/ISSUE_TEMPLATE/feature.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
        ):
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_installation_docs_use_the_public_tool_repository(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("github.com/lirt1231/skill-sync.git", readme)
        self.assertNotIn("github.com/YOUR_NAME/skill-sync", readme)
        self.assertIn("private Git repository", readme)
        self.assertIn("Technical preview", readme)

    def test_package_metadata_links_to_public_project_pages(self):
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('[project.urls]', metadata)
        self.assertIn('Homepage = "https://github.com/lirt1231/skill-sync"', metadata)
        self.assertIn('Development Status :: 3 - Alpha', metadata)

    def test_mit_license_matches_package_metadata(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 lijiaming", license_text)
        self.assertIn('license = { file = "LICENSE" }', metadata)
        self.assertIn('License :: OSI Approved :: MIT License', metadata)
        self.assertIn("[MIT License](LICENSE)", readme)

    def test_public_files_do_not_contain_local_home_paths(self):
        for relative in (
            "README.md",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CHANGELOG.md",
            "docs/RELEASING.md",
        ):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("/Users/", text)


if __name__ == "__main__":
    unittest.main()
