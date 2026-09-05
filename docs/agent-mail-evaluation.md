# Evaluation: osteele/agent-mail

Evaluated 2026-09-05 on Linux, Node v24.19.0 and Bun v1.4.2.
Fresh upstream checkout: `397a010355d3fabdab26bf8c6545dd8092e83f38`.
This is a different project from Dicklesworthstone/mcp_agent_mail, our current backend.

## Recommendation

Keep agent-bridge's current backend for this workflow. The alternative has useful
coordination features, but it is not a drop-in replacement for our shared repository
identity and separate worktrees. An adapter would add work without solving the
idle Codex delivery gap. No backend, live session, or global agent configuration
was changed during this evaluation.

## Fit for our workflow

| Concern | osteele/agent-mail | Implication for agent-bridge |
| --- | --- | --- |
| Architecture | Per-session stdio MCP, durable filesystem spool; optional daemon for cached presence and reminders | Removes the always-required HTTP mail server, but reminders still depend on a fresh daemon snapshot |
| Repository identity | Canonical absolute directory, including symlink resolution | Linked worktrees have separate claim and task namespaces |
| Task ownership | Exclusive logical work leases, independent of file claims | Useful model for claiming an issue before editing files |
| File coordination | Atomic groups of file/directory claims; rejects conflicts in one project | Advisory coordination only; does not prevent direct filesystem edits |
| Delivery | Receipts distinguish push/read/hold/refusal/expiry; Claude channel support | Useful visibility, but live Claude channel delivery was not tested here |
| Codex reminders | UserPromptSubmit and asynchronous PostToolUse hooks; fixed unread-count reminders | This integration provides no push that wakes an already-idle Codex session |
| Status | Process identity checks, work state/activity, optional dashboard | Work activity is agent-reported; process presence does not prove task completion |
| Handoff | Explicit transfer requests, with timeout-based takeover | Unsuitable default when the owner may be waiting for user input |

Implementation evidence: [paths and storage](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/paths.ts),
[MCP handlers](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/channel.ts),
[claims](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/claims.ts),
[work leases](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/work.ts),
[reminder hooks](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/integrations.ts),
[reminder decisions](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/remind.ts).

## Verified findings

**Worktree mismatch:** A probe created a real Git repository and linked worktree,
then exercised the built Node ClaimStore and WorkStore with temporary state roots.
In one directory, a second owner was rejected for both the same file and issue.
Across the linked worktrees, both owners could claim `shared.txt` and issue `432`.
This follows the upstream directory model; it is a compatibility gap, not a claim
that its same-project exclusion is broken. Cross-directory messages are supported.

Our launcher instead derives one project identity from Git's common directory
and instructs both agents to reserve repository-relative paths under that identity.
Changing only the MCP command would lose that coordination boundary.

**Transfer defect:** Two complete upstream test runs each produced **285 passed,
1 failed**. The failure was `work lease + transfer state machine preserves ownership
invariants`: `Expected: true; Received: false`, at
`src/work.statemachine.test.ts:327`. The test passed alone.

A separate fixed-clock probe reproduced the cause: acquire, request transfer,
update the lease, request again within the same millisecond. The lease revision
increments, but request deduplication compares `expectedUpdatedAt` instead of the
revision, returning the old request. Subsequent settlement checks revisions and
can supersede that request. This is not evidence of duplicate simultaneous owners.
See [transfer implementation](https://github.com/osteele/agent-mail/blob/397a010355d3fabdab26bf8c6545dd8092e83f38/src/transfers.ts).

**Timeout semantics:** The same implementation settles expired requests by
attempting ownership transfer when the expected owner/revision still match.
It does not require proof that the owner process died. The default request timeout
is 300 seconds. This behavior differs from its separate dead-owner recovery path.

## Validation and limits

- Frozen-lockfile dependency installation and TypeScript build passed.
- Upstream `bun run check` passed: Biome checked 60 files; TypeScript exited zero.
- `bun run test` used upstream's throwaway-home wrapper. Full runs failed as above;
  the second ran 286 tests across 28 files in 6.67 seconds.
- Passing tests included real MCP path batches, task lifecycle, startup backlog,
  and dead-session recovery. These are protocol tests, not live model behavior.
- The worktree and fixed-clock probes passed against built JavaScript under Node.
- No global install, daemon startup, Slack integration, migration, or native
  interactive Claude/Codex session was exercised. The complete upstream CI matrix,
  including macOS and packaged Git installation, was not run.

Temporary evidence retained locally: `/tmp/agent-mail-evaluation-suite.log` and
`/tmp/agent-mail-worktree-probe.mjs`. Upstream checkout remained clean. Agent-bridge
runtime code was unchanged, so its full test gate was not repeated for this report.

## Smallest useful next improvements

1. Add an atomic issue claim under our existing common repository identity,
   before file reservations. This addresses duplicate issue work directly.
2. Show pending handoffs and delivery/read/acknowledgement separately in status.
   Preserve the distinction between observed activity and reported completion.
3. Require explicit handoff acceptance for a live owner. A timeout should surface
   the unresolved handoff, rather than silently transfer responsibility.

A future backend trial would first need repository/path mapping, translated MCP
tools and identities, rewritten checkpoint queries, and an explicit policy for
retaining old mail and reservations. Trial it on a disposable repository with
native sessions before considering migration of trade-mommy.
