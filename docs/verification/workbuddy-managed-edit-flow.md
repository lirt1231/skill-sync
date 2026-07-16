# WorkBuddy Managed Edit Verification

Date: 2026-07-16

Client: WorkBuddy (`workbuddy`)

Fixture: `tests/fixtures/workbuddy/managed-edit-flow.json`

## Scope

This record reproduces a WorkBuddy-authored change with real WorkBuddy path
semantics in an isolated home directory. The WorkBuddy Skill path is a link to
an immutable rendered deployment, and the canonical Skill lives under
`~/.agents/skills`. No user Skill or live Agent link is changed by the test.

A read-only probe on the development machine also confirmed that WorkBuddy is
installed and that
`~/.workbuddy/skills/skill-sync-manager/SKILL.md` is reported as a healthy
`managed-deployment` with role `rendered-deployment-link` for concrete client
`workbuddy`. The mutation sequence below remains isolated so verification does
not alter that live managed Skill.

## Reproduced sequence

1. `skill-sync managed check "$WORKBUDDY_HOME/skills/alpha/SKILL.md" --client workbuddy --json`
2. `skill-sync edit begin alpha --base --actor workbuddy --json`
3. Write the requested content only to `.result.workspace_path/SKILL.md`.
4. `skill-sync edit diff <session-id> --json`
5. `skill-sync edit validate <session-id> --json`
6. `skill-sync edit impact <session-id> --json`
7. `skill-sync edit apply <session-id> --json`

The test consumes session fields from the JSON envelope's `.result` object,
matching the contract used by the `skill-sync-manager` Skill.

## Verified boundaries

- `managed check` identifies the WorkBuddy path as a healthy
  `rendered-deployment-link` before any authored write.
- WorkBuddy writes only `workspace_path/SKILL.md`. Canonical content, the
  current deployment, and the WorkBuddy link remain byte-for-byte unchanged
  through `diff`, `validate`, and `impact`.
- `impact` reports an unblocked WorkBuddy rebuild before `apply` is allowed.
- `apply` transactionally updates canonical content, creates a verified new
  WorkBuddy deployment, and switches the WorkBuddy link. The old immutable
  deployment remains unchanged.
- Every Git command is blocked by the test. The full flow completes without a
  Git call, commit, `skill-sync push`, or Git push.

## Reproduce

From the repository root:

```bash
python -m unittest tests.test_workbuddy_managed_edit_flow -v
```

The fixture is deliberately client-specific so the Codex, WorkBuddy, Kimi,
and Claude verification commits remain independent and can be merged in
roadmap order without sharing mutable test setup.
