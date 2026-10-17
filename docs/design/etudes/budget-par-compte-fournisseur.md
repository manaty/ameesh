---
type: Study
title: "Study — a shared daily USD budget per provider billing account (OpenAI 100 $/day, DeepSeek 100 $/day)"
description: "Billing account identity independent of key, agent, harness and host; calendar-day envelope in Europe/Paris; atomic admission by dollar reservation in the central database; conservative settlement; coverage boundary; migration and CLI. Design note for owner review before implementation (lot #1469, issue #1101)."
status: decided
tags: [budget, cout, compte, fournisseur, openai, deepseek, admission, reservation, tpk]
generated: { by: "ameesh-dev7/claude-opus-5-5", at: "2026-10-15T04:00:00+02:00" }
sources:
  - { resource: "../decisions/0019-budgets-et-routage.md", title: "Decision 0019 (budgets and routing)" }
  - { resource: "../../../site/docs/concepts/budgets.md", title: "Budgets (concept)" }
  - { resource: "https://github.com/manaty/ameesh/issues/1101", title: "Issue #1101" }
---

# Design note: daily budget per provider account

Lot #1469, issue #1101. Required for 1.11.0 (TPK). Sensitive class (money,
concurrency, SQL): two review families. This note is for the owner to decide
before any code is written.

