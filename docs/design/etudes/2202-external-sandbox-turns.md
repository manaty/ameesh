---
type: Study
title: "TPK #2202 — bounded external-sandbox reviewer turns: Job correlation, exactly-once result, sandbox limits and tranche admission"
description: "Design note: a native sandbox execution mode of the runner; a restartable sandbox supervisor owns one Kubernetes Job; a durable turn↔Job correlation; an exactly-once application-bound result; admitted profile/placement with enforced limits; a shared tranche admission on top of the account envelopes."
status: draft
tags: [sandbox, kubernetes, tour, bail, exactly-once, budget, tranche, relecteur, tpk, limites]
generated: { by: "ameesh-dev3", at: "2026-10-20T21:40:00Z" }
sources:
  - { resource: "manaty/ameesh#2202", title: "TPK: native support for bounded external-sandbox reviewer turns" }
  - { resource: "manaty/ameesh#2167", title: "Supported turn-context and turn-observation API (lot #2894, note first)" }
  - { resource: "manaty/ameesh#2110", title: "Container-aware admission and elastic capacity (shipped in v1.16.0)" }
  - { resource: "manaty/ameesh#2166 and lot #2893", title: "PendingSpend.clear_if precision; exact marker clear by turn_id" }
  - { resource: "thepackengers/issue-manager-agent#837 and #843", title: "IMA P4/M3 bounded reviewer preparation, focused proposal and qualification" }
  - { resource: "docs/V1-MAILBOX-RUNNER.md §3", title: "Leases, epochs and the turn fence" }
  - { resource: "src/ameesh/account_budget.py; src/ameesh/storage/postgres/budgets.py", title: "Daily account envelope, reserve/settle, kept uncertainty" }
  - { resource: "src/ameesh/relay.py", title: "Model relay, session token, model policy, relay cost rows" }
  - { resource: "src/ameesh/canon.py", title: "Agent, HostPolicy, Placement cards and placement predicate" }
  - { resource: "src/ameesh/mediated_executor/contract.py", title: "Versioned closed contract, idempotency and refusals" }
---

# Recommendation

**Run the bounded reviewer as a native sandbox turn of the existing runner.** The
runner keeps the lease, the epoch, the turn, the accounting marker and the spend
key. It launches a **sandbox supervisor** in the turn's process group; the
supervisor owns one Kubernetes Job and survives its own restart without the
lease changing. The runner remains the only writer of the turn's life.

Five pieces make that safe:

1. **One durable correlation record per turn.** `sandbox_turns` is written
   before the Job exists, carries the deterministic Job name and the admitted
   profile/placement/scope snapshot, and receives the Job namespace/name/UID
   exactly once. Adoption is by that record and by Job labels; a second Job for
   the same turn is refused, never "replaced".
2. **One exactly-once result function.** `ameesh_sandbox_complete()` locks the
   turn and the lease, verifies owner/epoch/deadline/scope/limits, inserts one
   result, closes the turn once, and settles the account and the tranche in the
   same transaction. `end_turn()` is **not** used for a sandbox turn: it is a
   blind fenced update, it is not unique, and it carries no application scope.
3. **Admitted profile and placement.** The profile is the `Agent` card
   (harness, model, tools) plus its `sandbox:` block (limits, tranche,
   capabilities); the placement is the `Host.policy.sandbox` block plus the
   existing `Placement` card. The bureau (the canon-reviewing role in this
   mesh) reviews the canon PR; the runner checks the pinned canon revision and
   its freshness, and refuses a stale, missing or revoked admission.
4. **Enforced limits.** Deadline and relay-token expiry are server-side; turn,
   tool and concurrency counters are counted by the server; the Job template
   (image digest, `activeDeadlineSeconds`, no service-account token, no provider
   key, read-only repository access, tool broker) is part of the admitted
   placement and is qualified before the pilot. The relay is the only model
   path for an admitted sandbox, so a turn cannot spend past its token.
5. **A shared tranche admission.** One SQL admission boundary reserves the
   turn's bound on the tranche **and** on the provider's calendar-day envelope,
   under a fixed lock order, for every ameesh path that runs in the migration
   scope. An unknown or unsettled cost stays `kept`; it is never counted as
   zero. The USD 50 is a parameter of the tranche record, not a compiled
   constant.

The consumer contract is read-only: IMA sends a structured review request and
reads a receipt. There is **no external lifecycle write API** in this design;
IMA does not claim leases, create Jobs or call `end_turn`. That is the point of
"ameesh stays the only lease/persona/turn/accounting authority". A v2 external
supervisor API remains possible, but it is not needed for this pilot and is
listed under "Alternatives considered".

# Context

