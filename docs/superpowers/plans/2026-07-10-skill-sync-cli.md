# Skill Sync CLI Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python 3 standard-library CLI named `skill-sync` that safely synchronizes explicitly selected Agent Skills through a private Git repository.

**Architecture:** Keep core behavior in small modules under `skill_sync/`, with `cli.py` only parsing arguments and formatting output. Persist portable sync state in remote `registry.yaml` and machine-local state in JSON config. Use deterministic hashing and fail-closed Git/copy semantics to prevent accidental overwrite or third-party upload.

**Tech Stack:** Python 3 standard library, `unittest`, system `git`, no runtime third-party packages.

---

## File Structure

- Create `skill_sync/__init__.py`: package metadata.
- Create `skill_sync/errors.py`: user-facing exception types.
- Create `skill_sync/hash.py`: deterministic Skill directory hashing and ignore rules.
- Create `skill_sync/registry.py`: constrained `registry.yaml` reader/writer.
- Create `skill_sync/config.py`: local JSON config path, load, save, and mutation helpers.
- Create `skill_sync/platforms.py`: platform adapter interface and initial Codex adapter.
- Create `skill_sync/copying.py`: safe directory copy/install logic.
- Create `skill_sync/git.py`: subprocess wrapper and Git state operations.
- Create `skill_sync/core.py`: command workflows shared by CLI and future frontend.
- Create `skill_sync/cli.py`: `argparse` entrypoint and output formatting.
- Create `skill-sync`: executable shim for local use.
- Create tests under `tests/` mirroring core modules.

## Chunk 1: Core Data Handling

### Task 1: Hashing

**Files:**
- Create: `skill_sync/hash.py`
- Test: `tests/test_hash.py`

- [ ] Write tests for deterministic hashes independent of creation order, ignored files, binary content, and symlink rejection.
- [ ] Run `python -m unittest tests.test_hash` and verify expected failures.
- [ ] Implement `hash_skill_dir(path) -> str` and shared ignore predicates.
- [ ] Run `python -m unittest tests.test_hash` and verify pass.
- [ ] Commit with `git commit -m "feat: add deterministic skill hashing"`.

### Task 2: Registry YAML Subset

**Files:**
- Create: `skill_sync/registry.py`
- Test: `tests/test_registry.py`

- [ ] Write tests for reading/writing versioned registry data, comments, booleans, integers, nested skill mappings, unknown fields, and invalid indentation.
- [ ] Run `python -m unittest tests.test_registry` and verify expected failures.
- [ ] Implement the constrained YAML subset parser/writer.
- [ ] Run `python -m unittest tests.test_registry` and verify pass.
- [ ] Commit with `git commit -m "feat: add sync registry storage"`.

### Task 3: Local Config

**Files:**
- Create: `skill_sync/config.py`
- Test: `tests/test_config.py`

- [ ] Write tests for XDG/default config path resolution, missing config defaults, save/load round trip, and `last_installed_hash` update.
- [ ] Run `python -m unittest tests.test_config` and verify expected failures.
- [ ] Implement JSON config load/save helpers.
- [ ] Run `python -m unittest tests.test_config` and verify pass.
- [ ] Commit with `git commit -m "feat: add local config storage"`.

## Chunk 2: Filesystem and Git Safety

### Task 4: Platform Adapter

**Files:**
- Create: `skill_sync/platforms.py`
- Test: `tests/test_platforms.py`

- [ ] Write tests for Codex default root, `$CODEX_HOME`, Skill discovery, selected/external marking inputs, and invalid platform lookup.
- [ ] Run `python -m unittest tests.test_platforms` and verify expected failures.
- [ ] Implement Codex adapter and adapter lookup.
- [ ] Run `python -m unittest tests.test_platforms` and verify pass.
- [ ] Commit with `git commit -m "feat: add codex platform adapter"`.

### Task 5: Safe Copy

**Files:**
- Create: `skill_sync/copying.py`
- Test: `tests/test_copying.py`

