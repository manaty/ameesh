# IMA → public module contract: inventory and replacement plan

Document revision 7, 28 November 2026. This is the consumer mapping for
[contract v1](../api/module-contract-v1.md), requested by
[#7664 / #5856](https://github.com/manaty/ameesh/issues/5856), under
[#7636 / #5827](https://github.com/manaty/ameesh/issues/5827).
Consumer removal is tracked in
[thepackengers/issue-manager-agent#1803 — native functions](https://github.com/thepackengers/issue-manager-agent/issues/1803).
Every replacement below is either an existing public command, a named native
feature to adopt through configuration, or a proposed extension with that
upstream ticket and a tranche identifier. A proposed extension is not a usable
API. Implementation lots created by the pilot are listed in the final table.

## Reproducible scope

The IMA default branch `main`, inspected at
`883d219b37962b39b0a551340f537559ccf26820`, does not contain these integration
scripts. The inventory uses **`develop` at
`23e0a944bc82647e929a2cb96b4af3245030354c`**, which does contain them. Compare
the upgrade branch `ameesh-v1.57.0` at
`63a017ccc7bc0911f7d4a62516e378e6d81f65eb`: develop adds the Memoria relay
and forwards the additional accounting keyword in `document_scheduling.py`.
These are observations of source trees, not proof of the image running in IMA.

The upstream baseline is
`aeca7e2ebca4994d477cc9e2a7cb2970a4fdf007`. IMA's image definition instead pins
ameesh v1.57.0, annotated tag object
`4101332dd3467b26a604170e4f5ce9ccf0f4a2e7`, peeled commit
`89f32d7da51a0c9f65bcc3da187177615d1dbf4e`.
Do not mistake the newer upstream baseline or an open PR for an adopted IMA
feature. Native replacements below require qualification against IMA's chosen
release before its old implementation is removed.

To reproduce the inventory on a read-only checkout of the IMA SHA:

```sh
git grep -n -E 'from ameesh[ .]|import ameesh\b|tests.support' -- '*.py'
git grep -n -E 'AgentWorker|runner.Runner|_tx_lock|agent_mailbox|agent_registry' -- scripts
git grep -l -E '89f32d7da51a0c9f65bcc3da187177615d1dbf4e|4101332dd3467b26a604170e4f5ce9ccf0f4a2e7|1.57.0'
```

The first query finds 18 scripts with direct Python ameesh imports and nine
files importing `tests.support`. It also finds integration-test dependencies
listed separately below. `document_scheduling.py` receives the imported runner
as an argument and `ima-db-bridge.py` receives the database implementation;
they are additional indirect dependencies, not a reason to inflate the direct
import count. Package-local names such as `ameesh_psycopg` are IMA modules and
are not counted as ameesh package imports.

All IMA source links below are pinned to the inventory SHA. Filenames in the
tables are relative to that repository, not this repository.

## The 18 directly importing scripts

Extension keys E1–E3 and fixture/stable work F/S are defined in the final
table. For every E key, the named upstream ticket is
[manaty/ameesh#5827](https://github.com/manaty/ameesh/issues/5827);
the consumer adoption/removal ticket is
[IMA#1803](https://github.com/thepackengers/issue-manager-agent/issues/1803).
Existing public commands do not automatically reproduce every check in a
private helper: the gap column preserves the remaining requirement.

| # | IMA script and internal dependency | Public/native replacement and remaining work |
| --- | --- | --- |
| 1 | [`scripts/ameesh-directory-snapshot.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-directory-snapshot.py): `canon.load_yaml/load/validate/errors`, member objects, `ameesh.__file__` | `ameesh canon check` for validation; **E1a** for a public immutable canon/directory snapshot with member metadata, source SHA and freshness. `GET /projects` alone does not return members or their directory metadata. Source-pin validation → S. |
| 2 | [`scripts/ameesh-mailbox-bridge.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-mailbox-bridge.py): `config`, `db`, `accounts`, `storage`, `mail`, `registry`; raw mailbox/registry/spend queries | Ordinary mail → `agent-mail send`; request observation → `ameesh turn observe --request ID --json` / HTTP `/requests/{request}`. **E2a/E2b** preserve request key dedupe, typed payload, lease-bound pending/reply and reply correlation; **E2d** replaces the production `config`/`db.connect`/`require_schema` imports with `ameesh.public.connect()`; **E1b** replaces account/capacity introspection, and **R** (`ameesh reservations`, #6583) replaces the `credit_snapshot` read of `account_reservations`. These guarantees are not provided by generic message send alone. |
| 3 | [`scripts/ameesh-process-qualification.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-process-qualification.py): `process_workflow`, `process_registry`, `process_verifiers`, `DbError`, `PgTestCase`; synthetic registry rows | Native Process cards through `ameesh canon check/sync`; **F** supplies an external fixture and CLI/API qualification path for acceptance, refusal and verifier outcomes without private workflow imports or synthetic core SQL. Pin checks → S. |
| 4 | [`scripts/ameesh-reviewer-observation.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-reviewer-observation.py): `registry.get/lease_matches/pending_spend_get/canon_claim_predicate_sql`, raw canon state | `turn current`, `turn observe`, `/agents/{agent}`, `/turns/{turn}`, `/sandbox/turns/{turn}` provide native references; **E1b** supplies the atomic admitted-profile/lease observation and freshness it additionally needs. Missing sandbox or tranche evidence remains unavailable, never invented or treated as admitted. |
| 5 | [`scripts/ameesh-reviewer-preparation.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-reviewer-preparation.py): `accounts`, `account_budget`, `exploitation`, `registry`, `storage`, private profile digest | Native Kubernetes sandbox turns and turn observation replace a parallel execution/accounting path. **E1b** for read-only prepared admission/capacity evidence (responsible, canon/placement references, lease epoch, bounded budget and limits). It must not reserve spend or launch a Job. Source checks → S. |
| 6 | [`scripts/ameesh-reviewer-qualification.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-reviewer-qualification.py): `PgTestCase`, accounts/budget/canon/persona/registry/storage internals | **F** for public fixture and API-based preparation/observation checks; **E1b** for the prepared profile; native sandbox/turn references for qualification; pin → S. |
| 7 | [`scripts/ameesh-role-bridge.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-role-bridge.py): mail/registry imports, raw role mailbox and lease transactions | Generic mail/turn observation plus **E2a/E2b** for atomic `submit_many`, request scope, lease-bound reply and dedupe. Reuse the same generic protocol as document requests; no role-specific core schema. |
| 8 | [`scripts/ameesh-role-runner.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-role-runner.py): config/db import, dynamically installed Runner replacement, direct `runner.main` | Installed `agent-runner`, governed harness adapters and `--agents-file`; native `own_cgroup` pressure. Account admission qualification → **E1b**; remaining policy/diagnostics → **E3a/E3b**. Remove the placeholder role harness only with real role execution correlated to the native turn/sandbox, tracked in IMA#1803. |
| 9 | [`scripts/ameesh-runtime-admission.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-runtime-admission.py): `accounts.parse`, direct `account_budgets` SQL | Native governed account admission/budget caps; HTTP `/budget` for observation. **E1b** supplies the admitted profile/capacity check required before runtime startup without exposing credentials. Keep IMA's own provider/endpoint allowlist in IMA. |
| 10 | [`scripts/ameesh-upgrade-runtime.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/ameesh-upgrade-runtime.py): `config`, `db`, `migrations`, `cli.main` | Execute `ameesh migrate --dry-run --json` (revision 1.10, `ameesh-migrate-dry-run/1`: pending core/module migrations with checksum and non-additive reasons, applied migrations modified after the fact, unknown versions) and the documented upgrade/doctor CLI under existing operator gates. IMA replaces `cli.cmd_migrate`, `migrations.check` and `migrations.non_additive` with that object once adopted. **F** for disposable upgrade fixtures; **S** for pin and qualification across stable versions. This mapping authorizes no migration application. |
| 11 | [`scripts/document-ameesh-preflight-smoke.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-ameesh-preflight-smoke.py): `PgTestCase`, canon sync, registry/storage, accounts/budgets, `CostBook/load_prices` | **F** replaces fixture/seed/ledger test internals with public setup and observations. Native canon, `/budget` and `turn observe` for assertions; **E1b** for admission capacity evidence; **PR** (`ameesh prices --json`, #6578) replaces `load_prices`. IMA owns its expected cost examples. |
| 12 | [`scripts/document-ameesh-preflight.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-ameesh-preflight.py): config/db/registry, private registry binding reads and package location | `ameesh --version`, public agents/hosts observations and **E1b** for complete admitted binding evidence. Immutable source identity comes from **S**, not `ameesh.__file__`. |
| 13 | [`scripts/document-ameesh-runner.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-ameesh-runner.py): canon/config/db imports, `cost.load_prices/max_price_of`, direct bindings, installed monkey patches | **PR** (`ameesh prices --json`, #6578) replaces `cost.load_prices/max_price_of`. Installed `agent-runner --agents-file`; native admission priority and `own_cgroup`; governed canon and budget admission. **E1b** for preflight bindings/capacity; **E3a/E3b** for policy/diagnostics; **E2c** for interrupted request replay. IMA's business binding restrictions stay in its module. |
| 14 | [`scripts/document-canon-check.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-canon-check.py): `canon`, `canon_sync`, `config`, `db` and package/source checks | `ameesh canon check/sync`; **E1a** for source SHA and monotonic snapshot admission evidence; **E1c** replaces the invalid `canon_state` write when IMA's own checks refuse the canon (`canon_sync.record_state(...)`, `document-canon-check.py:43`) with `ameesh canon refuse`. Synchronization must retain existing canon authority checks. Pin → **S**. |
| 15 | [`scripts/document-container-pressure.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-container-pressure.py): Runner subclass, `host_pressure`, `_pressure_lock`, replacement of `self.db._tx_lock` | Native canon `policy.resources.pressure_scope: own_cgroup`, resource thresholds and native shared-connection admission priority (`admission_lock`, on by default). Native priority boosts awakened/control work, not a permanent document-agent class: **E3b** must qualify IMA's latency/fairness requirement before removal. CPU/throttling/request-limit threshold parity must be checked, not assumed — the [#7675 qualification](../design/7675-native-pressure-admission-qualification.md) adds opt-in `max_cgroup_cpu_used` / `max_cgroup_throttled_periods`; requests stay a consumer scheduling input. |
| 16 | [`scripts/document-runtime-handover.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-runtime-handover.py): Runner shutdown subclass, worker locks/epoch/host_yield, raw mailbox and registry updates | Native drain/withdrawal is the control path; **E2c** adds core-owned interrupted-request requeue with captured lease identity, exact request IDs and reply-race protection. No external callback writes restored prompts. |
| 17 | [`scripts/document-session-policy.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/document-session-policy.py): AgentWorker overrides `current_session`, `session_policy`, `context_max_tokens`, `run_turn`, `_attendre_reprise` | **E3a** (#7674) delivers the declarative replacement: `session_policy`, `fresh_session`, `context_max_tokens` and `failure_wait_min` declared on the Agent/Persona card and consumed by the runner ([contract](../api/module-contract-v1.md#declarative-per-agent-session-policy-e3a-7674)); IMA#1803 removes the subclass. The public contract does not freeze the optional `harness` argument of `context_max_tokens`. |
| 18 | [`scripts/memoria-avatar-relay.py`](https://github.com/thepackengers/issue-manager-agent/blob/23e0a944bc82647e929a2cb96b4af3245030354c/scripts/memoria-avatar-relay.py): config/db/mail/registry; mailbox history and advisory locking for cursor/dedupe | Public mail send plus **E2a** generic scoped idempotent submission/history cursor. Memoria masking, aggregation, 72-hour horizon, daily recap and source cursor remain IMA logic; ameesh supplies durable mail identifiers and atomic dedupe, without a Memoria-specific table or endpoint. |

### Action-connector module (post-inventory, #6582)

The frozen inventory above predates IMA's `tpk_ima_connectors` module, which
declares an `ACTION_CONNECTORS` mapping and imports
`from ameesh.connectors import Outcome`. From contract revision 1.8 the
supported path is `from ameesh.public.connectors import Outcome` (with
`Action`, `NotSentError`, `ACTION_CLASSES`, `OUTCOMES` and `DEDUPE`); the
module keeps its `ACTION_CONNECTORS = {...}` declaration, and the core registry
discovers it through the ordinary `ameesh.modules` entry point. The connector
protocol, the governed gate and the canon `action_classes` activation are
unchanged. IMA#1803 removes the internal import; this contract changes no
connector code.

- [public action-connector contract](../api/module-contract-v1.md#action-connectors-declared-by-an-installed-module-18-8342)
- frozen surface: `python:connectors` in
  [module-contract-v1.surface.json](../api/module-contract-v1.surface.json)
- upstream: [#6582](https://github.com/manaty/ameesh/issues/6582);
  implementation lot #8342.

### Account profile layout (post-inventory, #6577)

`ameesh-runtime-admission.py` (row 9) and `ameesh-reviewer-preparation.py`
(row 5) call `accounts.parse(cfg.accounts)` to check that exactly one API
account exists per provider. From contract revision 1.9 the supported read is
`ameesh accounts profiles --json` (`ameesh-account-profiles/1`): every declared
profile with `name`, `kind`, `harness`, the variable names `env`/`key_env`,
`key_source`, `reserve_usd` and `billing_account`, never a key value or a key
file's path; an unreadable profile is listed under `refused` with a closed
reason instead of hiding the others, and `valid` says whether the runner admits
the configuration. IMA keeps its own "one API account per provider" rule and
drops the `accounts` import for this check.

- [CLI boundary](../api/module-contract-v1.md#cli-boundary), frozen as
  `cli:ameesh accounts profiles` in
  [module-contract-v1.surface.json](../api/module-contract-v1.surface.json)
- upstream: [#6577](https://github.com/manaty/ameesh/issues/6577);
  implementation lot #8337.

### Migration dry-run report (post-inventory, #6579)

IMA's upgrade rehearsal (`scripts/ameesh-upgrade-runtime.py`) reads missing,
modified and non-additive migrations by calling `cli.cmd_migrate`,
`migrations.check` and `migrations.non_additive`. From contract revision 1.10
the supported replacement is the documented `ameesh migrate --dry-run --json`
command (schema `ameesh-migrate-dry-run/1`), which returns the same facts as
data and keeps the operator gates on the applying path. IMA#1803 removes the
three private calls once it adopts the report; this mapping authorizes no
migration application.

- [migration dry-run report](../api/module-contract-v1.md#migration-dry-run-report-revision-110-8339)
- frozen surface: `cli:ameesh migrate` in
  [module-contract-v1.surface.json](../api/module-contract-v1.surface.json)
- upstream: [#6579](https://github.com/manaty/ameesh/issues/6579);
  implementation lot #8339.

### Account reservations still counted (post-inventory, #6583)

The `credit_snapshot` operation of `scripts/ameesh-mailbox-bridge.py` reads
`account_reservations` directly (lines 124–168 at the inventory SHA): it groups
`kept` rows by `billing_account` and `agent`, counts them, counts those created
in the last 15 minutes, and keeps the smallest reservation id, restricted to the
account's current calendar period or the last 15 minutes. From contract
revision 1.11 the supported read is
`ameesh reservations --json` (`ameesh-reservations/1`, also
`GET /api/v1/reservations`): every `open` reservation and every `kept`
reservation of the envelope's current calendar day, oldest first,
bounded by `limit` (default 100, maximum 500), with `id`, `agent`,
`agent_known`, `account_profile`, `billing_account`, `usd`, `state`, `period`,
`created_ts`, `age_s`, `account_envelope` and `period_reset_ts`; `observed_ts`
is the single instant of the age. The core applies the same current-period
filter as the admission (`_USAGE`), so an expired `kept` never saturates the
bound. The consumer keeps its own grouping, 15-minute window and capacity
decision; the core supplies no IMA-specific grouping or window. An agent that no longer
exists is still listed (`agent_known: false`), and a row that cannot be rendered
faithfully is under `unreadable` with a closed reason instead of disappearing.
`/budget` already publishes the per-account envelope and period used to apply
the same calendar-day restriction; `/agents` publishes the blocked state and
status text used by the adjacent `blocked` query. IMA removes the
`account_reservations` SQL and the private `storage.budgets` join, not its own
aggregation or the final capacity decision.

- [CLI boundary](../api/module-contract-v1.md#cli-boundary), frozen as
  `cli:ameesh reservations` in
  [module-contract-v1.surface.json](../api/module-contract-v1.surface.json);
  [HTTP boundary](../api/module-contract-v1.md#http-boundary) for
  `GET /api/v1/reservations`
- upstream: [#6583](https://github.com/manaty/ameesh/issues/6583);
  implementation lot #8343.

### Price schedule (post-inventory, #6578)

`scripts/document-ameesh-runner.py` (row 13, lines 221–224 cited by
#6578) imports `cost.load_prices()` and `cost.max_price_of()` to bound a
document turn's cost, and `document-ameesh-preflight-smoke.py` (row 11) plus
two IMA integration tests read the same schedule. From contract revision 1.13
the supported read is `ameesh prices --json` (`ameesh-prices/1`): each model's
`input`, `cached_input` and `output` (USD per million tokens) with its
`origin` (`default` or `file`), and per harness `max_price` — exactly
`max_price_of` on the effective schedule — and `unknown_model_price`, which
falls back to the whole schedule's maximum with `fallback: true` when a family
has no price, never to zero. `source` gives the file path and its origin
(`AMEESH_PRICES` or the default path), `status` (`absent`, `loaded`,
`unreadable`, `malformed`; an unreadable file is reported, the defaults stay in
force), and `sha256`/`mtime` of the content read: the core re-reads the
schedule on every turn, so a consumer compares the digest rather than caching
across a change. IMA removes both imports; its cost-control decision stays in
IMA.

- [CLI boundary](../api/module-contract-v1.md#cli-boundary), frozen as
  `cli:ameesh prices` in
  [module-contract-v1.surface.json](../api/module-contract-v1.surface.json)
- upstream: [#6578](https://github.com/manaty/ameesh/issues/6578);
  implementation lot #8338.

## Indirect runner and connection dependencies

These rows complete the dependency inventory even though they are not additional
direct package imports. All adoption/removal remains in IMA#1803.

| IMA file / usage | Replacement and boundary |
| --- | --- |
| `scripts/document_scheduling.py`: `install_worker_diagnostics(runner)` replaces AgentWorker; overrides `_compta_ecrit`, `budget_ok`, `_held_by_pressure`, `host_ready`, `workdir_ready` | Native turn/accounting and `turn observe` keep the reservation ledger authoritative. **E3b** exposes bounded structured deferral reasons; unknown/ambiguous ledger evidence retains native reservations and never creates a second settlement path. |
| `scripts/document_scheduling.py`: `PriorityAdmission`, attached by the pressure module to `_tx_lock` | Native shared-connection admission lock with FIFO, aging and bounded control burst; **E3b** qualifies document-vs-role latency and starvation behavior. If a native priority policy is missing, add it in that tranche through declared scheduling policy, not a public lock pointer. |
| `scripts/ima-db-bridge.py`: receives private database module, calls `connect/DbError`, wraps connection via `__getattr__`, rewrites DSN application name | **E2d** replaces the direct `connect`/`DbError` import with `ameesh.public.connect()` (usual configuration, immediate schema check, public errors), then migrates bridge operations to long-lived HTTP/CLI APIs and E1/E2. Connection limits, retry and database admission stay in the core. IMA may still limit its subprocess count, but must not need the core driver object or its locks. |
| `scripts/rehearse-ameesh-upgrade.py`: pinned source, copied test fixture/source runner in disposable upgrade exercise | **F/S**, a consumer-owned disposable database fixture plus public executable/API contract. Running upstream private tests may be an optional source qualification tool, never an installed-module dependency. |

## Core SQL access requiring removal

| Core objects / current purpose | Replacement (named upstream ticket #5827, consumer IMA#1803) |
| --- | --- |
| `agent_mailbox`: pending delivery, request/reply lookup, payload filtering, request key reuse and Memoria relay history | **E2a/E2b**. Typed payload is module-owned data; request identity, visibility, idempotency and transactionality belong to the core. Public ordinary mail and turn/request observations remain usable immediately. |
| `agent_registry`: checking owner/epoch/lease expiry, `FOR UPDATE`, changing `pending_prompt` and status during shutdown | **E1b** read-only admitted-profile observation, **E2b** fenced reply, **E2c** core-owned replay. `agent_mesh_overview` can replace fleet display, not authorization under a mutable lease. |
| `spend_pending` and ledger joins: match mailbox delivery to native turn/reservation and determine whether a request is still current | Native `turn current` and `turn observe`; **E1b/E2b** for atomic protocol checks. Never publish a writable reservation capability. |
| `account_reservations`: the `credit_snapshot` grouping of `kept` rows per agent and billing account (count, 15-minute count, smallest id, current calendar period) | **R** (#6583): `ameesh reservations --json` / `GET /api/v1/reservations` publishes every `open` row and every `kept` row of the envelope's current calendar day, oldest first and bounded, with `state`, `period`, `created_ts`, `age_s`, `account_envelope` and `period_reset_ts`. The consumer keeps its grouping, 15-minute window and capacity decision; the core publishes no IMA-specific shape and no writable reservation path. |
| `canon_state`, account/budget/binding tables: freshness, provider limits and admission preparation | **E1a/E1b/E1c** and native canon/budget APIs; return redacted facts and opaque references instead of raw credentials or core tables. |
| Core SQL in synthetic tests (`INSERT`, `UPDATE`, `DELETE` on registry/mail/budget/ledger) | **F**: consumer-owned isolated fixtures using public setup/operations; no test-only production bypass. |

## All nine `tests.support` consumers

The replacement for every row is **F**, upstream
[#7665 / #5857](https://github.com/manaty/ameesh/issues/5857), with removal and
public-fixture adoption in IMA#1803. Pin fixture API alongside the contract;
do not move `tests.support` to another private import path.

| IMA file | Public fixture / assertion to retain |
| --- | --- |
| `scripts/ameesh-process-qualification.py` | Disposable Process-card admission, verifier success/refusal and journal observation. |
| `scripts/ameesh-reviewer-qualification.py` | Prepared native profile, lease mismatch, budget refusal and no unauthorized dispatch. |
| `scripts/document-ameesh-preflight-smoke.py` | Canon binding, account/capacity refusal and native turn accounting. |
| `tests/integration/ameesh_mailbox_test.py` | Correlated request/reply, retry/dedupe and native ledger reference. |
| `tests/integration/data_scientist_usage_test.py` | Consumer-owned usage examples; native budget/accounting observation. |
| `tests/integration/document_codex_usage_test.py` | Native usage reporting without importing `CostBook` or storage writers. |
| `tests/integration/document_housekeeping_test.py` | Refused/deferred work observed through CLI/API instead of constructing AgentWorker. |
| `tests/integration/memoria_avatar_relay_test.py` | Idempotent relay delivery with module-owned payload/cursor, using E2a. |
| `tests/integration/migration_budget_test.py` | Public migration dry-run, budget behavior and accounting observations on an isolated fixture. |

Other direct private imports also occur in
`tests/integration/document_listen_test.py`,
`tests/integration/document_priority_test.py`,
`tests/integration/job_bridge_application_name_test.py`, and
`tests-integration/memoria-corpus-native-fixture.py`.
They are **F + IMA#1803** work as well: listen/reply through E2b,
priority through E3b, public native request/turn correlation rather than
driver application-name assertions, and module-owned corpus setup respectively.
`document_listen_test.py` also changes delivery/lease/prompt fields in SQL;
the fixture must recreate those races through its controlled process lifecycle.

## Version pin inventory

The original request reports 17 files. At the frozen develop SHA, the literal
tag-object, peeled-commit or `1.57.0` search finds **16 executable/config/test
files**, plus three historical knowledge documents. This is a source-snapshot
difference, not a claim that any duplication has already been removed.
Every row below has replacement **S**, upstream
[#7666 / #5858](https://github.com/manaty/ameesh/issues/5858), consumer IMA#1803.
One immutable manifest must drive all executable checks. Historical upgrade
evidence may retain its old version as history, identified as such.

| IMA file | Treatment |
| --- | --- |
| `.github/workflows/pr-ci.yml` | Read the single manifest for source/test qualification. |
| `Dockerfile` | Build from the manifest tag object and verify its peeled commit. |
| `document-agent-channel.js` | Derive the advertised installed version. |
| `helm-issue-manager/Chart.yaml` | Generate application version metadata. |
| `scripts/ameesh-directory-snapshot.py` | Use manifest identity, then public snapshot E1a. |
| `scripts/ameesh-process-qualification.py` | Use manifest identity and F. |
| `scripts/ameesh-reviewer-preparation.py` | Use manifest identity and E1b. |
| `scripts/ameesh-upgrade-runtime.py` | Read source/target manifest identities. |
| `scripts/document-ameesh-preflight.py` | Read manifest and installed public version. |
| `scripts/document-canon-check.py` | Read manifest and E1a source identity. |
| `scripts/rehearse-ameesh-upgrade.py` | Read source/target manifest identities. |
| `tests-integration/ameesh-upgrade.cjs` | Parameterize by the two manifests. |
| `tests-integration/ameesh-work-bindings.cjs` | Read fixture's version from the manifest. |
| `tests-integration/document-runtime-image.cjs` | Compare actual image/source identity with manifest. |
| `tests-integration/memoria-corpus.cjs` | Read native fixture identity from manifest. |
| `tests/ameesh-release.test.js` | Assert consumers derive metadata from one manifest. |
| `knowledge/operations/ameesh-v157-migrations.json` | Keep historical migration evidence; label its source/target versions. |
| `knowledge/operations/ameesh-v157-upgrade.md` | Keep historical upgrade evidence; link current manifest procedure. |
| `knowledge/operations/mesh-host-onboarding.md` | Replace current installation instructions with manifest procedure; retain dated historical examples only. |

## Native adoption and qualification

IMA#1803 names six already shipped native functions. Their availability does
not by itself establish equivalence with every IMA override:

| Native function | IMA replacement / qualification |
| --- | --- |
| Container pressure from `own_cgroup` | Replace `DocumentRunner.host_pressure`; compare container memory, CPU/throttling thresholds, unknown readings and admission behavior (E3b). The #7675 witness matches memory and adds the two opt-in CPU/period keys; unknown readings never breach. |
| Process cards | Replace custom workflow assembly through public canon/Process admission; qualify verifier and refusal cases (F). |
| Priority admission lock | Remove `_tx_lock` replacement after awakened documents/roles and background fairness satisfy the measured acceptance case (E3b). The #7675 witness qualifies it as-is: no permanent document class, bounded background latency, aging. |
| `turn current`, `turn observe` | Replace copied reservation/lease interpretation where the public document suffices; retain atomic write gaps under E2b. `turn observe` now also carries the bounded `ameesh-deferral/1` block (E3b). |
| Kubernetes sandbox turns | Replace the placeholder-harness/parallel real-execution pattern only after native Job/turn/result/cost correlation is proven; E1b supplies preparation evidence, IMA#1803 owns adoption. |
| HTTP `/api/v1` | Replace agents/hosts/budget/work/turn display and ordinary control bridges now; E1/E2 cover missing directory, profile and correlated mail operations. |

The two API consumers must keep unknown cost, expired lease, stale canon,
missing sandbox correlation and missing tranche admission as distinct states.
No absence is permission, successful settlement or zero spend.

## Implementation tranches for the pilot

Each row is at most one working day. The pilot created lots #7669–#7675
(mesh-design, 17 November 2026, message #223560), all under named upstream
#5827 with consumer removal in IMA#1803. E1a is implemented by the
[public snapshot CLI](../api/canon-snapshot-v1.md); the other rows remain
delivery work, not available APIs. Later consumer requests add E2d (#6573,
implemented by #8333), E3c (#6574, proposal only, implementation lot #8334)
A (#6582, implemented by #8342), P (#6577, implemented by #8337), the
migration dry-run report (#6579, implemented by #8339), R (#6583,
implemented by #8343), E1c (#6580, manaty/ameesh#6580, implemented by
#8340) and PR (#6578, implemented by #8338). F/S are the existing qualification/stable lots. Each
upstream delivery needs a corresponding removal commit referenced in IMA#1803
before the external request closes.

| Key | Estimate | Deliverable and acceptance | Dependencies / owner of next step |
| --- | --- | --- | --- |
| E1a (#7669) | 6 h | Read-only versioned canon/directory snapshot API: immutable source SHA, scope/trust/freshness, member metadata, validation findings; stale or moving source refuses authority. Test equivalent of `read_snapshot`, including monotonic source and metadata. | Implementation lot under #5827; IMA#1803 adopts it. |
| E1b (#7670) | 8 h | Read-only prepared admission/profile API: responsible and admitted canon/placement references, live lease owner/epoch, bounded redacted budget/capacity/limits, native turn/sandbox refs where available. Atomic consistent read; no claim, reservation, dispatch or credentials. Missing correlation explicitly unavailable. | Implementation lot under #5827 after E1a; IMA#1803 removes preparation/observation SQL. |
| E1c (#6580) | 4 h | Governed public canon-refusal write: `ameesh canon refuse --reason TEXT [--commit SHA]` records an `invalid` state for the host that runs it, with a bounded, secret-free reason; the state blocks every new claim on that canon until a successful `ameesh canon sync` replaces it; the call is idempotent, and an optional full refused commit is kept apart from `last_good_commit`. **Implemented by #8340** as contract revision 1.12 ([canon refusal](../api/module-contract-v1.md#public-canon-refusal-112-8340)); witnesses in `tests/test_8340_canon_refusal.py`. | IMA#1803 replaces its `canon_sync.record_state(..., 'invalid', ...)` call with the CLI; no IMA migration. |
| E2a (#7671) | 6 h | Generic typed mail submission/history API with actor/scope checks, durable request IDs, scoped idempotency keys and bounded batch submission. Same key/same content reuses ID; conflicting content refuses; concurrent retries make one durable submission. Module-owned payload supports role/document/Memoria without core-specific business fields. | Implementation lot under #5827; IMA#1803 removes submit/history SQL. |
| E2b (#7672) | 8 h | Lease-fenced pending/reply operation tied to request ID and native turn: expired/replaced epoch refuses; one correlated reply across concurrent retry; native spend marker stays authoritative. Test request/reply races and visibility. | Implementation lot under #5827 after E2a; IMA#1803 removes reply and lease SQL. |
| E2c (#7673) | 6 h | Core-owned interrupted-request replay at drain/shutdown: exact captured lease/request set, already-replied exclusion, preserve unrelated pending prompt, no duplicate reservation or response. Test shutdown before Popen publication and concurrent reply. **Implemented by #7673** as a core behavior of contract revision 1.4 ([interrupted requests](../api/module-contract-v1.md#requests-of-an-interrupted-turn-e2c-7673)); no runner hook or callback. | Implementation lot under #5827 after E2b; IMA#1803 removes HandoverRunner. |
| E3a (#7674) | 6 h | Declarative per-agent fresh-session/context and bounded refusal-retry policy consumed by the runner; no AgentWorker subclass. Test document and ordinary agent side by side, optional harness handling, failure delay and graceful shutdown. **Implemented by #7674** as the Agent/Persona session fields in the [public contract](../api/module-contract-v1.md#declarative-per-agent-session-policy-e3a-7674); IMA#1803 removes the session monkey patch. | Implementation lot under #5827; IMA#1803 adopts the card fields and removes the monkey patch. |
| E2d (#6573) | 3 h | Production connection for the E2 Python surfaces: `ameesh.public.connect()` opens the deployment's usual configuration (no DSN argument), checks the schema immediately, returns an opaque `Db` for `ameesh.public.mail`, and refuses with `DatabaseUnavailable`/`SchemaRefused`/`UnsupportedStorage` without exposing the DSN or a password. **Implemented by #8333** as contract revision 1.6 ([public connection](../api/module-contract-v1.md#public-production-connection-16-6573)); external witness in the [public fixture witness](../../examples/fixtures/public-fixture-witness) and `tests/test_8333_public_connect.py`. | IMA#1803 replaces its `config`/`db.connect`/`require_schema` imports; no new IMA migration. |
| A (#6582) | 4 h | Public action-connector surface: `ameesh.public.connectors` exports the frozen `Action`, `Outcome` and `NotSentError` protocol objects, the closed states/classes/dedupe vocabularies and `ACTION_CONNECTORS`; an installed module declares its connector mapping and the registry discovers it, refusing invalid declarations without any internal import. No protocol, gate or schema change. **Implemented by #8342** as contract revision 1.8 ([action connectors](../api/module-contract-v1.md#action-connectors-declared-by-an-installed-module-18-8342)); the public-only module-connector bench and `tests/test_7665_module_contract.PythonConnectorsContractTest` are the witnesses. | IMA#1803 replaces `from ameesh.connectors import Outcome` with the public import; no connector code change. |
| P (#6577) | 4 h | Public redacted read of the declared account profiles: `ameesh accounts profiles --json` lists every profile (name, kind, harness, `env`/`key_env` names, reserve, billing account), sorted; no key value, key file path or content; an unreadable profile is refused with a closed reason without hiding the list. **Implemented by #8337** as contract revision 1.9; witnesses in `tests/test_8337_account_profiles.py`. | IMA#1803 replaces `accounts.parse(cfg.accounts)` in its runtime-admission and reviewer-preparation checks. |
| R (#6583) | 4 h | Bounded read-only fleet view of the account reservations still counted: `ameesh reservations --json` / `GET /api/v1/reservations` lists every `open` row and every `kept` row of the envelope's current calendar day, oldest first, with agent, billing account, profile name, amount, state, period, creation instant, age, envelope flag and period reset; no `spend_key`, key, path or DSN; an unreadable row is reported with a closed reason without hiding the others; one repeatable-read read-only snapshot, no reservation opened, settled or released. **Implemented by #8343** as contract revision 1.11; witnesses in `tests/test_8343_account_reservations.py`. | IMA#1803 removes the `credit_snapshot` query on `account_reservations`; `/budget` and `/agents` already cover the adjacent account and blocked-agent reads. |
| E3b (#7675) | 6 h | Native pressure/admission qualification and structured bounded deferral observation. Native `own_cgroup`, budget/ledger and lock stay authoritative; fairness/latency witness for awakened document vs role/background, ambiguous accounting retained. Add missing declarative policy only if witness proves a gap; no arbitrary runner callback or lock replacement. **Implemented by [#7675 / #5868](https://github.com/manaty/ameesh/issues/5868)**: `ameesh-deferral/1` (closed reasons, bounded, no free text) on list/show/turn observations; opt-in `max_cgroup_cpu_used` and `max_cgroup_throttled_periods` for IMA's CPU/period gates (no defaults); the native admission lock qualified as-is, no document class. See the [qualification note](7675-native-pressure-admission-qualification.md). | Implementation lot under #5827; IMA#1803 removes pressure/scheduling subclasses after adopting revision 1.2. |
| F | 8 h | External module witness and public fixture/setup/observation protocol; contract element → test matrix, deliberate break witness. Include fixture use for Process, usage, refusal and mail races without importing upstream tests. **Implemented by [#7701 / #5902](https://github.com/manaty/ameesh/issues/5902)**: `ameesh.public.fixture` in contract revision 1.5 ([public fixture](../api/module-contract-v1.md#public-fixture-for-a-consumer-owned-database-15-7701)); the external [witness](../../examples/fixtures/public-fixture-witness) creates/migrates/drops its own database and covers Process admission/refusal, Pipeline adoption, usage, mail refusals and races, and the deliberate-break comparator. | Existing #7665 / #5857 and #7701 / #5902; IMA#1803 adopts the fixture and removes its `PgTestCase`/synthetic-SQL dependencies. [#8341 / #6581](https://github.com/manaty/ameesh/issues/6581) adds the agent qualification states (`Fixture.agent`/`agent_state`, revision 1.7: lease, canon, placement, spend marker, reservation, cost), so the qualification scripts and integration tests drop their synthetic core SQL. |
| E3c (#6574) | 3 h | Documented per-worker preview publisher setting (`peek: false` Host policy or runner configuration), read at worker startup, default unchanged (publisher active), a non-boolean value refused, no `AgentWorker` subclass; `Runner.peek()` and the `/peek` read API unchanged. **Proposition, non disponible** at 1.72.0: no documented setting exists and IMA subclasses `AgentWorker` to cut the publisher. | Implementation lot #8334 (upstream [#6574](https://github.com/manaty/ameesh/issues/6574)); next step mesh-design. |
| E4 (#6584) | 8 h | Application alerts through the notifier: the IMA Application card declares its alert types and team routes (`alerts:` block); `ameesh notify raise`, `resolve` and `list` (`--json`) opens one alert per (application, type, deduplication key), resolves it once, and the `alerts` pass delivers it with the mesh's routes, avatar first (#7634), rate limit per human, retries and `notified` ledger. Unknown type or route refused at raise, mesh alerts untouched. **Implemented by #8344** (migration 0612, [operations](../EXPLOITATION.md#alertes-dapplication--ameesh-notify-raiseresolvelist-8344)). The public surface is the CLI; a Python binding under `ameesh.public` is not part of this delivery. | IMA#1804 declares its card block, runs `ameesh notify` with routes for its humans, then removes its Slack sends and its interim mail to the avatar. |
| S | 6 h | Deterministic weekly stable designation, immutable consumer manifest and one unchanged witness across consecutive stables; all current checks derive from manifest. | Existing #7666 / #5858; publication and adoption retain their own gates. |

For #7665, the minimum contract coverage matrix is:

| Contract element | Required witness |
| --- | --- |
| Compatibility | Compare released baseline and candidate inputs/results; deliberate removed field or changed required argument fails the expected test. |
| CLI | Installed executables, documented JSON schemas, status/refusal behavior; no dispatcher imports. |
| HTTP | All documented methods/paths and schemas, identity/visibility, pagination, SSE resume/reset/delayed behavior and idempotency where promised. |
| Module | Real external distribution, discovery/refusals, descriptor and capability checks, canon-scoped activation, CLI help with no DB, audit/store named operations; default and bare test profiles. |
| Runner | Supported harness/config/admission-file protocol and native turn context/observation; add E1–E3 witnesses only when those surfaces are implemented. |
| Named SQL views | Explicit admitted columns, result types/meaning, column ACL retention and read-only consumer behavior. |
| Fixture | Installed consumer can create/tear down its isolated witness using only the fixture's published protocol, with no core table writes or `tests.support`. |
| Stable/pin | One consumer manifest, actual executable/image identity, unchanged witness on two consecutive stable versions (with S). |

No code removal, migration, deployment or release is performed by this document.
