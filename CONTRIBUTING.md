# Contributing

Skill Sync welcomes focused bug fixes, tests, documentation improvements, and
carefully scoped platform support.

## Development setup

Requirements are Python 3.10+, Git, and Node.js 20+ for the browser test
harnesses. The package has no runtime dependencies.

```bash
git clone https://github.com/lirt1231/skill-sync.git
cd skill-sync
python -m unittest discover -s tests
node --check skill_sync/web_static/app.js
git diff --check
```

Run the local Web UI with:

```bash
python -m skill_sync.cli web
```

## Safety requirements

- Never overwrite a real directory in an Agent Skill location.
- Never edit a rendered deployment, Agent link, or managed canonical source as
  a shortcut around the edit-session workflow.
- Keep Git commit and push explicit; no background workflow may publish content.
- Reject ambiguous ownership, traversal, symlinks, reparse points, and unknown
  filesystem entries before mutation.
- Add failure-path and rollback tests for changes that touch canonical content,
  deployments, links, backups, receipts, or edit sessions.
- Keep Web mutations behind both a confirmation flow and the per-process token.

Use temporary config, data, Agent, and Git roots in tests. Tests must never read
or mutate the developer's real Agent Skill directories.

## Pull requests

Keep changes narrow and explain user impact, failure behavior, and validation.
Update README or command help whenever a public workflow changes. Run the full
test suite before requesting review.

Security reports belong in private GitHub advisories, not public issues. See
[SECURITY.md](SECURITY.md).
