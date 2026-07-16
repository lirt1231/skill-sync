"""Documentation contracts for the implemented Variant resolution boundary."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

from skill_sync import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
ARCHITECTURE = PROJECT_ROOT / "docs" / "architecture" / "variant-resolution.md"
ROADMAP = (
    PROJECT_ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-11-skill-sync-platform-roadmap.md"
)


class VariantDocsContractTest(unittest.TestCase):
    def module_help(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "skill_sync.cli", *arguments, "--help"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return completed.stdout

    def test_module_help_matches_documented_variant_command_surface(self):
        top = self.module_help()
        self.assertIn("resolve", top)
        self.assertIn("diff", top)
        self.assertIn("variant", top)

        resolve = self.module_help("resolve")
        self.assertIn("skill-sync resolve", resolve)
        self.assertIn("--client", resolve)
        self.assertIn("--dry-run", resolve)
        self.assertIn("--json", resolve)
        self.assertNotIn("--output", resolve)

        diff = self.module_help("diff")
        self.assertIn("skill-sync diff", diff)
        self.assertIn("--base", diff)
        self.assertIn("--client", diff)
        self.assertIn("--json", diff)

        variant = self.module_help("variant")
        for action in ("list", "create", "validate"):
            self.assertIn(action, variant)
        self.assertNotIn("delete", variant)

    def test_architecture_bash_examples_are_accepted_by_the_real_parser(self):
        architecture = ARCHITECTURE.read_text(encoding="utf-8")
        commands = [
            line.strip()
            for block in re.findall(r"```bash\n(.*?)```", architecture, re.DOTALL)
            for line in block.splitlines()
            if line.strip().startswith("skill-sync ")
        ]
        self.assertEqual(len(commands), 9)
        parser = cli._build_parser()
        for command in commands:
            with self.subTest(command=command):
                parsed = parser.parse_args(shlex.split(command)[1:])
                self.assertTrue(callable(parsed.handler))

    def test_docs_state_safety_budgets_and_current_migration_limits(self):
        readme = README.read_text(encoding="utf-8")
        architecture = ARCHITECTURE.read_text(encoding="utf-8")
        roadmap = ROADMAP.read_text(encoding="utf-8")

        for text in (readme, architecture):
            self.assertIn("64 KiB", text)
            self.assertIn("256 KiB", text)
            self.assertNotRegex(
                text,
                r"skill-sync resolve[^\n]*--output",
            )

        required_architecture_terms = (
            "Base → family → exact client",
            "In the JSON/machine model",
            "variant_source_changed",
            "ambiguity introduced during the read transaction",
            "diff_omitted=total_size_limit",
            "no Variant-aware registry schema",
            "no Variant-aware deployment cache rebuild",
            "no Family/Client edit-session scope",
            "no Web Variant badges",
        )
        for term in required_architecture_terms:
            with self.subTest(term=term):
                self.assertIn(term, architecture)

        self.assertIn("Implemented through 7.6", roadmap)
        self.assertIn("Planned, not implemented", roadmap)
        self.assertIn("resolve <skill> --client <id> --dry-run", roadmap)
        self.assertIn("resolve <skill> --client <id> --output <path>", roadmap)


if __name__ == "__main__":
    unittest.main()
