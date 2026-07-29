# Security Policy

## Supported versions

Skill Sync is currently a technical preview. Security fixes are applied to the
latest release and the `main` branch. Older pre-1.0 versions are not maintained.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities or suspected credential leaks.
Use GitHub's private vulnerability reporting form:

https://github.com/lirt1231/skill-sync/security/advisories/new

Include the affected version, operating system, reproduction steps, expected
and actual behavior, and whether any canonical Skill, deployment, backup, or Git
remote was modified. Remove credentials and private Skill content from reports.

## Local trust model

Skill Sync is a local developer tool with permission to:

- read and update explicitly configured canonical Skill directories;
- clone, fetch, commit, and push the user-configured Skill data repository;
- create verified Agent links or Windows junctions;
- create private edit workspaces, deployments, backups, and operation receipts;
- launch installed Codex or Kimi Code sessions on macOS after confirmation.

The Web UI binds only to loopback addresses. Mutating requests require a random
per-process token and never accept a non-loopback bind address. The UI does not
store Git credentials; Git authentication remains with the user's SSH agent,
credential helper, or operating-system keychain.

## Sensitive data

The Skill data repository may contain user-authored files and should normally be
private. Do not place API keys, access tokens, private keys, cookies, or runtime
credentials in a Skill. The technical preview does not yet provide a push-time
secret scanner, so users must review staged Skill content before pushing.

Skill Sync fails closed on ambiguous ownership, links inside authored sources,
unexpected filesystem entries, tampered deployments, and unresolved Git
conflicts. It never intentionally synchronizes edit sessions, rendered
deployments, backups, credentials, or machine-local absolute paths.