ThePackengers is moving IMA's execution roles onto leased ameesh personas
(IMA #704, P4/M3). The first pilot is a **bounded reviewer**: one PR/head review,
no merge, fix, deploy or publish, running in a Kubernetes Job sandbox. IMA has
already qualified preparation against the pinned ameesh source
`44a42555406f30939285316119c7c66e60d39c13` and real PostgreSQL (IMA #837,
PR #843). ameesh is the lease/persona/turn/accounting authority; IMA keeps only
application facts: which PR/head was reviewed, and the verdict.

The missing native pieces are exactly four:

1. a leased turn ↔ external Job correlation with cancellation and
   observe/adopt recovery across a supervisor restart, before and after the Job
   is created;
2. an exactly-once, application-bound result (repo, PR, head), refusing a
   missing, stale, late, duplicate or revoked result;
3. an admitted reviewer profile and placement with native enforcement of turn,
   tool and time limits;
4. one admission boundary for a shared tranche (USD 50 in the pilot) across
   every enabled path and retry, on top of the existing USD 100 per provider
   and calendar day (Europe/Paris) account envelopes, with unknown cost counted
   as spent.

This note builds on #2167 (turn-context and observation API, lot #2894, note
PR #2221, owner decision #120568 pending). The turn-context note comes first;
the fields this note reuses are the turn id, owner, epoch, session, request
ids, accounting marker/spend key and the open reservation. If #2167 changes
those names, this note follows it rather than defining a second vocabulary.

# What already exists

* **Lease and epoch.** `agent_registry.lease_owner`, `lease_epoch`,
  `lease_expires_at`; `claim` increments the epoch, `renew`/`begin_turn`/
  `end_turn` are fenced by owner + epoch and re-check the real database clock.
  `end_turn()` is one `UPDATE … WHERE lease_owner = $ AND lease_epoch = $`
  (`storage/postgres/registry.py`); it does not check the turn state, does not
  insert anything unique and does not settle money.
* **Turn identity and accounting.** `turn_resources.turn_id` (and `pgid`,
  `label`, `containers`, `cgroup`), `spend_pending` (one marker per agent, with
  `start_index`, `turn`, `work_item_id`, `turn_id`), and `turn_costs` (with
  `spend_key`, `turn_id`, `work_item_id`, `started_at`, `source`, `executor`,
  `lease_owner`, `lease_epoch`, `billing_account`, `void_reason`).
  `runner._spend_key()` is currently `agent:start_index:marker_created_ts`;
  the contract treats it as **opaque** (a consumer never parses or re-derives
  it). The marker is written before the harness and cleared only after the
  ledger row is written.
* **Daily account envelope.** `account_budgets` and `account_reservations`
  (`0443`): admission reserves `profile.reserve_usd` under an advisory lock on
  the billing account; usage is settled + open + kept; an uncertain outcome is
  `kept` in full and only a human releases it (`account_budget.reserve/settle`,
  `storage/postgres/budgets.py`).
* **Marker-clear precision.** `spend_pending` is currently cleared by
`created_ts` (a float); #2166 reports the precision defect and lot #2893
carries the exact fix. This design **depends** on an exact clear by
`turn_id` (or #2893's exact key) and never uses `created_ts` alone.
* **Model relay.** `relay.py` verifies a short session token, checks the model
  policy and the budget, adds the server-side provider key, forwards the
  stream, and writes `turn_costs` rows with `source = 'relay'`, the agent,
  turn/session and lease. It is the hard, per-request meter. The device never
  holds the provider key. The mediated executor's contract already demonstrates
  the idempotency/refusal discipline (`Idempotency-Key`,
  `Idempotency-Replayed`, closed operation table, versioned schema).
* **Canon.** `Agent`, `Host`, `Placement` cards; `Host.policy` carries
  `harnesses`, `providers`, `models`, `credential_modes`, `max_agents`,
  `resources`, `work_roots`, `lease_ttl`, `volatile`, `lend`. `Placement` maps
  an agent to admitted hosts/tags; `canon_claim_predicate_sql` is the placement
  predicate. `canon_state` records the last good commit and its `checked_at`;
  the runner syncs every 300 s by default.
* **Container pressure and capacity.** `#2110` (lot #2814) ships
  `pressure_scope: own_cgroup`, the pluggable capacity provider and the visible
  `queued` state. That is admission *on a host under pressure*; it is not a
  sandbox time/tool envelope. This note uses the same observability style but a
  different object.
* **Turn resources/orphans.** `turn_resources` opens/closes a turn's resource
  attribution and marks survivors `orphan`; it never deletes anything.
* **Read API.** `ameesh serve` exposes `/api/v1` (read-only, Bearer token,
  `Context.sees` filtering, OpenAPI generated). There is no Kubernetes client
  in the package and no sandbox table.

The IMA qualification reproduced the central gap: two successful `end_turn`
calls under one owner/epoch increment `turns` twice, and a generic turn end
carries no PR/head identity. Native leases already fence a stale owner and an
expired lease, and `account_budget` settles once, but neither gives a
per-result receipt or a Job correlation.

# Design

## 1. Execution model

```
runner (lease owner, epoch, turn, spend marker, budget/tranche reservation)
  └─ sandbox supervisor (child in the turn's process group, host kubeconfig)
       └─ Kubernetes Job (pinned image digest, no kube token, no provider key)
            └─ harness (codex/deepseek) ── model calls ──▶ ameesh relay
```

* The agent's `sandbox:` block selects the sandbox execution mode. The inner
  `harness` (codex or deepseek) keeps its descriptor: it supplies the model
  flags and the price family. The Agent's `tools` list and its `sandbox` block supply the
  tools, limits, tranche and capabilities; the Host's `policy.sandbox` supplies the provider,
  cluster, image digest and Job defaults.
* The runner claims the lease and starts the turn exactly as today. It writes
  the accounting marker and the `sandbox_turns` intent row **before** any Job
  exists, then launches the supervisor instead of the harness CLI. The
  supervisor is a normal child of the turn: its process group is the turn's
  process group, so a lease loss, cancel or turn end can signal it.
* The supervisor is the only component with the cluster credentials. It reads
  the turn prompt from a private `0600` file, creates the Job with a
  deterministic name and labels, then watches it. It never receives the
  provider key and never opens the mesh database: the runner passes it a private
  turn file and consumes its JSONL events. The runner remains the only database
  writer.
* The Job runs the harness inside the admitted image. It is single-use:
  `backoffLimit: 0`, `restartPolicy: Never`, `activeDeadlineSeconds` >
  `turn_seconds`, `ttlSecondsAfterFinished` long enough for adoption,
  `automountServiceAccountToken: false`, read-only root filesystem, a tool
  broker, and a network policy that allows only the relay and the admitted
  repository endpoints. Repository credentials are read-only and scoped to the
  review. The prompt and the relay token are delivered as a Secret that is
  deleted with the Job; they are never in the Job's command line.
* The Job writes one result envelope to stdout (bounded, proposed 64 KiB) and a
  short digest/status to the pod termination message (the Kubernetes 4 KiB
  bound). The supervisor reads the log, checks the two agree, validates the
  envelope and writes it to a private result file. The runner validates it again
  and calls the exactly-once result function.
* The relay is the only model path for an admitted sandbox. The Job gets a
  short-lived relay token bound to the turn/epoch; its TTL is the turn deadline.
  The relay is therefore both the meter and the hard stop: after the deadline
  there is no model call, even if the pod is still alive.

**Turn-context handoff (#2167).** The runner's `ameesh-turn-context/1`
document (the `AMEESH_TURN_CONTEXT` file) is the single source of the turn
id, the live lease, the requests shown to this turn (the turn's reservation)
and the open accounting marker/spend key. The sandbox adapter reads it for
those facts; the review request is a block in one of the turn's requests'
payloads (`ameesh-sandbox-request/1`), not a fresh mailbox read. **One
authorization per turn**: a sandbox request is authorized only if its message
id is in the context's `requests[]`; a request absent from that set (not
reserved for this turn, including a message withdrawn before the turn's
reservation) is `unauthorized-request`. Presence in the list is the
authorization: do **not** filter `requests[]` on `delivered_ts`/`delivered_at`
— the document is written before launch, so `delivered_ts` is normally null
and delivery is settled just after launch. A request in the set whose payload
has no `ameesh-sandbox-request/1` is `not-a-sandbox-request` and is skipped.
After admission a cancel does **not** change the request's `request.state`: it
does not become `withdrawn`, and presence in `requests[]` remains what
authorizes the turn — the row's delivery bookkeeping (`pending`/`delivered`) is
not the authorization and may still be `pending` in the admission window. A
cancel sets `sandbox.state = cancelled` and `cancel_requested_at`.
The `turn_id` is the one allocated before the first side effect of the turn
(#2167), so `turn_resources`, `spend_pending`, `turn_costs` and
`sandbox_turns` carry the same id. The `spend_key` is opaque: the adapter
reads it from the context/marker and never re-derives it. The context's
`turn.session` is the ameesh harness session; it is not the reviewer's
session. A sandbox turn never writes `agent_registry.session_id` from the
sandbox: the reviewer's session (if any) is
`sandbox_turns.sandbox_session`, and it is null for `session: none`.
`turn.kind` is not overwritten: it stays `mail`/`prompt`/etc., and the sandbox
fact lives in the block (no new `turn.kind` value).

The sandbox turn is launched by the ameesh runner (adapter + supervisor
child), so it is inside #2167's covered boundary: the runner launches the
turn, writes the context and holds the lease; the supervisor never holds the
lease and never writes the context. The mediated-executor path is out of
scope for both notes.

If #2167 chooses a CLI-only or document-only shape, the runner passes the
same facts to the adapter from its turn state; the sandbox contract depends
on the facts, not on the file. If #2167's note changes a field name, this note
follows it.

There is no new external write API. IMA's controller sends the structured
`ameesh-sandbox-request/1` payload (in an existing mail/work request) and reads
the native receipt from `/api/v1`. It does not claim leases, create Jobs, call
`end_turn` or write a result.

## 2. The correlation record

New additive tables (proposed `0471_sandbox_turns.sql`,
`0472_tranche_admission.sql`; the implementation lot reserves the exact
numbers):

```sql
create table sandbox_turns (
    turn_id          text primary key,             -- turn_resources.turn_id
    agent            text not null,
    owner            text not null,
    lease_epoch      bigint not null,              -- agent_registry.lease_epoch is bigint
    sandbox_session  text,                         -- reviewer session, never the ameesh harness session
    request_ids      bigint[] not null default '{}',
    work_item_id     bigint,
    scope            jsonb not null,               -- repo, pr, head, base, generation, attempt
    scope_digest     text not null,                -- sha256(JCS(scope)), exact per attempt
    retry_family     text not null,                -- sha256(JCS(scope without attempt))
    scope_generation integer not null,
    attempt          integer not null,             -- server-assigned: family count + 1
    max_attempts     integer not null,
    unique (retry_family, attempt),
    profile_ref      text not null,
    profile_digest   text not null,
    placement_ref    text not null,
    placement_digest text not null,
    canon_ref        text not null,                -- canon revision of the admission
    tranche_id       text,
    limits           jsonb not null,               -- immutable snapshot
    image_digest     text not null,
    provider         text not null,                -- kubernetes
    namespace        text not null,
    job_name         text not null unique,         -- deterministic
    job_uid          text unique,                  -- bound once
    job_phase        text,
    state            text not null default 'intent'
        check (state in ('intent','created','bound','running','succeeded',
                         'failed','cancelled','expired','orphaned','lost')),
    result_id        text unique,
    result_sha256    text,
    refusal          text,
    cancel_requested_at timestamptz,
    cancel_observed_at  timestamptz,
    deadline_ts      timestamptz not null,
    started_at       timestamptz not null default now(),
    completed_at     timestamptz,
    source           jsonb not null                -- release/commit/contract
);
create index sandbox_turns_open on sandbox_turns (agent, state, deadline_ts);
create index sandbox_turns_job on sandbox_turns (namespace, job_name);

create table sandbox_results (
    turn_id       text primary key references sandbox_turns(turn_id),
    epoch         bigint not null,
    result_id     text not null unique,            -- client idempotency id
    result_sha256 text not null,
    payload       jsonb not null,                  -- bounded, sanitized
    receipt       jsonb not null,                  -- ameesh-sandbox-receipt/1
    accepted_at   timestamptz not null default now()
);
```

`request_ids` is the subset of the requests shown to this turn (`requests[]`)
whose payload carries `ameesh-sandbox-request/1`; it is not the full list.
The public sandbox block exposes `request_ids`, never a second `requests[]`. The public
`sandbox.work_item_id` is the context's text form (a lot reference, like
`agent_mailbox.work_item_id`); the internal `bigint` column is an
implementation detail. The public block exposes `started_ts` (epoch float,
like #2167), `sandbox.session` (the reviewer session or null, distinct from
#2167's `turn.session`) and `work_item_id` (text); `sandbox_turns.started_at`
and `work_item_id` are internal.

Invariants:

1. `sandbox_turns` is 1:1 with a turn; `job_name` and `job_uid` are unique.
2. The row is written before the Job exists. If the row cannot be written, no
   Job is created.
3. `scope`, `limits`, profile/placement/canon refs, `image_digest` and
   `deadline_ts` are snapshots of the admission; a later canon revision does not
   change them. A revocation is a cancel request, not a silent rewrite.
4. `job_uid` is written once by a compare-and-set; a different UID is a
   `job-conflict` refusal, never an overwrite.
5. `result_id` is the requester's idempotency id; `result_sha256` is the
   canonical digest of the result envelope; a second result for the same turn is
   either a replay (same digest) or a `duplicate` refusal (different digest).
6. `turn_resources` remains the generic resource ledger. `sandbox_turns` adds
   the external-unit correlation and the result; both are joined by `turn_id`.
7. `retry_family` excludes `attempt` and is immutable; attempts of one family
   are numbered `1..max_attempts` by the admission transaction under the family
   lock. `scope_digest` still hashes the exact scope including `attempt` and is
   what the result echoes.

The `scope` object is application-bound and generic:

```json
{
  "schema": "ameesh-sandbox-scope/1",
  "application": "github:thepackengers/issue-manager-agent",
  "repository": "thepackengers/issue-manager-agent",
  "pr": 837,
  "head": "0123456789abcdef0123456789abcdef01234567",
  "base": "develop",
  "generation": 3,
  "attempt": 1
}
```

The digest is `sha256(JCS(scope))` with the same canonicalisation as the rest of
ameesh (`jcs`). The result must echo the exact scope and digest. A new head or
generation is a **new turn**; it is never smuggled into an existing grant.

`retry_family = sha256(JCS(scope without attempt))` is the immutable retry-family
key: it covers application, repository, pr, head, base and generation, never
`attempt`. `attempt` is assigned by the admission transaction (family count + 1,
§3), not supplied by the caller; the stored `scope`/`scope_digest` include it, but
two retries of the same review share one `retry_family`.

## 3. Correlation and adoption protocol

The durable state is `sandbox_turns`, written only by the runner; the
supervisor's local state is only a cache and it re-adopts from the Job. The Job
name is derived from the turn id
(`ameesh-<first 12 hex of sha256(turn_id)>`), the labels are
`ameesh.dev/turn-id`, `ameesh.dev/agent`, `ameesh.dev/epoch`,
`ameesh.dev/placement` and `ameesh.dev/profile`. Names are unique in a
namespace, so a retried create cannot produce two Jobs; a conflict on the name
is resolved by reading the existing Job.

Normal sequence:

1. **Admit** (runner, one admission call or fail closed): write the accounting
   marker; admit the account/tranche reservation; write `sandbox_turns` with
   `state = 'intent'`, the deterministic `job_name`, the scope and the limits.
   The marker, the row and the two reservations are composed into one call; a
   failure at any step leaves no Job. The existing separate marker/account calls
   stay for non-sandbox turns.
2. **Create** (supervisor): `GET` the Job by name. If absent, `POST` it. If
   `POST` returns `409 AlreadyExists`, `GET` it. The supervisor reports the
   returned UID; the runner records it with a compare-and-set (`state =
   'created'`).
3. **Bind** (runner): `UPDATE sandbox_turns SET job_uid = $uid, state =
   'bound' WHERE turn_id = $ AND (job_uid IS NULL OR job_uid = $uid) RETURNING …`.
   If the row already has another UID, refuse `job-conflict`. The bind is
   idempotent; the supervisor never writes it.
4. **Run**: the supervisor reports phase and heartbeat events; the runner
   updates `job_phase`, counts the elapsed time and keeps the lease alive (the
   supervisor itself does not hold the lease).
5. **Result**: on `Complete`, the supervisor reads the bounded result, validates
   it, writes the private result file and exits with the result. The runner
   calls `ameesh_sandbox_complete()`.
6. **Done**: `state = 'succeeded'` (or `failed`/`cancelled`/`expired`), the turn
   is closed once, the accounting and tranche reservations are settled, and the
   `turn_resources` row is marked `done`.

Crash windows, all qualified on real PostgreSQL with a fake Job provider and,
for the real Job contract, a `kind` cluster:

| Window | Durable fact | Recovery |
|---|---|---|
| before the marker | nothing | no turn; nothing to do |
| marker written, no `sandbox_turns` row | marker | existing interrupt repair: turn not counted, prompt restored, reservation kept; no Job |
| `sandbox_turns` row, no Job | `intent`, deterministic name | supervisor restarts, `GET` by name, creates once |
| Job created, response lost | Job exists, row still `intent`/`created` | `GET` by name; bind the observed UID |
| Job bound, supervisor dies | `job_uid` | restart the supervisor with the same turn/epoch; adopt by UID, never create a second Job |
| Job completed, result file not written | Job's termination message/log | adopted supervisor reads the bounded result and exits |
| result validated, submit response lost | `sandbox_results` row | replay: same digest → stored receipt with `replayed` even though the turn is `succeeded`; different digest → `duplicate`; no second turn increment, no second settlement |
| cancel before create | `cancel_requested_at` | no Job; `state = 'cancelled'` after the row is fenced |
| cancel after create | `cancel_requested_at` + `job_uid` | supervisor deletes the Job, observes termination, writes `cancel_observed_at`; result refused `revoked` |
| cancel vs result | row lock | one wins; if the result wins, cancel returns `already-completed`; if cancel wins, the result is `revoked` |
| runner dies | turn/epoch lost; row remains | existing turn-loss recovery; a startup sweep lists Jobs by label for dead turns, deletes and observes them, marks `orphaned`; no result can be accepted (stale epoch) and no new sandbox turn for that agent until the row is terminal |
| database restarts | rows durable; lease TTL | the runner's renewals fail; it signals the supervisor, which deletes the Job; the turn is `expired`; a result is `late`/`stale` |

A **supervisor restart does not change the lease**: the runner still holds it.
The relaunched supervisor is passed the same `turn_id`, owner and epoch, reads
the row and adopts. A **runner restart** does change the epoch (the runner
re-claims), so the in-flight sandbox turn is lost exactly like a local harness
turn; the difference is that its Job can survive, so the startup sweep must
find and delete it. The design never adopts a Job under a new epoch: the old
result would be fenced by the epoch anyway, and a new turn must be a new
admission with a new reservation.

`max_attempts` is counted per `retry_family`, the immutable scope without
`attempt`. Admission takes a transaction-scoped advisory lock on `retry_family`,
counts every `sandbox_turns` row of the family (terminal rows included), refuses
`limit-exceeded` when the count is at or above `max_attempts`, and assigns
`attempt = count + 1` before it writes the row and the Job input. The lock is held
to commit, with the fixed order `retry_family` → tranche row → account (§8), so
two concurrent retries cannot both pass on the same count. The per-attempt
`scope_digest` still hashes the exact scope (attempt included) and is what the
result must echo. A retry is a new turn, a new spend key and a new reservation;
the previous turn's reservation is settled or kept. A new head or generation is a
new `retry_family` with a fresh attempt budget; it is never a way to smuggle
another attempt into the old one. The refusal happens **before** a Job can be
created.

## 4. Exactly-once application-bound result

`ameesh_sandbox_complete(turn_id, owner, epoch, result)` is the only completion
path for a sandbox turn. In one transaction:

1. `SELECT … FROM sandbox_turns WHERE turn_id = $ FOR UPDATE`. Refuse
   `unknown-turn` if absent.
2. Check `owner`/`epoch` against the row and the live lease
   (`agent_registry` `FOR UPDATE`, `lease_expires_at > clock_timestamp()`):
   a mismatch, a replaced owner or an expired lease is `stale`.
3. Check revocation: `cancelled`/`cancelling` or a revocation is `revoked`.
4. Canonicalise the result (JCS) and compute its digest; bound its size. An
   envelope that cannot be canonicalised cannot be a replay and is `missing`.
5. If a `sandbox_results` row already exists, compare digests **before** any
   terminal/deadline refusal: same digest → return the stored receipt with
   `replayed = true` (no state change, no second close, no second settlement),
   even when the turn is `succeeded` or the deadline has passed; different
   digest → `duplicate` (rollback). This ordering is the response-loss recovery
   path.
6. Only without a stored result: `deadline_ts <= clock_timestamp()` or an
   already terminal state is `late`; a different `scope_digest` (or
   generation/head) is `scope-mismatch`; validate the schema, the echoed scope,
   the declared tools and the counters; a missing required field is `missing`;
   too many tool calls/turns is `limit-exceeded`; a result that claims an
   unadmitted tool or capability is `profile-refused`.
7. Insert `sandbox_results` and set `sandbox_turns.state = 'succeeded'`,
   `result_id`, `result_sha256`, `completed_at`.
8. Close the turn **once**, by a shared internal `ameesh_turn_close()` factored
   out of `end_turn()`: `turns = turns + 1`, status `idle`/`queued`, clear the
   prompt, set `last_turn_at`/`last_seen`. Do **not** write
   `agent_registry.session_id` from the sandbox: the sandbox session (if any)
   stays in `sandbox_turns.sandbox_session` and the result; `session: none`
   leaves it null and the agent's own session unchanged.
9. Settle the account reservation by `spend_key` (actual cost from the relay
   rows tagged with the turn, or `kept` when the cost is unknown) and the
   tranche reservation in the same fixed lock order used by admission.
10. Clear the `spend_pending` marker **by `turn_id`** (the exact storage
    method), never by a re-read `created_at` float: the float `created_ts` path
    is #2166, and #2893 carries the exact fix. This is a declared dependency;
    the sandbox close does not use `created_ts` alone. Then mark the
    `turn_resources` row `done`.
11. Emit `sandbox.result` and the journal/event; return
    `ameesh-sandbox-receipt/1`.

The unique `sandbox_results.turn_id` is the last-resort backstop: even if two
calls bypass the row lock (a future code path), the database accepts one result.
`end_turn()` stays for local turns and is not exposed to a sandbox. The runner
refuses to call it for a sandbox turn (`wrong-completion`); the result function
is the only writer of `turns` for this turn, and a sandbox turn's generic close
path is not armed.

The result envelope (`ameesh-sandbox-result/1`) is bounded (proposed 64 KiB)
and sanitised:

```json
{
  "schema": "ameesh-sandbox-result/1",
  "turn_id": "…",
  "epoch": 7,
  "scope": { "…": "exact echo of the admitted scope" },
  "scope_digest": "sha256:…",
  "result_id": "uuid",
  "outcome": "accepted",
  "verdict": "green",
  "findings": [ { "severity": "N", "code": "…", "summary": "…" } ],
  "evidence": [ { "kind": "check", "head": "…", "name": "unit", "conclusion": "success" } ],
  "usage": { "turns": 1, "tool_calls": 37, "seconds": 412, "model_calls": 18 },
  "cost": { "usd": 0.84 },
  "sandbox_session": null,
  "source": { "release": "1.16.0", "commit": "…", "contract": "ameesh-sandbox/1" },
  "finished_at": "2026-10-20T21:40:00Z"
}
```

No credentials, raw transcripts, repository contents or private memory are
stored. `findings` and `evidence` are bounded references; the trusted IMA
candidate helper remains the judge of exact-head checks and of the verdict's
truth. Ameesh's receipt proves **which** scope/Job/turn produced which result,
not that the model's finding is correct.

## 5. Cancellation

Cancellation is a **two-phase** operation, because a lease expiry never proves a
Job has stopped.

* **Fence.** A cancel request sets `cancel_requested_at` in the authority
  (`ameesh sandbox cancel`, a work-item state, a lease loss, or a canon
  revocation). From that instant, `ameesh_sandbox_complete()` refuses every
  result with `revoked`. The relay rechecks the turn state on each request (and
  the token expires at the deadline), so a cancelled turn gets no new model
  call.
* **Stop and observe.** The supervisor receives the cancel (the runner signals
  the process group and writes the cancel marker in the private turn file),
  deletes the Job (`foreground`/`background` per the provider), waits until the
  Job/pod is observed gone or terminated, and reports `cancel_observed_at` and
  the final phase; the runner records them.
* **Recover.** Only after observation does the runner close the turn
  (`cancelled`) and settle the reservation as `kept` (actual cost if known). If
  observation fails, the turn stays `orphaned`/`cancelling`, the lease is **not**
  released while the Job may still be live, an alert `sandbox_orphan` fires, and
  a human/operator must finish the deletion. The result remains revoked.
* **Crash.** If the supervisor dies during cancellation, the runner's recovery
  or the startup sweep lists Jobs by the turn labels, deletes and observes them.
  The sweep never launches a replacement Job for a cancelling/expired turn.

**Lease loss is not a graceful cancel.** The old owner is fenced at once (a
replacement may claim), the relay token stops working, and the runner may
SIGKILL the process group before the supervisor can delete the Job. The
reliable deletion path is then the **orphan sweep**: it lists Jobs by the lost
turn's labels, deletes and observes them, and records the observation. Until
it does, `sandbox_orphan` fires and the runner refuses a new sandbox turn for
that agent while a non-terminal `sandbox_turns` row exists. A result from the
old Job is `stale`/`revoked`.

The `sandbox_turns` row keeps `cancel_requested_at`, `cancel_observed_at`,
`job_phase`, `job_uid` and the final state, so the audit can always tell
"requested" from "observed". This distinction is exactly what IMA's proposal
requires and what a lease-only design cannot prove.

## 6. Admitted reviewer profile and placement

The profile and placement are **canon cards**, not a second authority. This
reuses the existing `Agent`/`Host`/`Placement` model, the placement predicate
and `canon_state` freshness. The bureau reviews the canon PR; the owner (or the
canon maintainer) merges it. The card carries the admission reference.

`Agent` card (fragment, proposed):

```yaml
type: Agent
title: tpk-reviewer
harness: codex                    # inner harness; descriptor supplies model flags
provider: openai
model: gpt-6.1-sol
credential_mode: api-key          # the account; the Job never sees the key
tools: [repository.read, github.checks.read, review.report]
sandbox:
  profile: tpk-reviewer-2026-10
  admitted_ref: "thepackengers/home#239"   # bureau-reviewed canon/decision PR
  status: admitted                          # admitted | revoked
  tranche: tpk-migration-2026-10
  credential: relay                         # no provider key in the Job
  session: none                             # fresh context per attempt
  capabilities: {merge: false, fix: false, deploy: false, publish: false}
  limits:
    turns: 1
    max_attempts: 2
    tool_calls: 200
    turn_seconds: 900
    job_seconds: 1200
    context_tokens: 131072
    usd_per_turn: 2
```

`Host` card (fragment, proposed):

```yaml
type: Host
title: tpk-stg-reviewers
responsible: human:smichea
policy:
  harnesses: [codex, deepseek]
  providers: [openai, deepseek]
  credential_modes: [api-key]
  max_agents: 3
  lease_ttl: 300
  sandbox:
    provider: kubernetes
    kubeconfig: /etc/ameesh/kubeconfig.d/tpk-stg   # 0600, runner user
    namespace: reviewers
    image: ghcr.io/thepackengers/ima-reviewer@sha256:…
    allowed_profiles: [tpk-reviewer-2026-10]
    max_concurrent_jobs: 3
    admitted_ref: "thepackengers/home#239"
    job:
      activeDeadlineSeconds: 1200
      backoffLimit: 0
      ttlSecondsAfterFinished: 3600
      automountServiceAccountToken: false
```

The existing `Placement` card maps `tpk-reviewer` to `tpk-stg-reviewers`. The
`canon check` additions validate the blocks: range checks on the limits,
`job_seconds > turn_seconds`, a digest-shaped image, a private kubeconfig path,
`allowed_profiles` containing the profile, `max_concurrent_jobs` ≤
`max_agents`, `session: none` for a bounded reviewer, `credential: relay`, and
**all four capabilities false**. A profile with `merge`, `fix`, `deploy` or
`publish` true is refused for a bounded reviewer. At admission the runner also
checks `usd_per_turn` ≤ the account profile's `reserve_usd` (so the account and
the tranche reserve the same bound) and enforces `session: none` by ignoring
any stored session for a sandbox turn; the declaration alone is not a
guarantee.

Admission is checked at turn start against the **pinned canon revision**:
`canon_state.status = 'ok'`, `last_good_commit` equal to the profile/placement
revision and `checked_at` fresh (the runner's 300 s sync window). The snapshot
goes into `sandbox_turns`. At its 300 s canon sync the runner re-reads the
revision; if the profile/placement became `revoked` (or the canon is now
invalid over the grant), it requests cancellation. A stale canon refuses **new**
turns (`profile-refused` / `canon-stale`); it does not silently change an
in-flight turn's limits.

The bureau's admission evidence is the canon PR/decision and the `admitted_ref`
recorded on the card; the runner does not invent a second signature. The
qualification values above come from IMA #837 and are examples, not compiled
constants.

## 7. Enforced limits

The enforcement split is explicit, because a process outside the runner cannot
be stopped by an `UPDATE`.

| Limit | Declared in | Enforced by | Evidence |
|---|---|---|---|
| absolute time | `turn_seconds`, `deadline_ts` (DB clock at admission) | supervisor deletes the Job at the deadline; Job `activeDeadlineSeconds = job_seconds`; relay token expires at the deadline; a later result is `late` | real Postgres clock test + `kind` Job deadline test |
| tool calls | `tool_calls` | the sandbox tool broker allowlist; the Job reports monotonic counters; the server refuses a result over the limit (`limit-exceeded`) and a result without counters (`missing`) | result/heartbeat tests + broker test |
| turns / attempts | `turns`, `max_attempts` | the server counts `sandbox_turns` per `retry_family` (scope without `attempt`) under the family lock before admission, then assigns `attempt`; over the limit is `limit-exceeded` | concurrent distinct-digest attempt tests |
| budget | `usd_per_turn` | admission reserves the bound; the relay checks the remaining turn allowance and the account/tranche envelope on every call; token expiry stops the turn | relay + tranche tests |
| concurrency | `max_concurrent_jobs`, `max_agents` | the runner checks live `sandbox_turns` for the placement before creating a Job; `queued` is visible (same style as #2814) | 4 waiters / 3 slots test |
| tools/capabilities | `tools`, `capabilities` | the tool broker; the Job has no kube token and no provider key; a result claiming an unadmitted tool is `profile-refused`; merge/fix/deploy/publish are false | placement qualification |
| isolation | image digest, no kube token, read-only repo, network policy | the Job template and the placement admission; the supervisor binds the exact image digest into the receipt | `kind`/staging inspection |

The trust boundary is stated plainly: ameesh can enforce the deadline, the
relay spend, the counters it receives and the result gate; it cannot prove that
a pod did not execute a local command if the pod's image or network policy is
wrong. That is why the **placement** carries image digest, Job template and
isolation evidence, and why a placement without that evidence is refused. The
pilot's qualification must inspect the real Job, not just the manifest.

## 8. Shared tranche admission

The tranche is a **human-set budget row**, like `account_budgets`, not a canon
card and not a second spend ledger. It is the migration envelope; the account
envelope stays per provider and calendar day.

```sql
create table tranche_limits (
    id           text primary key,              -- tpk-migration-2026-10
    project      text not null,
    cap_usd      numeric(12,2) not null check (cap_usd > 0),
    currency     text not null default 'USD',
    tz           text not null default 'Europe/Paris',
    starts_at    timestamptz not null,
    ends_at      timestamptz,
    state        text not null default 'open' check (state in ('open','closed')),
    admitted_by  text not null,                 -- human:…
    admitted_ref text not null,                 -- signed decision/PR
    created_at   timestamptz not null default now()
);

create table tranche_reservations (
    id           bigserial primary key,
    tranche_id   text not null references tranche_limits(id),
    spend_key    text not null unique,          -- one per turn
    agent        text not null,
    scope_digest text,
    path         text not null,                 -- runner, sandbox, retry, …
    usd          numeric(12,6) not null check (usd > 0),
    state        text not null default 'open'
        check (state in ('open','settled','kept','released')),
    created_at   timestamptz not null default now(),
    closed_at    timestamptz,
    closed_by    text
);
```

`ameesh_admit_turn(agent, billing_account, reserve_usd, spend_key, tranche_id,
path)` is the one boundary for a turn in the tranche scope. A sandbox caller also
passes `retry_family`, `max_attempts` and its scope without `attempt`; the call
takes the family lock, assigns the attempt, computes the exact `scope` /
`scope_digest`, and returns them with both reservation ids.

Before step 1 of a sandbox admission, the transaction takes the `retry_family`
advisory lock, counts the family and assigns `attempt` (§3). The fixed order is
`retry_family` → tranche row → account, and the family count, the reservations
and the `sandbox_turns` intent row commit together. A non-sandbox turn has no
retry family and starts at step 1.

1. `SELECT … FROM tranche_limits WHERE id = $ FOR UPDATE`; if absent, closed or
   outside `[starts_at, ends_at)`, refuse `tranche-unavailable`.
2. Used = `settled + open + kept` for that tranche. If
   `used + reserve_usd > cap_usd`, refuse `tranche-exhausted`.
3. Call the account reservation logic in the **same transaction**, under the
   account's advisory lock, with the fixed order **tranche row, then account**.
   If the account is exhausted, roll back and return `account-exhausted`. The
   existing rule is kept: an open reservation of the same agent from an earlier
   turn is marked `kept` before the new one; it is never reused.
4. Insert the tranche reservation, the account reservation and (for a sandbox
   turn) the `sandbox_turns` intent row with the assigned attempt, all with the
   same `spend_key`; return the ids, the admitted scope and its digest. A
   duplicate `spend_key` is a replay, not a second reservation (the turn's
   idempotency key).

Settlement is `ameesh_settle_turn(spend_key, actual_usd_or_null)`:

* actual cost = the sum of `turn_costs` rows tagged with the turn and
  `billing_account` (relay rows carry the turn id/spend key; see §9), or the
  result's `cost.usd` when the relay is not the meter;
* `null`/unreadable/unsettled → `kept` in full, never zero;
* actual < reserved → the difference is released;
* actual > reserved → the actual is settled and an alert/overdraft flag blocks
  further admissions until a human reconciles; the boundary never lets a later
  turn spend the difference.

Retries are new turns with a new `spend_key`; the previous reservation stays
`settled` or `kept`. The retry-family count and `max_attempts` run before the
tranche reservation in the same transaction (§3), so a retry cannot create a Job
without passing both gates. A human release
(`ameesh budget tranche release <id> --why …`) mirrors
`account_release`; it is audited and never automatic.

The USD 50 example: with `usd_per_turn = 2`, the tranche admits at most 25
turns (floor), not 25.5; a partial reservation is refused. The tranche is
re-estimated before extension. The first tranche's `admitted_ref` is the owner's
signed decision; the bureau may review it, but no agent can raise the cap.

Scope of "every enabled path": the boundary covers every turn the ameesh runner
executes in this migration scope (local harness turns, sandbox turns, retries).
A path that bypasses the runner and the relay (a direct provider key in IMA)
is **not covered**; the pilot therefore refuses it for the admitted profile
(`credential: relay`). The consumer must inventory its Codex/direct-provider
paths and either route them through the relay or keep them disabled during the
tranche.

## 9. Contract, source reference and API

**Runtime contract `ameesh-sandbox/1`.** A versioned JSON contract shipped in
the wheel (`src/ameesh/sandbox_contract.json`, like the mediated executor
contract). It defines:

* the Job input `ameesh-sandbox-job/1` (turn id, owner/epoch, scope, limits,
  prompt reference, relay base/token, result path);
* the Job output `ameesh-sandbox-result/1` (the envelope in §4);
* the native receipt `ameesh-sandbox-receipt/1`;
* the standalone block `ameesh-sandbox-turn/1` and the list
  `ameesh-sandbox-turns/1`, additive with #2167's observation;
* the request `ameesh-sandbox-request/1` (scope + responsible + required
  checks), carried in the existing mail/work payload;
* the closed refusal table below.

An incompatible change is `ameesh-sandbox/2`, never a silent edit. A test
checks the contract against the storage interface and the golden envelopes.

**Metering link.** The columns `turn_costs.turn_id` (0423), `spend_key` (0041)
and `billing_account` (0443) already exist, as do `lease_owner`/`lease_epoch`
(0064); only the relay must fill them for a sandbox turn. Its session token
carries the turn id and the spend key; every relay cost row is written with
the existing `turn_costs.turn_id`, `spend_key` and the profile's
`billing_account`. That is what makes the account and tranche settlement
exact, and what lets the tranche count a turn whose result is never delivered.

**Source reference.** Every receipt carries
`source = {release, commit, contract}`. The implementation adds a build stamp
(`src/ameesh/source.json`, generated by the release workflow) or the
`AMEESH_SOURCE_COMMIT` environment variable; a production placement requires
it (`require_source: true`). A missing source refuses the admission
(`source-unavailable`); a result whose source/contract/image digest differs from
the admitted snapshot is `source-mismatch` / `contract-mismatch`. The receipt
is therefore reproducible from an immutable release/source reference.

**Read API.** `/api/v1` (existing Bearer token and `Context.sees`) gains the
sandbox view, composed with #2167's `ameesh-turn-observation/1`:

* `GET /api/v1/turns/{turn}` gains an optional `sandbox` block (state, Job
  namespace/name/UID/phase, receipt when accepted, refusal) in the
  `ameesh-turn-observation/1` object; the turn's `accounting` state stays
  #2167's. The nested block is additive: a consumer that does not know it
  ignores it;
* `GET /api/v1/sandbox/turns/{turn}` → the same block alone, schema
  `ameesh-sandbox-turn/1`; the block's `state` is the sandbox state above,
  while #2167's `turn.state` (`starting|running|ended|orphan|unknown`) is
  unchanged. `turn.state=orphan` is the resource ledger (a surviving
  process/container); `sandbox.state=orphaned` is the correlation (a Job
  whose turn is gone). They are different fields and can both be present;
* `GET /api/v1/sandbox/turns?agent=&state=&scope=&limit=` → schema
  `ameesh-sandbox-turns/1`, bounded, for adoption/reconciliation and for
  IMA's poll.

The block carries no secrets. The receipt is returned by the turn observation
too, so a consumer that already reads `/api/v1/turns/{turn}` does not need a
second call. The turn's `accounting` object stays #2167's
(`pending|settled|kept|released|none|unknown`, ledger first); the sandbox
block adds a **distinct** `sandbox.tranche` object
(`{tranche_id, state, reserved_usd, settled_usd, kept_usd}`, with `state`
`open|settled|kept|released`) and the result receipt, not a second accounting
state. `accounting.reservation` remains the
account envelope (`billing_account`, `usd`, `period`, `state
open|settled|kept|released`).

There is no `POST` for lifecycle in the pilot: the runner is the only writer.
IMA reads the receipt with the same credential it already uses for `/api/v1`.
The endpoint is part of the #2167 observation family; if #2167 publishes a
common turn-observation object, the sandbox fields nest under it.

**Refusals and non-error outcomes.** Stable `error.type` strings; the CLI/API
return them and the tests assert them:

| Code | When | Retry |
|---|---|---|
| `missing` | required field, scope, Job UID or counter absent; envelope not canonicalisable | no |
| `unauthorized-request` | request id not in the turn's `requests[]` (not reserved for this turn, including a message withdrawn before the reservation) | no |
| `not-a-sandbox-request` | request in `requests[]` without an `ameesh-sandbox-request/1` payload; skipped, not a sandbox trigger | n/a |
| `unknown-turn` | no `sandbox_turns` row | no |
| `stale` | owner/epoch no longer current; lease expired; superseded generation/head | no (new turn) |
| `late` | after `deadline_ts`, or on a terminal turn with no stored accepted result (a stored result is replay/duplicate first) | no |
| `duplicate` | a stored accepted result exists and the submitted digest differs; checked before `late`, including after the turn became terminal or passed the deadline | no |
| `revoked` | cancel/revocation; result fenced | no |
| `scope-mismatch` | result scope/digest ≠ admitted scope | no |
| `job-conflict` | a different Job UID is already bound; ambiguous adoption | no (human) |
| `limit-exceeded` | tool calls, turns or attempts over the admitted limit | no |
| `profile-refused` | profile/placement missing, stale, revoked or drifted; unadmitted tool/capability | no |
| `tranche-exhausted` | tranche cap would be exceeded | no (release/raise) |
| `account-exhausted` | daily provider envelope would be exceeded | no (reset) |
| `source-unavailable` / `source-mismatch` / `contract-mismatch` | immutable reference missing or different | no |
| `wrong-completion` | `end_turn()` reached for a sandbox turn | no |
| `not-cancellable` | terminal/completed; cancel is a no-op | no |
| `already-completed` | cancel lost the race to an accepted result | no |
| `replayed` (not an error) | same result digest re-submitted, including after the turn is terminal or past the deadline (response-loss recovery); returns the stored receipt | n/a |

Precedence is fixed and tested: `unknown-turn` → `revoked` → `stale` →
`replayed`/`duplicate` (stored accepted result) → `late` → `scope-mismatch` →
`missing`/`limit-exceeded`/`profile-refused` → insert. A replay of the stored
result is `replayed` even after the turn is terminal or past the deadline; a
different digest is `duplicate`, not `late`. Revocation and a stale owner/epoch
still win over the stored-result path: they are the authority. A late result on
a revoked turn is `revoked` (the most specific authority), never `late`; a
stale owner is `stale` even after the deadline. An envelope that cannot be
canonicalised is `missing` before the stored-result lookup, since it cannot
match a digest. Admission-time codes (`unauthorized-request`,
`not-a-sandbox-request`, `profile-refused`, `tranche-exhausted`,
`account-exhausted`, `missing`) are checked before a turn or a Job exists; the
precedence above is the result path only.

## 10. Observability

`ameesh sandbox list|show <turn_id> [--json]` returns the state, agent,
owner/epoch, scope digest, profile/placement, Job namespace/name/UID/phase,
deadline, counters, result digest/verdict and refusal. `ameesh show` and
`GET /api/v1/agents` add a `sandbox` object to the current turn, in the same
style as the `queued` object of #2814. Events:
`sandbox.created`, `sandbox.bound`, `sandbox.result`, `sandbox.cancelled`,
`sandbox.orphan`, `sandbox.tranche_overdraft`. Alerts: `sandbox_orphan` (a Job
whose turn has no live lease and no observation), `sandbox_stale` (a grant
whose canon revision was revoked), `sandbox_result_unread` (a completed Job
whose result was not submitted before the deadline). The result payload is
bounded and sanitised; no provider key, kube token or raw transcript is ever
stored or logged.

# Invariants

1. One turn, one `sandbox_turns` row, one Job identity, one accepted result.
2. A Job is created only after the durable intent and both reservations exist.
3. A **new** result is accepted only under the live owner/epoch, before the
   deadline, with the exact scope digest and within the admitted limits; a
   same-digest replay returns the stored receipt even after the turn is terminal
   or past the deadline and never accepts a second result.
4. `end_turn()` is never the completion path of a sandbox turn.
5. A cancel/revocation fences the result before the Job is asked to stop; the
   reservation is `kept` if the cost is unknown, never zero.
6. The profile, placement, limits and scope are immutable snapshots; a canon
   change affects new admissions or requests a cancel, never a silent rewrite.
7. The sandbox path does not release the lease while its Job may still be live;
   a surviving Job is observed and alerted, not replaced. A runner restart is a
   separate, explicit loss path that sweeps the Job.
8. The relay is the only meter for an admitted sandbox; unknown cost is kept.
9. The contract/source/image are pinned and checked; an incompatible change is
   a new contract version.
10. There is no external lifecycle write API in the pilot; IMA reads receipts.
11. Attempts are numbered per immutable `retry_family` under one admission lock;
    `attempt` never enters the family key, and the exact per-attempt
    `scope_digest` still binds the result.

# Qualification

Real PostgreSQL (`PgTestCase`, disposable schema), a fake Job provider for the
crash windows, and a `kind` cluster for the real Job contract; no production
and no paid dispatch.

* **Admission/concurrency**: N runners request the same agent → one lease,
  the others `busy`/`stale`; N requests for a tranche with a cap → at most
  `floor(cap / reserve)` accepted, the rest `tranche-exhausted`; four waiters
  and three slots; account exhausted vs tranche exhausted each tested
  independently. Three concurrent retries of one `retry_family` with
  `max_attempts = 2` and three distinct per-attempt `scope_digest`s admit
  exactly attempts 1 and 2; the third is `limit-exceeded` (the count is per
  family, not per digest).
* **Restart**: kill the supervisor before the Job, after the create response is
  lost, after the bind, after the Job completes and before the result is
  submitted; each time the same turn/epoch adopts exactly one Job and the
  result is submitted once.
* **Runner restart**: the turn is lost, the Job is found by label, deleted and
  observed; no result is accepted; the reservation is kept; a retry is a new
  turn under the attempt limit.
* **Duplicate/late/stale/revoked**: two concurrent result submissions → one
  accepted, one `duplicate`; a same-digest replay → `replayed`, no second turn
  increment and no second settlement, including after the first result set
  `state = succeeded` and after the deadline (response-loss recovery); a
  different digest submitted after that stored result → `duplicate`, not
  `late`; a result after the deadline with no stored result → `late`; an old
  owner/epoch → `stale`; a cancel before the result → `revoked`; cancel vs
  result → one winner and `already-completed` on the cancel side if the result
  won.
* **Limits**: a result over `tool_calls`, a result without counters, a
  `max_attempts` overrun (per retry family, all states counted), a relay call
  after the deadline, a Job that exceeds `activeDeadlineSeconds`; each is
  refused and the reservation is kept.
* **Canon**: missing/stale/revoked profile/placement, a drifted model/harness,
  a wrong image digest, a tool/capability not admitted, a `session: none`
  violation; each refuses before a Job.
* **Tranche**: concurrent reservations, restart between reservation and Job,
  unknown cost, actual > reserved (overdraft blocks the next admission), human
  release, and a replayed `spend_key`.
* **Contract**: golden job/result/receipt envelopes; an incompatible version
  is refused; the source/image digest is echoed and checked.
* **Job (kind)**: deterministic name/labels, no service-account token, no
  provider key, read-only repository, deadline deletion, adoption by UID,
  cancellation observation, result read from the log/termination message.

The acceptance mapping is one test per acceptance sentence: the five refusal
codes, the four gap items, and the restart/cancellation races.

# Delivery

The design must be signed off before implementation; #2167's note comes first.
Proposed lots (estimates in agent hours, to be re-estimated by the bureau):

| Lot | Scope | Class | Estimate |
|---|---|---|---|
| S1 | `sandbox_turns`/`sandbox_results`, `ameesh_turn_close`, `ameesh_sandbox_complete`, `ameesh_sandbox_bind/cancel`, refusal codes; exact `spend_pending` clear by `turn_id` (or #2893); real-Postgres race tests | sensitive (SQL, concurrency) | 8–10 h |
| S2 | Agent/Host `sandbox` blocks, `canon check`, bureau admission/revocation, placement freshness; tests | sensitive (canon, rights) | 5–7 h |
| S3 | `ameesh_admit_turn`/`ameesh_settle_turn`, tranche tables/CLI, account coupling, relay spend linkage; concurrency/kept tests | sensitive (money, SQL) | 6–8 h |
| S4 | sandbox supervisor + provider (create/get/list/logs/delete), deterministic Job identity, adoption/cancel, fake provider + `kind` tests | sensitive (infra, secrets) | 8–10 h |
| S5 | `/api/v1/sandbox` read routes/OpenAPI, `ameesh sandbox` CLI, observability/alerts, #2167 composition | light (API read) | 3–4 h |
| S6 | IMA consumer adaptation (remove its correlation fields, read the receipt), staging/`kind` qualification, pilot runbook | consumer side + sensitive deployment (owner go for staging/prod) | 5–8 h |

Total ~35–47 agent hours. No implementation is proposed before the owner
sign-off; no staging or production action is part of this note. The release
target is posted on #2202 after the design is agreed, as the issue says.

# Alternatives considered

* **External supervisor HTTP API (IMA's controller claims a lease and creates
  the Job).** It would move turn ownership to the consumer, duplicate the
  mediated executor's identity/contract, and make ameesh depend on IMA's
  dispatcher. Rejected for this pilot; a v2 could offer a narrow
  `sandbox.admit/bind/result` API for other consumers, built on the same
  tables/functions, without changing the native model.
* **Host runner creates Jobs directly, no supervisor child.** Rejected: the
  runner process restart would leave no component to adopt/observe the Job, and
  the crash windows "before/after the Job is created" would be untestable. The
  supervisor is a child, not a new service.
* **Direct provider keys inside the Job, cost reported after the fact.**
  Rejected for the tranche: a single turn could overspend its reservation and
  the boundary could not be hard. It remains a possible "reported" placement
  class, but it is not admitted in the pilot.
* **Reuse `end_turn()` plus a side result table.** Rejected: `end_turn()` is not
  exactly-once, does not carry a scope, and would leave the result and the turn
  state in two unrelated transactions.
* **New canon card types (`SandboxProfile`, `SandboxPlacement`).** Rejected for
  now: the existing `Agent`/`Host`/`Placement` cards already carry the
  authority and the placement predicate; two optional blocks are a smaller
  canon change. If the blocks grow into a second lifecycle, a dedicated card
  type is the natural v2.

# Decisions requested

1. Approve the **native supervisor** model: the runner owns the turn, the
   supervisor owns the Job, IMA only sends the request and reads the receipt.
2. Approve the **profile/placement blocks** on the existing `Agent`/`Host`
   cards (rather than new card types), with the bureau canon PR + `admitted_ref`
   as the admission evidence.
3. Require **relay-only** model access for an admitted sandbox, so the deadline
   and the tranche are hard stops; confirm that a direct-provider path is not
   admitted in the pilot.
4. Approve the **tranche table** and the human set/release path
   (`ameesh budget tranche …`), with the USD 50 first tranche and the
   "unknown = kept" rule.
5. Confirm the **result retention** (proposed: receipt/metadata with the turn
   ledger, bounded result payload 90 days, no raw transcripts or secrets).
6. Confirm the **order**: #2167 note first (#2221 / decision #120568), then
   this note's sign-off, then S1–S6; no implementation, staging or production
   action before sign-off.

# Sources

* `manaty/ameesh#2202` — the request and its acceptance.
* `manaty/ameesh#2167` / lot #2894 — turn-context and observation API (first).
* `manaty/ameesh#2110` / lot #2814 — container-aware admission and `queued`
  (the observability style reused, not the mechanism).
* `thepackengers/issue-manager-agent#837`, PR #843 — IMA's preparation,
  reproduction of the `end_turn` gap, and the focused correlation/result
  proposal.
* `docs/V1-MAILBOX-RUNNER.md` §3 — lease/epoch fence.
* `src/ameesh/account_budget.py`, `src/ameesh/storage/postgres/budgets.py`,
  `src/ameesh/storage/postgres/registry.py`, `src/ameesh/runner.py` —
  accounting, marker, turn end.
* `src/ameesh/relay.py` — relay token, model policy, cost rows.
* `src/ameesh/canon.py` — Agent/Host/Placement cards and placement predicate.
* `src/ameesh/mediated_executor/contract.py` — versioned contract and
  idempotency discipline.
* `docs/EXPLOITATION.md` — admission/capacity and budget operating rules.