**Owner decision (decision #45732, 15/10/2026 around 03:40): approved, minimal
version.** What was implemented differs from the proposal below in three
ways:

- the billing account is declared only in the profiles (`billing_account`),
  with no `billing_accounts` table;
- the bound R is `reserve_usd` per profile, 2 $ by default, with no p95;
- the relay and mediated executors stay outside the envelope.

Reference: `site/docs/concepts/budgets.md`.

## 1. What exists, and what is missing

| Already there | Missing |
|---|---|
| Central ledger `turn_costs` in the mesh database, shared by all hosts (`cost.py:1320`) | A billing account identity that is the same on every host: today `turn_costs.account` is the name of a profile **local to the host** (`accounts.py:178`) |
| Mesh and agent caps over sliding 1 h and 24 h windows (`budget.py:53`, `budget_limits`) | A calendar day: everything is sliding, and the balance bucket is UTC (`balance.py:239`) |
| Per-agent pre-turn fence `spend_pending`, fail-closed (`runner.py:2525`) | A **dollar** reservation: admission is check-then-act (`cost.py:1550`), so two hosts can both pass just under a cap |
| Billing mode from the harness descriptor `paid_per_token` | Billing mode from the **account**: an API-key Codex account is counted as a plan and excluded from caps |
| DeepSeek relay with an in-memory hold (`relay.py:777`) | OpenAI as a provider anywhere |

## 2. Proposed model

**2.1 Billing account (`billing_accounts`, new table, mesh-wide).**
`id` (slug chosen by a human, e.g. `openai-manaty`, `deepseek-manaty`),
`provider` (`openai` | `deepseek` | …), `billing` (`per_token` | `plan`),
`label`. It holds no secret. A host profile in `config.json` gains
`"billing_account": "<id>"`. Several keys, several hosts and several profile
names can point to the same id, so rotating a key, renaming a profile,
moving a host or recreating an agent changes nothing. The id goes on each
`turn_costs` row (new column `billing_account`), next to the existing
`account` column, which stays as it is.
A paid profile with no `billing_account` is refused for paid turns once an
envelope exists for its provider (no uncounted spend). Billing mode then
comes from the account, not from the harness: an `api_key_env` Codex profile
linked to a `per_token` account is paid.

**2.2 Envelope (`budget_limits`, extended).** New scope form
`account:<id>`, new window `day_paris` (calendar day, IANA zone stored with
the row, `Europe/Paris` by default). Initial configuration:
`account:openai-manaty 100 $/day_paris`, `account:deepseek-manaty 100 $/day_paris`.
These are separate envelopes, with no borrowing between them. The existing
mesh and agent caps (sliding 1 h and 24 h) keep their meaning and remain
**optional**; no per-agent monetary allowance is required. The existing
consumer EUR monthly budget is not touched.

**2.3 Period.** The day is `[00:00, 24:00)` Europe/Paris, computed with
`zoneinfo`. It lasts 23 h or 25 h on DST days, and the cap stays 100 $ for
that day. Spend is attributed to the day in which it is **settled**,
except that a reservation stays counted against the day it was taken until it
is settled or released. A turn that straddles midnight therefore weighs on
the old day while it runs, then its actual cost goes to the new day. It
never disappears, and it is never counted twice. Status shows
`reset 2026-10-16 00:00 Europe/Paris` and labels the sliding 24 h window
as "24 h sliding" to tell the two apart.

**2.4 Atomic admission (`account_reservations`, new table).** Before a paid
turn, in **one transaction** under `SELECT … FOR UPDATE` of the envelope row
(or `pg_advisory_xact_lock(hash(account))`):
`settled_today + open_reservations + R <= limit` → insert the reservation
(`id`, `billing_account`, `agent`, `host`, `period`, `usd_reserved`,
`state=open`, `lease_owner`, `lease_epoch`, `created_at`). Otherwise the
turn is refused. The reservation is taken in the same transaction as the
existing `spend_pending` marker, which it extends (the marker gains its
`reservation_id`).
- **Turn bound R.** A harness turn is not a single request, so it has to be
  bounded. Proposal: `R = max(min_reserve, p95 of the agent's last 50 paid
  turns on this model)`, capped by `max_turn_usd` per account (default 5 $).
  The turn is already bounded in time (`session_max_turn_seconds`) and in
  tool calls.
- **Settlement.** The `turn_costs` row (unique `spend_key`) and the move of
  the reservation to `settled` are written in the same transaction. A
  duplicate delivery hits `spend_key` and settles nothing twice.
- **Overrun.** If the actual cost exceeds R, it is counted in full. The next
  admissions see it and the account is blocked. The worst-case overshoot is
  therefore bounded by Σ(actual − R) over the turns running at that moment,
  which the note states openly in the docs. It is not a hard guarantee to
  the cent.
- **Relay path** (DeepSeek, mediated executors): admission is **per HTTP
  request**, with R = `max_tokens` × output price + input × input price. The
  bound is then exact, and the in-memory hold of `relay.py` is replaced by
  the table.
- **Uncertain outcome, crash.** An `open` reservation whose turn has died
  (expired lease, runner restarted) is **never** released automatically.
  It is converted to `settled_estimated` for its full amount R. A human can
  correct it through `ameesh cost correct` (already traced). An unknown
  outcome is never replayed: the agent resumes on a new turn that needs a
  new admission.

**2.5 Exhaustion.** Status is `blocked`, reason `budget: account
openai-manaty 100.00/100 $ (day Europe/Paris) until 00:00`, via the existing
pause surfaces (#819). Conversations and documents are not affected. A
**hard** account envelope has priority over everything:
- the critical-agent reserve and `critical_fallback_harnesses` can only fall
  back to **another account that still has capacity**;
- an agent or consumer whose routing pins the provider (`pin_provider`) has
  no fallback. It waits.

**2.6 Human authority.** `ameesh budget set --account <id> --per-day 100
[--tz Europe/Paris]` and `ameesh accounts bill <profile> <billing-account>`
are human-only, as `budget set` already is. Every change is journaled in
`budget_events` and posted to the thread. `ameesh budget show` and
`--json` (`ameesh-budget/2`) give, for each account: limit, settled,
reserved, remaining, period, reset, and the agents paused.

## 3. Coverage boundary

What is enforced: the paid turns of runners on hosts connected to the
database, mediated executors (through `/api/exec/v1`, which gets the
`reservation.*` operations), and the relay. What is **not** enforced: any use
of the same keys outside ameesh. That includes an application's own adapter
that calls OpenAI directly, a developer's shell, and other tools. For
DeepSeek, the balance poll (`provider_balances`) already measures the real
drop. The design counts the gap between the real drop and the ledger as
`external` spend in the day, so it eats into the envelope, but after the
fact. For OpenAI, there is no balance reading yet. External usage is not seen
until a usage API source is added (proposed as a later lot). The docs will say
so in those words.

**IMA consumer.** No IMA adapter exists in the ameesh source. If IMA calls the
providers itself, it is outside the boundary unless it goes through the relay
(the recommended path). Adoption: IMA's pin moves from `9de79648` to the 1.11.0
tag. The adapter declares its `billing_account` and goes through the relay with
its agent token.

## 4. Migration and compatibility

One SQL migration (number reserved by the lot): `billing_accounts`;
`turn_costs.billing_account` (nullable, indexed by `(billing_account,
recorded_at)`); `account_reservations`; the `budget_limits` constraint widened
(scope `account:*`, window `day_paris`, column `tz`); `spend_pending.reservation_id`.
Postgres drivers (`PsqlDriver`, `PsycopgDriver`) plus the mediated backend.
Nothing changes as long as no account envelope is set. Historical rows keep
`billing_account` null; a one-time `ameesh accounts bill --backfill` fills it
from the profiles declared today.

## 5. Acceptance tests (the 7 from the issue)

1. Two agents on two simulated hosts share one envelope; a third adds nothing.
2. N concurrent admissions near 100 $: only those whose reservations fit
   pass; the DeepSeek envelope does not move.
3. API-key Codex and DeepSeek turns settle once; a duplicate delivery, an
   uncertain outcome and a crash keep R counted.
4. Midnight Paris, the last Sunday of October (25 h), a reservation that
   straddles midnight: nothing erased, nothing counted twice.
5. Agent recreation, key rotation, two aliases, host migration: same history.
6. A critical agent and failover do not bypass; a pinned provider stays pinned.
7. Recovery after a restart, an audited limit change, the visible blocked state.

## 6. Points for the owner

- **A. Reservation bound** for a harness turn: p95 capped at `max_turn_usd`
  (recommended) / a fixed amount per account / per-request admission only
  (requires sending every paid harness through the relay, which is out of
  reach for 1.11.0).
- **B. Crash**: R counted in full until a human corrects it (recommended) /
  released after N hours.
- **C. OpenAI external spend**: stated boundary in 1.11.0, usage API source in
  a later lot (recommended) / required for 1.11.0.
