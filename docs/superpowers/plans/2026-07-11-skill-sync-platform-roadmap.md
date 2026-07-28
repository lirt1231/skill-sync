# Skill Sync Platform Roadmap

## 1. Product Direction

Evolve Skill Sync from a local Skill linker into an Agent Skill adaptation
platform with three first-class responsibilities:

1. synchronize selected private Skills across devices through a user-owned Git
   repository;
2. manage which Skills are exposed to each installed Agent client;
3. maintain and resolve client-specific variants of the same logical Skill.

The product is not intended to become a public Skill marketplace. Its primary
user is a person, or an Agent acting for that person, who owns a curated set of
private Skills and needs them to behave consistently across machines and Agent
clients.

The positioning is:

> Agent-first, Git-native, conflict-safe Skill synchronization and client
> adaptation, with `~/.agents` as the user-owned source of truth.

## 2. Product Principles

### 2.1 One logical Skill, multiple resolved client versions

A Skill has one common base and may define differences for an Agent family or
a specific Agent client. Users should not normally maintain several complete
copies of the same Skill.

Resolution order:

1. common Base Skill;
2. Agent-family Variant, such as `kimi`, overrides Base paths;
3. the exact-client Variant, such as `kimi-code`, overrides both.

### 2.2 Source files and generated files must be distinguishable

Only source files under `~/.agents` are editable. A resolved client version may
be materialized into an application-managed cache, but that output is a
reproducible, read-only build artifact and must never become an authoring source.

Every managed Agent link points to a rendered deployment, including a Skill
that currently has no variant. Agent clients never point directly to editable
source content. This creates a review boundary between an Agent proposing a
change and that change becoming visible to other clients.

### 2.3 No silent destructive behavior

- Never overwrite a real directory in an Agent client.
- Never choose a Git conflict winner automatically.
- Never push without an explicit user action.
- Never edit a generated client version directly.
- Never interpret Windows `.lnk` shortcuts as directory links.
- Back up user content before any explicit conflict-resolution operation.

### 2.4 Agent clients are adapters, not public platform flags

Users select logical targets in the CLI and UI. Adapters own installation
detection, supported paths, link behavior, capabilities, and client identifiers.
There is no required `--platform` argument.

### 2.5 Keep the core lightweight

The CLI, Git engine, resolver, and local Web UI remain usable with Python and
Git. Native desktop packaging, a hosted service, accounts, telemetry, and a
public marketplace are outside the near-term scope.

## 3. Domain Model

### 3.1 Agent family and Agent client

Separate the current Agent target into two concepts:

- **Agent family**: the user-facing product group, such as `kimi`.
- **Agent client**: a concrete installation endpoint with a stable ID, such as
  `kimi-code` or `claude-code`.

Initial model:

| Family | Client ID | Default Skill location |
| --- | --- | --- |
| Codex | `codex` | `${CODEX_HOME:-~/.codex}/skills` |
| WorkBuddy | `workbuddy` | `${WORKBUDDY_HOME:-~/.workbuddy}/skills` |
| Kimi | `kimi-code` | `$KIMI_CODE_SKILLS_DIR` or `~/.config/agents/skills` |
| Claude | `claude-code` | `${CLAUDE_HOME:-~/.claude}/skills` |

The UI shows one Kimi group backed by the Kimi Code client.

### 3.2 Canonical filesystem layout

```text
~/.agents/
├── skills/
│   └── meeting-note/
│       ├── SKILL.md
│       ├── scripts/
│       └── references/
└── variants/
    └── meeting-note/
        ├── kimi/
        │   ├── variant.yaml
        │   └── SKILL.md
        ├── kimi-code/
        │   ├── variant.yaml
        │   ├── SKILL.md
        │   └── references/code.md
        └── codex/
            └── variant.yaml
```

Rules:

- `skills/<name>/` is the portable common base.
- `variants/<name>/<target>/` contains only client differences.
- Variant directories are not scanned as independent Skills.
- Variant target names must resolve to a registered family or client ID.
- All source content synchronized between devices remains under `~/.agents`.

### 3.3 Variant manifest

Use an explicit manifest rather than implicit directory replacement:

```yaml
version: 1
target: codex
mode: overlay
delete: references/claude-tools.md
```

The dependency-free v1 parser also accepts multiple delete paths as a
mapping-only YAML subset, keeping the portable format deterministic without a
general YAML dependency:

