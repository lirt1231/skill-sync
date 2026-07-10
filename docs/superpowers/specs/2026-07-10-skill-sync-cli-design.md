# Skill Sync CLI Design

## Goal

Build a Python 3 command-line tool that synchronizes explicitly selected user-created Agent Skills across devices and Agent platforms through a private GitHub repository.

The tool preserves the standard Codex/Agent Skills directory format directly. It does not transform skill contents into an intermediate format.

## Non-Goals

- Do not synchronize every local Skill automatically.
- Do not upload third-party or bundled Skills unless the user explicitly selects them.
- Do not make the whole local Skills directory a Git worktree.
- Do not introduce a custom package format for Skills.
- Do not build the future graphical frontend in this phase.

## Core Decisions

Use a private Git repository as the canonical remote source. The CLI invokes the system `git` executable rather than using a Git library.

Use Python 3 and the standard library. Optional dependencies are avoided so the tool can run on fresh developer machines with only Python and Git installed.

Use `registry.yaml` as the sole synchronization allowlist. A Skill is synced only if it is listed in the registry.

Use content hashes to detect whether local Skill directories changed. Use Git commits as the durable remote version and update history.

Install Skills by copying files into the target platform Skill directory. Do not use symlinks.

Separate synchronization logic from terminal UI so a later frontend can reuse the same core behavior.

## Repository Layout

The project repository contains the CLI implementation and tests. The user's private Skill sync repository is a separate Git repository managed by the CLI.

The remote Skill sync repository stores:

```text
registry.yaml
skills/
  <skill-name>/
    SKILL.md
    ...
```

`registry.yaml` records selected Skills and platform metadata. The initial schema is intentionally small:

```yaml
version: 1
skills:
  skill-name:
    source_platform: codex
    selected: true
    local_path: /absolute/path/to/skill-name
```

The CLI must tolerate unknown registry fields so future UI metadata can be added without breaking older versions.

## Platform Adapters

Each Agent platform is represented by a small adapter that answers:

- platform name
- default Skill directory
- how to discover candidate Skill directories
- how to install a synced Skill

The initial adapter is `codex`, using `$CODEX_HOME/skills` when set and `~/.codex/skills` otherwise.

The adapter does not parse or reinterpret Skill contents. A valid Skill directory is any directory containing `SKILL.md`.

## Commands

### `skill-sync init`

Initialize local CLI state and connect to a private Skill sync Git repository.

Responsibilities:

- create the sync worktree if missing
- clone or initialize the configured Git repository
- create `registry.yaml` if missing
- verify `git` is available

### `skill-sync scan`

List candidate local Skills from the selected platform adapter.

Responsibilities:

- detect directories containing `SKILL.md`
- show whether each candidate is already selected
- avoid modifying registry or remote state

### `skill-sync select`

Add one or more local user-created Skills to `registry.yaml`.

Responsibilities:

- require explicit Skill names or paths
- reject paths that do not contain `SKILL.md`
- store the resolved local path
- never select third-party Skills implicitly

### `skill-sync status [--json]`

Report local, registry, and remote state.

Responsibilities:

- show selected Skills
- show local content hash changes
- show whether the sync repository has uncommitted changes
- show whether remote has commits not present locally
- support a JSON output mode for the future frontend

### `skill-sync pull`

Bring remote changes into the local sync repository and install updated selected Skills into the platform Skill directory.

Responsibilities:

- fetch remote before merging
- stop if the local sync repository has uncommitted changes
- stop if Git reports conflicts
- copy updated Skills into local platform directories only after Git state is clean

### `skill-sync push`

Copy selected local Skills into the sync repository, commit changes, and push.

Responsibilities:

- fetch remote first
- stop if remote has commits that are not present locally
- compute local Skill hashes before copying
- copy selected Skill directories into `skills/<skill-name>/`
- commit only when content changed
- push to the configured remote

### `skill-sync sync`

Run the safe default workflow:

1. fetch remote
2. stop if both remote and selected local Skills changed
3. pull and install remote updates when only remote changed
4. copy, commit, and push local updates when only local changed
5. report no-op when neither side changed

## Conflict Policy

The CLI must never overwrite when both local and remote changed.

If local selected Skills changed and remote has new commits, the command stops with a clear message. The user must manually resolve by pulling, inspecting conflicts or differences, and deciding which content should win.

Git merge conflicts are not auto-resolved.

## File Copy Rules

When copying Skill directories:

- preserve relative paths and file contents
- include hidden files inside selected Skill directories
- exclude common generated noise such as `__pycache__/`, `.DS_Store`, and `.git/`
- replace the destination Skill directory atomically enough to avoid mixed old/new contents on normal interruption

The implementation can stage through a temporary directory and then replace the target directory.

## Error Handling

The CLI should fail closed:

- missing Git executable: stop with setup guidance
- missing `SKILL.md`: reject the Skill
- invalid registry: stop and report the parse problem
- dirty sync repository: stop before pull or push
- remote ahead during push: stop and ask the user to pull or sync
- both sides changed during sync: stop and require manual resolution

## Testing Strategy

Use Python standard-library `unittest` and temporary directories.

Test coverage should include:

- Skill discovery through the Codex adapter
- registry read/write, including unknown field tolerance
- deterministic content hashing
- copy behavior and exclusion rules
- Git wrapper behavior using temporary local bare repositories where practical
- CLI JSON status output shape
- conflict policy for local-changed plus remote-ahead

## Future Frontend Compatibility

The CLI exposes reusable core modules and a JSON status mode. A future frontend can use the same registry and core synchronization functions to:

- select which Skills are synchronized
- display remote version information
- show whether updates are available
- trigger pull, push, or sync actions

The CLI remains the first complete interface and source of truth for behavior.
