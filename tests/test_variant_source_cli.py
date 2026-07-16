import contextlib
import io
import json
import unittest
from unittest import mock

import skill_sync.cli as cli


def run_cli(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class VariantSourceCliTest(unittest.TestCase):
    def test_variant_list_dispatches_filter_and_shared_json_envelope(self):
        result = {"variants_root": "/portable/variants", "variants": []}
        with mock.patch.object(cli.variant_source, "list_variants", return_value=result) as listing:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "variant", "list", "--skill", "alpha", "--json"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        listing.assert_called_once_with(skill="alpha", config_path="/tmp/config.json")
        self.assertEqual(json.loads(stdout)["command"], "variant list")
        self.assertEqual(json.loads(stdout)["result"], result)

    def test_variant_create_requires_exactly_one_scope_and_dispatches(self):
        with self.assertRaises(SystemExit):
            run_cli(["variant", "create", "alpha"])
        with self.assertRaises(SystemExit):
            run_cli(["variant", "create", "alpha", "--family", "kimi", "--client", "codex"])

        result = {
            "skill": "alpha",
            "scope": "family",
            "target": "kimi",
            "path": "/portable/variants/alpha/kimi",
            "resolution_order": ["base", "family:kimi", "client-specific"],
        }
        with mock.patch.object(cli.variant_source, "create_variant", return_value=result) as create:
            code, stdout, stderr = run_cli(
                ["--config", "/tmp/config.json", "variant", "create", "alpha", "--family", "kimi", "--json"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        create.assert_called_once_with(
            "alpha", scope="family", target="kimi", config_path="/tmp/config.json"
        )
        self.assertEqual(json.loads(stdout)["command"], "variant create")
        self.assertEqual(json.loads(stdout)["result"], result)

    def test_variant_validate_supports_text_and_json(self):
        result = {
            "skill": "alpha",
            "valid": True,
            "variant_count": 1,
            "issues": [],
            "variants": [{"target": "kimi", "valid": True}],
        }
        with mock.patch.object(cli.variant_source, "validate_variants", return_value=result) as validate:
            code, stdout, stderr = run_cli(["variant", "validate", "alpha"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("valid", stdout.lower())
        validate.assert_called_once_with("alpha", config_path=None)

        with mock.patch.object(cli.variant_source, "validate_variants", return_value=result):
            code, stdout, stderr = run_cli(["variant", "validate", "alpha", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["command"], "variant validate")

    def test_variant_validate_text_formats_base_issue_without_target(self):
        result = {
            "skill": "alpha",
            "valid": False,
            "variant_count": 1,
            "issues": [
                {
                    "code": "variant_base_missing",
                    "skill": "alpha",
                    "path": "/portable/skills/alpha",
                    "message": "canonical Base Skill does not exist",
                }
            ],
            "variants": [{"skill": "alpha", "target": "kimi", "valid": False}],
        }
        with mock.patch.object(
            cli.variant_source, "validate_variants", return_value=result
        ):
            code, stdout, stderr = run_cli(["variant", "validate", "alpha"])

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("invalid", stdout)
        self.assertIn("alpha: canonical Base Skill does not exist", stdout)

    def test_variant_help_lists_only_source_management_actions(self):
        parser = cli._build_parser()
        help_text = parser.format_help()
        self.assertIn("variant", help_text)
        variant_parser = next(
            action.choices["variant"]
            for action in parser._actions
            if getattr(action, "choices", None) and "variant" in action.choices
        )
        variant_help = variant_parser.format_help()
        self.assertIn("list", variant_help)
        self.assertIn("create", variant_help)
        self.assertIn("validate", variant_help)
        self.assertNotIn("resolve", variant_help)
        self.assertNotIn("delete", variant_help)


if __name__ == "__main__":
    unittest.main()