```yaml
delete:
  references/claude-tools.md: true
  scripts/legacy.sh: true
```

Variant behavior:

- Files present in the variant replace same-path base files.
- Files present only in the variant are added.
- Files listed in `delete` are removed from the resolved output.
- `SKILL.md` may be overridden like any other file.
- Paths must be relative, normalized, and confined to the Skill root.
- Paths use portable POSIX separators; Windows absolute/drive/UNC paths,
  reserved names, traversal, non-normalized paths, and case-insensitive
  duplicates fail closed.
- Symlinks inside base or variant sources remain rejected.
- Unknown manifest keys fail validation instead of being ignored silently.

`mode: replace` may be added later for exceptional clients, but the first
implementation should support only deterministic overlays to discourage full
Skill duplication.

### 3.4 Resolved build artifacts

Resolve every managed Skill into a content-addressed deployment cache. A
base-only deployment is still rendered instead of being linked directly to the
editable base:

```text
<local-data>/skill-sync/rendered/
└── sha256-<resolution-hash>/
    └── meeting-note/
        ├── SKILL.md
        └── references/
```

Requirements:

- Output is deterministic for the same base, variant chain, and resolver
  version.
- Build into a temporary sibling and atomically rename it into place.
- Write a provenance manifest containing base hash, applied variants, target,
  resolver version, and output hash.
- Mark the output read-only where practical, but do not rely on permissions for
  integrity.
- Verify its hash before treating a client link as managed.
- Garbage-collect only unreferenced generated outputs.
- Never include generated outputs in the private Skill repository.
- Treat a hash mismatch in rendered output as deployment tampering, not as an
  authored source change.

### 3.5 Portable registry

Extend the registry without writing machine paths into Git:

```yaml
version: 3
skills:
  meeting-note:
    selected: true
    targets: codex,workbuddy,kimi,claude
    variants: codex,kimi-code
```

The registry records intent. Detection results, absolute directories, rendered
cache paths, credentials, and local backup paths remain machine-local.

### 3.6 Managed edit session

An Agent client must not modify canonical source or a rendered deployment in
place. A managed edit is a local transaction with an explicit target scope:

- `base`: portable behavior intended for every client;
- `family:<id>`: behavior shared by clients in one Agent family;
- `client:<id>`: behavior specific to one concrete client.

Edit-session state is machine-local:

```text
<local-data>/skill-sync/edit-sessions/
└── <session-id>/
    ├── session.json
    ├── baseline/
    └── workspace/
```

`session.json` records the logical Skill, target scope, initiating client,
source hashes, resolved hashes, cached Git state, creation time, and affected
clients. It contains no credential.

Session rules:

- Allow at most one active local edit session per logical Skill by default.
- Copy only editable source inputs into the writable workspace.
- Never use a rendered deployment as the source of truth.
- Refuse apply if canonical source changed after the session began.
- Show source diff, resolved diff, and affected-client impact before apply.
- Validate Skill structure, variant manifest, paths, symlinks, and secret-scan
  findings before apply.
- Create a timestamped source backup before apply.
- Apply source changes atomically, then rebuild affected deployments and swap
  verified Agent links.
- `apply` changes local source and deployments only; it never commits or pushes.
- Keep an operation receipt with before/after hashes and backup ID.
- Expired or abandoned sessions may be resumed or aborted explicitly.

Read-only permissions on rendered output are a guardrail, not the sole security
boundary. `doctor` verifies provenance and content hashes. If an Agent bypasses
the workflow and changes rendered files, Skill Sync reports `tampered-render`
and offers to capture the diff into a new edit session before rebuilding the
clean deployment. It never promotes such edits automatically.

### 3.7 Managed ownership inspection

Before writing any Skill file, an Agent needs a read-only way to determine
whether the actual target path is owned by Skill Sync. Ownership inspection is
path-first because a project-local Skill and a global managed Skill may have the
same name.

The inspector accepts a Skill directory, `SKILL.md`, or any descendant file. It
walks to the logical Skill boundary, resolves POSIX symbolic links and Windows
junctions, and verifies ownership against all of the following:

- local Skill Sync configuration and portable registry;
- expected canonical source path;
- registered rendered deployment and provenance manifest;
- expected Agent link or junction target;
- content and resolution hashes when the path is a deployment;
- active edit-session workspaces.

A marker file alone is never sufficient proof of ownership. Inspection is
local-only and must not fetch Git refs or access the network.

