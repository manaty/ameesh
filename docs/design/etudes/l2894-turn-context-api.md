---
type: Study
title: "Study — a supported turn-context and turn-observation API for integrations"
description: "TPK #2167 (lot #2894): the runner hands every turn a versioned, lease-fenced context (delivered requests, turn identity, accounting marker and reservation); `ameesh turn current --json` reads it inside the turn; `ameesh turn observe` and the read API expose execution, delivery and accounting state to supervisors; versioning guarantees and implementation lots."
status: draft
tags: [api, tour, bail, comptabilite, integrations, versioning]
generated: { by: "mty-amh-impl-08/deepseek", at: "2026-10-20T21:45:00+00:00" }
sources:
  - { resource: "https://github.com/manaty/ameesh/issues/2167", title: "TPK request: supported turn-context and turn-observation API (current request, lease, accounting)" }
  - { resource: "https://github.com/manaty/ameesh/issues/2166", title: "PendingSpend.clear_if precision; lot #2893, target v1.17.0" }
  - { resource: "https://github.com/thepackengers/issue-manager-agent/issues/819", title: "IMA document edits: 3–7 s target, per-call bridge spawns, one authorization per turn" }
  - { resource: "https://github.com/thepackengers/issue-manager-agent/issues/811", title: "IMA document agents: failing tool calls and a turn left running" }
  - { resource: "https://github.com/thepackengers/issue-manager-agent/issues/704", title: "IMA-on-ameesh migration, staged" }
  - { resource: "../../../src/ameesh/runner.py", title: "turn launch: identity env, mail reservation/delivery, accounting marker and account reservation" }
  - { resource: "../../../src/ameesh/identity.py", title: "lease-bound session identity" }
  - { resource: "../../../src/ameesh/resident_runner.py", title: "per-turn file of a resident process (AMEESH_TURN_ID_FILE)" }
  - { resource: "../../../src/ameesh/storage/interface.py", title: "storage domains: PendingSpend, TurnCosts, TurnResources, Mailbox, Leases" }
  - { resource: "../../../src/ameesh/serve.py", title: "read API, route table and versioned JSON schemas" }
  - { resource: "../../api/openapi-v1.json", title: "committed OpenAPI 3.1 contract of the read API" }
  - { resource: "../decisions/0018-vitesse-des-agents.md", title: "proportionate review and agent speed" }
  - { resource: "../decisions/0030-pas-de-travail-sans-reveil-possible.md", title: "identity bound to the lease; no work without a possible wake-up" }
---

# Need

An integration that runs harnesses under an ameesh runner needs a supported
answer to two questions:

- **inside the turn**: which request(s) this turn is serving, under which
  lease, and what accounting is open for it;
- **outside the turn**: whether a request was delivered or withdrawn (the
  integration correlates its own reply), and the state of the agent and of the
  turn's accounting.

TPK's IMA document runtime is the first consumer. Its private facade,
`scripts/ameesh-mailbox-bridge.py`, currently serves four operations by reading
`agent_mailbox`, `agent_registry`, `spend_pending` and `account_reservations`
directly:

