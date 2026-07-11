# Skill Sync V2 Design

## Goal

Synchronize explicitly selected user-authored Agent Skills across macOS and Windows, keep one canonical local copy under `~/.agents/skills`, expose those Skills to installed Agent clients through filesystem links, and provide a local Web UI for management.

## Canonical Model

- `~/.agents/skills/<name>` is the only editable local copy of a managed Skill.
- The private Git sync repository stores the portable registry and selected Skill contents.
- Agent-specific Skill directories contain links to canonical Skills, never copied managed content.
- Public commands do not require a platform argument.
- Agent adapters remain internal and only detect installed clients and their Skill directories.

## Agent Targets

The first release detects:

- Codex: `$CODEX_HOME/skills` or `~/.codex/skills`
- WorkBuddy: `$WORKBUDDY_HOME/skills` or `~/.workbuddy/skills`
- Kimi Desktop: `$KIMI_SKILLS_DIR` or its effective Daimon managed root under `~/Library/Application Support/kimi-desktop/daimon-share/daimon/skills`; Kimi Code falls back to `~/.config/agents/skills`
- Claude Code: `$CLAUDE_HOME/skills` or `~/.claude/skills`

Detection is conservative. An adapter is installed when its home directory, Skill directory, or executable/application marker exists. Tests may inject environment and home paths.

## Links

- macOS/Linux: directory symbolic link.
- Windows: directory symbolic link when permitted; otherwise directory junction.
- `.lnk` shell shortcuts are not used because filesystem scanners do not traverse them as directories.
- Existing real directories are never overwritten.
- Existing correct links are idempotent.
- Incorrect or broken links are reported as conflicts.

The registry records per-Skill target preferences, defaulting to all detected supported Agents:

```yaml
version: 2
skills:
  my-skill:
    selected: true
    display_name: my-skill
    targets: codex,workbuddy
```

The owned YAML subset continues to use scalar values, so `targets` is a comma-separated string.

## Commands

- `init --repo ...`: configure the private repository and canonical Skill root.
- `scan`: list valid canonical Skills.
- `import --agent codex|claude <name>...`: safely globalize existing Agent-local Skills, replacing the verified source directory with a link.
- `select <name-or-path>`: select a canonical Skill; an external path must be imported explicitly before selection.
- `deselect <name>`: stop synchronization without deleting the canonical directory.
- `status`: report Git, canonical content, Agent detection, and link states.
- `pull`, `push`, `sync`: synchronize Git and canonical Skills safely.
- `link [--skill name] [--agent name]`: create missing Agent links.
- `unlink [--skill name] [--agent name]`: remove only links managed by skill-sync.
- `doctor`: report unsupported links, conflicts, missing canonical Skills, and Git state.
- `web [--host 127.0.0.1] [--port 8765]`: serve the local management UI.

`sync` repairs safe missing links after successfully resolving Skill contents. It never overwrites conflicts.

## Web UI

Use a Python standard-library HTTP server bound to `127.0.0.1` by default. It serves static HTML/CSS/JavaScript and JSON endpoints backed by the same core functions as the CLI.

The UI contains:

- overview cards for repository, selected Skills, detected Agents, and issues;
- a Skill table with local/Git state;
- a Skill-by-Agent link matrix;
- actions for sync, select/deselect, link/unlink, and doctor;
- an operation result panel with actionable errors.

Mutating API requests require a per-process CSRF token embedded into the initial page. The server rejects non-loopback binding unless explicitly allowed in a future version.

## Migration and Safety

- Existing V1 configs are read and migrated to `skills_root`; `platform` is ignored.
- V1 `source_platform` registry metadata is tolerated and removed on the next selection write.
- Existing Agent-local real Skill directories are shown as import/conflict candidates and are not moved automatically.
- Hidden files are included in hashing and synchronization, except existing ignore rules.
- Git remains noninteractive and conflicts stop without choosing a winner.

## Agent Management Skill

Create `skill-sync-manager` under the canonical root. It instructs compatible Agents to inspect status, run safe synchronization, repair links, start the Web UI, and stop on conflicts instead of deleting user content.

## Validation

- Unit tests cover macOS/Linux symlinks, simulated Windows symlink fallback/junction behavior, detection, conflict safety, APIs, and config migration.
- A local bare Git repository provides a two-machine sync smoke test.
- Real-machine smoke tests link `skill-sync-manager` into both Codex and WorkBuddy and verify that each path resolves to the same canonical `SKILL.md`.
