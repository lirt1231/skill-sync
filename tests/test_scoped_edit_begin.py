import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import skill_sync.core as core_module
import skill_sync.variant_source as variant_source_module
from skill_sync.agents import AgentClient
from skill_sync.config import empty_config, save_config
from skill_sync.core import (
    edit_begin,
    edit_diff,
    edit_impact,
    edit_validate,
)
from skill_sync.edit_session import (
    EditLayerBaselineState,
    EditSessionScopeKind,
    EditSessionStatus,
    EditSessionStore,
)
from skill_sync.errors import SkillSyncError
from skill_sync.hash import hash_skill_dir
from skill_sync.protocol import EXIT_SAFETY
from skill_sync.registry import save_registry


class ScopedEditBeginTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.skills_root = self.root / "global" / "skills"
        self.variants_root = self.skills_root.parent / "variants"
        self.data_root = self.root / "data"
        self.config_path = self.root / "config.json"

        for name in ("alpha", "beta"):
            skill = self.skills_root / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            (skill / "base-only.txt").write_text("base\n", encoding="utf-8")

        config = empty_config()
        config["sync_repo_path"] = str(self.repo)
        config["skills_root"] = str(self.skills_root)
        config["data_root"] = str(self.data_root)
        config["skills"] = {
            name: {"local_path": str(self.skills_root / name)}
            for name in ("alpha", "beta")
        }
        save_config(self.config_path, config)
        save_registry(
            self.repo / "registry.yaml",
            {
                "version": 1,
                "skills": {
                    name: {
                        "selected": True,
                        "source_platform": "global",
                        "display_name": name,
                    }
                    for name in ("alpha", "beta")
                },
            },
        )
        self.clients = [
            AgentClient(
                client_id,
                family_id,
                display_name,
                self.root / "clients" / client_id / "skills",
                True,
            )
            for client_id, family_id, display_name in (
                ("codex", "codex", "Codex"),
                ("workbuddy", "workbuddy", "WorkBuddy"),
                ("kimi-code", "kimi", "Kimi Code"),
                ("claude-code", "claude", "Claude Code"),
            )
        ]

    def make_variant(self, skill: str, target: str) -> Path:
        variant = self.variants_root / skill / target
        variant.mkdir(parents=True)
        (variant / "variant.yaml").write_text(
            f"version: 1\ntarget: {target}\nmode: overlay\n",
            encoding="utf-8",
        )
        return variant

    @staticmethod
    def relative_files(root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }

    def begin(self, skill: str, *, scope: str, target: str):
        with mock.patch.object(
            core_module, "detect_clients", return_value=self.clients
        ):
            return edit_begin(
                skill,
                scope=scope,
                target=target,
                actor="codex",
                config_path=self.config_path,
            )

    def test_existing_family_variant_workspace_contains_only_authored_layer(self):
        variant = self.make_variant("alpha", "kimi")
        (variant / "family-only.md").write_text("Kimi guidance\n", encoding="utf-8")
        (variant / "nested").mkdir()
        (variant / "nested" / "tool.txt").write_text("kimi-tool\n", encoding="utf-8")
        variant_hash = hash_skill_dir(variant)

        result = self.begin("alpha", scope="family", target="kimi")

        baseline = Path(result["baseline_path"])
        workspace = Path(result["workspace_path"])
        expected_files = {"variant.yaml", "family-only.md", "nested/tool.txt"}
        self.assertEqual(self.relative_files(baseline), expected_files)
        self.assertEqual(self.relative_files(workspace), expected_files)
        self.assertFalse((workspace / "SKILL.md").exists())
        self.assertFalse((workspace / "base-only.txt").exists())
        self.assertEqual(hash_skill_dir(baseline), variant_hash)
        self.assertEqual(hash_skill_dir(workspace), variant_hash)
        self.assertEqual(result["scope"], "family")
        self.assertEqual(result["target"], "kimi")
        self.assertEqual(
            result["layer_baseline"],
            {"state": "present", "hash": variant_hash},
        )

        metadata = EditSessionStore(self.data_root).load(result["session_id"])
        self.assertEqual(metadata.target_scope.kind, EditSessionScopeKind.FAMILY)
        self.assertEqual(metadata.target_scope.target, "kimi")
        self.assertEqual(metadata.layer_baseline.state, EditLayerBaselineState.PRESENT)
        self.assertEqual(metadata.layer_baseline.hash, variant_hash)

    def test_missing_client_variant_uses_minimal_workspace_without_source(self):
        canonical_variant = self.variants_root / "alpha" / "codex"
        self.assertFalse(canonical_variant.exists())

        result = self.begin("alpha", scope="client", target="codex")

        expected_manifest = "version: 1\ntarget: codex\nmode: overlay\n"
        baseline = Path(result["baseline_path"])
        workspace = Path(result["workspace_path"])
        self.assertEqual(self.relative_files(baseline), {"variant.yaml"})
        self.assertEqual(self.relative_files(workspace), {"variant.yaml"})
        self.assertEqual(
            (baseline / "variant.yaml").read_text(encoding="utf-8"),
            expected_manifest,
        )
        self.assertEqual(
            (workspace / "variant.yaml").read_text(encoding="utf-8"),
            expected_manifest,
        )
        self.assertFalse(canonical_variant.exists())
        self.assertEqual(result["scope"], "client")
        self.assertEqual(result["target"], "codex")
        self.assertEqual(
            result["layer_baseline"], {"state": "absent", "hash": None}
        )

        metadata = EditSessionStore(self.data_root).load(result["session_id"])
        self.assertEqual(metadata.target_scope.kind, EditSessionScopeKind.CLIENT)
        self.assertEqual(metadata.target_scope.target, "codex")
        self.assertEqual(metadata.layer_baseline.state, EditLayerBaselineState.ABSENT)
        self.assertIsNone(metadata.layer_baseline.hash)

    def test_kimi_family_reports_kimi_code_as_affected(self):
        result = self.begin("alpha", scope="family", target="kimi")

        self.assertEqual(result["affected_clients"], ["kimi-code"])

    def test_same_codex_target_preserves_family_and_client_scope_kinds(self):
        family = self.begin("alpha", scope="family", target="codex")
        client = self.begin("beta", scope="client", target="codex")

        family_metadata = EditSessionStore(self.data_root).load(family["session_id"])
        client_metadata = EditSessionStore(self.data_root).load(client["session_id"])
        self.assertEqual(family["affected_clients"], ["codex"])
        self.assertEqual(client["affected_clients"], ["codex"])
        self.assertEqual(family_metadata.target_scope.kind, EditSessionScopeKind.FAMILY)
        self.assertEqual(client_metadata.target_scope.kind, EditSessionScopeKind.CLIENT)
        self.assertEqual(family_metadata.target_scope.target, "codex")
        self.assertEqual(client_metadata.target_scope.target, "codex")

    def test_scoped_begin_waits_for_variant_create_and_records_present_layer(self):
        create_holds_locks = threading.Event()
        release_create = threading.Event()
        begin_attempted_lock = threading.Event()
        begin_done = threading.Event()
        results: dict[str, dict] = {}
        errors: list[BaseException] = []
        real_create_locked = variant_source_module._create_variant_locked
        real_begin_lock = core_module.local_file_lock

        def paused_create_locked(**kwargs):
            create_holds_locks.set()
            if not release_create.wait(timeout=3):
                raise TimeoutError("test did not release Variant create")
            return real_create_locked(**kwargs)

        def observed_begin_lock(path, **kwargs):
            begin_attempted_lock.set()
            return real_begin_lock(path, **kwargs)

        def create_worker() -> None:
            try:
                results["create"] = variant_source_module.create_variant(
                    "alpha",
                    scope="family",
                    target="kimi",
                    config_path=self.config_path,
                )
            except BaseException as exc:  # assertion reports thread failures
                errors.append(exc)

        def begin_worker() -> None:
            try:
                results["begin"] = edit_begin(
                    "alpha",
                    scope="family",
                    target="kimi",
                    actor="codex",
                    config_path=self.config_path,
                )
            except BaseException as exc:  # assertion reports thread failures
                errors.append(exc)
            finally:
                begin_done.set()

        with mock.patch.object(
            variant_source_module,
            "_create_variant_locked",
            side_effect=paused_create_locked,
        ), mock.patch.object(
            core_module,
            "local_file_lock",
            side_effect=observed_begin_lock,
        ):
            create_thread = threading.Thread(target=create_worker)
            begin_thread = threading.Thread(target=begin_worker)
            create_thread.start()
            self.assertTrue(create_holds_locks.wait(timeout=3))
            begin_thread.start()
            self.assertTrue(begin_attempted_lock.wait(timeout=3))
            self.assertFalse(begin_done.is_set())
            release_create.set()
            create_thread.join(timeout=3)
            begin_thread.join(timeout=3)

        self.assertFalse(create_thread.is_alive())
        self.assertFalse(begin_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results["create"]["created"])
        canonical_variant = self.variants_root / "alpha" / "kimi"
        canonical_hash = hash_skill_dir(canonical_variant)
        begin = results["begin"]
        self.assertEqual(
            begin["layer_baseline"],
            {"state": "present", "hash": canonical_hash},
        )
        self.assertEqual(begin["baseline_hash"], canonical_hash)
        metadata = EditSessionStore(self.data_root).load(begin["session_id"])
        self.assertEqual(metadata.layer_baseline.state, EditLayerBaselineState.PRESENT)
        self.assertEqual(metadata.layer_baseline.hash, canonical_hash)
        self.assertEqual(
            EditSessionStore(self.data_root).list_metadata(), [metadata]
        )

    def test_unknown_family_or_client_target_fails_closed_without_mutation(self):
        for index, scope in enumerate(("family", "client")):
            with self.subTest(scope=scope):
                before_variants = self.relative_files(self.variants_root)
                before_sessions = EditSessionStore(self.data_root).list_metadata()
                with self.assertRaises(SkillSyncError) as raised:
                    edit_begin(
                        "alpha",
                        scope=scope,
                        target=f"unknown-{index}",
                        config_path=self.config_path,
                    )
                self.assertEqual(raised.exception.exit_code, EXIT_SAFETY)
                self.assertEqual(
                    self.relative_files(self.variants_root), before_variants
                )
                self.assertEqual(
                    EditSessionStore(self.data_root).list_metadata(), before_sessions
                )

    def test_existing_variant_with_nonportable_paths_fails_without_session(self):
        if os.name == "nt":
            self.skipTest("Windows cannot create the unsafe fixture names")
        cases = (
            ("alpha", "references/NUL.txt"),
            ("beta", "references/bad:name.txt"),
        )
        for skill, relative_path in cases:
            with self.subTest(relative_path=relative_path):
                variant = self.make_variant(skill, "kimi")
                unsafe = variant / relative_path
                unsafe.parent.mkdir(exist_ok=True)
                unsafe.write_text("unsafe\n", encoding="utf-8")

                with self.assertRaises(SkillSyncError) as raised:
                    self.begin(skill, scope="family", target="kimi")

                self.assertEqual(raised.exception.code, "variant_source_unsafe")
                self.assertEqual(EditSessionStore(self.data_root).list_metadata(), [])
                self.assertEqual(list(self.data_root.glob(".scoped-begin-*")), [])

    def test_existing_variant_with_casefold_collision_fails_without_session(self):
        variant = self.make_variant("alpha", "kimi")
        upper = variant / "A.txt"
        lower = variant / "a.txt"
        upper.write_text("upper\n", encoding="utf-8")
        lower.write_text("lower\n", encoding="utf-8")
        if upper.samefile(lower):
            self.skipTest("filesystem is case-insensitive")

        with self.assertRaises(SkillSyncError) as raised:
            self.begin("alpha", scope="family", target="kimi")

        self.assertEqual(raised.exception.code, "variant_source_unsafe")
        self.assertEqual(EditSessionStore(self.data_root).list_metadata(), [])

    def test_scoped_inspection_is_read_only(self):
        variant = self.make_variant("alpha", "kimi")
        (variant / "family-only.md").write_text("old\n", encoding="utf-8")
        result = self.begin("alpha", scope="family", target="kimi")
        workspace = Path(result["workspace_path"])
        (workspace / "family-only.md").write_text("new\n", encoding="utf-8")

        diff = edit_diff(result["session_id"], config_path=self.config_path)
        self.assertEqual(diff["scope"], "family")
        self.assertEqual(diff["source_diff"]["summary"]["modified"], 1)
        self.assertEqual(
            [item["client"] for item in diff["resolved_diffs"]],
            ["kimi-code"],
        )
        validation = edit_validate(
            result["session_id"], config_path=self.config_path
        )
        self.assertTrue(validation["valid"])
        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            impact = edit_impact(
                result["session_id"], config_path=self.config_path
            )
        self.assertEqual(
            [row["client"] for row in impact["clients"] if row["affected"]],
            ["kimi-code"],
        )

        metadata = EditSessionStore(self.data_root).load(result["session_id"])
        self.assertEqual(metadata.status, EditSessionStatus.ACTIVE)
        self.assertEqual(
            (variant / "family-only.md").read_text(encoding="utf-8"), "old\n"
        )
        self.assertFalse((self.data_root / "operations").exists())

    def test_family_impact_matrix_leaves_other_clients_unchanged(self):
        session = self.begin("alpha", scope="family", target="kimi")
        workspace = Path(session["workspace_path"])
        (workspace / "SKILL.md").write_text("# Kimi family\n", encoding="utf-8")

        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            impact = edit_impact(session["session_id"], config_path=self.config_path)

        rows = {row["client"]: row for row in impact["clients"]}
        self.assertEqual(
            {client for client, row in rows.items() if row["scope_affected"]},
            {"kimi-code"},
        )
        self.assertEqual(
            {client for client, row in rows.items() if row["affected"]},
            {"kimi-code"},
        )
        for client in ("codex", "workbuddy", "claude-code"):
            self.assertEqual(
                rows[client]["current_resolution_hash"],
                rows[client]["proposed_resolution_hash"],
            )
            self.assertEqual(rows[client]["action"], "noop")
        self.assertEqual(impact["summary"]["affected"], 1)

    def test_missing_client_variant_preview_does_not_create_canonical_source(self):
        canonical = self.variants_root / "alpha" / "codex"
        session = self.begin("alpha", scope="client", target="codex")
        workspace = Path(session["workspace_path"])
        (workspace / "SKILL.md").write_text("# Codex only\n", encoding="utf-8")

        diff = edit_diff(session["session_id"], config_path=self.config_path)
        self.assertEqual([item["client"] for item in diff["resolved_diffs"]], ["codex"])
        self.assertTrue(diff["resolved_diffs"][0]["changed"])
        validation = edit_validate(session["session_id"], config_path=self.config_path)
        self.assertTrue(validation["valid"])
        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            impact = edit_impact(session["session_id"], config_path=self.config_path)
        self.assertEqual(
            [row["client"] for row in impact["clients"] if row["affected"]],
            ["codex"],
        )
        self.assertFalse(canonical.exists())

    def test_resolved_diff_can_be_limited_to_one_affected_client(self):
        session = self.begin("alpha", scope="family", target="kimi")
        workspace = Path(session["workspace_path"])
        (workspace / "family.txt").write_text("family\n", encoding="utf-8")

        result = edit_diff(
            session["session_id"],
            resolved_client="kimi-code",
            config_path=self.config_path,
        )
        self.assertEqual(
            [item["client"] for item in result["resolved_diffs"]],
            ["kimi-code"],
        )
        with self.assertRaises(SkillSyncError) as raised:
            edit_diff(
                session["session_id"],
                resolved_client="codex",
                config_path=self.config_path,
            )
        self.assertEqual(raised.exception.code, "edit_resolved_client_invalid")

    def test_scoped_inspection_marks_concurrent_layer_change_stale(self):
        variant = self.make_variant("alpha", "kimi")
        (variant / "family.txt").write_text("before\n", encoding="utf-8")
        session = self.begin("alpha", scope="family", target="kimi")
        workspace = Path(session["workspace_path"])
        (workspace / "family.txt").write_text("proposed\n", encoding="utf-8")
        (variant / "family.txt").write_text("concurrent\n", encoding="utf-8")

        diff = edit_diff(session["session_id"], config_path=self.config_path)
        validation = edit_validate(session["session_id"], config_path=self.config_path)
        with mock.patch.object(core_module, "detect_clients", return_value=self.clients):
            impact = edit_impact(session["session_id"], config_path=self.config_path)

        self.assertTrue(diff["stale_baseline"])
        self.assertTrue(validation["stale_baseline"])
        self.assertTrue(impact["blocked"])
        self.assertEqual(
            impact["blocked_reason"], "canonical-layer-changed-since-begin"
        )
        self.assertEqual(
            {row["action"] for row in impact["clients"] if row["scope_affected"]},
            {"blocked"},
        )
        self.assertEqual(
            {row["action"] for row in impact["clients"] if not row["scope_affected"]},
            {"noop"},
        )

    def test_invalid_scoped_manifest_is_reported_and_blocks_diff_and_impact(self):
        session = self.begin("alpha", scope="client", target="codex")
        workspace = Path(session["workspace_path"])
        (workspace / "variant.yaml").write_text(
            "version: 1\ntarget: workbuddy\nmode: overlay\n",
            encoding="utf-8",
        )

        validation = edit_validate(session["session_id"], config_path=self.config_path)
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["issues"][0]["code"], "invalid_variant_overlay")
        with self.assertRaises(SkillSyncError) as diff_error:
            edit_diff(session["session_id"], config_path=self.config_path)
        self.assertEqual(diff_error.exception.code, "variant_resolution_invalid")
        with self.assertRaises(SkillSyncError) as impact_error:
            edit_impact(session["session_id"], config_path=self.config_path)
        self.assertEqual(impact_error.exception.code, "invalid_edit_workspace")

    def test_legacy_base_begin_call_and_result_remain_compatible(self):
        baseline_hash = hash_skill_dir(self.skills_root / "alpha")

        result = edit_begin("alpha", actor="codex", config_path=self.config_path)

        self.assertEqual(
            result,
            {
                "session_id": result["session_id"],
                "skill": "alpha",
                "scope": "base",
                "status": "active",
                "actor": "codex",
                "baseline_hash": baseline_hash,
                "baseline_path": result["baseline_path"],
                "workspace_path": result["workspace_path"],
            },
        )
        metadata = EditSessionStore(self.data_root).load(result["session_id"])
        self.assertEqual(metadata.target_scope.kind, EditSessionScopeKind.BASE)
        self.assertIsNone(metadata.target_scope.target)
        self.assertEqual(hash_skill_dir(Path(result["workspace_path"])), baseline_hash)


if __name__ == "__main__":
    unittest.main()
