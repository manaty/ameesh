---
type: Study
title: "#1408 — externally managed, versioned persona memory projections: an opt-in managed mode without a second writer"
description: "Design note (lot #4516, owner decision #164208): what the native persona memory does today and what it lacks; an opt-in `managed` mode where an admitted external memory service supplies an audience-authorised, immutable projection; separate bindings (persona, verified requester, session, harness, host, projection); idempotent attach and compare-and-retire; retirement of the model context through non-resumable sessions; observations as proposals with receipts; a complete-or-not session-set observation; refusal/capability matrix; eight proposed lots of at most one day; questions for the owner. No code, no real service."
status: draft
tags: [persona, memoire, projection, audience, revocation, session, vie-privee, identite, opt-in]
generated: { by: "ameesh-dev7/claude-opus-5-5", at: "2026-10-29T21:30:00Z" }
sources:
  - { resource: "manaty/ameesh#1408", title: "Support externally managed, versioned persona memory projections (16/10/2026, with the two clarifications of the same day)" }
  - { resource: "manaty/ameesh#1379", title: "Contact/workflow delivery thread (A+B), out of scope here" }
  - { resource: "../decisions/0029-persona-et-session.md", title: "Decision 0029 (persona, session, visibility, neutral memory)" }
  - { resource: "../decisions/0032-persona-roles-and-sessions.md", title: "Decision 0032 (persona memory priority in v2)" }
  - { resource: "../decisions/0025-une-session-par-lot.md", title: "Decision 0025 (one session per lot)" }
  - { resource: "persona-et-harnais.md", title: "Study: persona and harness (neutral memory contract)" }
  - { resource: "l155-governance-of-a-local-authority.md", title: "Study L155 (isolation layers, memory rights, retention)" }
  - { resource: "l2894-turn-context-api.md", title: "Study #2894 (turn-context file, idempotent requests)" }
  - { resource: "executeur-mediee.md", title: "Study: mediated executor (closed contract, idempotency)" }
  - { resource: "src/ameesh/persona_memory.py", title: "Native memory: clone, preface, sync, consolidation, isolation" }
  - { resource: "src/ameesh/persona_sessions.py", title: "Session history (one open row, best-effort writes)" }
  - { resource: "src/ameesh/runner.py", title: "Memory preface at activation, turn-end sync, session discard and parking" }
  - { resource: "src/ameesh/visibility.py", title: "Visibility rule at placement (forge-readable repository)" }
  - { resource: "src/ameesh/identity.py; src/ameesh/resident_queue.py", title: "Session binding; verified human sender of a request" }
  - { resource: "src/ameesh/turn_context.py", title: "ameesh-turn-context/1 (0600, additive schema)" }
---

# Recommendation

Add an **opt-in `managed` memory mode** to the persona card, beside the
unchanged `native` mode (the default, and the only mode today). In
managed mode:

1. ameesh never clones, pushes or consolidates anything for the persona;
   the native paths (`open_copy`, `sync`, `close`, `consolidate`) refuse.
2. At the start of each session, the runner asks an **admitted memory
   service** for a projection, giving it separate binding facts (persona,
   verified requester, session, harness, host). The service alone decides
   the audience. It returns one **immutable, digested, bounded** text. The
   runner checks it, loads it into the first prompt, and records the load.
3. A loaded projection is tied to **one harness session**. A refresh or a
   revocation **ends that session**: it becomes non-resumable, its
   transcript is withdrawn from the harness store, and a new session
   starts with the new projection or with none. Filtering tool output is
   not retirement.
4. What a session learns leaves only as a **proposal** in an outbox,
   delivered with an idempotency key and answered by a recoverable receipt.
   ameesh never marks a proposal as confirmed.
5. A **session-set observation** says whether it is complete, and gives
   the last transition to zero sessions. It never infers this from
   `persona_sessions`.

