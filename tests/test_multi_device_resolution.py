import json
import shutil
import tempfile
import unittest
from pathlib import Path

from skill_sync.agents import AgentClient
from skill_sync.config import load_config, save_config
from skill_sync.core import deploy_migrate, init_sync, pull, push, select_skills
from skill_sync.git import run_git
from skill_sync.variant_resolution import resolve_variant_for_client
from skill_sync.variant_source import create_variant
from tests.test_core import configure_identity, create_remote_with_registry, make_skill, write_file


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "multi-device-resolution"
    / "matrix.json"
)


@unittest.skipIf(shutil.which("git") is None, "git executable is not available")
class MultiDeviceResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        _, self.remote = create_remote_with_registry(self.work)
        self.first_config = self.work / "machine-a" / "config.json"
        self.first_repo = self.work / "machine-a" / "sync"
        self.first_skills = self.work / "machine-a" / "portable" / "skills"
        self.second_config = self.work / "machine-b" / "config.json"
        self.second_repo = self.work / "machine-b" / "sync"
        self.second_skills = self.work / "machine-b" / "portable" / "skills"
        self._init_machine(
            self.first_config,
            self.first_repo,
            self.first_skills,
            "machine-a",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _init_machine(
        self,
        config_path: Path,
        repo_path: Path,
        skills_root: Path,
        name: str,
    ) -> None:
        init_sync(
            str(self.remote),
            sync_dir=repo_path,
            platform=None,
            skills_root=skills_root,
            config_path=config_path,
        )
        configure_identity(repo_path)
        config = load_config(config_path)
        config["data_root"] = str(self.work / name / "state")
        save_config(config_path, config)

    def _author_first_machine(self) -> None:
        for name in ("base-shared", "kimi-shared", "codex-client"):
            make_skill(self.first_skills, name, f"# {name}\n")
            select_skills(
                [name],
                platform=None,
                config_path=self.first_config,
                skill_dir=self.first_skills,
            )
        kimi = create_variant(
            "kimi-shared",
            scope="family",
            target="kimi",
            config_path=self.first_config,
        )
        write_file(Path(kimi["path"]), "family.txt", "kimi family\n")
        codex = create_variant(
            "codex-client",
            scope="client",
            target="codex",
            config_path=self.first_config,
        )
        write_file(Path(codex["path"]), "client.txt", "codex client\n")
        push(config_path=self.first_config, message="publish multi-device fixture")

    def test_git_sources_reproduce_resolution_matrix_and_local_deployments(self) -> None:
        self._author_first_machine()
        self._init_machine(
            self.second_config,
            self.second_repo,
            self.second_skills,
            "machine-b",
        )
        pull(config_path=self.second_config)
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                first = resolve_variant_for_client(
                    self.first_skills / case["skill"],
                    self.first_skills.parent / "variants" / case["skill"],
                    case["machine_a_client"],
                )
                second = resolve_variant_for_client(
                    self.second_skills / case["skill"],
                    self.second_skills.parent / "variants" / case["skill"],
                    case["machine_b_client"],
                )
                self.assertEqual(
                    list(first.applied_variant_targets),
                    case["applied_targets"],
                )
                self.assertEqual(
                    list(second.applied_variant_targets),
                    case["applied_targets"],
                )
                self.assertEqual(
                    first.output_hash == second.output_hash,
                    case["same_output"],
                )
                self.assertEqual(
                    first.resolution_hash == second.resolution_hash,
                    case["same_resolution"],
                )
                self.assertEqual(
                    [layer.content_hash for layer in first.layers],
                    [layer.content_hash for layer in second.layers],
                )
                self.assertNotEqual(
                    first.layers[0].source_path,
                    second.layers[0].source_path,
                )

        machine_a_clients = (
            self._client("codex", "codex", "Codex", "machine-a"),
            self._client(
                "kimi-code",
                "kimi",
                "Kimi Code",
                "machine-a",
            ),
        )
        machine_b_clients = (
            self._client("workbuddy", "workbuddy", "WorkBuddy", "machine-b"),
            self._client("kimi-code", "kimi", "Kimi Code", "machine-b"),
        )
        first_deploy = deploy_migrate(
            config_path=self.first_config,
            _clients=machine_a_clients,
        )
        second_deploy = deploy_migrate(
            config_path=self.second_config,
            _clients=machine_b_clients,
        )
        self.assertEqual(
            {row["client"] for row in first_deploy["deployments"]},
            {"codex", "kimi-code"},
        )
        self.assertEqual(
            {row["client"] for row in second_deploy["deployments"]},
            {"workbuddy", "kimi-code"},
        )

        tracked = run_git(self.first_repo, ["ls-files"])
        self.assertNotIn(str(self.work / "machine-a"), tracked)
        self.assertNotIn(str(self.work / "machine-b"), tracked)

    def _client(
        self,
        client_id: str,
        family_id: str,
        display_name: str,
        machine: str,
    ) -> AgentClient:
        return AgentClient(
            client_id,
            family_id,
            display_name,
            self.work / machine / "agents" / client_id / "skills",
            True,
        )


if __name__ == "__main__":
    unittest.main()
