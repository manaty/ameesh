---
type: Study
title: "#1408 — externally managed, versioned persona memory projections: an opt-in managed mode without a second writer"
description: "Design note (lot #4516, owner decision #164208), revised after the review of 29/10 (mty-amh-docs-01): what the native persona memory does today and what it lacks; an opt-in `managed` mode where an admitted external memory service supplies an audience-authorised, immutable projection; separate bindings (persona, verified requesters, session, harness, host, projection); a session pinned to one requester set; idempotent attach; global revocation separate from session rotation, marked centrally and authorised; observations as proposals with receipts; a complete-or-not session-set observation; refusal/capability matrix; nine proposed lots of at most one day; questions for the owner. No code, no real service."
status: draft
tags: [persona, memoire, projection, audience, revocation, session, vie-privee, identite, opt-in]
generated: { by: "ameesh-dev7/claude-opus-5-5", at: "2026-10-30T01:30:00Z" }
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
  - { resource: "src/ameesh/identity.py; src/ameesh/resident_queue.py; src/ameesh/mail.py", title: "Session binding; human proof carried by a request (payload.human_account) and its verification" }
  - { resource: "src/ameesh/storage/postgres/lot_sessions.py; src/ameesh/migrations/0384_agent_lot_sessions.sql", title: "Parked lot sessions (no host column)" }
  - { resource: "src/ameesh/adapters.py", title: "Harness command line (prompt passed as an argument)" }
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
   the turn's set of verified requesters, session, harness, host). The
   service alone decides the audience. It returns one **immutable,
   digested, bounded** text. The runner checks it, passes it to the harness
   outside the command line, and records the load.
3. A session is **pinned to the requester set** it was loaded for. A turn
   whose requester set differs does not run in that session: the runner
   rotates first.
4. A loaded projection is tied to **one harness session**. A rotation (one
   session) or a revocation (every session holding that projection)
   **ends the session**: it is marked non-resumable **centrally**, before
   any host acts, then its transcript is withdrawn from the harness store
   by the host that holds it. Filtering tool output is not retirement.
   Only the admitted service and the mesh's humans may ask for either.
5. What a session learns leaves only as a **proposal** in an outbox,
   delivered with an idempotency key and answered by a recoverable receipt.
   ameesh never marks a proposal as confirmed.
6. A **session-set observation** says whether it is complete, and gives
   the last transition to zero sessions. It never infers this from
   `persona_sessions`.