Eight lots of at most one day each (about 50 agent hours), all
**sensitive** (privacy, identity, rights, concurrency). Owner objective
(decision #164208): finish the Nexlink and ameesh roadmaps first. So this
note proposes these lots **unscheduled (P2)**, after an independent review
and an owner decision on the questions at the end. Nothing here is
implemented, and no real service or data was touched.

# Need

Issue #1408 (16/10/2026) comes from an integrating service that already
holds attributed human-memory facts, their confirmation lifecycle and their
per-recipient disclosure. It wants ameesh to run durable personas against a
**pinned, authorised context**, without a second memory writer and without
handing a session the full private repository. Two clarifications of the
same day add:

* **session set**: a consumer needs a fresh, complete observation of the
  admitted sessions of a persona, scoped to the admission version, and the
  last transition to zero sessions. Unknown, partial or stale observations
  must stay distinct from a complete zero. The consumer owns commit
  batching. No global native cadence change is asked for.
* **refresh and retirement**: attaching the same projection to the same
  reader/recipient/host/session must be idempotent. A clean context, an
  already-attached matching projection and an occupied context holding
  another projection must be distinguishable. Retirement must compare the
  expected projection identity/version: a late request for an old context
  must not stop a newer one. A timeout or an unscoped acknowledgement does
  not prove retirement. The host must refuse to resume revoked context.

Out of scope, as the issue says: the consumer's fact schema, policy,
content and cadence; the consumer adapter itself; the A+B workflow of #1379.

# What exists today (main `f30f5380`)

| Area | What exists | Gap for the request |
|---|---|---|
| Memory source | `persona_memory.open_copy` reclones the persona repository at each activation, into `<state>/memory/<persona>`, 0700 (`persona_memory.py:153`) | the whole repository and its history are on the host; there is no audience |
| Loading | `preface()` gives the session the path, `HEAD` revision and `MEMORY.md` index (≤ 200 lines/8 kB), at activation or for a new session (`runner.py:7519-7535`) | no projection, no digest; the revision is not recorded outside the prompt |
| Writing | the session appends to `journal/<session>.md`; `sync()` commits and pushes at turn end in a background thread, with secret and destructive-deletion guards (`runner.py:8876`, `persona_memory.py:264`) | a second writer by construction; no receipts |
| Consolidation | `ameesh memory consolidate` sends a memory turn; the session rewrites `memories/` (`persona_memory.py:426`) | autonomous consolidation; cannot be disabled per persona |
| Isolation | `require_isolation` refuses memory transport if another active persona may share the UID, except under an admitted `memory_isolation: shared-uid-accepted` Host card (`persona_memory.py:43-77`) | keep as is; no automatic exception |
| Visibility | at placement, the host's humans must be able to read the memory repository through the forge, fail-closed, cached (`visibility.py:265-314`, called from `canon_sync.py:1423, 1795`) | depends on a forge repository; no notion of an external service |
| Canon | the Agent card keeps only `memory.repository` (`canon.py:3617-3624`); all its fields are scalars or lists of text | no mode, no service; data only, which we keep |
| Identity | a session proves its persona through the lease (`AGENT_MAIL_NAME` + runner id + epoch) or an explicit binding (`identity.py:121-220`) | sound; reused |
| Requester | a request's human sender is "verified" when `user@host` is in the member card's `local_accounts` (`resident_queue.py:140-170`); this only appears in the prompt text | not attached to the turn or the session; not passed to anything |
| Sessions | one live lease means at most one running harness session per persona; lot sessions can be **parked** and resumed (`runner.py:3047, 3111`, #2055); `persona_sessions` keeps one open row and writes history best-effort (`persona_sessions.py:58`) | history is not proof that sessions ended; parked sessions are resumable transcripts |
| Discarding | the runner unlinks its `session` file and falls back to a known session or the brief (`runner.py:3527-3590`); old ids go to `session-history.jsonl` | **nothing** forbids resuming a given id, and **nothing** removes a transcript from `~/.claude/projects` or `sessions/` (`reprise.py:102`) |
| Native harness memory | disabled for runner sessions (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `runner.py:7639`) | keep |
| Turn context | `turn_context.py` writes `ameesh-turn-context/1` (0600, additive, refuses secrets), `AMEESH_TURN_CONTEXT` | natural slot for the projection reference (never its content) |
| Idempotency | mail `idempotency_key` (`mail.py:40-117`); the exec API's `exec_idempotency` table with request digest (`mediated_executor/dispatcher.py:360`) | patterns to reuse; no proposal/receipt type |
| Credentials | config keys that name 0600 files, `httpkit.read_token_file`, `storage/remote/http.py` (TLS off loopback, same `Idempotency-Key` on retry) | reuse for the service client |
| Mediated executor | `require_isolation` runs SQL; on a mediated connection it raises `NotSupportedRemotely` (a `DbError`), caught at `runner.py:7533`: memory is **silently unavailable** there | managed mode needs its own exec-API operations |

No lot or interface already covers this request. The closest pieces are
#2894 (turn context, delivered as lot A), the L155 isolation layers
(one Unix user and one database role per persona, not yet delivered), and
0029's neutral contract. **#1408 is the owning issue; this note proposes
its lots.**

# Design

## 1. The mode: data in the canon, endpoints on the host

```yaml
# Agent (persona) card — data only
memory:
  mode: managed            # native (default) | managed
  service: acme-memory     # name of an admitted MemoryService
  on_unavailable: postpone # postpone (default) | run-without
```

* `mode` defaults to `native`. An existing card behaves exactly as today:
  no field is renamed, and the native defaults stay unchanged.
* `managed` and `memory.repository` are **mutually exclusive**. `canon
  check` refuses a card with both. In managed mode, `open_copy`, `sync`,
  `close`, `consolidate`, the journal path and the consolidation prompt all
  refuse with `managed-mode`, before any Git command runs.
* The **MemoryService** admission is a canon card held by the bureau, with
  data only: `name`, `contract: ameesh-memory-projection/1`, `admitted_hosts`
  or tags, `max_projection_bytes` (at most 32 kB), `responsible`. There is
  no URL, no credential, no import path and no expression. Its admission
  follows the existing pattern (bureau canon PR, approved commit), like the
  `shared-uid-accepted` exception.
* The **endpoint and the credential** live in the host configuration:
  `[persona_memory.services.acme-memory] url = https://…`,
  `token_file = …` (0600, checked by `httpkit.check_private`, TLS required
  off loopback). A host without that section **cannot** place the persona.
  This fails closed, and the placement verdict says why.
* Placement keeps every current rule: admission, `require_isolation` (no
  automatic shared-UID exception), and visibility. For a managed persona,
  visibility asks the service, not the forge: the projection request
  carries the host's humans and admins, and the service answers `admitted`
  or `refused`. The placement cache stays per fingerprint, with a TTL.

## 2. Separate bindings

The projection request carries **six separate facts**. None of them grants
anything by itself:

| Fact | Source | Note |
|---|---|---|
| `persona` | lease (`identity.resolve_binding`, source `runner`) | never self-declared (`explicit` refused) |
| `requester` | the verified human sender of the request served by the turn (`resident_queue.Identity`), or `null` | `verified: false` and `null` are passed as they are; the service decides |
| `session` | harness session id; `new` before the first launch | |
| `harness`, `host` | runner | |
| `admission` | MemoryService card commit and Host card commit | lets the service refuse an outdated admission |

A memory declaration gives **no right**. A projection's text is context,
never authority: it approves nothing (0012) and authorises no disclosure.
The service is the only judge of the audience. ameesh does not interpret
the projection's content.

To pass `requester`, the verified-sender fact must leave the prompt text
and reach the turn: an additive `requests[].sender = {member, verified}`
field in `ameesh-turn-context/1` (lot M3). A turn that serves no human
request (an event, a wake) has `requester: null`, and the service chooses
the audience for that case.

## 3. Loading an immutable projection

At session start (activation, rotation, new lot session), and only then:

1. `POST /v1/projections` with the bindings and an `Idempotency-Key` =
   `persona:session:turn_id`.
2. The answer is `{projection_id, version, digest (sha256 of content),
   content (UTF-8 Markdown), audience_ref, expires_at, admission_epoch}`.
   The runner refuses an answer whose digest does not match, whose size is
   above the bound, which has no version, or in which the existing
   `SECRET_PATTERNS` match.
3. **Last check before launch**: just before building `argv`, the runner
   reads `GET /v1/projections/{id}/status`. If the admission epoch has
   moved, or the projection is revoked or expired, it does not launch: rights
   lost while loading never reach the model.
4. The content goes into the session's first prompt, in a block marked
   with `projection_id`, `version` and `digest`. **It is never written to
   disk by ameesh.** It will still be in the harness transcript: that is
   exactly why retirement (§5) must deal with transcripts.
5. A `persona_projection_loads` row (persona, session, turn, host,
   harness, projection_id, version, digest, loaded_at, retired_at,
   retire_reason, retire_receipt) records the load in the same transaction
   as `begin_turn`. `turn-context.json` names the projection
   (`{id, version, digest}`), never its content.

**Context slot states** (2nd clarification), per harness session:

| State | Meaning | Attach of projection P@v |
|---|---|---|
| `clean` | no projection loaded in this session | load, then `attached(P@v)` |
| `attached(P@v)` | the same identity and version are already loaded | **idempotent no-op**, same receipt |
| `occupied(Q@w)` | another projection or version is loaded | refused (`occupied`); only a retirement of `Q@w` frees it, by ending the session |

The same attach repeated (a duplicate refresh or a lost answer) gets back
the same `load_id`. An attach for another host, recipient or session is a
new load, never a reuse.

## 4. Refresh

A harness cannot unload part of its context. A **refresh is therefore a
rotation**: the runner ends the current session at a safe point between
two turns, then opens a new one with the new projection. Reusing a resident
session is refused. The old session goes through retirement (§5). A
refresh asked during a turn takes effect at the end of that turn, or at
once if it comes with a revocation.

## 5. Revocation and retirement

`POST /v1/retire {persona, expected: {projection_id, version}, reason}` on
the ameesh side (the CLI `ameesh memory retire` and an API route).
Retirement is a **compare-and-retire**:

* if the current load of a session is `expected`, it is retired;
* if the current load is newer, the answer is `superseded`, **with no
  effect**. A late retirement never stops a newer context, even when the
  service has already invalidated the old receipt;
* if nothing is loaded, the answer is `already-clean`.

Retiring one session, in this order, under the persona's registry lock:

1. **stop protected use**: a running turn is stopped (the L498 cgroup
   kill), and its pending mail goes back to the queue;
2. mark the session id **non-resumable**: a `revoked_sessions` table read
   by `_session_connue` and the parked-session resume (`runner.py:3111`).
   A revoked id is never passed to `--resume`, including for a parked lot
   session, on another host, or after a harness change;
3. **withdraw the transcript** from the harness store (Claude `projects/…
   /<id>.jsonl`, Codex/dsh `sessions/…`) into a 0700 quarantine outside
   every harness home. Question Q1 asks whether to purge it instead;
4. write the receipt: `{persona, projection_id, version, sessions:[id…],
   transcripts: withdrawn|absent|failed, turn_stopped, at, lease_epoch}`.

A step that fails leaves the load at `retiring`, **never** `retired`. A
timeout or an unscoped acknowledgement is not proof. The service may re-ask
with the same key, and gets back the same receipt once it is complete. A
host that is down keeps its sessions at `retiring`. Its runner applies the
retirement **before any turn** when it comes back. Until then, the lease
fence stops another host from resuming those sessions.

Limit, stated in the matrix: model providers keep their own logs, and a
process under the same UID could already have copied a transcript. ameesh
retires what the host holds. Until L155 layer 1 exists, "isolated" means
"no other active persona on the host" (current `require_isolation`).

## 6. Observations become proposals

In managed mode, the preface has no journal path and asks the session not
to write memory files. Instead, it offers:

```
ameesh memory propose --key <K> [--kind fact|preference|correction] < body.md
ameesh memory receipt <K>
```

* The proposal goes into a `memory_proposals` outbox (persona, session,
  turn, projection_id@version, requester, key, body digest, state
  `pending → submitted → acknowledged | refused`, receipt). The insert is
  idempotent on `(persona, key)`. The same key with another digest is
  refused (`key-reused`), as with mail.
* A background job delivers it to `POST /v1/proposals` with the same
  `Idempotency-Key`. A lost answer is retried with the same key. A
  duplicate gets the service's same receipt. The receipt is the service's,
  verbatim. ameesh never writes "confirmed".
* Body: at most 16 kB, `SECRET_PATTERNS` refused at entry. The body is
  kept **until acknowledgement, then erased**, and only the digest stays
  (Q3). A proposal made after the retirement of its projection is refused
  (`projection-retired`), because it came from withdrawn context.
* No turn-end push and no consolidation turn: the service owns
  synchronisation and cadence.

## 7. Session-set observation (1st clarification)

`ameesh memory sessions <persona> --json` and `GET
/api/v1/personas/{p}/memory-sessions`:

```json
{"schema": "ameesh-memory-sessions/1", "observed_at": 1761772800.0,
 "admission": "<commit>", "completeness": "complete|partial|unknown",
 "sessions": [{"session": "…", "host": "…", "state": "running|parked|retiring",
               "projection": {"id": "…", "version": 7}}],
 "last_zero_at": 1761771000.0, "last_zero_seq": 41}
```

* The set is **resumable sessions holding a projection**, read from
  `persona_projection_loads` (not retired) joined with the live lease and
  the parked sessions. It is never read from `persona_sessions`, whose
  writes are best-effort.
* `complete` only if every host that holds or may hold such a session has
  a fresh runner heartbeat. Otherwise `partial` (some hosts are stale) or
  `unknown` (the database or the API cannot be read). An empty set that is
  not `complete` is **not** a zero.
* `last_zero_at`/`last_zero_seq` is written in the same transaction as the
  retirement or end that empties the set. Its monotonic sequence lets the
  consumer tell a new closure from an old one. Batching and the "final
  flush" policy stay with the consumer.

## 8. Mediated executor

The mediated executor has no SQL. Managed mode needs three exec-API
operations in the closed contract: `memory.load` (record and recheck),
`memory.retire_local` (steps 1-3 on the host) and `memory.propose`. The
projection request itself goes from the executor's host to the service:
the content never passes through the ameesh server. Until that lot (M7), a
managed persona is **not placeable** on a mediated executor. It is refused
at placement, not silently degraded as native memory is today.

# Refusal and capability matrix

| Situation | Behaviour | Code |
|---|---|---|
| card with `managed` and `repository` | `canon check` error | `memory-mode-conflict` |
| host without the service's section or token | not placeable | `memory-service-unconfigured` |
| host not admitted by the MemoryService | not placeable | `memory-service-host-refused` |
| another active persona on the host, no admitted exception | not placeable (current rule) | `memory-isolation` |
| mediated executor (before M7) | not placeable | `memory-managed-unsupported` |
| service unreachable at session start | `on_unavailable: postpone`: turn postponed, request kept, retried with backoff; `run-without`: session opens `clean`, and the turn context says so | `memory-service-unavailable` |
| service refuses the audience | no projection; `postpone` or `run-without` as configured; never a fallback to another source | `memory-audience-refused` |
| persona identity not proven by the lease | no request sent | `memory-identity-unproven` |
| requester unverified or absent | sent as `verified:false` / `null`; the service decides | — |
| digest mismatch, oversize, secret pattern | load refused, nothing reaches the model | `memory-projection-invalid` |
| revoked between fetch and launch | launch cancelled | `memory-revoked-before-launch` |
| attach of the same P@v | no-op, same receipt | `already-attached` |
| attach while another projection is loaded | refused | `occupied` |
| retirement of an older version | no effect | `superseded` |
| a retirement step failed | load stays `retiring` | `retire-incomplete` |
| resume of a revoked session | refused, new session | `session-revoked` |
| native command on a managed persona | refused | `managed-mode` |
| proposal key reused with another body | refused | `key-reused` |
| proposal after retirement | refused | `projection-retired` |

**Concurrency, as it really is**: at most one *running* harness session per
persona (one live lease). It can have several *resumable* sessions (one per
parked lot, 0025), each with its own load. Retirement applies to all of
them. Per host, managed personas share the current isolation limit.

# Qualification (no real service)

A fake service in the tests (local HTTP, injected answers and failures):

* **native regression**: unchanged behaviour of `open_copy`/`sync`/
  `consolidate` for a card without `mode`; the full existing suite;
* audience admitted and refused; host admitted and refused; host without a
  token;
* duplicate and lost answer for a load, a proposal and a retirement (same
  key, same receipt);
* rights lost between fetch and launch; projection expired during a turn;
* late retirement of v6 after v7 is loaded (`superseded`); retirement
  while the host is down, then applied on its return before any turn;
* reconnection and harness change (Claude → Codex relay): the revoked
  session is not resumed under either harness, and its transcript is
  withdrawn from both stores;
* parked lot session revoked, then the lot resumes: new session;
* the session-set observation with one stale host (`partial`), the empty
  set when complete, and `last_zero_seq` that goes up once per closure;
* the mediated executor: refused at placement before M7, and the three
  operations after M7.

# Proposed lots (after review and owner decision)

All are **sensitive** (§1: privacy, identity, rights, SQL, concurrency):
another family's review, two families for M2/M4 (rights, secrets).
Estimates are in agent hours, to be re-estimated by the bureau.

| Lot | Scope | Estimate |
|---|---|---|
| M1 | `memory.mode`/`service`/`on_unavailable` on the Agent card, MemoryService card, `canon check`; native paths refuse `managed-mode`; host config section and 0600 token; placement codes | 6 h |
| M2 | service client (`ameesh-memory-projection/1`), load, digest/size/secret checks, recheck before launch, preface block; migration `persona_projection_loads`; turn-context `projection` field | 8 h |
| M3 | `requests[].sender{member,verified}` in the turn context; binding facts of the request; `identity-unproven` | 4 h |
| M4 | compare-and-retire, `revoked_sessions` read by resume and parked sessions, stopping the turn, withdrawing transcripts per harness, receipt, `retiring` applied on the host's return | 8 h |
| M5 | `memory_proposals` outbox, `ameesh memory propose/receipt`, idempotent delivery, body erased after acknowledgement | 7 h |
| M6 | session-set observation (CLI, API route, OpenAPI), completeness by heartbeats, `last_zero_seq` | 6 h |
| M7 | exec-API operations `memory.load`/`retire_local`/`propose`, managed placement on a mediated executor | 6 h |
| M8 | contract and matrix in `docs/`, fake service, qualification scenarios above, consumer guide | 5 h |

Order: M1 → M2 → (M3, M4 in parallel) → M5 → M6 → M8; M7 only if a
consumer runs on a mediated executor. Total about 50 h. The consumer
adapter stays on the consumer's side, against M8.

# Questions for the owner

1. **Q1, transcript of a retired session**: move it to a 0700 quarantine
   with a retention period (recommended: 30 days, then purge by the
   auditor), or purge it at once? Purging is irreversible. Quarantine keeps
   an audit trail but leaves the disclosed context on the host.
2. **Q2, default when the service is unavailable**: `postpone` (recommended:
   no turn without the admitted context) or `run-without`?
3. **Q3, body of a proposal**: kept in the mesh database until the service
   acknowledges it, then erased (recommended), or never stored (delivery
   during the turn only, and a lost proposal if the service is down)?
4. **Q4, isolation**: is "no other active persona on the host" (current
   rule) enough for a first managed consumer, or must L155 layer 1 (one
   Unix user per persona) come first?
5. **Q5, sequencing**: keep M1-M8 unscheduled (P2) until the Nexlink and
   ameesh roadmaps are finished (recommended, per the objective in
   #164208), or schedule them now for the consumer?

# Alternatives considered

* **One restricted Git repository per audience** (the service publishes a
  projection branch, and the native path clones it). Rejected: it keeps a
  clone and a history on the host, keeps the turn-end push (a second
  writer), and does not solve retirement of what the model has already
  seen.
* **An MCP memory tool that the session queries**. Rejected as the main
  path: the content reaches the context through tool results that are
  impossible to retire, and the audience would be checked per call rather
  than per session. It remains possible later as a *read* of the same
  admitted projection.
* **Drop the projection into `CLAUDE.md`/`AGENTS.md` of the working
  directory**. Rejected: a file on disk, readable by any process under the
  same UID, and native memories are disabled for runner sessions (0029).

# State

Draft for review by another family. No code, no service and no real data
were used. Lots M1-M8 are proposals: none is created before the review and
the owner's decision on Q1-Q5.
