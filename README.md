# Skill Sync

Synchronize selected user-authored Agent Skills across devices. Managed Skills have one canonical local copy under `~/.agents/skills`; Codex, WorkBuddy, and Kimi consume them through directory links.

## Requirements

- Python 3.10+
- Git
- A private Git repository

## Quick start

```bash
./skill-sync init --repo git@github.com:YOUR_NAME/agent-skills.git
./skill-sync scan
./skill-sync select skill-sync-manager my-skill
./skill-sync push --message "Add my Skills"
./skill-sync link
./skill-sync web
```

Open <http://127.0.0.1:8765> to view the Skill-by-Agent matrix.

On another computer:

```bash
skill-sync init --repo git@github.com:YOUR_NAME/agent-skills.git
skill-sync sync
```

`sync` installs selected Skills into `~/.agents/skills` and creates safe links for detected clients:

- Codex: `${CODEX_HOME:-~/.codex}/skills`
- WorkBuddy: `${WORKBUDDY_HOME:-~/.workbuddy}/skills`
- Kimi: `${KIMI_SKILLS_DIR:-~/.config/agents/skills}` (Kimi's recommended user Skill directory)

On macOS/Linux the links are symbolic links. On Windows the tool first tries a directory symbolic link and falls back to a directory junction. Windows `.lnk` shortcuts are intentionally unsupported.

## Safety

Skill Sync never overwrites a real directory in an Agent Skill location. Run `skill-sync doctor --json` to inspect conflicts, broken links, and missing canonical Skills. Git divergence and simultaneous local/remote changes stop for manual resolution.

The Web UI only binds to a loopback address and requires a per-process token for mutations.
