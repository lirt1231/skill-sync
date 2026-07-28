# Claude Code and Kimi Managed Edit Verification

Date: 2026-07-16  
Clients: Claude Code (`claude-code`) and Kimi Code (`kimi-code`)

## Scope

The fixtures reproduce the manager-prescribed flow with each client's real
Skill directory semantics in an isolated home directory. They do not change
the user's current global Skills or live Agent links.

The local machine audit also confirmed that the live `skill-sync-manager`
deployment is a healthy managed link in both directories:

- `~/.claude/skills/skill-sync-manager`
- `~/.config/agents/skills/skill-sync-manager`

## Reproduced sequence

Every client fixture executes the same contract:

1. `skill-sync managed check <client-skill-path> --client <client-id> --json`
2. `skill-sync edit begin alpha --base --actor <client-id> --json`
3. Write only to `.result.workspace_path/SKILL.md`.
4. Run `edit diff`, `edit validate`, and `edit impact`.
5. Run the explicit `edit apply` action.

Each run starts from one concrete client path and verifies that its endpoint
stays unchanged before apply and switches to a verified deployment after
apply.

## Verified boundaries

- Ownership is checked before the only Agent-authored write.
- Canonical content, rendered deployments, and client links remain unchanged
  through `diff`, `validate`, and `impact`.
- The old immutable deployments remain byte-for-byte unchanged after apply.
- Every resulting deployment has provenance for its concrete client.
- Git access is blocked during the test, proving there is no implicit fetch,
  commit, sync, or push.

## Reproduce

```bash
python -m unittest tests.test_remaining_clients_managed_edit_flow -v
```

These are compatibility fixtures and verification records only. They do not
add client-specific edit behavior or change the managed-edit schema.
