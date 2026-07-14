import tempfile
import unittest
from pathlib import Path

from skill_sync.skill_metadata import read_skill_description


class SkillMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.skill = Path(self.tmp.name) / "example"
        self.skill.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_skill(self, text: str) -> None:
        (self.skill / "SKILL.md").write_text(text, encoding="utf-8")

    def test_reads_plain_and_quoted_descriptions(self):
        cases = (
            ("A plain description", "A plain description"),
            ('"A quoted description: with colon"', "A quoted description: with colon"),
            ("'It''s quoted'", "It's quoted"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.write_skill(f"---\nname: example\ndescription: {source}\n---\n# Example\n")
                self.assertEqual(read_skill_description(self.skill), expected)

    def test_reads_folded_and_literal_block_descriptions(self):
        self.write_skill(
            "---\nname: example\ndescription: >\n  First line\n  second line\n\n  Next paragraph\n---\n"
        )
        self.assertEqual(
            read_skill_description(self.skill / "SKILL.md"),
            "First line second line\nNext paragraph",
        )

        self.write_skill(
            "---\nname: example\ndescription: |-\n  First line\n  second line\n---\n"
        )
        self.assertEqual(read_skill_description(self.skill), "First line\nsecond line")

    def test_missing_or_malformed_metadata_returns_empty_string(self):
        cases = (
            "# No frontmatter\n",
            "---\ndescription: never closed\n",
            "---\ndescription: \"unterminated\n---\n",
            "---\nname: example\n---\n",
        )
        for source in cases:
            with self.subTest(source=source):
                self.write_skill(source)
                self.assertEqual(read_skill_description(self.skill), "")
        self.assertEqual(read_skill_description(self.skill.parent / "missing"), "")
