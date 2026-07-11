# Skill Sync V2 Implementation Plan

1. Add failing tests for canonical root configuration, Agent detection, link lifecycle, and Windows fallback.
2. Replace the public platform adapter with internal Codex and WorkBuddy target adapters.
3. Refactor core scan/select/pull/push/sync around `~/.agents/skills` and add link/unlink/doctor.
4. Update CLI arguments and output while retaining config migration compatibility.
5. Add failing HTTP API tests, then implement the local server and static management UI.
6. Generate and validate the `skill-sync-manager` Agent Skill.
7. Run unit tests, a two-machine Git smoke test, and real Codex/WorkBuddy link verification.
8. Create private GitHub repositories and push after GitHub authentication is available.
