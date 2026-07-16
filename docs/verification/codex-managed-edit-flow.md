# Codex Managed Edit Verification

Date: 2026-07-16

Client: Codex (`codex`)

Fixture: `tests/fixtures/codex/managed-edit-flow.json`

## Scope

This record reproduces a Codex-authored change with real Codex path semantics in
an isolated home directory. The Codex Skill path is a link to an immutable
rendered deployment, and the canonical Skill lives under `~/.agents/skills`.
No user Skill or live Agent link is changed by the test.

## Reproduced sequence

1. `skill-sync managed check "$CODEX_HOME/skills/alpha/SKILL.md" --client codex --json`
2. `skill-sync edit begin alpha --base --actor codex --json`
3. Write the requested content only to `.result.workspace_path/SKILL.md`.
4. `skill-sync edit diff <session-id> --json`
5. `skill-sync edit validate <session-id> --json`
6. `skill-sync edit impact <session-id> --json`
7. `skill-sync edit apply <session-id> --json`

The test consumes session fields from the JSON envelope's `.result` object,
matching the contract used by the `skill-sync-manager` Skill.

## Verified boundaries

- `managed check` identifies the Codex path as a healthy
  `rendered-deployment-link` before any authored write.
- Codex writes only `workspace_path/SKILL.md`. Canonical content, the current
  deployment, and the Codex link remain byte-for-byte unchanged through
  `diff`, `validate`, and `impact`.
- `impact` reports an unblocked Codex rebuild before `apply` is allowed.
- `apply` transactionally updates canonical content, creates a verified new
  Codex deployment, and switches the Codex link. The old immutable deployment
  remains unchanged.
- Every Git command is blocked by the test. The full flow completes without a
  Git call, commit, `skill-sync push`, or Git push.

## Reproduce

From the repository root:

```bash
python -m unittest tests.test_codex_managed_edit_flow -v
```

The fixture is deliberately client-specific so later WorkBuddy, Kimi, and
Claude verification commits can remain independent and be merged in roadmap
order without sharing mutable test setup.
