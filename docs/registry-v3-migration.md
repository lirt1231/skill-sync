# Registry v3 Migration

Registry v3 adds a portable allowlist of authored Variant targets to each
selected Skill. It is the first registry version that synchronizes
`variants/<skill>/<target>/` through Git.

## Compatibility

| Repository schema | Current client behavior | Older v1/v2 client behavior |
| --- | --- | --- |
| v1 | Base-only read/write | Base-only read/write |
| v2 | Base-only read/write | Base-only read/write |
| v3 | Base plus declared Variants | Unsupported; do not use against this repository |

Reading, validating, resolving, previewing, or deploying a v1/v2 repository
does not rewrite it. The first successful `variant create` or first scoped
Variant publication upgrades the registry to v3 in the same rollback boundary
as the authored source change.

## Upgrade

1. Upgrade Skill Sync on every machine that can push to the repository.
2. Confirm the repository and authored sources are clean with
   `skill-sync status --json` and `skill-sync variant validate <skill>`.
3. Create the narrowest required family or client Variant.
4. Edit it only through a scoped edit session.
5. Review `skill-sync preview --json` and explicitly publish it.

```bash
skill-sync variant create my-skill --family kimi
skill-sync edit begin my-skill --family kimi --actor codex
skill-sync edit diff <session-id>
skill-sync edit validate <session-id>
skill-sync edit impact <session-id>
skill-sync edit apply <session-id>
skill-sync push --skill my-skill --message "Add Kimi variant"
```

The Git commit contains `registry.yaml`, `skills/<skill>/`, and only the target
directories named by that Skill's v3 `variants` field. Absolute local paths,
rendered deployments, edit sessions, backups, credentials, and undeclared
local Variant directories are excluded.

## Add A Machine

Install the same or a newer Skill Sync version, initialize with a machine-local
canonical root, and pull:

```bash
skill-sync init --repo git@github.com:YOUR_NAME/agent-skills.git
skill-sync preview --json
skill-sync pull
skill-sync variant validate my-skill
skill-sync deploy preview
skill-sync deploy migrate
skill-sync deploy status --json
```

The receiver reconstructs Base and declared Variant sources below its own
configured root. It builds deployments only for clients detected and enabled
on that machine. Equal Git sources reproduce equal layer and output hashes.
The resolution hash also includes the exact client ID, so equal outputs for
different clients still retain distinct resolution IDs.

For daily work, use `skill-sync sync`. It fetches and previews first. Remote
changes to one unit can be installed while a different locally changed unit is
preserved. A local and remote edit to the same Base or Variant stops as a
conflict before Git fast-forward, source replacement, or deployment relinking.
The preserved local change remains pending until an explicit `push`; sync never
pushes automatically.

## Roll Back To Base-Only

Do not point a v1/v2 client at a v3 repository. First make a reviewed forward
commit that returns the repository to schema v2:

1. Create a backup branch or tag at the current v3 commit.
2. Confirm no machine has an active Variant edit session or unpublished
   Variant change.
3. Remove every Skill's `variants` field from `registry.yaml` and set
   `version: 2`.
4. Remove the corresponding tracked `variants/` trees from the Git change.
5. Review the diff, commit it as a new commit, and push normally.
6. On every machine, upgrade or check out that commit and run `pull`, then
   rebuild Base-only deployments.

There is intentionally no automatic downgrade or `variant delete` command in
this release. Local authored Variant directories are not credentials or
runtime state, but a v2 workflow ignores them. Archive them outside the
portable root only after reviewing their content; never silently choose Base
or Variant as a conflict winner.

If a same-unit conflict occurs during migration, stop. Preserve the current
local sources and deployments, inspect `skill-sync preview --json` and
`skill-sync status --json`, then resolve the authored content explicitly on one
machine. Publish the resolution with a normal forward commit and let the other
machines pull it.