Nine lots of at most one day each (about 58 agent hours), all
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
| Isolation | `require_isolation` refuses memory transport if another active persona may share the UID, except under an admitted `memory_isolation: shared-uid-accepted` Host card (`persona_memory.py:43-77`). It is **not** a placement rule: at activation the runner catches the refusal, logs "memory unavailable" and the turn runs without memory (`runner.py:7519-7535`) | keep the check, no automatic exception; managed mode must not degrade silently (§3) |
| Visibility | at placement, the host's humans must be able to read the memory repository through the forge, fail-closed, cached (`visibility.py:265-314`, called from `canon_sync.py:1423, 1795`) | depends on a forge repository; no notion of an external service |
| Host configuration | `config.json` (JSON object, `config.py:893-925`), with a `persona_memory` dict (`config.py:546`) | natural slot for the service endpoint and token file |
| Canon | the Agent card keeps only `memory.repository` (`canon.py:3617-3624`); all its fields are scalars or lists of text | no mode, no service; data only, which we keep |
| Identity | a session proves its persona through the lease (`AGENT_MAIL_NAME` + runner id + epoch) or an explicit binding (`identity.py:121-220`) | sound; reused |
| Requester | the depositing process's `user@host` is carried as `payload.human_account` (`cli.py:678`, read by `mail.human_account`, `mail.py:701-706`); `resident_queue.identity_for` (`resident_queue.py:256-290`, through `verify_human`, l.128) checks it against the member card's `local_accounts`. `ameesh-turn-context/1` already has `requests[].sender` (a string) and the raw `requests[].payload` (`turn_context.py:139-151`) | the **verification result** is not a field of the turn context; nothing derives a requester set per turn or per session |
| Sessions | one live lease means at most one running harness session per persona; lot sessions can be **parked** (`agent_lot_sessions`, `runner.py:2945`, `_has_parked_session` l.3111, #2055) and resumed by `lot_sessions.switch` on harness/account/cwd only (`storage/postgres/lot_sessions.py:35-100`); `agent_lot_sessions` has **no host column** (`0384_agent_lot_sessions.sql:21-31`); `persona_sessions` keeps one open row and writes history best-effort (`persona_sessions.py:58`) | history is not proof that sessions ended; parked sessions are resumable transcripts, resumable from another host once the lease has moved |
| Discarding | the runner unlinks its `session` file and falls back to a known session (`_session_connue`, `runner.py:3597`) or the brief (`runner.py:3527-3590`); old ids go to `session-history.jsonl`; `ameesh adopt` takes over a closed session | **nothing** forbids resuming a given id, and **nothing** removes a transcript from `~/.claude/projects` or `sessions/` (`reprise.py:102`) |
| Prompt channel | `adapter.command` puts the prompt text in the last argument (`adapters.py:1154-1170`; ACP `--prompt`, l.1220-1238) | anything in the prompt is readable by **every local user** through `ps` and `/proc/<pid>/cmdline` while the harness runs |
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
* The **MemoryService** card holds data only: `name`, `contract:
  ameesh-memory-projection/1`, `admitted_hosts` (names or tags),
  `max_projection_bytes` (at most 32 kB), `responsible`, and the
  identifier (never the value) of the credential the service uses to call
  ameesh (§5). There is no URL, no credential, no import path and no
  expression. Admitting a service that will receive member identities and
  persona context is an **owner decision** (canon §10: a new external
  service, data leaving the mesh), signed with `ameesh decide`; the
  bureau's canon PR only records the signed decision (Q6).
* The **endpoint and the credential** live in the host's `config.json`,
  under the existing `persona_memory` object:

  ```json
  {"persona_memory": {"services": {"acme-memory": {
     "url": "https://memory.example/",
     "token_file": "/home/ameesh/.config/ameesh/acme-memory.token"}}}}
  ```

  `token_file` is 0600, checked by `httpkit.check_private`; TLS is required
  off loopback.
* **Placement is local and does not call the service.** A managed persona
  is placeable on a host only if the host is in the card's
  `admitted_hosts`, has the service section with a readable token file, and
  passes every current rule (admission; the isolation check of §3). The
  audience is not a placement question: it is decided per load (§3), by the
  service. So a service outage never makes a placement inadmissible, and
  `on_unavailable` governs the load, not the placement. The forge
  visibility rule does not apply to a managed persona (there is no
  repository); the audience check at load replaces it.

## 2. Separate bindings, and a session pinned to a requester set

The projection request carries **separate facts**. None of them grants
anything by itself:

| Fact | Source | Note |
|---|---|---|
| `persona` | lease (`identity.resolve_binding`, source `runner`) | never self-declared (`explicit` refused) |
| `requesters` | the set of `{member, verified}` of the turn's requests (§2.1), sorted; empty when the turn serves no human request | unverified entries are passed as they are; the service decides |
| `session` | harness session id; `new` before the first launch | |
| `harness`, `host` | runner | |
| `admission` | MemoryService card commit and Host card commit | lets the service refuse an outdated admission |

A memory declaration gives **no right**. A projection's text is context,
never authority: it approves nothing (0012) and authorises no disclosure.
The service is the only judge of the audience. ameesh does not interpret
the projection's content.

### 2.1 The requester of a request

`requests[].sender` stays a string: `ameesh-turn-context/1` accepts only
additive optional fields. The turn context gains an **optional, distinct**
field per request, `requests[].requester = {member, verified}`, computed
by the runner from the transported proof (`payload.human_account`) with
`resident_queue.identity_for`; it is absent for a request that is not from
a human. Readers that do not know it are unaffected. The turn's
**requester set** is the set of these values over the turn's requests.

### 2.2 Pinning

A session serves several turns, and a turn can carry requests from several
humans. So the audience is fixed **per session**, on the requester set:

* the projection is loaded for the requester set of the session's first
  turn, and the load records that set (`requester_key`, a digest of the
  sorted set);
* before each later turn, the runner computes the turn's set. Equal set:
  the turn runs in the session. Different set (a second human, a mix of
  humans, a human after a `null` turn, or the reverse): the runner
  **rotates** (§4) and loads a new projection for the new set before the
  turn runs. A turn is never served by a session loaded for another set;
* for a multi-request turn, the service receives the whole set and decides
  the audience for it (typically what all of them may see); ameesh does not
  split or reorder the queue for this.

Test cases: second human in the same session; a turn with requests from
two humans; `null` (event, wake) then a human, and a human then `null`.

## 3. Loading an immutable projection

At session start (activation, rotation, new lot session), and only then:

0. If another active persona may share the UID (`require_isolation`, no
   admitted exception), the load is refused with `memory-isolation` and
   handled like an unavailable service (`postpone` or `run-without`), never
   silently: the turn context and the lot journal say so.
1. `POST /v1/projections` with the bindings and an `Idempotency-Key` =
   `persona:session:requester_key:turn_id`.
2. The answer is `{projection_id, version, digest (sha256 of content),
   content (UTF-8 Markdown), audience_ref, expires_at, admission_epoch}`.
   The runner refuses an answer whose digest does not match, whose size is
   above the bound, which has no version, or in which the existing
   `SECRET_PATTERNS` match.
3. **Last check before launch**: just before starting the harness, the
   runner reads `GET /v1/projections/{id}/status`. If the admission epoch
   has moved, or the projection is revoked or expired, it does not launch:
   rights lost while loading never reach the model.
4. The content reaches the harness **outside the command line**: through
   standard input or an inherited private file descriptor (pipe), in a
   block marked with `projection_id`, `version` and `digest`. Today the
   prompt is an argument (`adapters.py:1154-1170`), readable by every local
   user through `/proc/<pid>/cmdline`. A harness adapter without such a
   channel cannot host a managed persona (`memory-prompt-channel`,
   refused at load). ameesh never writes the content to disk; the harness
   transcript still holds it, which is why retirement (§5) deals with
   transcripts.
5. A `persona_projection_loads` row records the load in the same
   transaction as `begin_turn`: `load_id`, persona, session, turn, host,
   harness, `requester_key`, projection_id, version, digest,
   `audience_ref`, `expires_at`, `admission_epoch`, loaded_at, state
   (`loaded | retiring | retired`), retired_at, retire_reason,
   retire_receipt. The `host` column is what tells §5 and §7 where a
   transcript lives (`agent_lot_sessions` has none). `turn-context.json`
   names the projection (`{id, version, digest}`), never its content.
6. **Expiry** (`expires_at`) is not revocation: the model already holds the
   content, and stopping the turn would not withdraw it. A projection that
   expires during a turn lets the turn finish; the session is then rotated
   before any next turn. A revocation (§5) stops the turn at once.

**Context slot states** (2nd clarification), per harness session:

| State | Meaning | Attach of projection P@v |
|---|---|---|
| `clean` | no projection loaded in this session | load, then `attached(P@v)` |
| `attached(P@v)` | the same identity, version and requester set are already loaded | **idempotent no-op**, same `load_id` |
| `occupied(Q@w)` | another projection, version or requester set is loaded | refused (`occupied`); only a rotation or a revocation frees it, by ending the session |

The same attach repeated (a duplicate refresh or a lost answer) gets back
the same `load_id`. An attach for another host, requester set or session
is a new load, never a reuse.

## 4. Refresh is a session rotation

A harness cannot unload part of its context. A **refresh is a rotation of
one session**: `POST /v1/rotate {persona, load_id, reason}`, scoped to that
load. The runner ends the session at a safe point between two turns, the
session goes through the retirement steps of §5 (central mark, then local
withdrawal), and a new session opens with a new load. Reusing a resident
session is refused. Other sessions holding the same projection are **not**
touched: a refresh is never global. A rotation asked during a turn takes
effect at the end of that turn. Requester-set changes (§2.2) and expiry
(§3.6) use the same rotation internally.

## 5. Revocation and retirement

Two operations, with different scopes:

* **rotate** (§4): one `load_id`, one session;
* **revoke**: `POST /v1/revoke {persona, expected: {projection_id,
  version}, reason}`, **every** load of `P@v` for that persona, on every
  host, running or not. It is a **compare-and-revoke**: loads of `P@v` are
  revoked; a load of a newer version is untouched, and if only newer
  versions are loaded the answer is `superseded`, **with no effect**. A
  late revocation never stops a newer context, even when the service has
  already invalidated the old receipt. Nothing loaded: `already-clean`.

### 5.1 Who may ask

Both are rights gestures. They are accepted from:

* the **admitted MemoryService** of that persona, with the ameesh API
  token named on its card: a server-side token with the single scope
  `memory:retire:<service>`, limited to personas whose card names that
  service, stored hashed on the server, issued and revoked like the other
  API tokens;
* a **human member** with the admin role, through `ameesh memory revoke` /
  `rotate` under their proven identity.

An agent, a persona or another service is refused (`memory-retire-forbidden`),
including the persona itself. Every call, accepted or refused, is
journaled: actor, token id or member, operation, expected identity, result,
receipt.

### 5.2 Central first, local second

The server cannot stop a process or delete a file on a host. So:

1. **central mark**, on the server, in the **same transaction** as the
   accepted request: the loads go to `retiring`, and each session id goes
   into `revoked_sessions` (session, harness, persona, reason, at). This
   table is read on **every** resume path, whatever host asks:
   `_session_connue`, the parked-session resume and `lot_sessions.switch`,
   `ameesh adopt`, and the relay to another harness. A revoked id is never
   passed to `--resume`. Since the mark is central and written before any
   host acts, a host that takes over the lease after the holder went down
   cannot resume the session, even if the holder never applied its local
   steps;
2. **signal**: the server sends a `memory-revoked` event to the persona
   (existing mail/event path). The runner of the host named in the load
   also reads `revoked_sessions` before every turn and on heartbeat, so a
   lost event delays the local steps but not the fence;
3. **local steps**, by the runner of the host named in the load: stop a
   running turn of that session (the L498 cgroup kill), then withdraw the
   transcript from the harness store (Claude `projects/…/<id>.jsonl`,
   Codex/dsh `sessions/…`) according to Q1. A host that is down applies
   them **before any turn** when it comes back. What happens to the mail of
   a stopped turn follows the existing behaviour of an interrupted turn;
   M6 checks and tests it, and adds no requeue mechanism;
4. **receipt**: `{persona, operation, expected, loads:[{load_id, session,
   host, central: marked, local: pending|done|failed, transcript:
   withdrawn|absent|failed, turn_stopped}], at}`. A load is `retired` only
   when its local part is `done`. A timeout or an unscoped acknowledgement
   is not proof. The service may re-ask with the same key and gets back
   the same receipt, updated as the hosts report.

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
  turn, load_id, requester_key, key, body digest, state
  `pending → submitted → acknowledged | refused`, receipt). The insert is
  idempotent on `(persona, key)`. The same key with another digest is
  refused (`key-reused`), as with mail.