| Operation | What it reads today | Why it hurts |
|---|---|---|
| `pending` | live lease; an undelivered `request` from a fixed sender with an `ima` payload | one Python spawn and one DB connection per tool call (1–3 s, #819); re-implements the lease fence |
| `reply` | live lease under lock; the request row | same latency; the mailbox protocol is reimplemented |
| `observe` | agent row, `spend_pending`, `turn_in_progress`, `agent_mailbox.delivered_at` | re-derives "quiescent" and "accounting pending" from internal state |
| `lookup` / `submit` | `agent_mailbox` by a payload key, under an advisory lock | dedup protocol of the integration, on internal columns |

Two recent incidents make the cost concrete. A turn stayed `running` for hours
because IMA's observation waited for "no pending accounting" while
`PendingSpend.clear_if` left the marker after settlement (#2166, lot #2893). And
a tool call re-authorised the request on every call, so a stale or other-epoch
turn was not fenced by the protocol itself (#819).

The schema is not a contract: it may change in a minor release, and its
consumers cannot be warned. A supported API must freeze the *facts*, not the
tables.

# What already exists

The facts are already written by the runner; only their contract is missing.

| Fact | Source today | Stable identity |
|---|---|---|
| turn | `turn_resources` (`turn_id`, agent, host, `started_at`, `ended_at`, `status` running/done/orphan) | `turn_id` (UUID hex) |
| lease | `agent_registry` (`lease_owner`, `lease_epoch`, `lease_expires_at`); identity env `AGENT_MAIL_NAME`, `AMEESH_RUNNER_ID`, `AMEESH_LEASE_EPOCH` | `(owner, epoch)` |
| delivered request | `agent_mailbox` (`id`, sender, kind, `payload`, body, `delivered_at`, `meta.consigne` with runner/epoch/token) | message `id` |
| accounting marker | `spend_pending` (agent, `start_index`, `turn`, `model`, `work_item_id`, `turn_id`, `created_at`) | `spend_key` = `agent:start:created_at` (L60) |
| open reservation | `account_reservations` (`billing_account`, `usd`, `period`, `state`, `spend_key`) | `spend_key` |
| ledger | `turn_costs` (`turn_id`, `spend_key`, `billing_account`, `usd`, `model`, `recorded_at`, `source`) | `turn_id` / `spend_key` |

The runner already has everything needed to hand a context to the harness at
launch: the turn id, the lease, the reserved mail ids and their rows, the
marker returned by `pending_spend_begin`, and the account reservation. A
resident process already names its current turn in a file
(`AMEESH_TURN_ID_FILE`), which is the precedent for a per-turn file whose
contents change while the process stays alive.

# The contract to sign

## 1. A turn-context document handed to the harness

Before the harness can act, the runner writes
`<state>/<agent>/turn-context.json` (mode `0600`, in a `0700` directory,
written to a temporary file and atomically renamed; never through a symlink)
and points the turn's environment at it with
**`AMEESH_TURN_CONTEXT=<absolute path>`**. The document is removed (or emptied)
when the turn ends; a resident process keeps the same path across its turns.

The document is the harness's **statement of its own turn**. It contains no
secret: no DSN, no provider key, no token. The harness already holds the mesh
environment and credentials; this document is an authorization *fence* (a stale
or other-turn process must not be treated as current), not a security boundary
against a malicious agent that already holds those credentials.

Schema `ameesh-turn-context/1` (JSON; timestamps are epoch seconds as float,
like the other `*_ts` fields of the CLI):

```json
{
  "schema": "ameesh-turn-context/1",
  "generated_ts": 1792531400.123,
  "turn": {
    "id": "8f3c…",
    "agent": "doc-agent-…",
    "host": "ameesh-agents-2",
    "kind": "mail",
    "started_ts": 1792531399.900,
    "session": "…",
    "work_item_id": "2894"
  },
  "lease": {"owner": "…", "epoch": 42, "expires_ts": 1792531699.900},
  "requests": [
    {
      "id": 123456,
      "sender": "doc-ima-…",
      "kind": "request",
      "created_ts": 1792531300.000,
      "delivered_ts": null,
      "work_item_id": null,
      "redelivered": false,
      "body": "…",
      "payload": {"ima": {"…": "…"}}
    }
  ],
  "agent": {
    "name": "doc-agent-…",
    "harness": "deepseek",
    "model": "deepseek-flash",
    "budget_usd": null,
    "spent_usd": 0.0
  },
  "accounting": {
    "spend_key": "doc-agent-…:17:1792531399.900000",
    "marker": {
      "start_index": 17,
      "created_ts": 1792531399.900,
      "model": "deepseek-flash",
      "work_item_id": null,
      "turn_id": "8f3c…"
    },
    "reservation": {
      "billing_account": "deepseek-manaty",
      "usd": 2.0,
      "period": "2026-10-20",
      "state": "open"
    }
  }
}
```

Invariants:

- **one turn id, allocated first.** The runner allocates `turn.id` before the
  first side effect of the turn (mail reservation, accounting marker, account
  reservation) and uses it unchanged for `turn_resources.turn_id`,
  `spend_pending.turn_id`, `turn_costs.turn_id`, `AMEESH_TURN_ID` (or the
  resident turn file) and this document. Today the id is created after the
  mail reservation; the implementation moves it earlier.
- **`requests` is exactly what this turn showed the harness.** It is the set of
  mail ids reserved for the turn's prompt, with the rows as reserved (the
  "redelivered" flag included). A message delivered later by a harness hook
  during the same turn is not in this list. A request that is not in this list
  is **not authorised by this turn** — this is the rule #819 asks for, and it
  replaces "any undelivered request of the agent". `delivered_ts` is normally
  `null` in the document: the turn is written before the harness is launched,
  and delivery is settled just after the launch. Presence in `requests` means
  "shown in this turn"; `delivered_ts` is mailbox bookkeeping, not the
  authorization.
- **the delivery turn is recorded with the reservation.** The turn's own mail
  reservation (the one the runner makes for the prompt) records `turn.id` in
  `meta.consigne.turn` (JSONB, additive: no migration). Observation reads it as
  `request.delivered_turn_id`; a message delivered by a harness hook during the
  turn, or delivered before this version, has `null` there, and its delivery
  state is still known from `delivered_at`.
- **written before the harness can act.** For a turn process, the document is
  written before `Popen`; for a resident process, the document and the existing
  per-turn file (`AMEESH_TURN_ID_FILE`) are written before the driver is
  started. If the harness cannot be launched, the document is removed before
  the turn is abandoned. At the end of every turn it is removed.
- **`lease` is the live lease that runs the turn.** Losing it kills the
  harness group; `ameesh turn current` re-checks it (below) before treating the
  document as current.
- **`accounting` is the open state at launch.** During the turn it is
  `marker` present, `reservation.state = "open"`. It is a snapshot; the settled
  state is observation (section 3), not this document.
- **`spend_key` is opaque.** Consumers pass it back or compare it; they never
  parse it or re-derive it from a timestamp. This is what removes the
  precision mismatch class of bugs (#2166) from the integration. The exact
  fix remains #2893; this contract does not depend on it.
- **`work_item_id` is the lot reference as text** (nullable), like
  `agent_mailbox.work_item_id`; the observation uses the same text form. The
  internal ledger column is a `bigint`, and that cast stays private.

Why a file and not only environment variables: the request set and the
accounting block are structured and change per turn; a resident process cannot
change its environment between turns. The environment carries only the path and
the existing identity variables. Reading the file costs a local `open()` — no
Python process, no database connection — which is the latency fix for #819.

## 2. `ameesh turn current --json`

Reference reader and CLI, schema `ameesh-turn-context/1`:

```
ameesh turn current [--local] [--json]
```

- Without `--local` (default), the command prints the current turn's document
  only after re-checking, in two indexed reads (or one join), that
  `(lease.owner, lease.epoch)` is still the live lease of `turn.agent` and
  that `spend_pending.turn_id` is `turn.id`. The check is the same fence as the
  mailbox protocol (`identity.resolve_binding`, `registry.lease_matches`).
- `--local` skips the database check and only requires that the caller's turn
  identity matches `turn.id` (resident: the `AMEESH_TURN_ID_FILE` content,
  else `AMEESH_TURN_ID`) and that `lease` matches the environment. It is the
  documented path for a tool call that must not open a connection; the file
  is already `0600` in the agent's state directory.
- It never writes. Exit codes: `0` current document; `3` not in a turn, stale
  document, unreadable document, unknown `schema`, or lease mismatch (the
  error says which, without a DSN or a stack); `2` usage/configuration error.
  On `3`, nothing is printed on stdout that could be mistaken for a context.
- The same document is what a consumer may `open()` directly; the CLI is the
  reference implementation of the checks, not a required subprocess.

`ameesh turn current` is not a general-purpose query: it answers only for the
turn of the calling process. A supervisor uses section 3.

## 3. Turn observation for supervisors

```
ameesh turn observe (--turn <turn-id> | --request <mailbox-id>) [--json]
```

Schema `ameesh-turn-observation/1`, built from the facts, never from a
consumer's re-derivation:

```json
{
  "schema": "ameesh-turn-observation/1",
  "observed_ts": 1792531500.0,
  "by": {"request": 123456},
  "request": {
    "id": 123456,
    "agent": "doc-agent-…",
    "state": "delivered",
    "created_ts": 1792531300.0,
    "delivered_ts": 1792531400.0,
    "delivered_turn_id": "8f3c…"
  },
  "turn": {
    "id": "8f3c…",
    "state": "running",
    "started_ts": 1792531399.9,
    "ended_ts": null,
    "work_item_id": null
  },
  "agent": {
    "name": "doc-agent-…",
    "status": "running",
    "status_since_ts": 1792531399.0,
    "status_text": "…",
    "harness": "deepseek",
    "model": "deepseek-flash",
    "session_id": "…",
    "turns": 17,
    "budget_usd": null,
    "spent_usd": 0.0,
    "turn_in_progress": true,
    "last_turn_ts": 1792531200.0
  },
  "lease": {"owner": "…", "epoch": 42, "live": true, "expires_ts": 1792531699.9},
  "accounting": {
    "state": "pending",
    "spend_key": "doc-agent-…:17:1792531399.900000",
    "marker": {"created_ts": 1792531399.9, "model": "deepseek-flash", "turn_id": "8f3c…"},
    "reservation": {"billing_account": "deepseek-manaty", "usd": 2.0, "period": "2026-10-20", "state": "open"},
    "ledger": null
  },
  "quiescent": false
}
```

- `request.state` is `pending` (not delivered, no withdrawal), `delivered`, or
  `withdrawn` (removed by `ameesh` before delivery). Whether a delivered
  request was *answered* is the integration's own correlation; ameesh does not
  guess it. A correlated reply is read through the integration's own
  mailbox read, never guessed from the observation.
- With `--request`, `request` is the observed message and `turn` is its
  delivery turn when recorded (null otherwise; the agent block is still
  useful). With `--turn`, `request` is null.
- `turn.state` is `starting` (marker present, no resource row yet),
  `running`, `ended` (resource `done`, or a ledger row without a resource),
  `orphan`, or `unknown`.
- `agent.status` is the registry status (`idle`, `queued`, `running`,
  `blocked`, `stopped`, `dead`); `turn_in_progress` is the existing rule
  (`status = running` and a live lease), not a new judgement.
- `accounting.state` is computed from the ledger first, then the marker, then
  the reservation:

  1. a `turn_costs` row for this `turn_id` (or this `spend_key`) exists →
     `settled`; if its reservation is `kept` or `released`, that state wins;
  2. else a `spend_pending` marker with this `turn_id`/`spend_key` exists →
     `pending`;
  3. else no reservation → `none`; a reservation without marker or ledger →
     `unknown` (never silently "done").

  This precedence is deliberate. `spend_pending` alone is not proof of
  pending accounting: a stale marker after settlement must read as `settled`,
  which is exactly what #2166 broke. #2893 still fixes the marker; the
  observation contract remains correct either way.
- `quiescent` is the exact conjunction IMA needs: `agent.status == idle` and
  `accounting.state != pending` and `not turn_in_progress`. It is derived and
  documented, not a new source of truth.
- The observation is **metadata only**: no message body and no `payload`.
  Reading content stays the mailbox API (`ameesh mail`, `ameesh fil`). It
  carries no secret and no DSN.

## 4. Read API routes

The same builders answer `GET /api/v1/turns/{turn}` and
`GET /api/v1/requests/{request}` (schemas `ameesh-turn-observation/1`),
with the existing `/api/v1` rules: Bearer token, read-only Postgres session,
`ameesh-error/1` on 404/401/429/503, and the same rate limit. The OpenAPI
document is regenerated and committed; a test checks the committed document
and each route against its schema. No write route is added: this contract is
observation.

## 5. Warm path and per-agent settings

The supported low-latency configuration is the existing one:

- `ameesh set <agent> mail_batch=0` — an interactive agent is woken at once
  instead of the 90 s batching window (#819 asks to confirm this; it is per
  agent, so passive agents keep batching);
- `ameesh set <agent> resident=on` — one harness process across turns, where
  the harness protocol supports it (today `claude-stream-json`; Codex and
  DeepSeek stay one process per turn until their usage is counted, L80).

The turn context works in both modes. For a resident process the file path is
fixed at start; its content is replaced per turn, and the existing
`AMEESH_TURN_ID_FILE` still names the current turn to the hook. No new
"session reuse" API is proposed: the context is per turn regardless of the
process lifetime.

# Guarantees and versioning

- **Two schema ids**: `ameesh-turn-context/1` and
  `ameesh-turn-observation/1`. They are the contract. The Postgres schema is
  not: migrations may change tables in a minor release, and this API absorbs
  that.
- **Within `/1`**: only additive optional fields; no field is removed,
  renamed, retyped, or given a new meaning. Consumers must ignore unknown
  fields and must require `schema`. Enum values may be added; a consumer
  treats an unknown enum value as `unknown` and never as success.
- **Breaking change**: a new major (`/2`), published in parallel with `/1` for
  at least one minor release. The file/CLI/route stay `/1` until the release
  notes say otherwise.
- **Fail closed**: no file, unreadable file, unknown schema, stale turn id or
  lease mismatch all mean "not current" (exit 3 / 404), never best effort.
  `--local` skips only the database re-check, not the turn-id and file checks.
- **No secret** in either document; no DSN, key, token or password. The
  request bodies are already the agent's mail; observation is metadata.
- **Performance**: a tool call reads the file, target under 2 ms; `ameesh turn
  current --local` target under 150 ms (Python startup); the verified command
  is two indexed reads. These are acceptance targets, measured in the
  implementation lot on the IMA document path (#819: non-model overhead ≤ 3 s
  per turn).
- **Tests**: contract tests for both schemas, a fake-harness end-to-end
  (mail → turn → context → observation), lease loss → not current, stale
  marker + ledger row → `settled`, unknown schema → refused, and the doc
  readers list updated. The read API test is the existing
  `tests/test_l84_serve.py` contract.

# What this note does not cover

- **Dispatch idempotency** (`lookup`/`submit` in IMA's bridge). Request
  submission stays on the existing `mail.send` / `ameesh mail send` path; the
  remaining direct read is a payload-key lookup. A companion lot can add a
  generic `idempotency_key` to `ameesh mail send` (same key + same content →
  same id; different content → refused) so the integration stops reading
  `agent_mailbox` for dedup. It is a write API and deserves its own sensitive
  review; it is not needed for the latency fix. The bridge's reply lookup is
  its own conversation data (a private payload key), not turn state; a
  `mail show`/`find` primitive can move it out of direct SQL in the same
  companion lot.
- **Reply correlation** (`answered` vs `awaiting_reply`). That is the
  integration's own payload namespace; ameesh exposes delivery, not meaning.
  `ameesh mail show <id> --json` may be added with the dispatch lot if the
  integration needs a supported read of a known reply id.
- **Human authentication and the HTTP surface beyond `/api/v1`** — unchanged
  (`ameesh serve`, L84/L86).
- **A turn context for a mediated executor** (no local database): the same
  file is written by the runner, but its supported read path needs the
  mediated backend's own contract; out of scope here.

# Implementation lots to open after sign-off

| Lot | Content | Notes |
|---|---|---|
| A | Turn id allocated first; `turn-context.json` + `AMEESH_TURN_CONTEXT`; `ameesh turn current`; `meta.consigne.turn`; contract tests | runner/accounting: sensitive, another family's review and green CI (§2). ~2 days |
| B | `ameesh turn observe`; `/api/v1/turns` and `/api/v1/requests`; OpenAPI; docs and contract tests | ~1.5 days |
| C | IMA bridge migration (TPK): read the context file, use `turn observe`; drop the direct reads it can drop | outside ameesh; acceptance on the document path (#819) |
| D (optional) | generic `mail send --idempotency-key`; `mail show` | companion, separate sensitive review |

# Decision requested

The owner is asked to sign the shape above, not the implementation:

- **a (recommended)**: the file + `ameesh turn current` + observation CLI/read
  API, with the two versioned schemas and the guarantees above; lots A and B,
  then the IMA migration C.
- **b**: CLI/read API only (no document handed to the harness). Simpler, but
  every tool call is back to Python + a database connection; it does not meet
  #819's non-model overhead.
- **c**: document only (no read API). Fastest inside the turn, but a
  supervisor outside the turn still needs database access; it does not answer
  the observation half of #2167.
- **d**: amend — the owner states the change; the note and lots are revised
  before any implementation.