Example machine-readable result:

```json
{
  "managed": true,
  "healthy": true,
  "state": "managed-deployment",
  "role": "deployment",
  "skill": "meeting-note",
  "input_path": "/home/user/.codex/skills/meeting-note/SKILL.md",
  "source_path": "/home/user/.agents/skills/meeting-note",
  "client": "codex",
  "resolution": ["base", "client:codex"],
  "edit_required": true,
  "active_session": null
}
```

Ownership and health are separate. A broken link, stale render, wrong link, or
tampered render may still belong to Skill Sync, so `managed` remains true while
`healthy` is false and `state` explains why. An ambiguous path is never treated
as safely unmanaged.

## 4. Target User Workflows

### 4.1 Create a client variant

```bash
skill-sync variant create meeting-note --client kimi-code
skill-sync variant validate meeting-note
skill-sync resolve meeting-note --client kimi-code --dry-run
skill-sync diff meeting-note --base --client kimi-code
```

Through 9.1, create/validate/resolve/diff remain local source and inspection
commands. Deployment preview/migrate, `link`, doctor, ownership, and managed
Base/Family/Client edit apply resolve local Variants. Creating or first
publishing a Variant records portable target intent in registry v3; `sync`,
`pull`, `push`, and Web flows are not yet Variant source-aware.

### 4.2 Inspect a resolved Skill

```bash
skill-sync resolve meeting-note --client codex --dry-run
skill-sync diff meeting-note --base --client codex
```

Inspection is read-only. Arbitrary output materialization is a later roadmap
capability and is deliberately absent from the current CLI.

### 4.3 Sync on a second machine

```bash
pipx install "git+ssh://git@github.com/USER/skill-sync.git"
skill-sync init --repo git@github.com:USER/agent-skills.git
skill-sync preview
skill-sync sync
skill-sync doctor
```

This is the target-state second-machine flow. Through 7.6, `sync` installs Base
sources but does not package or reconstruct `variants/`; Phase 2 must land
before this workflow is Variant-aware.

### 4.4 Resolve a Git conflict

The Web UI shows base and variants as separate conflict units. For each unit it
offers:

- keep local;
- use remote;
- keep both as separately named Skills or variants;
- open containing directories;
- create a timestamped backup;
- view a text diff when files are text.

No option is preselected, and applying a choice requires explicit confirmation.

### 4.5 Add an unsupported Agent client

```bash
skill-sync agent add my-agent \
  --family my-agent \
  --skills-dir ~/.my-agent/skills
skill-sync agent list --json
```

Custom adapters are machine-local by default because paths differ by device.
An optional portable adapter template may contain environment-variable-based or
home-relative paths but never absolute machine paths.

### 4.6 Modify a managed Skill from an Agent client

Before any write, inspect the exact target path:

```bash
skill-sync managed check ~/.codex/skills/meeting-note/SKILL.md \
  --client codex --json
```

If `managed` is true, the Agent must not write that path and must use an edit
session. If `managed` is false, Skill Sync imposes no managed-edit workflow and
the Agent follows the normal rules for that local Skill. If the state is
ambiguous or inspection fails, the Agent stops instead of assuming the Skill is
unmanaged.

For a portable change requested while using Codex:

```bash
skill-sync edit begin meeting-note --base --actor codex
# The Agent edits only the returned workspace path.
skill-sync edit diff <session-id>
skill-sync edit validate <session-id>
skill-sync edit impact <session-id>
skill-sync edit apply <session-id>
```

For Codex-specific tool vocabulary or behavior:

```bash
skill-sync edit begin meeting-note --client codex --actor codex
```

For behavior shared by the Kimi family:

```bash
skill-sync edit begin meeting-note --family kimi --actor kimi-code
```

Scope selection rules for `skill-sync-manager`:

1. Resolve the exact intended file or Skill path and run `managed check` before
   every modification.
2. If it is managed, never write through the Agent path or directly into the
   canonical source; start or resume a managed edit session.
3. If it is unmanaged, do not import or globalize it automatically.
4. Use `base` when the requested behavior is portable and should affect every
   client.
5. Use `family` when the difference follows one Agent product family.
6. Use `client` when the difference depends on a concrete client's tools,
   paths, syntax, or runtime behavior.
7. If scope is materially ambiguous, show the affected-client list and ask the
   user before starting the session.