* A background job delivers it to `POST /v1/proposals` with the same
  `Idempotency-Key`. A lost answer is retried with the same key. A
  duplicate gets the service's same receipt. The receipt is the service's,
  verbatim. ameesh never writes "confirmed".
* Body: at most 16 kB, `SECRET_PATTERNS` refused at entry. The body is
  kept **until acknowledgement, then erased**, and only the digest stays
  (Q3). A proposal made after the revocation or rotation of its load is
  refused (`projection-retired`), because it came from withdrawn context.
* No turn-end push and no consolidation turn: the service owns
  synchronisation and cadence.

## 7. Session-set observation (1st clarification)

`ameesh memory sessions <persona> --json` and `GET
/api/v1/personas/{p}/memory-sessions`:

```json
{"schema": "ameesh-memory-sessions/1", "observed_at": 1761772800.0,
 "admission": "<commit>", "completeness": "complete|partial|unknown",
 "sessions": [{"session": "…", "host": "…",
               "state": "running|parked|ended|retiring",
               "projection": {"id": "…", "version": 7}}],
 "last_zero_at": 1761771000.0, "last_zero_seq": 41}
```

* The set is **every session whose transcript may still hold an admitted
  projection**: every load not yet `retired`, whatever the session's state
  (running, parked, ended normally, or retiring). A session that ends
  normally is retired like a rotation (reason `ended`, transcript withdrawn
  per Q1); until its local part is done, it stays in the set. The set is
  never read from `persona_sessions`, whose writes are best-effort.
