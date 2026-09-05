# Architecture and contracts

Agent Bridge runs on one Linux host under one OS user. It coordinates participating
agents; it does not execute model requests, enforce filesystem permissions, replace
native approvals, or merge work.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `cli` | Git worktrees, native configuration, launch, status and reports |
| `server` | Authenticated MCP transport and bounded tool contracts |
| `store` | SQLite schema, migration, scoped mail and atomic leases |
| `process` | Linux process identity and pidfd shutdown |
| `issues` | Claim and handoff state transitions |
| `checkpoints` | Lifecycle observations and bounded context delivery |
| `state` | Private atomic JSON publication and operation locks |

The service is a singleton **per private state directory**. An exclusive startup
lock serializes launch and shutdown; the loopback port prevents a second listener.
Independent state directories remain independent. No global mutable application
singleton is needed. State paths and authenticated actors are explicit inputs,
which keeps transactions testable without HTTP.

Use abstractions for actual boundaries. Do not add factories, interfaces, or
inheritance solely to name a pattern. The HTTP server implements its standard-
library base contracts; the type gate checks method overrides.

## Authentication and protocol

The launcher registers lanes locally. Registration is not exposed over MCP.
A bearer token identifies exactly one project and agent. The database stores its
SHA-256 digest; private identity files retain the credential for restart. Tool
arguments cannot select another project or impersonate an agent. A separate
health token grants no tool access. Credentials travel through the native
client's environment, never the model prompt.

The server binds `127.0.0.1`, checks Host and Origin, rejects unauthenticated
requests, and avoids credential/body logging. It supports stateless JSON responses
over MCP Streamable HTTP, not SSE sessions or remote hosting. The independent
official MCP SDK exercises initialization and calls in CI.

Six tools cover sending, fetching, acknowledging, marking read, reserving files,
and releasing reservations. Unknown arguments fail. Sends require an idempotency
key: identical retries return the original message ID; changed retries fail.
Fetching never marks a message read or acknowledges it.

## Persistence and concurrency

Mail uses SQLite WAL with indexed inbox and active-lease queries. Each write
acquires an immediate transaction, validates and mutates, then commits once.
Connections close after every operation. Lock acquisition times out after 300 ms.
Conflicting reservation batches grant no paths. Directory overlaps are detected;
two globs conservatively conflict when either lease is exclusive. Use exact paths
when disjoint globs would otherwise be rejected. Renewals replace the owner's
previous lease atomically. Expired or released leases no longer block work.

Issue mutations use a repository-scoped lock and atomic JSON replacement. Only
the owner can offer work; only the named recipient can accept the current offer
ID. Cancellation invalidates that ID. No timeout or process exit transfers
ownership. Reported `ready` outcomes do not establish verified completion.

Shutdown verifies the module, state path and process creation ticks, then pins
the process with Linux pidfd before signaling. It does not kill arbitrary PIDs.

## Context and resource budgets

| Boundary | Limit |
| --- | --- |
| HTTP request | 16,384 bytes |
| Concurrent workers / socket timeout | 16 / 3 seconds |
| New message body | 4,096 UTF-8 bytes |
| Inbox page | Up to 5 messages; bodies omitted by default |
| Body page | Up to 1,024 Unicode characters |
| Serialized inbox result | At most 8,192 UTF-8 bytes |
| Hook preview batch | Up to 3 messages |
| Injected notice | At most 1,536 UTF-8 bytes |
| Active reservations | At most 128 per lane |
| Reservation lifetime | 30–3,600 seconds |

Inbox pages return `next_after_id` and `has_more`. For `next_body_offset`, refetch
with `after_id=message_id-1`, `limit=1`, and that `body_offset` before advancing.
Stored legacy text is not discarded to satisfy response budgets.

Hooks read local state without network requests or model calls. Session cursors
prevent duplicate delivery; issue revisions suppress unchanged reminders. New
sessions receive a bounded briefing of still-unreviewed mail. Stop retries do
not create continuation loops. Failed observations never request replay of an
already completed action. Coordination errors before edits pause work.

Status reports notice counts and injected UTF-8 bytes, not tokenizer counts or
API billing. Deterministic tests enforce context budgets. `make benchmark`
measures latency separately; results depend on hardware and workload.

## Migration and verification limits

Version 0.3 creates `bridge.sqlite3` and imports `mail.sqlite3` through a consistent
read-only snapshot. IDs, acknowledgements and leases are retained; the original
remains unchanged. Imported rows and the schema version commit together.
Existing identities are rebound locally at launch. Stop old services and sessions
before upgrading; no live workspace is automatically migrated or terminated.

CI covers temporary Git repositories, independent MCP clients, concurrent calls,
authorization failures, persistence, resource budgets and isolated wheel installs.
It does not demonstrate compliance by a paid live model session.
