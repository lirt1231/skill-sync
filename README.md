# Skill Sync

Synchronize selected user-authored Agent Skills across devices. Managed Skills have one canonical local copy under `~/.agents/skills`; Codex, WorkBuddy, Kimi, and Claude Code consume them through directory links.

## Requirements

- Python 3.10+
- Git
- A private Git repository

## Install skill-sync

Install directly from the tool's Git repository with
[pipx](https://pipx.pypa.io/). This installs the `skill-sync` command in an
isolated environment and makes it available from any directory:

```bash
pipx install "git+https://github.com/YOUR_NAME/skill-sync.git"
skill-sync --help
```

For a private repository over SSH:

```bash
pipx install "git+ssh://git@github.com/YOUR_NAME/skill-sync.git"
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
./skill-sync init --repo git@github.com:YOUR_NAME/agent-skills.git
./skill-sync scan
./skill-sync select skill-sync-manager my-skill
./skill-sync push --message "Add my Skills"
./skill-sync link
./skill-sync web
```

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

When local and remote content both changed, Skill Sync stops. The UI shows the
affected state and can create a timestamped local backup, but never chooses a
version, overwrites a real directory, or merges content for you.

Import an existing real Skill directory from Codex or Claude Code into the canonical root:

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
pipx install "git+ssh://git@github.com/YOUR_NAME/skill-sync.git"
skill-sync init --repo git@github.com:YOUR_NAME/agent-skills.git
skill-sync sync
skill-sync doctor
```

`init` is required only once per computer. It clones the private Skill data
repository and records the local paths. The selected Skill list lives in that
repository, so it does not need to be selected again on each computer.

`sync` installs selected Skills into `~/.agents/skills` and creates safe links for detected clients:

- Codex: `${CODEX_HOME:-~/.codex}/skills`
- WorkBuddy: `${WORKBUDDY_HOME:-~/.workbuddy}/skills`
- Kimi: detects Kimi Code at `$KIMI_CODE_SKILLS_DIR` or `~/.config/agents/skills`, and Kimi Desktop at `$KIMI_DESKTOP_SKILLS_DIR` or `~/Library/Application Support/kimi-desktop/daimon-share/daimon/skills`
- Claude Code: `${CLAUDE_HOME:-~/.claude}/skills`

On macOS/Linux the links are symbolic links. On Windows the tool first tries a directory symbolic link and falls back to a directory junction. Windows `.lnk` shortcuts are intentionally unsupported.

### Windows PowerShell example

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# Open a new PowerShell window after ensurepath.
pipx install "git+ssh://git@github.com/YOUR_NAME/skill-sync.git"
skill-sync init --repo git@github.com:YOUR_NAME/agent-skills.git
skill-sync sync
skill-sync web
```

GitHub SSH authentication must already work on the new computer. HTTPS URLs
can be used instead when Git Credential Manager or another credential helper is
configured.

## Safety

Skill Sync never overwrites a real directory in an Agent Skill location. Run `skill-sync doctor --json` to inspect conflicts, broken links, and missing canonical Skills. Git divergence and simultaneous local/remote changes stop for manual resolution.

The Web UI only binds to a loopback address and requires a per-process token for mutations.