* The **host universe** is the set of hosts named by those loads. The
  observation is `complete` only if every one of them has a fresh runner
  heartbeat and no pending local step older than that heartbeat. Otherwise
  `partial` (some hosts are stale) or `unknown` (the database or the API
  cannot be read). An empty set that is not `complete` is **not** a zero.
* `last_zero_at`/`last_zero_seq` is written in the same transaction as the
  last load of the set becoming `retired`. Its monotonic sequence lets the
  consumer tell a new closure from an old one. Batching and the "final
  flush" policy stay with the consumer.

## 8. Mediated executor

The mediated executor has no SQL. Managed mode needs three exec-API
operations in the closed contract: `memory.load` (record and recheck),
`memory.retire_local` (the local steps of §5.2) and `memory.propose`. The
projection request itself goes from the executor's host to the service:
the content never passes through the ameesh server. Until that lot (M9), a
managed persona is **not placeable** on a mediated executor. It is refused
at placement, not silently degraded as native memory is today.

# Refusal and capability matrix

| Situation | Behaviour | Code |
|---|---|---|
| card with `managed` and `repository` | `canon check` error | `memory-mode-conflict` |
| host without the service's section or token | not placeable | `memory-service-unconfigured` |
| host not in the card's `admitted_hosts` | not placeable | `memory-service-host-refused` |
| mediated executor (before M9) | not placeable | `memory-managed-unsupported` |
| another active persona may share the UID, no admitted exception | no load; `postpone` or `run-without`, said in the turn context and journal (never silent) | `memory-isolation` |
| harness adapter without a private prompt channel | no load; `postpone` or `run-without` | `memory-prompt-channel` |
| service unreachable at session start | `on_unavailable: postpone`: turn postponed, request kept, retried with backoff; `run-without`: session opens `clean`, and the turn context says so | `memory-service-unavailable` |
| service refuses the audience | no projection; `postpone` or `run-without` as configured; never a fallback to another source | `memory-audience-refused` |
| persona identity not proven by the lease | no request sent | `memory-identity-unproven` |
| requester unverified or absent | sent as `verified:false` / empty set; the service decides | — |
| turn's requester set differs from the session's | rotation before the turn | `requester-set-changed` |
| digest mismatch, oversize, secret pattern | load refused, nothing reaches the model | `memory-projection-invalid` |
| revoked between fetch and launch | launch cancelled | `memory-revoked-before-launch` |
| projection expired during a turn | turn finishes; rotation before the next turn | `memory-projection-expired` |
| attach of the same P@v, same set | no-op, same `load_id` | `already-attached` |
| attach while another projection or set is loaded | refused | `occupied` |
| revocation of an older version only | no effect | `superseded` |
| revoke/rotate by an agent, a persona or another service | refused, journaled | `memory-retire-forbidden` |
| a local step failed or the host is down | load stays `retiring`; the central fence holds | `retire-incomplete` |
| resume of a revoked session, on any host or harness | refused, new session | `session-revoked` |
| native command on a managed persona | refused | `managed-mode` |
| proposal key reused with another body | refused | `key-reused` |
| proposal after revocation or rotation | refused | `projection-retired` |

