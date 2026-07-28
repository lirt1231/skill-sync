import tempfile
import unittest
from pathlib import Path

from skill_sync.registry import (
    empty_registry,
    load_registry,
    register_variant_target,
    registry_variant_targets,
    save_registry,
    serialize_registry,
)


class RegistryTest(unittest.TestCase):
    def test_empty_registry_has_initial_schema(self):
        self.assertEqual(empty_registry(), {"version": 1, "skills": {}})

    def test_loads_comments_blank_lines_scalars_and_nested_skill_mappings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"
            registry_path.write_text(
                """
# portable sync state
version: 1

skills:
  example-skill:
    selected: true
    source_platform: codex
    display_name: Example Skill
    priority: 3
  disabled-skill:
    selected: false
    source_platform: codex
""".lstrip(),
                encoding="utf-8",
            )

            self.assertEqual(
                load_registry(registry_path),
                {
                    "version": 1,
                    "skills": {
                        "example-skill": {
                            "selected": True,
                            "source_platform": "codex",
                            "display_name": "Example Skill",
                            "priority": 3,
                        },
                        "disabled-skill": {
                            "selected": False,
                            "source_platform": "codex",
                        },
                    },
                },
            )

    def test_loads_and_writes_unknown_top_level_and_per_skill_fields(self):
        registry = {
            "version": 1,
            "ui": {"sort_order": 10, "show_hidden": False},
            "skills": {
                "agent-helper": {
                    "selected": True,
                    "source_platform": "codex",
                    "display_name": "Agent Helper",
                    "notes": "keep this unknown field",
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"
            save_registry(registry_path, registry)

            self.assertEqual(load_registry(registry_path), registry)
            self.assertEqual(
                registry_path.read_text(encoding="utf-8"),
                "\n".join(
                    [
                        "version: 1",
                        "ui:",
                        "  sort_order: 10",
                        "  show_hidden: false",
                        "skills:",
                        "  agent-helper:",
                        "    selected: true",
                        "    source_platform: codex",
                        "    display_name: Agent Helper",
                        "    notes: keep this unknown field",
                        "",
                    ]
                ),
            )

    def test_loads_v2_without_rewriting_or_upgrading_it(self):
        text = "\n".join(
            [
                "version: 2",
                "skills:",
                "  alpha:",
                "    selected: true",
                "    targets: codex,kimi",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"
            registry_path.write_text(text, encoding="utf-8")

            registry = load_registry(registry_path)

            self.assertEqual(registry["version"], 2)
            self.assertEqual(registry_variant_targets(registry, "alpha"), ())
            self.assertEqual(registry_path.read_text(encoding="utf-8"), text)

    def test_registering_first_variant_lazily_upgrades_v2_to_v3(self):
        registry = {
            "version": 2,
            "skills": {
                "alpha": {
                    "selected": True,
                    "display_name": "alpha",
                    "targets": "codex,kimi",
                }
            },
        }

        self.assertTrue(register_variant_target(registry, "alpha", "kimi"))
        self.assertEqual(registry["version"], 3)
        self.assertEqual(registry_variant_targets(registry, "alpha"), ("kimi",))
        self.assertFalse(register_variant_target(registry, "alpha", "kimi"))
        self.assertTrue(register_variant_target(registry, "alpha", "codex"))
        self.assertEqual(registry["skills"]["alpha"]["variants"], "codex,kimi")

    def test_v3_serialization_is_deterministic_and_normalizes_variant_order(self):
        first = {
            "notes": {"z": 2, "a": 1},
            "skills": {
                "zeta": {"variants": "kimi", "selected": True},
                "alpha": {
                    "variants": "kimi-code,codex",
                    "targets": "codex,kimi",
                    "display_name": "alpha",
                    "selected": True,
                },
            },
            "version": 3,
        }
        second = {
            "version": 3,
            "skills": {
                "alpha": {
                    "selected": True,
                    "display_name": "alpha",
                    "targets": "codex,kimi",
                    "variants": "codex,kimi-code",
                },
                "zeta": {"selected": True, "variants": "kimi"},
            },
            "notes": {"a": 1, "z": 2},
        }

        serialized = serialize_registry(first)

        self.assertEqual(serialized, serialize_registry(second))
        self.assertEqual(
            serialized,
            "\n".join(
                [
                    "version: 3",
                    "skills:",
                    "  alpha:",
                    "    selected: true",
                    "    display_name: alpha",
                    "    targets: codex,kimi",
                    "    variants: codex,kimi-code",
                    "  zeta:",
                    "    selected: true",
                    "    variants: kimi",
                    "notes:",
                    "  a: 1",
                    "  z: 2",
                    "",
                ]
            ),
        )

    def test_v3_rejects_invalid_variant_intent(self):
        for variants in ("", "kimi,", "kimi,kimi", "Kimi", "../kimi", "/kimi"):
            with self.subTest(variants=variants):
                registry = {
                    "version": 3,
                    "skills": {
                        "alpha": {"selected": True, "variants": variants},
                    },
                }
                with self.assertRaisesRegex(ValueError, "variant|Variant|absolute path"):
                    serialize_registry(registry)

    def test_rejects_invalid_indentation(self):
        cases = {
            "odd indentation": "version: 1\n skills:\n",
            "indentation jump": "skills:\n    bad: true\n",
            "indented root": "  version: 1\n",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            for name, text in cases.items():
                with self.subTest(name=name):
                    registry_path = Path(tmp_dir) / f"{name}.yaml"
                    registry_path.write_text(text, encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "indent"):
                        load_registry(registry_path)

    def test_rejects_unsupported_yaml_features(self):
        cases = {
            "sequence": "skills:\n  - skill-name\n",
            "flow mapping": "skills: {example: true}\n",
            "anchor": "skills: &skills\n",
            "alias": "skills: *skills\n",
            "tag": "version: !!int 1\n",
            "multiline": "notes: |\n  hello\n",
            "multiline strip chomping": "notes: |-\n",
            "multiline keep chomping": "notes: |+\n",
            "folded strip chomping": "notes: >-\n",
            "folded keep chomping": "notes: >+\n",
            "double quoted key": '"skill-name": true\n',
            "double quoted value": 'display_name: "Example"\n',
            "single quoted value": "display_name: 'Example'\n",
            "flow sequence-like key": "[a]: b\n",
            "flow mapping-like key": "{a: b}: c\n",
            "literal-like key": "|bad: value\n",
            "folded-like key": ">bad: value\n",
            "explicit key": "? key: value\n",
            "boolean-like key": "true: value\n",
            "integer-like key": "123: value\n",
            "null-like key": "null: value\n",
            "yes-like key": "yes: value\n",
            "date-like key": "2024-01-01: value\n",
            "base-like key": "0x10: value\n",
            "separator-like key": "1_000: value\n",
            "null value": "x: null\n",
            "tilde null value": "x: ~\n",
            "float value": "x: 1.0\n",
            "mixed boolean value": "x: True\n",
            "upper boolean value": "x: FALSE\n",
            "exponent value": "x: 1e3\n",
            "negative exponent value": "x: -1e-3\n",
            "signed float value": "x: +1.0\n",
            "nan value": "x: .nan\n",
            "inf value": "x: .inf\n",
            "negative inf value": "x: -.inf\n",
            "trailing dot float value": "x: 0.\n",
            "leading zero integer-like value": "x: 01\n",
            "yes value": "x: yes\n",
            "mixed no value": "x: No\n",
            "upper on value": "x: ON\n",
            "off value": "x: off\n",
            "date-like value": "x: 2024-01-01\n",
            "hex-like value": "x: 0x10\n",
            "octal-like value": "x: 0o10\n",
            "binary-like value": "x: 0b10\n",
            "separator-like value": "x: 1_000\n",
            "plus integer value": "x: +1\n",
            "plus zero value": "x: +0\n",
            "negative zero value": "x: -0\n",
            "plus integer key": "+1: value\n",
            "plus zero key": "+0: value\n",
            "negative zero key": "-0: value\n",
            "colon value": "x: a: b\n",
            "multiple colon mapping": "a:b: value\n",
            "missing separator space": "a:b\n",
            "space before colon missing after": "a :b\n",
            "space before colon": "a : b\n",
            "extra separator space": "a:  b\n",
            "trailing scalar padding": "a: b   \n",
            "empty padded scalar": "a:    \n",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            for name, text in cases.items():
                with self.subTest(name=name):
                    registry_path = Path(tmp_dir) / f"{name}.yaml"
                    registry_path.write_text(text, encoding="utf-8")

                    with self.assertRaises(ValueError):
                        load_registry(registry_path)

    def test_save_rejects_values_that_look_like_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for path_value in [
                "/Users/example/.codex/skills/bad",
                r"C:\Users\example\.codex\skills\bad",
                r"\\server\share\.codex\skills\bad",
            ]:
                with self.subTest(path_value=path_value):
                    with self.assertRaisesRegex(ValueError, "absolute path"):
                        save_registry(
                            registry_path,
                            {
                                "version": 1,
                                "skills": {
                                    "bad": {
                                        "selected": True,
                                        "source_platform": "codex",
                                        "display_name": "bad",
                                        "local_path": path_value,
                                    }
                                },
                            },
                        )

    def test_load_rejects_values_that_look_like_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for path_value in [
                "/Users/example/.codex/skills/bad",
                r"C:\Users\example\.codex\skills\bad",
                r"\\server\share\.codex\skills\bad",
            ]:
                with self.subTest(path_value=path_value):
                    registry_path.write_text(
                        "\n".join(
                            [
                                "version: 1",
                                "skills:",
                                "  bad:",
                                "    selected: true",
                                "    source_platform: codex",
                                "    display_name: bad",
                                f"    local_path: {path_value}",
                                "",
                            ]
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, "absolute path"):
                        load_registry(registry_path)

    def test_save_rejects_keys_that_look_like_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for path_key in [
                "/Users/example/.codex/skills/bad",
                r"C:\Users\example\.codex\skills\bad",
                r"\\server\share\.codex\skills\bad",
            ]:
                with self.subTest(path_key=path_key):
                    with self.assertRaisesRegex(ValueError, "absolute path"):
                        save_registry(
                            registry_path,
                            {
                                "version": 1,
                                "skills": {
                                    path_key: {
                                        "selected": True,
                                        "source_platform": "codex",
                                        "display_name": "bad",
                                    }
                                },
                            },
                        )

    def test_load_rejects_keys_that_look_like_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for path_key in [
                "/Users/example/.codex/skills/bad",
                r"C:\Users\example\.codex\skills\bad",
                r"\\server\share\.codex\skills\bad",
            ]:
                with self.subTest(path_key=path_key):
                    registry_path.write_text(
                        "\n".join(
                            [
                                "version: 1",
                                "skills:",
                                f"  {path_key}:",
                                "    selected: true",
                                "    source_platform: codex",
                                "    display_name: bad",
                                "",
                            ]
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, "absolute path"):
                        load_registry(registry_path)

    def test_load_rejects_windows_drive_absolute_path_keys_with_inline_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for line in [
                r"C:\Users\example\.codex\skills\bad: true",
                "C:/Users/example/.codex/skills/bad: true",
                r"D:\foo: bar",
            ]:
                with self.subTest(line=line):
                    registry_path.write_text(f"{line}\n", encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "absolute path|unsupported"):
                        load_registry(registry_path)

    def test_save_rejects_strings_that_would_reload_as_comments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for value in ["#hidden", "a\t#lost"]:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "comment"):
                        save_registry(
                            registry_path,
                            {"version": 1, "skills": {"bad": {"display_name": value}}},
                        )

    def test_save_rejects_strings_containing_colons(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            with self.assertRaisesRegex(ValueError, "colon"):
                save_registry(
                    registry_path,
                    {"version": 1, "skills": {"bad": {"display_name": "a: b"}}},
                )

    def test_save_rejects_keys_that_would_reload_as_sequences(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            with self.assertRaisesRegex(ValueError, "key"):
                save_registry(registry_path, {"version": 1, "-bad": {"selected": True}})

    def test_save_rejects_keys_that_would_not_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for key in [
                " bad",
                "bad ",
                "#bad",
                "bad # comment",
                "bad\t#lost",
                "bad\nkey",
                "? key",
                '"bad"',
                "'bad'",
                "true",
                "123",
                "null",
                "yes",
                "No",
                "ON",
                "off",
                "2024-01-01",
                "0x10",
                "0o10",
                "0b10",
                "1_000",
                "+1",
                "+0",
                "-0",
                "True",
                "FALSE",
                "1e3",
                "-1e-3",
                "+1.0",
                ".nan",
                ".inf",
                "-.inf",
                "0.",
                "01",
            ]:
                with self.subTest(key=key):
                    with self.assertRaisesRegex(ValueError, "key"):
                        save_registry(registry_path, {"version": 1, key: {"selected": True}})

    def test_load_rejects_duplicate_keys_at_the_same_mapping_level(self):
        cases = {
            "top level": "version: 1\nversion: 2\n",
            "nested": "\n".join(
                [
                    "version: 1",
                    "skills:",
                    "  bad:",
                    "    selected: true",
                    "    selected: false",
                    "",
                ]
            ),
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            for name, text in cases.items():
                with self.subTest(name=name):
                    registry_path = Path(tmp_dir) / f"{name}.yaml"
                    registry_path.write_text(text, encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "Duplicate key"):
                        load_registry(registry_path)

    def test_save_rejects_strings_that_would_reload_as_other_scalar_types(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for value in [
                "true",
                "false",
                "123",
                "-1",
                "null",
                "~",
                "1.0",
                "True",
                "FALSE",
                "1e3",
                "-1e-3",
                "+1.0",
                ".nan",
                ".inf",
                "-.inf",
                "0.",
                "01",
                "yes",
                "No",
                "ON",
                "off",
                "2024-01-01",
                "0x10",
                "0o10",
                "0b10",
                "1_000",
                "+1",
                "+0",
                "-0",
            ]:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "ambiguous"):
                        save_registry(
                            registry_path,
                            {
                                "version": 1,
                                "skills": {
                                    "bad": {
                                        "selected": True,
                                        "source_platform": "codex",
                                        "display_name": value,
                                    }
                                },
                            },
                        )

    def test_save_rejects_strings_that_loader_treats_as_unsupported_token_hazards(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.yaml"

            for value in ["A & B", "use * marker", "hello ! wow", '"Example"', "'Example'"]:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "supported"):
                        save_registry(
                            registry_path,
                            {
                                "version": 1,
                                "skills": {
                                    "bad": {
                                        "selected": True,
                                        "source_platform": "codex",
                                        "display_name": value,
                                    }
                                },
                            },
                        )


if __name__ == "__main__":
    unittest.main()
