# Variant Resolution Architecture

This document describes the Variant source, resolver, scoped-edit, deployment,
portable registry intent, and multi-device synchronization model available
through roadmap commit 9.5.

## Source and resolved views

One logical Skill may have one Base source plus sparse overlays for an Agent
family or a concrete Agent client:

```text
authored sources                         immutable resolved view

~/.agents/skills/<skill>/        ─┐
                                  ├─ Base → family → exact client ─> client files
~/.agents/variants/<skill>/kimi/ ─┤
~/.agents/variants/<skill>/       │
  kimi-code/                     ─┘
```

| View | Meaning | Managed operation available through 9.1 |
| --- | --- | --- |
| Base source | Portable common Skill under `skills/<skill>/` | Full managed Base edit workflow; apply preserves applicable Variants |
| Family source | Sparse overlay such as `variants/<skill>/kimi/` | Create plus transactional scoped edit workflow |
| Client source | Sparse overlay such as `variants/<skill>/kimi-code/` | Create plus transactional scoped edit workflow |
| Resolved view | Immutable in-memory file plan for one exact client | Read-only resolve/diff and apply evidence; never an authoring source |
| Rendered deployment | Content-addressed directory used by Agent links | Atomic schema-v2 layered render when a Variant applies; Base-only clients retain schema v1 |

The Base and Variant directories are authored sources. A resolved view is the
deterministic result of applying source layers; it is not another source tree
and must never be edited or copied back implicitly.

`variant create` scaffolds only `variant.yaml`. Author overlay files through
`edit begin --family/--client`, and edit only the returned machine-local
workspace. Agents must never edit `~/.agents/variants` directly.

## Layer selection and overlay semantics

The resolver accepts only a registered concrete client ID. It derives the
family, then applies layers from least to most specific:

1. Base;
2. the family Variant, when present;
3. the exact-client Variant, when present.

For `kimi-code`, `kimi` overrides Base and `kimi-code` overrides both.
For Codex and WorkBuddy, the family ID and sole client ID are identical, so the
same Variant directory is applied once rather than twice. Missing family and
client layers are valid and produce a Base-only or partially adapted view.

Every Variant uses `mode: overlay`:

- a Variant file replaces the same normalized Base path;
- a new Variant path is added;
- a manifest `delete` path removes that file or subtree before additions;
- root `variant.yaml` is resolution metadata and is excluded from output;
- the resolved root must still contain `SKILL.md`.

Paths are normalized portable POSIX paths. Absolute, drive, UNC, traversal,
case-insensitive duplicate, Windows-reserved, control-character, symlink,
reparse-point, special-file, and file/directory-collision inputs fail closed.
File modes are content-derived and host-independent: shebang files resolve as
`0755`; other regular files resolve as `0644`.

## Determinism and provenance

One resolution owns immutable byte and portable-mode snapshots for Base,
family, and client layers. Manifest parsing, deletes, output files, layer
hashes, output hash, and explanation metadata all derive from those same
snapshots. The resolution hash length-prefixes resolver version, exact client,
family, ordered layer roles/targets, and layer hashes. Machine-local source
paths and filesystem identities remain safety evidence but do not enter the
portable resolution hash.

Consequently, equal source content at different machine paths has the same
portable resolution identity. A client-specific change affects only clients
whose layer chain includes it, while a Base change affects every client view.

When at least one Variant applies, the deployment stores portable schema-v2
provenance: resolver version, exact client and family, ordered layer
roles/targets/content hashes, final output hash, and rendered content hash. It
never stores canonical or workspace absolute paths. Verification recomputes the
resolution hash from the persisted layer chain and fails closed on malformed,
stale, or tampered provenance. A Base-only client continues to use the existing
schema-v1 deployment identity, avoiding an unnecessary fleet-wide migration.

## Source management and read-only inspection commands

These are the Variant source-management and inspection commands implemented
through 9.1:

```bash
skill-sync variant list
skill-sync variant list --skill my-skill --json
skill-sync variant create my-skill --family kimi
skill-sync variant create my-skill --client kimi-code
skill-sync variant validate my-skill
skill-sync resolve my-skill --client kimi-code --dry-run
skill-sync resolve my-skill --client codex --dry-run --json
skill-sync diff my-skill --base --client kimi-code
skill-sync diff my-skill --base --client claude-code --json
skill-sync edit begin my-skill --family kimi --actor kimi-code
skill-sync edit begin my-skill --client codex --actor codex
skill-sync edit diff <session-id>
skill-sync edit validate <session-id>
skill-sync edit impact <session-id>
skill-sync edit apply <session-id>
```