8. A user request to modify a Skill authorizes local `edit apply` within that
   scope, but never authorizes Git push.

After apply, other Agent clients see the change only if their resolved version
is affected. An open Agent process may still require its normal Skill reload or
restart behavior.

## 5. CLI Roadmap

### 5.1 Variant commands

Variant inspection commands implemented through 8.4:

- `variant list [--skill name] [--json]`
- `variant create <skill> --family|--client <id>`
- `variant validate <skill> [--json]`
- `resolve <skill> --client <id> --dry-run [--json]`
- `diff <skill> --base --client <id> [--json]`

Planned, not implemented:

- `variant delete <skill> --family|--client <id>`
- `resolve <skill> --client <id> --output <path>`

### 5.2 Agent adapter commands

- `agent list [--json]`
- `agent enable|disable <family-or-client>`
- `agent add <id> --skills-dir <path>`
- `agent remove <id>`
- `agent detect [--json]`

### 5.3 Managed ownership commands

- `managed check <path-or-name> [--client id] [--json]`
- `managed list [--client id] [--json]`

Path input is preferred. `check` exits successfully when inspection completed,
whether `managed` is true or false, so Agents must read the structured result.
Inspection errors, ambiguous ownership, invalid configuration, and inaccessible
paths return a nonzero exit status. Human-readable output always includes the
recommended next action.

Stable ownership states include:

- `managed-source`;
- `managed-deployment`;
- `managed-edit-workspace`;
- `unmanaged`;
- `broken-link`;
- `wrong-link`;
- `stale-render`;
- `tampered-render`;
- `ambiguous`.

### 5.4 Managed editing commands

- `edit begin <skill> (--base|--family id|--client id) [--actor id]`
- `edit list [--json]`
- `edit status <session-id> [--json]`
- `edit diff <session-id> [--resolved-client id]`
- `edit validate <session-id> [--json]`
- `edit impact <session-id> [--json]`
- `edit apply <session-id>`
- `edit abort <session-id>`
- `edit recover <skill> --client <id>` for a tampered deployment

`begin` returns an absolute writable workspace path and a stable session ID.
`apply` requires an unchanged baseline, a successful validation result, and an
operation preview. It does not run Git commit or push.

### 5.5 History and recovery commands

- `history [skill] [--json]`
- `backup create [skill]`
- `backup list [skill]`
- `backup restore <backup-id>`
- `restore <skill> --revision <sha>`

Git restore creates a new forward commit and never rewrites published history.

### 5.6 Security commands

- `scan-secrets [--skill name] [--json]`
- `push --scan-secrets` enabled by default
- `push --no-scan-secrets` only after an explicit warning

The scanner starts with deterministic patterns for private keys, common token
formats, high-entropy values, and sensitive filenames. Findings include file,
line, rule, and severity without printing full secret values.

### 5.7 Machine-readable behavior

Every read-only command and every preview operation should support stable JSON.
Write commands should support a non-interactive mode only when all choices are
explicit; destructive confirmation must never be bypassed accidentally by an
Agent.

## 6. Web UI Roadmap

### 6.1 Skill inventory

- Search and filter by selected, modified, conflicted, variant, and target.
- Show base Skill metadata and last local modification.
- Show managed ownership, source, deployment, and active edit-session state.
- Show available family/client variants as badges.
- Add a Skill detail view with rendered `SKILL.md`, file tree, source hashes,
  and Git history.

### 6.2 Client adaptation matrix

Replace the current family-only matrix with a grouped matrix:

```text
Skill          Codex   WorkBuddy   Kimi                  Claude
meeting-note   base    base        Code: family          client
```

Each cell shows:

- selected or excluded;
- detected or unavailable;
- base, family variant, or client variant;
- linked, missing, partial, conflict, wrong-link, or stale-render;
- resolved output hash and applied variant chain on demand.

### 6.3 Variant editor

- Create family/client variant.
- Edit or upload the override `SKILL.md`.
- Compare base versus resolved output.
- Add, replace, or delete resource files.
- Validate before saving.
- Rebuild and relink only affected clients.

The editor must label generated output as read-only and always direct edits to
base or variant source files.

### 6.4 Managed edit sessions

