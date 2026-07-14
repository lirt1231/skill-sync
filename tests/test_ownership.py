import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_sync.agents import AgentClient, AgentTarget
from skill_sync.ownership import OwnershipResult, inspect_ownership


def make_skill(skills_root: Path, name: str) -> Path:
    skill = skills_root / name
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (skill / "scripts" / "run.py").write_text("pass\n", encoding="utf-8")
    return skill


class OwnershipInspectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.skills_root = self.root / "global" / "skills"
        self.alpha = make_skill(self.skills_root, "alpha")
        self.codex_root = self.root / "codex" / "skills"
        self.workbuddy_root = self.root / "workbuddy" / "skills"
        self.clients = (
            AgentClient("codex", "codex", "Codex", self.codex_root, True),
            AgentClient(
                "workbuddy",
                "workbuddy",
                "WorkBuddy",
                self.workbuddy_root,
                True,
            ),
        )

    def inspect(self, input_path: str | Path, **kwargs: object) -> OwnershipResult:
        return inspect_ownership(
            input_path,
            skills_root=self.skills_root,
            selected_skills={"alpha"},
            clients=self.clients,
            **kwargs,
        )

    def test_canonical_root_skill_file_and_descendant_are_managed_sources(self):
        for input_path in (
            self.alpha,
            self.alpha / "SKILL.md",
            self.alpha / "scripts" / "run.py",
        ):
            with self.subTest(input_path=input_path):
                result = self.inspect(input_path)
                self.assertTrue(result.managed)
                self.assertTrue(result.healthy)
                self.assertEqual(result.state, "managed-source")
                self.assertEqual(result.role, "source")
                self.assertEqual(result.skill, "alpha")
                self.assertEqual(result.source_path, str(self.alpha))
                self.assertIsNone(result.client)
                self.assertTrue(result.migration_required)

    def test_correct_agent_link_is_direct_source_link(self):
        destination = self.codex_root / "alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.alpha, target_is_directory=True)

        result = self.inspect(destination / "SKILL.md")

        self.assertEqual(
            result.to_dict(),
            {
                "managed": True,
                "healthy": True,
                "state": "managed-source",
                "role": "direct-source-link",
                "skill": "alpha",
                "input_path": str(destination / "SKILL.md"),
                "source_path": str(self.alpha),
                "client": "codex",
                "migration_required": True,
            },
        )

    def test_wrong_link_remains_managed_but_is_unhealthy(self):
        other = make_skill(self.skills_root, "other")
        destination = self.codex_root / "alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(other, target_is_directory=True)

        result = self.inspect(destination / "scripts" / "run.py")

        self.assertTrue(result.managed)
        self.assertFalse(result.healthy)
        self.assertEqual(result.state, "wrong-link")
        self.assertEqual(result.role, "direct-source-link")
        self.assertEqual(result.source_path, str(self.alpha))
        self.assertEqual(result.client, "codex")

    def test_broken_link_remains_managed_but_is_unhealthy(self):
        destination = self.codex_root / "alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.root / "missing", target_is_directory=True)

        result = self.inspect(destination / "SKILL.md")

        self.assertTrue(result.managed)
        self.assertFalse(result.healthy)
        self.assertEqual(result.state, "broken-link")
        self.assertEqual(result.skill, "alpha")

    def test_real_directory_at_expected_agent_path_is_unmanaged(self):
        destination = self.codex_root / "alpha"
        make_skill(self.codex_root, "alpha")

        result = self.inspect(destination / "SKILL.md")

        self.assertFalse(result.managed)
        self.assertTrue(result.healthy)
        self.assertEqual(result.state, "unmanaged")
        self.assertEqual(result.role, "unmanaged")
        self.assertEqual(result.skill, "alpha")
        self.assertEqual(result.client, "codex")

    def test_same_named_project_skill_is_not_mistaken_for_managed_skill(self):
        project_skill = make_skill(self.root / "project" / ".agents" / "skills", "alpha")

        result = self.inspect(project_skill / "SKILL.md")

        self.assertFalse(result.managed)
        self.assertTrue(result.healthy)
        self.assertEqual(result.state, "unmanaged")
        self.assertIsNone(result.skill)
        self.assertIsNone(result.source_path)

    def test_registry_selection_and_target_intent_are_honored(self):
        destination = self.workbuddy_root / "alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.alpha, target_is_directory=True)
        registry = {
            "version": 1,
            "skills": {"alpha": {"selected": True, "targets": "codex"}},
        }

        result = inspect_ownership(
            destination,
            skills_root=self.skills_root,
            registry=registry,
            clients=self.clients,
        )

        self.assertFalse(result.managed)
        self.assertEqual(result.state, "unmanaged")
        self.assertEqual(result.client, "workbuddy")

    def test_unselected_canonical_skill_is_unmanaged(self):
        beta = make_skill(self.skills_root, "beta")

        result = self.inspect(beta / "SKILL.md")

        self.assertFalse(result.managed)
        self.assertEqual(result.state, "unmanaged")
        self.assertEqual(result.skill, "beta")

    def test_windows_junction_is_recognized_with_samefile(self):
        destination = self.codex_root / "alpha"
        destination.mkdir(parents=True)

        with mock.patch("skill_sync.ownership._same_file", return_value=True):
            result = self.inspect(destination / "SKILL.md")

        self.assertTrue(result.managed)
        self.assertTrue(result.healthy)
        self.assertEqual(result.role, "direct-source-link")
        self.assertEqual(result.client, "codex")

    def test_legacy_target_extra_directories_are_inspected(self):
        primary = self.root / "kimi-code" / "skills"
        desktop = self.root / "kimi-desktop" / "skills"
        destination = desktop / "alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.alpha, target_is_directory=True)
        target = AgentTarget("kimi", "Kimi", primary, True, (desktop,))

        result = inspect_ownership(
            destination,
            skills_root=self.skills_root,
            selected_skills={"alpha"},
            targets=(target,),
        )

        self.assertTrue(result.managed)
        self.assertEqual(result.client, "kimi")

    def test_client_hint_resolves_overlapping_agent_roots(self):
        shared_root = self.root / "shared" / "skills"
        destination = shared_root / "alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.alpha, target_is_directory=True)
        overlapping = (
            AgentClient("first", "family-one", "First", shared_root, True),
            AgentClient("second", "family-two", "Second", shared_root, True),
        )

        ambiguous = inspect_ownership(
            destination,
            skills_root=self.skills_root,
            selected_skills={"alpha"},
            clients=overlapping,
        )
        resolved = inspect_ownership(
            destination,
            skills_root=self.skills_root,
            selected_skills={"alpha"},
            clients=overlapping,
            client="second",
        )

        self.assertEqual(ambiguous.state, "ambiguous")
        self.assertFalse(ambiguous.healthy)
        self.assertTrue(resolved.managed)
        self.assertEqual(resolved.client, "second")

    def test_selected_name_input_resolves_to_canonical_skill(self):
        result = self.inspect("alpha")

        self.assertTrue(result.managed)
        self.assertEqual(result.input_path, "alpha")
        self.assertEqual(result.source_path, str(self.alpha))

    def test_missing_and_unknown_name_inputs_are_ambiguous(self):
        for input_path in (self.root / "does-not-exist", "unknown"):
            with self.subTest(input_path=input_path):
                result = self.inspect(input_path)
                self.assertFalse(result.managed)
                self.assertFalse(result.healthy)
                self.assertEqual(result.state, "ambiguous")
                self.assertEqual(result.role, "unknown")


if __name__ == "__main__":
    unittest.main()