Limits stated with the matrix: provider-side logs; another process under
the same UID (until L155 layer 1); content visible to the harness process
itself and its tools.

**Concurrency, as it really is**: at most one *running* harness session per
persona (one live lease). It can have several *resumable* sessions (one per
parked lot, 0025), each with its own load and requester set. A revocation
reaches all loads of the revoked version; a rotation reaches one. Per host,
managed personas share the current isolation limit.

# Qualification (no real service)

A fake service in the tests (local HTTP, injected answers and failures),
delivered first (M1):

* **native regression**: unchanged behaviour of `open_copy`/`sync`/
  `consolidate` for a card without `mode`; the full existing suite;
  `requests[].sender` unchanged in the turn context;
* audience admitted and refused; host admitted and refused; host without a
  token; service down at placement (still placeable) and at load;
* requester set: second human in the same session, a turn with two
  humans, `null` then a human, a human then `null` (each rotates);
* duplicate and lost answer for a load, a proposal, a rotation and a
  revocation (same key, same receipt);
* revoke/rotate by an agent, by the persona, by a token of another service
  (refused, journaled);
* rights lost between fetch and launch; projection expired during a turn;
* late revocation of v6 after v7 is loaded (`superseded`); revocation of
  P@v loaded in two parked sessions (both); rotation of one of them (the
  other untouched);
* holder host down, lease taken by another host, resume attempted before
  the holder's local steps (refused by the central mark); local steps
  applied on the holder's return before any turn;
* reconnection and harness change (Claude → Codex relay): the revoked
  session is not resumed under either harness, and its transcript is
  withdrawn from both stores;