- Start an edit from a Skill, family cell, or client cell.
- Require selection of base, family, or client scope.
- Show the writable workspace and initiating Agent client.
- Show source diff, resolved diff, validation, and impact before apply.
- Display every client that will receive a changed deployment.
- Apply locally with a mandatory backup and operation receipt.
- Resume or abort abandoned sessions.
- Report rendered-output tampering and offer capture-to-session or discard and
  rebuild; never treat deployment edits as canonical automatically.

### 6.5 Conflict center

- Group conflicts by Skill and variant target.
- Display local, base, and remote hashes.
- Render text diffs with binary-file fallback.
- Offer keep-local, use-remote, and keep-both after backup.
- Preview resulting Git and filesystem operations.
- Require explicit confirmation and never push automatically.

### 6.6 History and backups

- Timeline of local backups and Git commits.
- Per-Skill and per-variant history.
- Preview restore changes.
- Restore through a new commit.
- Show the device identifier responsible for a commit when available.

### 6.7 Security view

- Show secret-scan findings without exposing complete values.
- Explain ignored files and repository boundaries.
- Block push on high-severity findings until the user resolves or explicitly
  acknowledges each finding.

## 7. Delivery Plan

## Phase 0: Architecture stabilization

Priority: P0

Deliverables:

- Document family/client IDs and adapter contracts.
- Define registry v3 and local state schema.
- Define source, variant, rendered-cache, and backup paths on macOS, Linux, and
  Windows.
- Add compatibility tests proving existing registry v2 repositories continue
  working unchanged.
- Record resolver invariants and threat model.

Acceptance criteria:

- Existing users see no link changes after upgrade.
- `kimi` remains one user-facing family while doctor reports Kimi Code as its
  concrete client.
- No machine-specific absolute path is written to the private repository.

## Phase 1: Variant resolver MVP

Priority: P0

Deliverables:

- Base + family + client overlay resolver.
- `variant list/create/validate`.
- `resolve` and `diff` read-only commands.
- Deterministic content-addressed rendering.
- Provenance manifest and stale-render detection.
- Render base-only Skills as immutable deployments instead of linking Agent
  clients directly to canonical source directories.
- Managed edit-session engine with begin, diff, validate, impact, apply, abort,
  and tampered-render recovery.
- Path-first managed ownership inspector with stable JSON states.
- Link engine integration for resolved client outputs.
- Update `skill-sync-manager` after the ownership and edit commands exist so it
  requires `managed check` before every Skill modification and chooses the
  smallest unambiguous Base/Family/Client authored scope.
- Hashing, path traversal, symlink, atomic build, and cleanup tests.

Implementation checkpoint: commits 7.1 through 8.4 provide the strict
`variant.yaml` parser, registry-independent immutable overlay core, portable
mode-aware resolution hashes, Variant source list/create/validate,
resolve/Base-to-client diff, scoped Base/Family/Client edit sessions, and
transactional local deployment apply. The resolver derives the registered
family from an exact client ID, selects Base → family → exact-client sources,
and applies a shared family/client target only once when the IDs coincide.
Source paths and filesystem identities remain local safety evidence and are
excluded from portable hashes and schema-v2 deployment provenance.

Scoped apply replaces or first publishes exactly one authored Variant layer,
rebuilds only scope-affected detected/enabled clients, and transactionally swaps
their Agent links. Base apply preserves applicable Variant layers. Ordinary
failure restores the previous or absent source-layer state and old links;
ambiguous durability moves the session and receipt to recovery-required state.
Normal deployment preview/status/migrate, `link`, doctor, and ownership checks
resolve current local Variants. Base-only clients retain schema-v1 deployment
identity, while clients with an applicable Variant use schema-v2 layered
provenance. These local workflows never invoke Git, commit, or push.

The implemented model and its non-goals are maintained in
[`docs/architecture/variant-resolution.md`](../../architecture/variant-resolution.md).
Through 9.1 the CLI does not provide `resolve --output` or `variant delete`.
Registry v3 target intent is implemented; Variant Git packaging/sync conflicts,
fresh-machine Variant reconstruction, and Web Variant flows remain later work.

Acceptance criteria:

- One Skill can produce different Codex and Claude `SKILL.md` files while
  sharing scripts and references from the base.
- `kimi-code` overrides `kimi`, and `kimi` overrides base.
- Editing a base file invalidates every applicable rendered output.
- Editing a client variant invalidates only affected outputs.
- Repeated resolution produces the same output hash.
- Agent links never point at a temporary or partially built directory.
- Editing through a Codex or WorkBuddy managed link cannot mutate canonical
  source or another client's active deployment.
