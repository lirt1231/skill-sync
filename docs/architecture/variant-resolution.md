# Variant Resolution Architecture

This document describes the Variant source and read-only resolver available
through roadmap commit 7.6. It separates implemented behavior from later
deployment, editing, registry, and multi-device work.

## Source and resolved views

One logical Skill may have one Base source plus sparse overlays for an Agent
family or a concrete Agent client:

```text
authored sources                         immutable resolved view

~/.agents/skills/<skill>/        ─┐
                                  ├─ Base → family → exact client ─> client files
~/.agents/variants/<skill>/kimi/ ─┤
~/.agents/variants/<skill>/       │
  kimi-desktop/                  ─┘
```

| View | Meaning | Managed operation available through 7.6 |
| --- | --- | --- |
| Base source | Portable common Skill under `skills/<skill>/` | Full managed Base edit workflow |
| Family source | Sparse overlay such as `variants/<skill>/kimi/` | Minimal manifest creation plus read-only list/validate only; no content edit/apply workflow |
| Client source | Sparse overlay such as `variants/<skill>/kimi-desktop/` | Minimal manifest creation plus read-only list/validate only; no content edit/apply workflow |
| Resolved view | Immutable in-memory file plan for one exact client | No; it is derived evidence, not an authoring source |
| Rendered deployment | Content-addressed directory used by Agent links | Existing deployments are not Variant-aware yet |

The Base and Variant directories are authored sources. A resolved view is the
deterministic result of applying source layers; it is not another source tree
and must never be edited or copied back implicitly.

`variant create` scaffolds only `variant.yaml`; it does not provide a managed
way to author overlay files. Agents must not work around that limit by editing
`~/.agents/variants` directly. Managed Family/Client content editing and apply
belong to the later scoped edit-session roadmap.

## Layer selection and overlay semantics

The resolver accepts only a registered concrete client ID. It derives the
family, then applies layers from least to most specific:

1. Base;
2. the family Variant, when present;
3. the exact-client Variant, when present.

For `kimi-desktop`, `kimi` overrides Base and `kimi-desktop` overrides both.
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

## Source management and read-only inspection commands

These are the complete Variant source-management and inspection commands
implemented through 7.6:

```bash
skill-sync variant list
skill-sync variant list --skill my-skill --json
skill-sync variant create my-skill --family kimi
skill-sync variant create my-skill --client kimi-desktop
skill-sync variant validate my-skill
skill-sync resolve my-skill --client kimi-desktop --dry-run
skill-sync resolve my-skill --client codex --dry-run --json
skill-sync diff my-skill --base --client kimi-desktop
skill-sync diff my-skill --base --client claude-code --json
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

Both commands use the shared JSON v1 envelope. They do not write source,
configuration, registry, provenance files, deployments, caches, or Agent
links. They do not invoke Git, fetch, commit, or push.

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

## Migration limits through 7.6

The resolver is intentionally inspection-only. Do not infer any of these later
roadmap capabilities:

- no `resolve --output` or arbitrary resolved-directory materialization;
- no `variant delete` command;
- no Family/Client edit-session scope or Variant apply workflow;
- no Variant-aware registry schema, Git packaging, pull/push conflict unit, or
  fresh-machine reconstruction of `variants/`;
- no Variant-aware deployment cache rebuild or Agent-link switching;
- no Web Variant badges, editor, client matrix, or resolved diff screen;
- no persisted provenance manifest or stale Variant-render detection;
- no custom adapter schema or dynamic client/family registry.

Today, `variant create` writes only a local minimal source manifest and
`resolve`/`diff` inspect existing local sources. Existing `sync`, `pull`,
`push`, `link`, deployment, Web, and Base edit-session flows must not be treated
as Variant-aware. Until the registry and multi-device commits land, do not
claim that normal Skill Sync reconstructs Variant sources on another machine.

Before relying on a Variant, validate it and inspect every relevant exact
client separately. Kimi Code and Kimi Desktop share the `kimi` family layer but
may differ because an exact-client layer has higher precedence.
