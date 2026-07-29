---
name: skill-sync-manager
description: Inspect, configure, safely edit, recover, link, and synchronize user-authored Agent Skills managed by Skill Sync. Use before modifying any Skill path that may be managed, when setting up Skill Sync with a private data repository, when exposing Skills to Codex, WorkBuddy, Kimi Code, or Claude Code, and when diagnosing conflicts, stale or tampered deployments, edit sessions, or synchronization failures.
---

# Skill Sync Manager

Use the `skill-sync` executable as the control plane for managed Skills. Treat
Agent Skill paths and rendered deployments as read-only outputs. Modify a
managed Skill only through the isolated workspace returned by `skill-sync edit
begin`.

## Establish the environment

1. Check that the CLI exists and is current enough:

   ```bash
   skill-sync version
   skill-sync managed check --help
   skill-sync edit begin --help
   ```

   This workflow requires Skill Sync 0.1.0 or later. If a command is missing,
   stop and ask the user to install or upgrade the public tool repository. Do
   not replace a missing command with direct filesystem edits.
2. Run `skill-sync preview --json`. This is a cached, local inspection and does
   not fetch the network.
3. If Skill Sync is not initialized, ask for the user's private Skill data
   repository URL before running:

   ```bash
   skill-sync init --repo <private-skill-data-repository>
   ```

   Never use `https://github.com/lirt1231/skill-sync` as the data repository.
   That public repository distributes the tool and this manager Skill; the
   user's authored Skills belong in a separate repository that should normally
   be private.
4. Run `skill-sync doctor --json` before the first mutation and after setup.

The default canonical root is `~/.agents/skills`. Per-machine configuration is
stored under `~/.config/skill-sync/config.json`; rendered deployments, edit
sessions, backups, and receipts stay in the platform-specific local data root.
Do not copy those machine-local files into the user's Skill data repository.

## Mandatory pre-edit gate

Before writing, deleting, renaming, or generating files in any existing Skill:

1. Identify the exact path and concrete client ID. Supported client IDs are
   `codex`, `workbuddy`, `kimi-code`, and `claude-code`.
2. Run the read-only ownership check:

   ```bash
   skill-sync managed check <exact-path-or-skill-name> --client <client-id> --json
   ```

   Omit `--client` only for an unambiguous canonical path or logical Skill name.
3. Read command data from `.result` in the JSON envelope and enforce it:
   - `managed=false`: Skill Sync does not control the path. Continue with the
     normal local workflow, but never import or globalize it implicitly.
   - `managed=true, healthy=true`: do not edit the checked path, deployment, or
     canonical source directly. Use a managed edit session.
   - `managed=true, healthy=false`: stop and diagnose or recover first.
   - Ambiguous output, a safety error, or exit code `4`: fail closed. Report the
     details and never guess ownership.

Include this ownership check in every execution plan that can modify an
existing Skill. A writable path is not evidence that direct editing is safe.

## Choose the authored scope

Use the smallest scope matching the request:

- `--base`: shared content that should reach every configured client.
- `--family <id>`: content shared by a registered Agent family. The `kimi`
  family currently contains only `kimi-code`.
- `--client <id>`: content intended for one concrete client only.

Never widen Client to Family or Base for convenience. Ask the user when two
possible scopes have different effects.

## Run a managed edit

1. Create exactly one session:

   ```bash
   skill-sync edit begin <skill> --base --actor <client-id> --json
   skill-sync edit begin <skill> --family <family-id> --actor <client-id> --json
   skill-sync edit begin <skill> --client <client-id> --actor <client-id> --json
   ```

2. Read `.result.session_id` and `.result.workspace_path`. Edit only that exact
   workspace. Never edit its parent session directory, baseline snapshot,
   canonical source, rendered deployment, or Agent link.
3. Inspect in order:

   ```bash
   skill-sync edit diff <session-id> --json
   skill-sync edit validate <session-id> --json
   skill-sync edit impact <session-id> --json
   ```

   For a Family session, add `--resolved-client <client-id>` to `edit diff` when
   one concrete-client view needs separate inspection.
4. Stop when validation is invalid, the authored baseline is stale, impact is
   blocked, scope differs from the request, or any client state is unsafe.
5. Summarize the authored diff, resolved client changes, and impact for the
   user. Require confirmation before applying unless the user already gave
   explicit authorization for this exact reviewed change.
6. Apply and read back the result:

   ```bash
   skill-sync edit apply <session-id> --json
   skill-sync doctor --json
   ```

7. Stop on `cleanup_pending` or `needs-recovery`. Preserve every reported
   receipt, backup, quarantine, and recovery path.

Use `skill-sync edit abort <session-id> --json` only to discard an active
workspace. Do not abort `applying` or `needs-recovery` sessions. Use
`skill-sync edit delete <session-id> --json` only when the user explicitly asks
to remove deletable local session history.

`edit apply` never commits or pushes Git. Synchronization is a separate action
that requires explicit user authorization.

## Recover a tampered deployment

When inspection reports `tampered-render`, preview without mutation:

```bash
skill-sync edit recover <skill> --client <client-id> --json
```

Require the user to choose one action:

- Preserve changes: use `--capture`, then review the new session through diff,
  validate, impact, and apply.
- Discard changes: use `--discard` to quarantine and rebuild from canonical.

Never choose capture versus discard automatically. Layered deployment previews
offer discard only. A single-client recovery must not attempt to repair a
multi-client transactional `needs-recovery` state.

## Synchronize and expose Skills

1. Run `skill-sync preview --json` to explain the next action without network
   access.
2. Run `skill-sync doctor --json` and stop on unsafe ownership, real-directory
   conflicts, damaged deployments, or unresolved receipts.
3. Select only Skills explicitly requested:

   ```bash
   skill-sync scan --json
   skill-sync select <skill-name>...
   ```

4. Import existing real Agent-local Skills through the CLI instead of moving
   them manually:

   ```bash
   skill-sync import --agent <codex|claude|workbuddy> <skill-name>...
   ```

5. Use `skill-sync sync` for the explicit network synchronization workflow.
   It may fetch, pull, commit, or push according to the previewed state.
6. Expose selected Skills to detected clients with `skill-sync link`. Restrict
   scope with repeatable `--skill` and `--agent` filters when requested.
7. Start `skill-sync web` only when the user wants the local visual interface.

Stop rather than selecting a conflict winner when local and remote changes
touch the same synchronization unit. Do not force-push.

## Install or update this manager Skill

The distributable source is
`https://github.com/lirt1231/skill-sync/tree/main/skills/skill-sync-manager`.

- For a first installation, follow the public repository README and refuse to
  overwrite an existing destination directory.
- If the installed manager is already managed, run the mandatory ownership
  check and update it through a Base edit session. Copy the repository version
  only into that session's workspace, then diff, validate, impact, and request
  confirmation before apply.
- Never update this Skill by writing through an Agent link or by replacing its
  canonical directory in place.

## Safety rules

- Never modify a managed Agent path, rendered deployment, or canonical source
  directly.
- Never delete or overwrite a real directory in an Agent Skill location.
- Stop on `conflict`, `wrong-link`, `tampered-render`, ambiguous ownership,
  stale baselines, or locally changed content.
- Never upload bundled or third-party Skills without explicit user direction.
- Never expose credentials, private Skill content, edit sessions, backups,
  receipts, or machine-local absolute paths in a public repository or report.
- Never force-push or choose a conflict winner automatically.