- Edit apply refuses when canonical source changed after session creation.
- Edit apply creates a backup, updates source atomically, rebuilds affected
  deployments, and performs no Git commit or push.
- Direct modification of rendered output is detected and can be captured into
  a session or discarded safely.
- The same managed Skill is identified from its canonical directory, Agent
  link, `SKILL.md`, descendant resource file, and active edit workspace.
- An unmanaged project-local Skill with the same name is not misclassified.
- The installed management Skill never references an ownership or edit command
  before that command is available in the installed CLI version.

## Phase 2: Git and multi-device variant sync

Priority: P0

Deliverables:

- Synchronize `variants/` alongside `skills/`.
- Extend preview/status/doctor to report variant changes and conflicts.
- Install variants on a fresh machine.
- Registry v2-to-v3 migration on the first variant mutation.
- Two-machine integration tests covering base-only, family variant, client
  variant, remote-only change, local-only change, and simultaneous changes.

Acceptance criteria:

- A fresh machine reconstructs identical resolved outputs from Git-tracked
  sources.
- Local and remote edits to the same base or variant stop before overwrite.
- A conflict in one Skill does not silently modify that Skill's links.
- No sync path performs an automatic push without explicit user action.

## Phase 3: Web UI client adaptation management

Priority: P1

Deliverables:

- Grouped family/client matrix.
- Variant badges and resolution details.
- Variant creation, validation, file management, and base/resolved diff.
- Managed edit-session creation, diff, validation, impact, apply, and abort.
- Incremental rebuild and relink endpoints.
- UI performance budget and cached read-only state.

Acceptance criteria:

- Ordinary page refresh performs no network request.
- A local state refresh completes within 300 ms for 100 Skills on a typical
  developer machine, excluding initial hash-cache population.
- Editing one variant does not rescan or relink unrelated Skills.
- Every mutation returns an operation plan/result and refreshed affected state.
- The UI never makes a rendered deployment editable.

## Phase 4: Custom Agent adapters and broader support

Priority: P1

Deliverables:

- Machine-local custom Agent path management.
- Environment-variable and home-relative portable adapter templates.
- Adapter capability metadata, including link, copy-only, global, project, and
  variant support.
- Add a small set of verified built-ins: OpenCode, Gemini CLI, Cursor, GitHub
  Copilot, and Windsurf.
- Add fixtures and OS-specific tests for every built-in adapter.

Acceptance criteria:

- Adding a custom Agent never edits the portable registry with an absolute
  path.
- Unsupported link behavior is declared by capability instead of failing
  halfway through an operation.
- Every built-in adapter has documented detection evidence and test coverage.

## Phase 5: Conflict center, history, and rollback

Priority: P1

Deliverables:

- Timestamped backup inventory.
- Text diff and binary conflict summaries.
- Explicit keep-local, use-remote, and keep-both workflows.
- Per-Skill and per-variant Git history.
- Forward-only Git restore.
- Recovery integration tests, including interrupted operations.

Acceptance criteria:

- Every conflict decision creates a restorable backup first.
- Keep-both produces valid unique Skill or variant names.
- Restore never uses force push or history rewriting.
- Process interruption leaves either the old or new complete state, never a
  partially applied state.

## Phase 6: Secret scanning and auditability

Priority: P1

Deliverables:

- Deterministic secret scanner.
- Push gate with explicit per-finding acknowledgement.
- Redacted JSON and Web UI findings.
- Local activity log for import, select, resolve, link, backup, restore, sync,
  and delete operations.
- Diagnostic export that excludes source content and credentials by default.

Acceptance criteria:

- Private keys and representative API token fixtures are detected.
- Output never prints a complete detected secret.
- False positives can be acknowledged through a versioned rule without using
  a global disable switch.
- Activity logs contain operation metadata, not Skill secrets.

## Phase 7: Skill inspection and authoring assistance

Priority: P2

Deliverables:

- Skill detail page with Markdown preview and file tree.
- Base-versus-client compatibility checklist.
- Optional scaffolding recommendations for common client differences.
- Variant lint rules for unsupported tool names, path assumptions, and client
  vocabulary.
- Agent-readable guidance in `skill-sync-manager` for choosing base versus
  client-specific changes.
- Extend `skill-sync-manager` authoring guidance after the Phase 1 ownership
  and managed-edit protocol is already active.

