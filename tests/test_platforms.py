import tempfile
import unittest
from pathlib import Path

from skill_sync.platforms import CodexAdapter, SkillCandidate, get_adapter


class PlatformAdapterTest(unittest.TestCase):
    def test_codex_default_skill_dir_uses_codex_home_when_set(self):
        path = CodexAdapter.default_skill_dir(
            env={"CODEX_HOME": "/tmp/custom-codex"},
            home=Path("/home/example"),
        )

        self.assertEqual(path, Path("/tmp/custom-codex") / "skills")

    def test_codex_default_skill_dir_falls_back_to_home_codex_skills(self):
        path = CodexAdapter.default_skill_dir(env={}, home=Path("/home/example"))

        self.assertEqual(path, Path("/home/example/.codex/skills"))

    def test_discover_returns_skill_directories_in_name_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            skill_dir = Path(tmp_dir) / "skills"
            self._write_skill(skill_dir / "z-skill")
            self._write_skill(skill_dir / "a-skill")
            (skill_dir / "not-a-skill").mkdir()

            self.assertEqual(
                CodexAdapter.discover(
                    skill_dir=skill_dir,
                    env={"CODEX_HOME": tmp_dir},
                ),
                [
                    SkillCandidate(
                        name="a-skill",
                        path=skill_dir / "a-skill",
                        selected=False,
                        external=False,
                    ),
                    SkillCandidate(
                        name="z-skill",
                        path=skill_dir / "z-skill",
                        selected=False,
                        external=False,
                    ),
                ],
            )

    def test_discover_marks_selected_names(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            skill_dir = Path(tmp_dir) / "skills"
            self._write_skill(skill_dir / "selected-skill")
            self._write_skill(skill_dir / "other-skill")

            candidates = CodexAdapter.discover(
                skill_dir=skill_dir,
                selected_names={"selected-skill"},
            )

            self.assertEqual(
                {candidate.name: candidate.selected for candidate in candidates},
                {"other-skill": False, "selected-skill": True},
            )

    def test_discover_marks_external_when_input_is_not_default_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = Path(tmp_dir) / "home"
            default_skill_dir = home / ".codex" / "skills"
            external_skill_dir = Path(tmp_dir) / "external-skills"
            self._write_skill(default_skill_dir / "default-skill")
            self._write_skill(external_skill_dir / "external-skill")

            default_candidates = CodexAdapter.discover(
                skill_dir=default_skill_dir,
                env={},
                home=home,
            )
            external_candidates = CodexAdapter.discover(
                skill_dir=external_skill_dir,
                env={},
                home=home,
            )

            self.assertEqual(default_candidates[0].external, False)
            self.assertEqual(external_candidates[0].external, True)

    def test_get_adapter_returns_codex_adapter(self):
        self.assertIsInstance(get_adapter("codex"), CodexAdapter)

    def test_get_adapter_rejects_unknown_platform(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            get_adapter("unknown")

    def _write_skill(self, path: Path) -> None:
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"# {path.name}\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
