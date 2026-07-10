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

Use `registry.yaml` as the sole remote synchronization allowlist. A Skill is synced only if it is listed in the registry.

Because Python's standard library does not include a YAML parser, `registry.yaml` uses a deliberately small YAML subset that the CLI owns:

- two-space indentation
- string, boolean, and integer scalar values only
- mappings only; no anchors, aliases, tags, multiline strings, or flow style
- comments and blank lines allowed

The CLI preserves unknown top-level and per-Skill fields when possible, but may normalize formatting when writing.

Use content hashes to detect whether local Skill directories changed. Use Git commits as the durable remote version and update history.

Install Skills by copying files into the target platform Skill directory. Do not use symlinks.

Separate synchronization logic from terminal UI so a later frontend can reuse the same core behavior.

## Repository Layout

The project repository contains the CLI implementation and tests. The user's private Skill sync repository is a separate Git repository managed by the CLI.

The remote Skill sync repository stores portable state only:

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
    selected: true
    source_platform: codex
    display_name: skill-name
```

The CLI must tolerate unknown registry fields so future UI metadata can be added without breaking older versions.

Machine-local state is not committed to the sync repository. It is stored in a JSON config file at:

```text
${XDG_CONFIG_HOME:-~/.config}/skill-sync/config.json
```

The local config records:

```json
{
  "sync_repo_path": "/absolute/path/to/local/sync/repo",
  "platform": "codex",
  "branch": "main",
  "skills": {
    "skill-name": {
      "local_path": "/absolute/path/to/local/skill-name",
      "last_installed_hash": "sha256:..."
    }
  }
}
```

Absolute local paths must never be written to `registry.yaml`.

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

Usage:

```text
skill-sync init --repo <git-url-or-local-path> [--sync-dir <path>] [--branch <name>] [--platform codex]
```

Responsibilities:

- create the sync worktree if missing
- clone the configured Git repository into `--sync-dir`, or use the existing repository at `--sync-dir`
- if `--repo` is a local path that does not exist, initialize it as a normal non-bare Git repository
- create `registry.yaml` if missing
- verify `git` is available
- store `sync_repo_path`, `branch`, and `platform` in local config
- default `--sync-dir` to `${XDG_DATA_HOME:-~/.local/share}/skill-sync/repo`
- default `--branch` to `main`
- assume Git authentication is handled by the user's existing Git credential setup; report Git auth failures without trying to manage credentials

### `skill-sync scan`

List candidate local Skills from the selected platform adapter.

Responsibilities:

- detect directories containing `SKILL.md`
- show whether each candidate is already selected
- mark candidates outside the adapter's default user Skill root as `external`
- avoid modifying registry or remote state

Usage:

```text
skill-sync scan [--platform codex] [--json]
```

### `skill-sync select`

Add one or more local user-created Skills to `registry.yaml`.

Usage:

```text
skill-sync select <skill-name-or-path>... [--platform codex] [--allow-external]
```

Responsibilities:

- require explicit Skill names or paths
- reject paths that do not contain `SKILL.md`
- store the resolved local path
- add portable selection metadata to remote `registry.yaml`
- never select third-party Skills implicitly
- require `--allow-external` when selecting a path outside the adapter's default user Skill root
- if a selected name already exists with a different local path, stop and require the user to pass `skill-sync deselect <skill-name>` first

`skill-sync deselect <skill-name>...` removes entries from the remote registry and local config. It does not delete local Skill directories.

`select` and `deselect` modify `registry.yaml` in the local sync repository but do not commit immediately. The next `push` is allowed to include these expected registry changes in its commit. Other unrelated dirty sync repository changes still cause `push` to stop.

### `skill-sync status [--json]`

Report local, registry, and remote state.

Responsibilities:

- show selected Skills
- show local content hash changes
- show whether the sync repository has uncommitted changes
- show whether remote has commits not present locally
- support a JSON output mode for the future frontend

Status operates on all selected Skills by default. `--skill <name>` may be repeated to restrict output.

The JSON output schema is versioned:

```json
{
  "schema_version": 1,
  "repo": {"path": "...", "branch": "main", "clean": true, "ahead": 0, "behind": 0, "diverged": false},
  "skills": [
    {"name": "skill-name", "platform": "codex", "local_path": "...", "local_hash": "sha256:...", "remote_hash": "sha256:...", "changed_local": false, "selected": true}
  ]
}
```

### `skill-sync pull`

Bring remote changes into the local sync repository and install updated selected Skills into the platform Skill directory.

Responsibilities:

- fetch remote before merging
- stop if the local sync repository has uncommitted changes
- stop if any destination local Skill has changed relative to its recorded `last_installed_hash`
- stop if Git reports conflicts
- copy updated Skills into local platform directories only after Git state is clean
- update `last_installed_hash` after a successful install

`pull` operates on all selected Skills by default. `--skill <name>` may be repeated.

### `skill-sync push`

Copy selected local Skills into the sync repository, commit changes, and push.

Responsibilities:

- fetch remote first
- stop if remote has commits that are not present locally
- compute local Skill hashes before copying
- copy selected Skill directories into `skills/<skill-name>/`
- commit only when Skill content or expected registry changes changed
- push to the configured remote
- after a successful push, update local config `last_installed_hash` for each pushed Skill to the deterministic source hash

`push` operates on all selected Skills by default. `--skill <name>` may be repeated.

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

Local changed means the current hash of the local Skill directory differs from the local config's `last_installed_hash` or, if no baseline exists, differs from the corresponding `skills/<name>/` directory in the current sync repository checkout.

Remote changed means the configured branch has commits locally behind its upstream after `git fetch`.

Git merge conflicts are not auto-resolved.

## Git Semantics

The CLI uses one configured branch, default `main`.

Before operations that compare local and remote state, it runs:

```text
git fetch origin <branch>
```

It computes ahead/behind using:

```text
git rev-list --left-right --count HEAD...origin/<branch>
```

Stop conditions:

- dirty sync repository before pull, push, or sync
- for push, dirty changes are allowed only when they are expected `registry.yaml` changes from `select` or `deselect`
- local branch and upstream diverged
- remote branch missing for commands that require a remote
- unrelated histories
- force-push detected as divergence
- push rejected by Git

Pull uses fast-forward only:

```text
git merge --ff-only origin/<branch>
```

Push uses:

```text
git push origin HEAD:<branch>
```

## File Copy Rules

When copying Skill directories:

- preserve relative paths and file contents
- include hidden files inside selected Skill directories
- exclude common generated noise such as `__pycache__/`, `.DS_Store`, and `.git/`
- copy into a temporary directory under the destination parent, then replace the destination directory
- keep a timestamped backup of the previous destination directory until the replacement succeeds
- remove the backup only after the final content hash matches the source hash

If replacement fails, the CLI restores the backup when possible and reports the failure.

Cross-device moves are avoided by creating the temporary directory in the same parent as the destination.

## Hash Algorithm

Skill content hashes use SHA-256 and are reported as `sha256:<hex>`.

Rules:

- traverse files in sorted relative path order using POSIX-style `/` separators
- hash each file with unambiguous framing: the literal bytes `file\0`, an unsigned 64-bit big-endian path byte length, the UTF-8 relative path bytes, an unsigned 64-bit big-endian content byte length, and the exact file bytes
- ignore directories and files excluded by copy rules
- ignore empty directories
- do not normalize line endings
- include regular file bytes exactly as stored, including binary files
- reject symlinks by default with a clear error, except inside ignored directories that are not traversed; a future version may add explicit symlink policy
- ignore file permissions in the hash

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
- pull refusing to overwrite local changes
- local config not leaking absolute paths into remote registry
- remote/local divergence stop conditions
- explicit external Skill selection guard
- invalid or missing local paths
- JSON schema stability

## Future Frontend Compatibility

The CLI exposes reusable core modules and a JSON status mode. A future frontend can use the same registry and core synchronization functions to:

- select which Skills are synchronized
- display remote version information
- show whether updates are available
- trigger pull, push, or sync actions

The CLI remains the first complete interface and source of truth for behavior.
