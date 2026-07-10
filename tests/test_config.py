import json
import tempfile
import unittest
from pathlib import Path

from skill_sync.config import (
    default_config_path,
    empty_config,
    load_config,
    save_config,
    set_skill_baseline,
)


class ConfigTest(unittest.TestCase):
    def test_default_config_path_uses_xdg_config_home_when_set(self):
        path = default_config_path(
            env={"XDG_CONFIG_HOME": "/tmp/custom-config"},
            home=Path("/home/example"),
        )

        self.assertEqual(path, Path("/tmp/custom-config") / "skill-sync" / "config.json")

    def test_default_config_path_falls_back_to_home_config_directory(self):
        path = default_config_path(env={}, home=Path("/home/example"))

        self.assertEqual(path, Path("/home/example/.config/skill-sync/config.json"))

    def test_load_missing_config_returns_empty_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "missing" / "config.json"

            self.assertEqual(load_config(config_path), empty_config())

    def test_save_and_load_round_trip_creates_parent_directories(self):
        config = {
            "sync_repo_path": "/tmp/sync-repo",
            "platform": "codex",
            "branch": "main",
            "skills": {
                "example-skill": {
                    "local_path": "/tmp/skills/example-skill",
                    "last_installed_hash": "sha256:abc123",
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "nested" / "config.json"
            save_config(config_path, config)

            self.assertTrue(config_path.exists())
            self.assertEqual(load_config(config_path), config)

    def test_set_skill_baseline_updates_last_installed_hash(self):
        config = empty_config()
        config["skills"]["example-skill"] = {
            "local_path": "/tmp/skills/example-skill",
            "last_installed_hash": "sha256:old",
        }

        set_skill_baseline(config, "example-skill", "sha256:new")

        self.assertEqual(
            config["skills"]["example-skill"],
            {
                "local_path": "/tmp/skills/example-skill",
                "last_installed_hash": "sha256:new",
            },
        )

    def test_set_skill_baseline_creates_skill_entry(self):
        config = empty_config()

        set_skill_baseline(config, "new-skill", "sha256:new")

        self.assertEqual(
            config["skills"]["new-skill"],
            {"last_installed_hash": "sha256:new"},
        )

    def test_set_skill_baseline_rejects_non_sha256_hash(self):
        with self.assertRaisesRegex(ValueError, "sha256"):
            set_skill_baseline(empty_config(), "example-skill", "md5:bad")

    def test_load_rejects_non_mapping_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "root"):
                load_config(config_path)

    def test_load_rejects_non_mapping_skills(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(json.dumps({"skills": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "skills"):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
