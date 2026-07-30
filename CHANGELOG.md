# Changelog

All notable changes to Skill Sync will be documented in this file.

The project follows semantic versioning after 1.0. During the technical preview,
minor releases may contain breaking changes when they are documented here.

## Unreleased

### Added

- Cross-device synchronization for explicitly selected Agent Skills.
- Codex, WorkBuddy, Kimi Code, and Claude Code detection and managed links.
- Base, family, and exact-client Variant sources and deterministic resolution.
- Transactional managed edit sessions with validation, impact, apply, recovery,
  Agent launch, and asynchronous local session deletion.
- Local Web UI with mutation previews, confirmations, inventory filters, link
  repair, import, backup, deployment inspection, and managed editing.
- Version-controlled `skill-sync-manager` Agent Skill with guided installation,
  private repository setup, ownership checks, recovery, and safe updates.

### Security

- Loopback-only Web binding and per-process mutation tokens.
- Fail-closed ownership, traversal, link, reparse-point, tamper, and conflict
  checks around managed sources and deployments.

### Known limitations

- Opening Codex or Kimi Code edit sessions is currently macOS-only.
- Windows support is experimental, and Linux/Windows CI validation is deferred
  while filesystem identity, junction, and recovery behavior is completed.
- Web tamper capture/discard recovery and guided conflict resolution are not yet
  implemented.
- Push-time secret scanning is planned but not yet available.
