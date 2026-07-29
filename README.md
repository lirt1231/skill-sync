# Skill Sync

[![CI](https://github.com/lirt1231/skill-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/lirt1231/skill-sync/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)

Synchronize selected user-authored Agent Skills across devices and expose one
managed source to Codex, WorkBuddy, Kimi Code, and Claude Code.

> **Technical preview:** Skill Sync is pre-1.0 software that manages local
> files, Git repositories, and Agent links. Keep an independent backup and
> review every mutation plan before confirming it.

## What it does

- Keeps one canonical local Skill under `~/.agents/skills`.
- Synchronizes only explicitly selected Skills through your own private Git
  repository.
- Creates verified links or Windows junctions for detected Agent clients.
- Supports Base, Agent-family, and exact-client variants without duplicating
  unchanged files.
- Provides a local Web UI for diagnosis, repair, import, managed editing, and
  explicit synchronization.

The public `skill-sync` tool repository and your private Skill data repository
are different repositories. Skill content, credentials, edit sessions,
deployments, backups, and local machine paths are not stored in this project.

## Platform status

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| CLI synchronization and local Web UI | Verified | Automated tests | Automated tests; real-machine smoke pending |
| Agent links | Symbolic links | Symbolic links | Symbolic link or directory junction |
| Open Codex/Kimi Code edit session | Supported | Not yet supported | Not yet supported |

Detected Skill targets are Codex, WorkBuddy, Kimi Code, and Claude Code. Agent
installation paths vary by client; run `skill-sync doctor --json` before the
first mutation.

## Requirements

- Python 3.10+
- Git
- An empty or existing private Git repository for your Skill data

## Install skill-sync

Install directly from the tool's Git repository with
[pipx](https://pipx.pypa.io/). This installs the `skill-sync` command in an
isolated environment and makes it available from any directory:

```bash
pipx install "git+https://github.com/lirt1231/skill-sync.git"
skill-sync version
```

Install the tool over SSH instead when preferred:

```bash
pipx install "git+ssh://git@github.com/lirt1231/skill-sync.git"
```

Upgrade an existing installation after the tool repository changes:

```bash
pipx upgrade agent-skill-sync
```

The same commands work in macOS/Linux shells and Windows PowerShell. Install
Python 3.10+, Git, and pipx first. On Windows, if `skill-sync` is not found
after installation, run `pipx ensurepath` and open a new PowerShell window.

## Quick start

```bash
skill-sync init --repo git@github.com:YOUR_ACCOUNT/agent-skills.git
skill-sync scan
skill-sync select my-skill
skill-sync push --message "Add my Skills"
skill-sync link
skill-sync web
```

The repository passed to `init` contains your Skill data and should normally be
private. Opening the Web UI does not fetch the network; synchronization remains
an explicit action.

## Daily use

The Web UI is the easiest daily workflow. On a new computer it starts with a
guided setup screen: provide the private Git repository, confirm the canonical
Skill directory, and let Skill Sync detect supported Agents. Git credentials
remain in your existing SSH agent, credential manager, or system Git setup;
the UI never stores a token.

The homepage intentionally uses cached Git refs, so opening it does not access
the network. It tells you whether the next safe action is to pull, push, repair
links, or resolve a conflict. Clicking **Sync** is the explicit action that
checks the remote repository.

The equivalent CLI inspection command is:

```bash
skill-sync preview
skill-sync preview --json
```

### Edit through Codex or Kimi Code

On macOS, a Skill detail page can create an isolated managed workspace and open
it in an installed Codex or Kimi Code terminal session. The Agent receives the
exact Skill, scope, workspace path, safety boundaries, and completion workflow.
It cannot apply, synchronize, commit, or push the result on the user's behalf.

After editing, return to Skill Sync, select **Check Changes**, review the diff,
validation, and client impact, then explicitly apply or discard the session.

### Machine-readable CLI output

`version`, `scan`, `status`, `preview`, `doctor`, `managed check`, `resolve`,
`diff`, all current `variant` and `edit` subcommands, `deploy status`, and
`deploy gc` support `--json`. During the pre-1.0 releases, their
machine-readable contract uses schema version 1 and always returns the same
top-level envelope:

```json
{
  "schema_version": 1,
  "command": "version",
  "ok": true,
  "result": {"version": "0.1.0"},
  "warnings": [],
  "errors": []
}
```

Command-specific data is contained in `result`. New compatible fields may be
added without changing `schema_version`; removing or changing existing fields
requires a schema version increase.

In JSON mode, a `SkillSyncError` is emitted as the same envelope with
`ok: false` and structured entries in `errors`. The error envelope is written
to stderr and the command returns its structured exit code:

- `0`: success
- `1`: operation failed
- `2`: invalid CLI usage (reported by `argparse`)
- `3`: conflict requiring a user decision
- `4`: operation blocked by a safety check

Text mode remains intended for interactive use and keeps its concise output.

### Create and inspect Variant sources

The complete current model and safety invariants are in
[Variant Resolution Architecture](docs/architecture/variant-resolution.md).
Upgrade, rollback, and multi-device procedures are in
[Registry v3 Migration](docs/registry-v3-migration.md).

Portable client differences live beside the canonical Skill root. With the
default `~/.agents/skills` root, Variant sources are stored under
`~/.agents/variants/<skill>/<target>/`; a custom `skills_root` likewise uses a
sibling `variants` directory. These machine paths are derived from local
configuration and are never written into the portable registry.

Create either a registered Agent-family overlay or a registered concrete-client
overlay, then inspect it:

```bash
skill-sync variant create my-skill --family kimi
skill-sync variant create my-skill --client kimi-code
skill-sync variant list
skill-sync variant list --skill my-skill --json
skill-sync variant validate my-skill
```

`variant create` atomically creates only a minimal `variant.yaml`; it does not
copy the Base Skill, resolve an overlay, rebuild a deployment, alter Agent
links, or invoke Git. It records the target in portable registry schema v3;
source publication and registry update share one rollback boundary. A minimal
overlay with no content files is valid. Family and client flags are mutually
exclusive and checked against the static client registry. An existing target,
unsafe path, linked source, or case-insensitive name ambiguity is a stop
condition.

`variant list` and `variant validate` are fully local and read-only. They
cross-check each discovered or requested Variant Skill against a real,
portable, link-free canonical Base using the same read-only Base plan and path
rules as the 7.2 resolver. They report malformed manifests, orphan or unsafe
Bases, and invalid top-level Skill names in `issues` without hiding other
inspectable Variant rows. Each JSON row exposes `manifest_valid`,
`base_valid`, `skill_name_valid`, and their conjunction `valid`; callers must
also check the top-level `result.valid` field before treating a Variant source
as usable. `overlay_file_count` excludes only the target root's manifest, so a
nested file named `variant.yaml` remains normal overlay content.

Inspect one resolved client view, or compare it with the authored Base, without
creating a rendered directory:

```bash
skill-sync resolve my-skill --client kimi-code --dry-run
skill-sync resolve my-skill --client codex --dry-run --json
skill-sync diff my-skill --base --client kimi-code
skill-sync diff my-skill --base --client claude-code --json
```

Both commands derive Base and resolved sides from one immutable, layered
snapshot in Base → family → exact-client order. `resolve` requires the explicit
`--dry-run` safety flag in this release and reports ordered layers and hashes;
its JSON output also reports final-file origins. It never accepts an output
path. `diff` reports deterministic added/modified/deleted paths. Binary files
and files whose combined comparison input exceeds 64 KiB are metadata-only
(hash, size, and portable mode). Text unified-diff input is also capped at
256 KiB across one command, in deterministic path order; later changes become
metadata-only when that budget is exhausted.
Neither command materializes a deployment, changes source/config/registry
state, invokes Git, fetches, commits, or pushes.

Variant support includes local source management, read-only resolution
inspection, transactional family/client edit sessions, content-addressed
layered deployments, and portable multi-device synchronization. Creating or
first publishing a Variant lazily upgrades the portable registry to schema v3
and records sorted target intent. Existing v1/v2 registries remain unchanged
until that point. A v3 `push` packages only registry-declared Variant targets;
`pull` reconstructs them below the receiving machine's locally configured
portable root. `preview` and `status` report Base and Variant sync units
separately. Different units can merge during `sync`, while simultaneous local
and remote changes to the same unit stop before fast-forward or source writes.
No sync path automatically pushes local changes. The Web UI does not yet expose
tamper capture/discard recovery, and there is no `resolve --output` or
`variant delete` command.

Before migrating existing Agent links away from editable canonical sources,
preview every affected Skill/client pair, perform the migration, and inspect
the rendered deployment state:

```bash
skill-sync deploy preview
skill-sync deploy migrate
skill-sync deploy status
skill-sync deploy status --json
skill-sync deploy gc --dry-run
```

`deploy preview` is read-only. `deploy migrate` renders immutable snapshots and
switches managed Agent links only after the deployment is verified. Deployment
garbage collection removes only verified snapshots that no detected client
currently references; run it with `--dry-run` first.

Before modifying a Skill from an Agent client, inspect the exact path first:

```bash
skill-sync managed check ~/.codex/skills/my-skill/SKILL.md --client codex
skill-sync managed check ~/.codex/skills/my-skill/SKILL.md --client codex --json
```

A completed check exits with `0` for both managed and unmanaged paths. Read the
`managed` field in JSON mode. Wrong or broken managed links remain
`managed: true` with `healthy: false`; ambiguous ownership exits nonzero so an
Agent cannot mistake an inconclusive check for permission to edit. The check is
fully local: it loads the configured registry and detected client paths but
does not fetch, write files, or change links.

### Edit a managed Base Skill safely

An Agent must not edit a path reported as managed, even when the path appears
to be inside that Agent's normal Skills directory. Managed Agent paths point to
rendered deployments, while `~/.agents/skills` is the canonical source. Use a
Base edit session whenever a portable change should reach every affected Agent
client.

First check the exact file or Skill path that the Agent intends to change:

```bash
skill-sync managed check ~/.codex/skills/my-skill/SKILL.md --client codex --json
```

Continue only after the command completes conclusively. If `managed` is
`false`, Skill Sync imposes no managed-edit workflow. If `managed` is `true`
and `healthy` is `false`, repair the reported state before editing. An
ambiguous result exits nonzero and must be treated as a stop condition.

For a healthy managed Skill, create a Base session and edit only the absolute
workspace path printed by `edit begin`:

```bash
skill-sync edit begin my-skill --base --actor codex
# Record the printed session ID and edit only the printed Workspace directory.

skill-sync edit diff <session-id>
skill-sync edit validate <session-id>
skill-sync edit impact <session-id>
skill-sync edit apply <session-id>
```

To prepare an isolated authored Variant-layer workspace, choose exactly one
registered family or concrete client:

```bash
skill-sync edit begin my-skill --family kimi --actor kimi-code
skill-sync edit begin my-skill --client codex --actor codex
```

An existing Variant session contains only that authored overlay layer, never a
copy of Base or a resolved deployment. If the Variant does not exist, the
baseline and workspace contain only a minimal `variant.yaml`; begin and abort
do not create the canonical Variant source. `edit diff` reports both the
authored-layer diff and each affected client's resolved diff, `edit validate`
checks the overlay in client context, and `edit impact` distinguishes Base,
Family, and Client blast radius. Use
`edit diff <session-id> --resolved-client <id>` to limit a Family preview to
one affected concrete client.

`diff` shows authored file changes and, for a Variant scope, resolved client
changes. `validate` rejects unsafe paths, links, invalid Base `SKILL.md` content,
and invalid Variant manifests or overlays. `impact` is a read-only preview of
the concrete clients whose resolved output would change; do not apply when it
reports `Blocked: yes`. `apply` rechecks the authored-layer baseline and
workspace, replaces or first publishes exactly that Base/Family/Client source
transactionally, rebuilds only affected enabled and detected client
deployments, verifies them, and switches their Agent links as one operation.
A Base edit preserves applicable Variant layers; a Family edit such as `kimi`
affects Kimi Code; an exact Client edit affects only that client. Unaffected
clients are neither rebuilt nor relinked.

Use these inspection and cancellation commands when needed:

```bash
skill-sync edit list
skill-sync edit status <session-id>
skill-sync edit abort <session-id>
```

`abort` is for an active session whose workspace should be discarded. The
durable session states are:

- `active`: the workspace can be edited and inspected;
- `applying`: an apply transaction is in progress;
- `applied`: canonical content and affected deployments committed;
- `aborted`: the workspace was discarded without applying it;
- `needs-recovery`: rollback or durable state is uncertain; stop editing and
  inspect `skill-sync doctor --json`, the session, and the receipt/recovery
  paths reported by the failed command.

A normal failure before the transaction commits restores the old Agent links
and canonical content, marks the operation receipt `rolled-back`, and returns
the session to `active`. After canonical content, links, session metadata, and
the completed receipt have committed, leftover backup cleanup failures are
reported in `cleanup_pending`; the edit is still applied and must not simply be
run again. Preserve every reported backup, receipt, quarantine, and recovery
path until the state has been reconciled.

`edit apply` is a local content/deployment transaction. It never runs Git,
creates a Git commit, calls `skill-sync push`, or pushes a remote repository.
Review the applied result first, then synchronize it only with a separate,
explicit command authorized by the user, for example:

```bash
skill-sync push --skill my-skill --message "Update my-skill"
```

All `managed check` and `edit` commands shown in the workflow accept `--json`.
In JSON mode, read fields such as `valid`, `blocked`, `status`, `receipt_path`,
and `cleanup_pending` instead of parsing the human-readable text.

### Recover a tampered rendered deployment

If an Agent or editor wrote through a managed Agent path, the rendered
deployment may be reported as `tampered-render`. Preview the authored-content
diff first; this command is read-only and excludes Skill Sync provenance:

```bash
skill-sync edit recover my-skill --client codex
skill-sync edit recover my-skill --client codex --json
```

Then choose exactly one explicit action:

```bash
# Preserve the authored changes in a new active Base edit session.
skill-sync edit recover my-skill --client codex --capture

# Discard the authored changes and rebuild from current canonical layers.
skill-sync edit recover my-skill --client codex --discard
```

`--capture` does not apply the tampered files directly. It copies safe authored
content into a new writable workspace; continue with `edit diff`, `validate`,
`impact`, and `apply` using the returned session ID. `--discard` quarantines
the tampered deployment, rebuilds it from current canonical Base/Variant
content, and preserves the existing verified Agent link. Capturing a layered
deployment is blocked until a scoped recovery workflow can attribute changes to
one authored layer; its preview offers `discard` only. Both supported actions
write a local recovery receipt but perform no Git operation.

Capture and discard fail closed when another unfinished session or ambiguous
receipt/recovery state exists. They recover one tampered concrete-client
deployment; they do not automatically reconcile a multi-link transactional
`needs-recovery` state left by `edit apply`. Cleanup after a committed recovery
can likewise return `cleanup_pending` without undoing the successful action.

When local and remote content both changed, Skill Sync stops. The UI shows the
affected state and can create a timestamped local backup, but never chooses a
version, overwrites a real directory, or merges content for you.

Import an existing real Skill directory from Codex, Claude Code, or WorkBuddy
into the canonical root:

```bash
skill-sync import --agent codex my-skill
skill-sync import --agent claude another-skill
skill-sync import --agent workbuddy team-helper
```

The import copies and hash-verifies the Skill under `~/.agents/skills`, replaces the original Agent directory with a link, and selects the Skill for synchronization. A different same-name global Skill is reported as a conflict and neither copy is changed.

To detach a global Skill back into an Agent as a real local directory, use a
copy instead of a link. This is useful after importing the wrong Skill: copy
it back to the original Agent, then deselect or delete the global copy when
ready. The command never overwrites an existing real Agent directory.

```bash
skill-sync copy --skill my-skill --agent codex
skill-sync copy --skill my-skill --agent claude
skill-sync copy --skill my-skill --agent workbuddy
```

Open <http://127.0.0.1:8765> to view the Skill-by-Agent matrix.

The Web UI supports search and bulk select/deselect, link repair, Agent
enable/disable, safe import, local backups, and permanent deletion. Deletion
removes only verified managed Agent links, removes the Skill from the registry,
and asks for browser confirmation before the request.

On another computer:

```bash
pipx install "git+ssh://git@github.com/lirt1231/skill-sync.git"
skill-sync init --repo git@github.com:YOUR_ACCOUNT/agent-skills.git
skill-sync sync
skill-sync doctor
```

`init` is required only once per computer. It clones the private Skill data
repository and records the local paths. The selected Skill list lives in that
repository, so it does not need to be selected again on each computer.

`sync` installs selected Skills into `~/.agents/skills` and creates safe links for detected clients:

- Codex: `${CODEX_HOME:-~/.codex}/skills`
- WorkBuddy: `${WORKBUDDY_HOME:-~/.workbuddy}/skills`
- Kimi Code: `$KIMI_CODE_SKILLS_DIR` or `~/.config/agents/skills`
- Claude Code: `${CLAUDE_HOME:-~/.claude}/skills`

On macOS/Linux the links are symbolic links. On Windows the tool first tries a directory symbolic link and falls back to a directory junction. Windows `.lnk` shortcuts are intentionally unsupported.

### Windows PowerShell example

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# Open a new PowerShell window after ensurepath.
pipx install "git+ssh://git@github.com/lirt1231/skill-sync.git"
skill-sync init --repo git@github.com:YOUR_ACCOUNT/agent-skills.git
skill-sync sync
skill-sync web
```

GitHub SSH authentication must already work on the new computer. HTTPS URLs
can be used instead when Git Credential Manager or another credential helper is
configured.

## Safety

Skill Sync never overwrites a real directory in an Agent Skill location. Run `skill-sync doctor --json` to inspect conflicts, broken links, and missing canonical Skills. Git divergence and simultaneous local/remote changes stop for manual resolution.

The Web UI only binds to a loopback address and requires a per-process token for mutations.

See [SECURITY.md](SECURITY.md) for the local trust model and private reporting
instructions. Development setup and safety requirements are documented in
[CONTRIBUTING.md](CONTRIBUTING.md).