Acceptance criteria:

- Validation clearly distinguishes schema errors, portability warnings, and
  client compatibility warnings.
- Suggested changes are never applied without an explicit user or Agent write
  command.
- The management Skill never instructs an Agent to edit a client link,
  canonical source, or rendered cache directly.

## 8. Testing Strategy

### 8.1 Unit tests

- Variant precedence and overlay semantics.
- Manifest validation and unknown targets.
- Windows and POSIX path normalization.
- Traversal, symlink, and generated-output tamper rejection.
- Stable content hashes and provenance.
- Family aggregation from multiple client states.
- Edit-session locking, baseline changes, scope selection, impact calculation,
  validation, atomic apply, abort, expiration, and recovery.
- Tampered rendered-output detection and provenance verification.
- Ownership inspection from canonical, deployment, link, junction, descendant,
  edit-workspace, broken-link, wrong-link, and same-name unmanaged paths.

### 8.2 Integration tests

- Bare Git remote with two independent machine homes.
- Base and variant changes across machines.
- Fresh-machine installation with different detected clients.
- Windows junction fallback simulation.
- Interrupted render, install, backup, and restore operations.
- Existing real directories, broken links, and wrong links.
- Concurrent Agent edit attempts against the same logical Skill.
- Codex base edit affecting every client and Codex client edit affecting only
  Codex.
- Agent writes attempted through read-only managed deployments on macOS and
  Windows.

### 8.3 UI tests

- Grouped client matrix rendering.
- Variant create/edit/delete confirmation.
- Conflict resolution with mandatory backup.
- Cached refresh performance and explicit network boundaries.
- CSRF and loopback binding protections.
- Managed edit-session scope, preview, apply, abort, and tamper recovery.
- Ownership result rendering and safe handling of ambiguous states.

### 8.4 Real-machine smoke tests

- macOS: Codex, WorkBuddy, Kimi Code, Claude Code.
- Windows: at least Codex and Claude Code using junction fallback.
- Second machine: clone, sync, resolve, and compare output hashes.

## 9. Migration Strategy

1. Registry v2 remains readable indefinitely.
2. Do not write registry v3 until the user creates or imports a variant.
3. Existing `~/.agents/skills/<name>` directories remain base Skills without
   moves.
4. Create `~/.agents/variants` lazily.
5. Existing direct-to-base Agent links remain valid during compatibility mode.
6. The managed-editing migration converts direct-to-base links to base-only
   rendered deployments after an explicit preview.
7. Removing the last applicable variant rebuilds a base-only deployment; it
   does not restore a writable direct-to-base Agent link.
8. A legacy `kimi-code` registry deployment target migrates to the `kimi`
   family while remaining a valid client ID for Variant resolution.

## 10. Explicit Non-goals for the Near Term

- Public Skill marketplace or rankings.
- Hosted account and cloud storage service.
- Automatic background push.
- Automatic conflict winner selection.
- Supporting dozens of unverified Agent paths for marketing purposes.
- Full visual code editor.
- Arbitrary executable transformations during variant resolution.
- Synchronizing MCP credentials, Agent runtime memory, or unrelated dotfiles.

## 11. Success Metrics

- A user can maintain one logical Skill with at least three client adaptations
  without duplicating its shared resources.
- A second machine can reproduce every resolved Skill from Git with identical
  hashes.
- No normal workflow requires editing an Agent-specific Skill directory.
- An Agent can determine locally and unambiguously whether the exact Skill path
  it plans to modify is managed.
- No automated workflow overwrites a real directory or pushes implicitly.
- A user can identify which source and variants produced any installed Skill.
- An Agent can propose and apply a scoped Skill change without mutating other
  clients before validation and apply.
- Every Agent-initiated change has a session, diff, impact report, backup, and
  operation receipt.
- A conflict can be understood, backed up, resolved, and restored entirely from
  the CLI or Web UI.
- Adding a new Agent client usually requires an adapter definition and tests,
  not changes throughout the synchronization core.

## 12. Recommended Implementation Order

Start with Phase 0 through Phase 2 before expanding the Web UI. The resolver,
portable registry, provenance, and two-machine tests are platform foundations;
building UI workflows before those contracts stabilize would create rework.

After Phase 2, implement the grouped client matrix and variant editor, then add
custom adapters. Conflict resolution, rollback, and secret scanning should be
completed before marketing the tool as a general-purpose platform.