- [ ] Write tests for copying hidden files, excluding generated noise, replacing existing destination, restoring on failure where practical, and matching final hash.
- [ ] Run `python -m unittest tests.test_copying` and verify expected failures.
- [ ] Implement temp-dir copy, backup, replace, and restore behavior.
- [ ] Run `python -m unittest tests.test_copying` and verify pass.
- [ ] Commit with `git commit -m "feat: add safe skill copy"`.

### Task 6: Git Wrapper

**Files:**
- Create: `skill_sync/git.py`
- Test: `tests/test_git.py`

- [ ] Write tests using temporary repositories for init, clone/use existing, dirty detection, ahead/behind counts, fast-forward pull, commit-if-changed, and push to local remote.
- [ ] Add fail-closed Git tests for diverged branches, missing remote branch, unrelated histories, force-push divergence represented as non-fast-forward divergence, and push rejection.
- [ ] Run `python -m unittest tests.test_git` and verify expected failures.
- [ ] Implement subprocess Git helpers and typed state results.
- [ ] Run `python -m unittest tests.test_git` and verify pass.
- [ ] Commit with `git commit -m "feat: add git sync wrapper"`.

## Chunk 3: Workflows and CLI

### Task 7: Core Workflows

**Files:**
- Create: `skill_sync/core.py`
- Create: `skill_sync/errors.py`
- Test: `tests/test_core.py`

- [ ] Write tests for `init`, `scan`, `select`, `deselect`, `status`, `pull`, `push`, and `sync` using temporary Skill roots and local Git repos.
- [ ] Include explicit `init` tests for non-existent local `--repo` initialization as a normal non-bare repo, `registry.yaml` creation, Git availability verification failure, default sync-dir, default branch, and Git auth/subprocess failure reporting.
- [ ] Include tests for remote ahead plus local changed stop, pull refusing local overwrite, expected registry dirty changes during push, external selection guard, no local paths in remote registry, baseline update after push, invalid paths, missing local paths, and paths without `SKILL.md`.
- [ ] Include workflow tests for repeated `--skill` filtering semantics on `status`, `pull`, and `push`, verifying scoped commands do not touch unselected or unfiltered Skills.
- [ ] Include workflow tests for Git fail-closed cases from `skill_sync/git.py`: divergence, missing remote branch, unrelated histories, force-push divergence, and push rejection.
- [ ] Run `python -m unittest tests.test_core` and verify expected failures.
- [ ] Implement workflow functions with user-facing exceptions.
- [ ] Run `python -m unittest tests.test_core` and verify pass.
- [ ] Commit with `git commit -m "feat: add skill sync workflows"`.

### Task 8: CLI

**Files:**
- Create: `skill_sync/cli.py`
- Create: `skill-sync`
- Test: `tests/test_cli.py`

- [ ] Write tests for argument parsing, command dispatch, text output basics, JSON status schema, and non-zero exits on user-facing errors.
- [ ] Add CLI tests for repeated `--skill` options on `status`, `pull`, and `push`.
- [ ] Add a golden JSON contract test covering exact top-level `schema_version`, `repo` fields, `skills` fields, and filtered output shape.
- [ ] Run `python -m unittest tests.test_cli` and verify expected failures.
- [ ] Implement `argparse` commands and the executable shim.
- [ ] Run `python -m unittest tests.test_cli` and verify pass.
- [ ] Commit with `git commit -m "feat: add skill-sync cli"`.

## Chunk 4: Final Verification

### Task 9: Repository Verification

**Files:**
- Modify as needed based on verification failures.

- [ ] Run `python -m unittest discover -s tests`.
- [ ] Run `./skill-sync --help`.
- [ ] Run a manual temp-repo smoke test: initialize, select a temporary Skill, push to local remote, clone into another config root, pull, and verify Skill files installed.
- [ ] Run `git status --short --branch`.
- [ ] Commit any verification fixes with focused messages.