* `/proc/<pid>/cmdline` of a managed harness never contains the content;
* the session-set observation with one stale host (`partial`), a session
  ended but not yet withdrawn (still in the set), the empty set when
  complete, and `last_zero_seq` that goes up once per closure;
* the mediated executor: refused at placement before M9, and the three
  operations after M9.

# Proposed lots (after review and owner decision)

All are **sensitive** (§1: privacy, identity, rights, SQL, concurrency):
another family's review; **two families** for M2 (admission, placement),
M4 (identity, audience), M5 (rights, tokens) and M6 (deletion on the host).
Estimates are in agent hours, to be re-estimated by the bureau.

| Lot | Scope | Estimate |
|---|---|---|
| M1 | contract `ameesh-memory-projection/1` and matrix in `docs/`; fake service for the tests; consumer guide | 5 h |
| M2 | `memory.mode`/`service`/`on_unavailable` on the Agent card, MemoryService card, `canon check`; native paths refuse `managed-mode`; `config.json` section and 0600 token; local placement check and codes | 6 h |
| M3 | optional `requests[].requester {member, verified}` in `ameesh-turn-context/1` (additive, `sender` unchanged); turn requester set and `requester_key` | 4 h |
| M4 | service client, load, digest/size/secret checks, isolation as a load refusal, recheck before launch, private prompt channel per adapter; migration `persona_projection_loads`; pinning and rotation on set change and expiry; turn-context `projection` field | 8 h |
| M5 | revoke/rotate API and CLI, service token scope and authorisation, audit journal; central mark `revoked_sessions` read by every resume path (`_session_connue`, parked resume, `lot_sessions.switch`, `adopt`, harness relay); receipt (central part) | 8 h |
| M6 | local steps on the host: `memory-revoked` event, check before every turn and on heartbeat, turn stop, transcript withdrawal per harness (per Q1), local part of the receipt, application on return; behaviour of the stopped turn's mail checked | 8 h |
| M7 | `memory_proposals` outbox, `ameesh memory propose/receipt`, idempotent delivery, body erased after acknowledgement | 7 h |
| M8 | session-set observation (CLI, API route, OpenAPI), host universe from the loads, completeness by heartbeats and pending local steps, `last_zero_seq` | 6 h |
| M9 | exec-API operations `memory.load`/`retire_local`/`propose`, managed placement on a mediated executor | 6 h |

Order: M1 → M2 → M3 → M4 → M5 → M6 → (M7, M8 in parallel); M9 only if a
consumer runs on a mediated executor. M4 depends on M3 for the requester
set; M5 and M6 are split so that no lot exceeds one day. Total about 58 h
(52 h without M9). The consumer adapter stays on the consumer's side,
against M1.

# Questions for the owner

1. **Q1, transcript of a retired session**: purge it at once, keeping only
   an audit record (session, path, size, digest, time), or move it to a
   0700 quarantine with a retention period? Under the same UID, a 0700
   quarantine does **not** protect it from the other personas of the host,
   so a 30-day copy of restricted content stays readable there.
   Recommended: **purge at once** until L155 layer 1 (one Unix user per
   persona) exists; quarantine only afterwards, or encrypted with a key the
   personas cannot read.
2. **Q2, default when the service is unavailable**: `postpone` (recommended:
   no turn without the admitted context) or `run-without`?
3. **Q3, body of a proposal**: kept in the mesh database until the service
   acknowledges it, then erased (recommended), or never stored (delivery
   during the turn only, and a lost proposal if the service is down)?
4. **Q4, isolation**: is "no other active persona on the host" (current
   rule) enough for a first managed consumer, or must L155 layer 1 (one
   Unix user per persona) come first?
5. **Q5, sequencing**: keep M1-M9 unscheduled (P2) until the Nexlink and
   ameesh roadmaps are finished (recommended, per the objective in
   #164208), or schedule them now for the consumer?
6. **Q6, admission of a MemoryService**: confirm that each service is
   admitted by a signed owner decision (it receives member identities and
   persona context, and holds a token that can revoke), the bureau's canon
   PR only recording it (recommended), rather than by the bureau alone.

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

Draft, revised after the blocked review of 29/10 (mty-amh-docs-01, freeze
#26650): points 1 to 15 and the minor remarks are addressed above. No code,
no service and no real data were used. Lots M1-M9 are proposals: none is
created before the review and the owner's decision on Q1-Q6.
