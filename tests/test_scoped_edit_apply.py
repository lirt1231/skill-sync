import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_sync.core as core_module
import skill_sync.edit_apply as edit_apply_module
from skill_sync.agents import AgentClient
from skill_sync.config import empty_config, save_config
from skill_sync.core import (
    deploy_migrate,
    deploy_preview,
    deploy_status,
    edit_apply,
    edit_begin,
    edit_impact,
)
from skill_sync.deployment import render_base_deployment, verify_deployment
from skill_sync.edit_session import EditSessionStatus, EditSessionStore
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.linking import create_directory_link
from skill_sync.registry import load_registry, save_registry


SKILL_TEXT = "---\nname: alpha\ndescription: Test Skill\n---\n\n# Alpha\n"


class ScopedEditApplyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.skill = self.skills_root / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        (self.skill / "base.txt").write_text("base\n", encoding="utf-8")
        self.variants_root = self.skills_root.parent / "variants"
        self.data_root = self.root / "data"
        self.config_path = self.root / "config.json"
        config = empty_config()
        config.update(
            {
                "sync_repo_path": str(self.repo),
                "skills_root": str(self.skills_root),
                "data_root": str(self.data_root),
                "skills": {"alpha": {"local_path": str(self.skill)}},
            }
        )
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    "alpha": {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": "alpha",
                    }
                },
            },
        )
        self.clients = [
            AgentClient(
                client_id,
                family,
                client_id,
                self.root / "clients" / client_id / "skills",
                True,
            )
            for client_id, family in (
                ("codex", "codex"),
                ("workbuddy", "workbuddy"),
                ("kimi-code", "kimi"),
                ("claude-code", "claude"),
            )
        ]

    def make_variant(self, target: str) -> Path:
        variant = self.variants_root / "alpha" / target
        variant.mkdir(parents=True)
        (variant / "variant.yaml").write_text(
            f"version: 1\ntarget: {target}\nmode: overlay\n",
            encoding="utf-8",
        )
        return variant

    def install_base_links(self) -> dict[str, Path]:
        targets = {}
        for client in self.clients:
            deployed = render_base_deployment(
                self.skill,
                self.data_root / "rendered",
                "alpha",
                client.id,
            )
            create_directory_link(deployed.path, client.skills_dir / "alpha")
            targets[client.id] = deployed.path
        return targets

    def receipt(self) -> dict:
        paths = sorted((self.data_root / "operations").glob("edit-apply-*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_family_apply_replaces_only_variant_and_kimi_code_link(self):
        variant = self.make_variant("kimi")
        (variant / "family.txt").write_text("old\n", encoding="utf-8")
        old_variant_hash = hash_skill_dir(variant)
        base_hash = hash_skill_dir(self.skill)
        old_targets = self.install_base_links()
        session = edit_begin(
            "alpha",
            scope="family",
            target="kimi",
            actor="kimi-code",
            config_path=self.config_path,
        )
        workspace = Path(session["workspace_path"])
        (workspace / "family.txt").write_text("new family\n", encoding="utf-8")
        workspace_hash = hash_skill_dir(workspace)

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ), mock.patch.object(
            core_module.git,
            "run_git",
            side_effect=AssertionError("scoped apply must not access Git"),
        ):
            result = edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(result["scope"], "family")
        self.assertEqual(result["target"], "kimi")
        self.assertTrue(result["registry_updated"])
        self.assertEqual(result["registry_version"], 3)
        registry = load_registry(self.repo / "registry.yaml")
        self.assertEqual(registry["version"], 3)
        self.assertEqual(registry["skills"]["alpha"]["variants"], "kimi")
        self.assertEqual(result["applied_hash"], workspace_hash)
        self.assertEqual(hash_skill_dir(self.skill), base_hash)
        self.assertEqual(hash_skill_dir(variant), workspace_hash)
        self.assertEqual(hash_skill_dir(Path(result["backup_path"])), old_variant_hash)
        self.assertEqual(
            {item["client"] for item in result["deployments"]},
            {"kimi-code"},
        )
        for client in self.clients:
            target = (client.skills_dir / "alpha").resolve()
            if client.family_id == "kimi":
                self.assertNotEqual(target, old_targets[client.id].resolve())
                verification = verify_deployment(target)
                self.assertTrue(verification.ok)
                self.assertEqual(verification.provenance["schema_version"], 2)
                self.assertEqual(
                    verification.provenance["applied_layers"],
                    ["base", "family:kimi"],
                )
            else:
                self.assertEqual(target, old_targets[client.id].resolve())
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.APPLIED,
        )
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["scope"], "family")
        self.assertEqual(set(receipt["completed_clients"]), {"kimi-code"})

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ):
            preview = deploy_preview(config_path=self.config_path)
            status = deploy_status(config_path=self.config_path)
        rows = preview["skills"][0]["clients"]
        self.assertEqual({row["action"] for row in rows}, {"noop"})
        self.assertEqual(
            {
                row["client"]: row["resolver_version"]
                for row in rows
                if row["agent"] == "kimi"
            },
            {"kimi-code": "variant-overlay-v2"},
        )
        self.assertFalse(
            any(
                row["migration_required"]
                for row in status["skills"][0]["clients"]
            )
        )

    def test_absent_client_apply_creates_variant_and_changes_only_codex(self):
        old_targets = self.install_base_links()
        canonical = self.variants_root / "alpha" / "codex"
        session = edit_begin(
            "alpha",
            scope="client",
            target="codex",
            actor="codex",
            config_path=self.config_path,
        )
        workspace = Path(session["workspace_path"])
        (workspace / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Codex\n---\n\n# Codex\n",
            encoding="utf-8",
        )

        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            result = edit_apply(session["session_id"], config_path=self.config_path)

        self.assertTrue(canonical.is_dir())
        self.assertIsNone(result["backup_path"])
        self.assertEqual(result["previous_layer"], {"state": "absent", "hash": None})
        self.assertTrue(result["registry_updated"])
        registry = load_registry(self.repo / "registry.yaml")
        self.assertEqual(registry["version"], 3)
        self.assertEqual(registry["skills"]["alpha"]["variants"], "codex")
        self.assertEqual([item["client"] for item in result["deployments"]], ["codex"])
        for client in self.clients:
            target = (client.skills_dir / "alpha").resolve()
            if client.id == "codex":
                self.assertNotEqual(target, old_targets[client.id].resolve())
                self.assertEqual(verify_deployment(target).provenance["schema_version"], 2)
            else:
                self.assertEqual(target, old_targets[client.id].resolve())
        receipt = self.receipt()
        self.assertIsNone(receipt["backup_path"])
        self.assertEqual(receipt["layer_baseline"]["state"], "absent")

    def test_deploy_migrate_uses_family_variant_only_for_kimi_clients(self):
        variant = self.make_variant("kimi")
        (variant / "family.txt").write_text("family\n", encoding="utf-8")
        old_targets = self.install_base_links()

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ):
            result = deploy_migrate(config_path=self.config_path)
            second = deploy_migrate(config_path=self.config_path)

        self.assertEqual(
            {item["client"] for item in result["migrated"]},
            {"kimi-code"},
        )
        self.assertEqual(
            {item["client"] for item in result["deployments"]},
            {"kimi-code"},
        )
        for client in self.clients:
            target = (client.skills_dir / "alpha").resolve()
            if client.family_id == "kimi":
                self.assertNotEqual(target, old_targets[client.id].resolve())
                provenance = verify_deployment(target).provenance
                self.assertEqual(provenance["schema_version"], 2)
                self.assertEqual(
                    provenance["applied_layers"],
                    ["base", "family:kimi"],
                )
            else:
                self.assertEqual(target, old_targets[client.id].resolve())
        self.assertTrue(second["noop"])

    def test_base_apply_preserves_family_variant_in_kimi_outputs(self):
        variant = self.make_variant("kimi")
        (variant / "family.txt").write_text("family\n", encoding="utf-8")
        self.install_base_links()
        session = edit_begin(
            "alpha",
            scope="base",
            actor="codex",
            config_path=self.config_path,
        )
        workspace = Path(session["workspace_path"])
        (workspace / "base.txt").write_text("updated base\n", encoding="utf-8")

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ):
            impact = edit_impact(session["session_id"], config_path=self.config_path)
            result = edit_apply(session["session_id"], config_path=self.config_path)
            preview = deploy_preview(config_path=self.config_path)

        impact_rows = {row["client"]: row for row in impact["clients"]}
        self.assertEqual(
            impact_rows["kimi-code"]["proposed_applied_layers"],
            ["base", "family:kimi"],
        )
        self.assertEqual(
            {item["client"] for item in result["deployments"]},
            {client.id for client in self.clients},
        )
        for client in self.clients:
            target = (client.skills_dir / "alpha").resolve()
            self.assertEqual(
                (target / "base.txt").read_text(encoding="utf-8"),
                "updated base\n",
            )
            provenance = verify_deployment(target).provenance
            if client.family_id == "kimi":
                self.assertEqual(
                    (target / "family.txt").read_text(encoding="utf-8"),
                    "family\n",
                )
                self.assertEqual(provenance["schema_version"], 2)
            else:
                self.assertFalse((target / "family.txt").exists())
                self.assertEqual(provenance["schema_version"], 1)
        self.assertEqual(
            {row["action"] for row in preview["skills"][0]["clients"]},
            {"noop"},
        )

    def test_absent_variant_publish_interruption_is_restart_inspectable(self):
        self.install_base_links()
        canonical = self.variants_root / "alpha" / "codex"
        session = edit_begin(
            "alpha",
            scope="client",
            target="codex",
            config_path=self.config_path,
        )
        workspace = Path(session["workspace_path"])
        (workspace / "codex.txt").write_text("codex\n", encoding="utf-8")
        workspace_hash = hash_skill_dir(workspace)
        original_rename = edit_apply_module.rename_no_replace

        def exit_after_publish(source: Path, destination: Path) -> None:
            source = Path(source)
            destination = Path(destination)
            original_rename(source, destination)
            if (
                source.name.startswith(".codex.apply-stage-")
                and destination == canonical
            ):
                raise SystemExit("simulated process exit after Variant publish")

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ), mock.patch.object(
            edit_apply_module,
            "rename_no_replace",
            side_effect=exit_after_publish,
        ):
            with self.assertRaises(SystemExit):
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(hash_skill_dir(canonical), workspace_hash)
        restarted = EditSessionStore(self.data_root)
        self.assertEqual(
            restarted.load(session["session_id"]).status,
            EditSessionStatus.APPLYING,
        )
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "applying")
        self.assertEqual(receipt["phase"], "canonical-replace")
        self.assertIsNone(receipt["backup_path"])

    def test_stale_variant_is_rejected_before_receipt_or_backup(self):
        variant = self.make_variant("kimi")
        (variant / "family.txt").write_text("old\n", encoding="utf-8")
        session = edit_begin(
            "alpha",
            scope="family",
            target="kimi",
            config_path=self.config_path,
        )
        (Path(session["workspace_path"]) / "family.txt").write_text(
            "proposed\n",
            encoding="utf-8",
        )
        (variant / "family.txt").write_text("concurrent\n", encoding="utf-8")

        with self.assertRaises(SkillSyncError) as raised:
            edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_baseline_conflict")
        self.assertEqual((variant / "family.txt").read_text(), "concurrent\n")
        self.assertFalse((self.data_root / "operations").exists())
        self.assertFalse((self.data_root / "backups" / "edit-apply").exists())

    def test_render_failure_restores_active_session_and_existing_variant(self):
        variant = self.make_variant("kimi")
        (variant / "family.txt").write_text("old\n", encoding="utf-8")
        old_hash = hash_skill_dir(variant)
        session = edit_begin(
            "alpha",
            scope="family",
            target="kimi",
            config_path=self.config_path,
        )
        (Path(session["workspace_path"]) / "family.txt").write_text(
            "new\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=[self.clients[2]],
        ), mock.patch.object(
            core_module,
            "render_layered_deployment",
            side_effect=OSError("render failed"),
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertEqual(hash_skill_dir(variant), old_hash)
        self.assertEqual(load_registry(self.repo / "registry.yaml")["version"], 1)
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.ACTIVE,
        )
        self.assertEqual(self.receipt()["status"], "rolled-back")

    def test_registry_upgrade_failure_restores_absent_variant_and_active_session(self):
        canonical = self.variants_root / "alpha" / "codex"
        session = edit_begin(
            "alpha",
            scope="client",
            target="codex",
            config_path=self.config_path,
        )
        (Path(session["workspace_path"]) / "codex.txt").write_text(
            "codex\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=[],
        ), mock.patch.object(
            core_module,
            "save_registry",
            side_effect=OSError("registry write failed"),
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertFalse(canonical.exists())
        self.assertEqual(load_registry(self.repo / "registry.yaml")["version"], 1)
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.ACTIVE,
        )

    def test_family_link_failure_restores_variant_and_links(self):
        variant = self.make_variant("kimi")
        (variant / "family.txt").write_text("old\n", encoding="utf-8")
        old_hash = hash_skill_dir(variant)
        old_targets = self.install_base_links()
        session = edit_begin(
            "alpha",
            scope="family",
            target="kimi",
            config_path=self.config_path,
        )
        (Path(session["workspace_path"]) / "family.txt").write_text(
            "new\n",
            encoding="utf-8",
        )
        real_apply = core_module.DirectoryLinkSwap.apply
        calls = 0

        def fail_family_link(swap):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("family link failed")
            return real_apply(swap)

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ), mock.patch.object(
            core_module.DirectoryLinkSwap,
            "apply",
            autospec=True,
            side_effect=fail_family_link,
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertEqual(hash_skill_dir(variant), old_hash)
        self.assertEqual(load_registry(self.repo / "registry.yaml")["version"], 1)
        for client in self.clients:
            self.assertEqual(
                (client.skills_dir / "alpha").resolve(),
                old_targets[client.id].resolve(),
            )
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.ACTIVE,
        )

    def test_absent_variant_link_failure_restores_absent_state(self):
        canonical = self.variants_root / "alpha" / "codex"
        self.install_base_links()
        session = edit_begin(
            "alpha",
            scope="client",
            target="codex",
            config_path=self.config_path,
        )
        (Path(session["workspace_path"]) / "codex.txt").write_text(
            "codex\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            core_module,
            "detect_clients",
            return_value=self.clients,
        ), mock.patch.object(
            core_module.DirectoryLinkSwap,
            "apply",
            side_effect=OSError("link failed"),
        ):
            with self.assertRaises(SkillSyncError) as raised:
                edit_apply(session["session_id"], config_path=self.config_path)

        self.assertEqual(raised.exception.code, "edit_apply_failed")
        self.assertFalse(canonical.exists())
        self.assertEqual(load_registry(self.repo / "registry.yaml")["version"], 1)
        self.assertEqual(
            EditSessionStore(self.data_root).load(session["session_id"]).status,
            EditSessionStatus.ACTIVE,
        )


if __name__ == "__main__":
    unittest.main()
