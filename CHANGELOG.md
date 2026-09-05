# Changelog

## [0.2.0] - 2026-09-05

First published release of Agent Bridge.

### Added

- Claude Code and Codex launchers with separate persistent Git worktrees and a
  shared repository identity.
- Authenticated local messaging, acknowledgements, and advisory file reservations.
- Atomic issue claims across worktrees, explicit handoff acceptance and decline,
  cancellation, stale-offer rejection, and persistent ownership after restarts.
- Native lifecycle checkpoints for coordination notices and observed activity.
- Status and progress reports that distinguish activity, ownership, verification
  evidence, pending mail, and unfinished work.
- Claude Code and Codex plugins sharing the `coordinate` skill.
- Python wheel and source packages, user and administrator installation targets,
  and `python -m agent_bridge` support.
- Google-style documentation and configured style checks, plus integration tests
  for real MCP transport, concurrent claims, process exits, and installed packages.
- Tagged release automation with plugin bundles, locked runtime requirements,
  SHA-256 checksums, and verification of downloaded assets before publication.

### Changed

- Moved runtime code to the top-level `agent_bridge/` package.
- Made `make install` install a package snapshot; editable installation is now
  explicitly available through `make install-dev`.
- Kept project documentation focused on Agent Bridge and removed the earlier
  competitor evaluation document.

### Requirements and limitations

- Python 3.12+, Git, uv, and separately installed, authenticated native clients.
  Installation may download dependencies; the wheel is not a standalone binary.
- Linux integration is tested. macOS support has not been validated in this release.
- The plugins provide a coordination skill; the launcher configures worktrees,
  MCP, and lifecycle hooks. Start new sessions to load installed plugin skills.
- Reservations are advisory. Hooks do not wake already-idle sessions. Reports are
  agent-provided evidence, not independent proof of completion.
- All-user installation requires administrator privileges. No automatic merging,
  pushing, issue closure, or timeout-based ownership takeover is performed.
- The test suite reports upstream transport deprecation warnings. Live model
  compliance requires a separate two-terminal trial.

[0.2.0]: https://github.com/suneel944/agent-bridge/releases/tag/v0.2.0