`resolve` requires an explicit `--dry-run`; it has no output/materialization
flag. Text output summarizes layer and hash evidence. JSON schema v1 also
reports ordered layers, final files, and each final file's source role/target.

`diff` compares Base with the exact client's resolved view. Both sides come
from one `LayeredVariantResolution.overlay_plan`; the command never rereads a
live source tree to construct the second side. Changes are ordered by portable
path. In the JSON/machine model, each present Base/client side reports size,
SHA-256, and portable mode. Human output prints unified diff content for
emitted text changes; binary, large, and budget-omitted changes print metadata
instead, including the omission reason when present.

- Binary changes are metadata-only.
- A change whose combined Base/client input exceeds 64 KiB is metadata-only
  with `diff_omitted=size_limit`.
- Safe UTF-8 unified-diff input has a 256 KiB aggregate budget per command.
  The budget is consumed in path order; after exhaustion, later text changes
  remain `kind=text` but are metadata-only with
  `diff_omitted=total_size_limit`.
- Small binary files do not consume the aggregate text budget.

`resolve` and top-level `diff` use the shared JSON v1 envelope. They do not write source,
configuration, registry, provenance files, deployments, caches, or Agent
links. They do not invoke Git, fetch, commit, or push.

A scoped edit workspace contains only its authored Family/Client layer. An
absent Variant remains absent at begin and is published only during apply.
Apply holds the deployment and per-Skill locks, validates the layer baseline,
renders only affected detected/enabled clients, atomically replaces their
links, and rolls back the source layer and prior links on ordinary failure.
Family `kimi` therefore affects both Kimi clients, while Client `codex` does not
rebuild or relink WorkBuddy. Apply writes local receipts/backups but never runs
Git, commits, or pushes.

## Source-boundary threat model

Checking only `skills/<skill>` or a Variant target leaf is insufficient: an
attacker or concurrent process can replace an ancestor with a symlink, reparse
point, or different real directory while leaving the leaf looking ordinary.
The read transaction therefore:

1. captures the portable parent, configured Skills root, and present/missing
   Variants root before initial Base validation;
2. retains the exact immutable Base layer produced by that validation;
3. captures the logical Variant Skill root and target-directory identities;
4. rechecks source boundaries before resolution;
5. rechecks them after resolution and binds the final Base/Variant layer
   identities to the captured evidence.

Real-directory replacement, symlink/reparse replacement, missing-to-present,
target ABA, ambiguity introduced during the read transaction, or identity
drift fails with the structured safety error `variant_source_changed`. A case
ambiguity already present during initial validation fails closed with its
existing structured name/Base ambiguity error. Guard paths and filesystem
identities are local evidence only and never change cross-machine hashes.

## Portable synchronization and migration limits through 9.5

Registry schema v3 makes Variant intent portable. A v3 repository stores Base
under `skills/<skill>/`, each declared authored layer under
`variants/<skill>/<target>/`, and a sorted target allowlist in `registry.yaml`.
Machine-local source roots, deployments, sessions, backups, credentials, and
client detection never enter the Git package. Per-target installed baselines
remain in local config only.

`preview` and `status` expose Base and Variant sync units independently. A
remote change to one unit can be pulled while preserving a local change to a
different unit. A local and remote change to the same Base or Variant is a
conflict and stops before fast-forward or authored-source replacement. A
direct `pull` retains its stricter refusal to proceed over any local authored
change; the merge behavior belongs to the preflighted `sync` workflow. Push is
always explicit.

Registry v1/v2 remains readable and Base-only. Read-only commands do not
rewrite it; the first Variant mutation upgrades it to registry schema v3.
Detailed upgrade, rollback, and new-machine procedures are in
[Registry v3 Migration](../registry-v3-migration.md).

Do not infer any of these later roadmap capabilities:

- no `resolve --output` or arbitrary resolved-directory materialization;
- no `variant delete` command;
- no Web Variant badges, editor, client matrix, or resolved diff screen;
- no custom adapter schema or dynamic client/family registry.

Tampered layered deployments can be previewed against their current resolved
output and discarded/rebuilt. Capture remains Base-only: a synthesized client
output cannot be copied into Base without incorrectly widening its blast
radius, so layered preview offers only explicit discard until scoped recovery
can attribute authored changes to one layer.

Before relying on a Variant, validate it and inspect every relevant exact
client separately. Kimi Code receives the `kimi` family layer before its
optional exact-client layer.
