# External module public contract, version 1

Contract revision: **1.2, 17 November 2026** (additive E1a snapshot, E1b
observation, E2a mail submission and history, E3a session policy, E3b bounded
deferral observation, module schema and migrations snapshot). Scope: an
installed external module and its client processes. Reference:
[#5827](https://github.com/manaty/ameesh/issues/5827),
first document tranche [#5856](https://github.com/manaty/ameesh/issues/5856).
Revision 1.1 adds the E1a canon/directory snapshot and the read-only agent
observation (tranche E1b; CLI `ameesh agent observe`, HTTP
`/api/v1/agents/{agent}/observation`); it is additive to revision 1.0.
Revision 1.2 adds the Python mail submission and history surface (tranche E2a,
#7671) — with the #7706 security correction that reserves the core's sender
identities and confines module data to the imposed `payload.module` namespace —
the declarative per-agent session policy (tranche E3a, #7674), the bounded
deferral observation and native qualification (tranche E3b, #7675) and the
module schema/migrations snapshot; all are additive to revision 1.1.
The [IMA mapping](../design/ima-public-contract-mapping.md) records the concrete
consumer and the missing surfaces. This document defines the supported boundary;
it does not claim that the remaining adapters have already been implemented.

## Compatibility and availability

No public contract element breaks in a minor release. A removal or incompatible
change requires a major release, with deprecation announced at least one
released version beforehand. The announcement names the replacement, affected
element and earliest removal version. A patch release preserves the same
contract. Security checks and admission checks remain in force; compatibility
never promises that an unauthorized operation succeeds.

An additive field or endpoint is compatible. Clients ignore unknown response
fields, select named SQL columns, preserve opaque identifiers and check the
advertised schema version. A previously valid required field, argument, result
type, identity binding or idempotency guarantee cannot silently change. Missing
and null retain their documented meanings. A new required request field, a
narrower accepted input, or a changed side effect is a breaking change.

Revision 1.2 is the security revision of the mail surface before its first
release, so it tightens admission before any installed client can rely on the
wider input. A caller written against the pre-release 1.1 draft that already put
its data under `payload.module` is source-compatible. A caller that wrote a bare
top-level module key moves it under `payload.module`: the 1.2 surface refuses the
old spelling before any deposit (`InvalidSubmission`) and names the namespace in
the message. Rows already stored by a pre-release caller remain readable through
`history` unchanged; the revision performs no data rewrite. A consumer still
pinning a pre-release draft applies this migration before enabling 1.2.

The version of this document is independent of the package version. Its scope
is the surfaces listed below, not every importable Python name. A future
surface becomes available only in a released version that implements it and
lists it in this contract. Clients check that version before enabling it.
In particular, the baseline here is ameesh commit
`aeca7e2ebca4994d477cc9e2a7cb2970a4fdf007`; an open PR is not an installed feature.
The revision 1.1 elements (#7669 E1a, #7670 E1b) and revision 1.2 elements
(#7671 E2a, #7674 E3a, #7675 E3b) are available only in a released version
that carries those lots; at the 1.0 baseline they are absent, and a consumer
must still treat their absence as "unknown", never as permission.

External modules follow the [weekly stable channel](stable-channel.md)
([#7666 / #5858](https://github.com/manaty/ameesh/issues/5858)) through one
pin file, `ameesh-stable.json`, from which image, tests and diagnostics derive.
A deprecation announced for stable followers must have been part of at least
one published stable before its removal. Construction tags cut twice a day do
not count toward that notice. Until the first stable is published, keep an
immutable upstream tag and peeled commit in that same single file. Never track
a moving construction branch as a stable version.

## CLI boundary

Call the installed executable as a subprocess, rather than importing its
dispatcher. The public surface is the documented command and options in that
release's `--help`. These entry points cover the module integration:

| Entry point | Purpose and result contract |
| --- | --- |
| `ameesh --version` | Installed package version; it does not prove the running process uses that installation. |
| `ameesh modules --json` | Discovered module descriptions and refused entries. Additive since 1.2: `schema`, `requires`, `migrations` (`applied` and `schema_actual` when the database is readable). A refused entry is not an active provider. |
| `ameesh module list` and `ameesh module NAME …` | Installation evidence and dispatch to the named module's CLI. `list` is reserved. The module owns the arguments after its name. |
| `ameesh canon check`, `ameesh canon sync` | Validate and admit configured canon through the core's authority checks. |
| `ameesh canon snapshot --expect-commit SHA --json` | [E1a immutable canon/directory snapshot](canon-snapshot-v1.md), available in the release containing #7669 / #5862. No database access or registry admission; profile/lease preparation remains E1b. |
| `agent-runner --agents-file FILE` | Dynamic admission filter; [format and withdrawal](../RUNNER-ADMISSION.md). It narrows canon admission, never grants authority. |
| `ameesh turn current --json [--expect-turn ID]` | Current turn's versioned authorization document, with identity and database recheck. `--local` omits the database recheck and must not be treated as proof of a live lease. |
| `ameesh turn observe (--turn ID \| --request ID) --json` | Read-only `ameesh-turn-observation/1`: request, turn, accounting, bounded deferral and quiescence. Absence of a known cost is not zero cost. |
| `ameesh agent observe AGENT [--account-profile NAME] [--json]` | Read-only `ameesh-agent-observation/1`: admitted canon/placement references, responsible, live lease owner/epoch, declared and effective bounds, redacted provider budget and native turn/sandbox references. One repeatable-read snapshot; no claim, reservation, settlement, dispatch or canon synchronization. A missing fact is `null` plus a code in `missing`, never permission or zero cost. |
| `ameesh list --json`, `ameesh show --json` | `ameesh-agents/1`: the same bounded `deferral` block (revision 1.2, #7675), closed reasons; its absence or `null` is not permission to run. |
| `agent-mail send`, `agent-mail show` | Ordinary governed mail. The help documents flags and stdin. These commands alone do not provide IMA's atomic request/reply/deduplication protocol (E2). |
| `ameesh migrate --dry-run --additive-only` | Core migration classification; the module does not import or invoke the migration implementation. Actual application retains its operator and staging gates. |
| `ameesh action propose\|approve\|execute\|reconcile` | Governed action lifecycle; external connector availability is conditional on #7628, below. |

Use JSON only where the command offers it. Human tables, log text, Python
tracebacks and private exit details are not machine schemas. A successful exit
means the documented command completed, not that a deferred request is already
delivered or that a turn is authorized. Preserve the command's refusal and retry
semantics; do not convert an unavailable observation into permission.

## HTTP boundary

The normative method, path, input and response schemas are the checked-in
[OpenAPI v1 document](openapi-v1.json), also served at
`GET /api/v1/openapi.json`. At this revision there are 35 paths, including the
discovery document `/.well-known/ameesh`. Revision 1.0 had 34; revision 1.1
adds `/api/v1/agents/{agent}/observation`. The path count is an observation, not
a limit. Only the methods described there are public.

| Area | Public paths (prefix `/api/v1`) |
| --- | --- |
| Identity and fleet | `/me`, `/agents`, `/agents/{agent}`, `/hosts`, `/hosts/{host}`, `/projects` |
| Work and progress | `/lots`, `/lots/{id}`, `/lots/{id}/assign`, `/projects/{project}/progress`, `/projects/{project}/timeline` |
| Agent interaction | `/agents/{agent}/chat`, `/agents/{agent}/messages`, `/agents/{agent}/peek`, and the documented interrupt/pause/resume/stop methods |
| Observation | `/agents/{agent}/observation`, `/requests/{request}`, `/turns/{turn}`, `/sandbox/turns`, `/sandbox/turns/{turn}`, `/events`; `/agents` and `/agents/{agent}` also carry the bounded `deferral` block |
| Governance | `/budget`, `/budget/caps`, `/decisions`, `/alerts`, `/alerts/{key}/ack`, `/actions/{action}/approval-request` |
| Threads | `/threads/{project}`, `/threads/{project}/{lot}` |

`/agents/{agent}/observation` is built inside one `REPEATABLE READ, READ ONLY`
transaction and answers the same document as `ameesh agent observe`. It
performs no claim, renewal, reservation, settlement, dispatch or canon
synchronization, and it cannot write even the alias-resolution journal. The
projection is closed: prompts, working directories, session paths, credential
values or key paths are never selected. Provider budget amounts and the
billing-account name are included because `/budget` already publishes them.
`admission.admitted` is the profile verdict (executable mode, responsible when
the host requires one, fresh canon whose pinned commit is the last good one,
placement for the current profile); `admission.claim_predicate` is the core SQL
predicate alone. Host-local facts — declared account profiles and the
responsible policy — are read only when the agent runs on the observer's host.
Off-host the budget is refused (`foreign_host`) and `admission.admitted` is
`null` with `responsible_policy` in `missing`: another host's account named
`p1` may bill a different account, and a local `false` policy must not turn a
remote admission into `true`. `null` is unknown, never permission. Any other
fact this observation cannot supply is `null` with a stable code in `missing`;
it is never permission, admission or zero cost.

Use the server's configured authentication and visibility rules. The client's
actor, project and lease scope are not elevated by using HTTP. Retry a write
only according to its documented request/idempotency contract; observation of
`/requests/{request}` does not create an atomic mailbox reply operation.
`/events` uses SSE event IDs and `Last-Event-ID`; a `reset` requires reloading
state, and `delayed` means observation is delayed. Neither is a completed turn.
Follow the OpenAPI bounds, pagination and error schemas, including `Retry-After`
where supplied. Importing `ameesh.serve` is not the HTTP client interface.

At this baseline, `ameesh serve` is a loopback-only local server with a
configured human identity and a bearer token from its private token file.
Human-action POSTs require `Idempotency-Key`. It is not a remote multi-tenant
module service or an independently scoped agent credential. Do not give a
module the human token as a shortcut to authorization; E1/E2 must specify
their authenticated caller scope when implemented. The discovery and OpenAPI
documents are the two unauthenticated discovery surfaces.

## Installed Module boundary

An external Python distribution declares an `ameesh.modules` entry point whose
name equals its `NAME` and whose target provides the descriptor interface. It
is installed code; a canon selects a name, never an executable import path.
`ameesh.modules.registry.Module` is the read-only discovery record, not a runner
base class or a required superclass of the installed implementation.

| Element | Version 1 baseline contract |
| --- | --- |
| `NAME` | Unique installed name, matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; reserved or duplicate names are refused. |
| `SCHEMA`, `MIGRATIONS`, `REQUIRES` | Optional storage facts, additive since 1.2. `SCHEMA` is the logical schema name (the module name by default); `MIGRATIONS` is a folder of `NNNN_name.sql` files **inside the installed package**, or None; `REQUIRES` lists modules whose schema must exist first. A module without `MIGRATIONS` declares no schema. Migration SQL is installed code reviewed as sensitive SQL; the canon never supplies an executable path. The runner derives `<core schema>__<module>`, applies the module's migrations in the core migration transaction and records them in `module_schema_migrations`. Every `SECURITY DEFINER` routine must set `search_path` in its own definition: the runner refuses a routine without it, and refuses a migration whose dynamic SQL or uncertain parse prevents that proof. |
| `descriptor(options=None)` | Returns exactly `types`, `schemas`, `rules`: unique type-name list, schema mapping, list of rule mappings. The baseline requires an external lifecycle module to have a type. |
| `CAPABILITIES` | Optional unique names among `lifecycle`, `cli`, `audit`, `workspaces`, backed by the corresponding implementation. |
| `main(db, argv, *, actor)` | Optional CLI callback. Receives the resolved actor and a core-owned connection for ordinary dispatch. Help can receive `db=None`, `actor=""`; do not query in that case. Return an exit status. |
| `audit(store, project)` | Optional callback for active project modules; returns findings through the existing module audit protocol. Host-only behavior is reserved to the core's always-active modules. |
| `ameesh.modules.registry.discover(*, builtin=True, canon=None)` | Discovery record with `modules`, `refused`, `names()` and `get(name)`; each Module offers `as_dict()`. Discovery errors never authorize an entry. |

The discovery record's public fields are `name`, `version`, `source`,
`capabilities`, `cli`, `storage`, `distribution`, `entry_point`,
`descriptor_sha256`, `card_ref`, and — additive since 1.2 — `schema`,
`migrations`, `requires`. `storage` on the Python record is a tuple of
types; `as_dict()` represents it as `{"types": [...]}`. These are different
representations of the same declared types.

Configure selection under `extensions.ameesh.modules`,
`extensions.ameesh.teams.TEAM.modules`, or
`extensions.ameesh.projects.PROJECT.modules`. The narrowest named selection
wins its options. Discovery is not project activation. Canon compilation and
database admission remain the core's responsibility. `AMEESH_MODULES=bare`
is a test profile that removes built-ins, retains installed entry points and
must not be used to configure a live host.

The supplied `store` is a capability, not a license to access its `.db` member.
Version 1 admits these existing `ModuleStore` operations for module lifecycle
data: `enabled(project, module)`, `admits(project, kind)`,
`latest(item_id, module, kind)`, `milestones(item_id, limit=100)`, and
`record(item_id, module, kind, subject, payload, *, actor, binding_id=None)`.
The core continues to validate the project's module policy. Never call
`replace` to rewrite admitted policy from a callback. No part of this contract
permits raw SQL against core storage through the `db` callback argument.

`workspaces` is discoverable at the baseline but has no external callback
schema specified here; a consumer needing it records that gap against #5827
before relying on the built-in development module's implementation.
Likewise, `OWNS_CONNECTION` is used internally by development tooling, not a
public escape hatch for external modules.

External action connectors are tracked by
[#7628 / #5818](https://github.com/manaty/ameesh/issues/5818):
[`ACTION_CONNECTORS`, operation classes and test bench](https://github.com/manaty/ameesh/blob/9755b4f3265a19afe6105e687be88edaab4ab459/docs/design/module-action-connectors.md)
are specified in [PR #5836](https://github.com/manaty/ameesh/pull/5836), not present
at this document's baseline. Connector-only modules and `action_classes`
become available when that implementation is released. A consumer must not
infer their availability from this contract's revision alone.

## Runner extension boundary

Use configuration and executable harness adapters through the documented
runner CLI and the [harness descriptor design](../design/descripteurs-de-harnais.md).
That design describes the existing integration, rather than making the
internal adapter classes a public Python inheritance interface.
The core owns leases, reservation/settlement, delivery, scheduling, admission,
shutdown and restart. The extension does not replace `Runner`, `AgentWorker`,
their methods, a connection driver, or `_tx_lock`.

Existing declarative integration includes canon Agent/Placement admission,
the `--agents-file` filter, Process cards, resource policy
`pressure_scope=own_cgroup`, account budgets, current-turn authorization,
turn observation, and the native Kubernetes sandbox execution path. These
features retain their existing gates and configuration. A command that merely
starts an unrelated Kubernetes Job does not create a native sandbox turn.

### Declarative per-agent session policy (E3a, #7674)

An `Agent` or `Persona` card may declare its own session and retry policy at
the top level. `canon sync` carries the declaration to the agent's admitted
record; the runner consumes it, so a module needs no `AgentWorker` or
`Runner` subclass for these settings:

| Key | Accepted value | Effect |
| --- | --- | --- |
| `fresh_session` | boolean | `true`: every turn opens a new harness session; no previous session id is resumed. Default `false` (existing resume behaviour). |
| `session_policy` | `par-lot`, `taille`, `jamais` | Same vocabulary as `ameesh set`. `jamais` disables rotation; `fresh_session: true` implies it. |
| `context_max_tokens` | integer ≥ 0, or a token count with `k`/`M` suffix | Per-agent context cap; `0` disables the cap, including the harness default. |
| `failure_wait_min` | duration (`60s`, `1m`, seconds) | Floor on the delay between an ordinary failed or refused turn and the next attempt; the executor's `failure_backoff_max` remains the ceiling. A durable provider rate-limit deadline remains authoritative. |

A declared value takes precedence over the agent's imperative setting
(`ameesh set`) and the executor default; a key absent from the card leaves the
existing resolution unchanged. Attribute all four keys to the card, not to the
release's package version: enable them only in a release that lists them here.
An unreadable value or a contradictory `fresh_session: true` with a rotating
`session_policy` is a card error, so the agent is not claimable. The wait after
a failure is interrupted by shutdown and draining; graceful stop remains the
core's responsibility. The optional `harness` argument of the internal
`context_max_tokens()` helper is not frozen by this contract: a declared cap
applies to every harness.

IMA's per-document fresh-session policy is covered by this declarative
surface. The remaining gap is the lease-bound replay requirement (tranche
E2c), inventoried in the mapping and not available as a runner hook. Until it
is delivered, IMA's remaining adaptation stays tracked by its
[#1803](https://github.com/thepackengers/issue-manager-agent/issues/1803),
rather than acquiring compatibility guarantees for private methods.

### Bounded deferral observation and native qualification (E3b, #7675)

Revision 1.2 (#7675, E3b) adds two **optional** resource-policy keys for a
bounded `own_cgroup`: `max_cgroup_cpu_used` (CPU used against the `cpu.max`
quota) and `max_cgroup_throttled_periods` (share of CPU periods with a
throttled task). Neither has a default: absent means unwatched, and no existing
Host fiche changes verdict. The corresponding observation is the bounded
`ameesh-deferral/1` block ([contract](deferral-observation-v1.md)), whose
closed reasons replace private diagnostics parsing. The
[qualification note](../design/7675-native-pressure-admission-qualification.md)
records the parity witness, including the retained authority of accounting
when the ledger is ambiguous. The native admission lock is qualified as-is: no
permanent document priority class and no `_tx_lock` replacement are part of
this contract.

## Mail submission and history (Python)

Contract 1.2 adds the first write surface for a module that already holds the
deployment's database identity: `ameesh.public.mail`. This is the replacement
for direct `agent_mailbox` submission and history SQL (mapping E2a); the
governed `agent-mail` commands and the HTTP boundary above remain the public
surfaces for an ordinary caller. This Python surface does not authenticate a
remote client: the database credential remains the trust boundary. A process
that runs under a mesh runner must use the identity that runner verified; a
module service with no declared runner identity keeps the actor it names, as
long as that name is not one the core reserves (below). Mediated executor
connections are refused — their contract is separate.

Enable the surface only when `ameesh.public.CONTRACT_VERSION` is `"1.2"` or a
compatible later minor. A consumer written against the 1.1 draft applies the
`payload.module` migration above before treating 1.2 as compatible.

| Element | Contract |
| --- | --- |
| `Submission(recipient, body, kind="notify", payload=None, work_item_id=None, thread=True, idempotency_key=None)` | One typed message. `recipient` is a registered agent; `kind` is `request`, `reply`, `notify` or `event`; `payload` is the module-owned JSON object carried under `payload.module`; `work_item_id` optionally links a lot; `thread=False` keeps the content out of the readable project thread (the mailbox row and this history remain); `idempotency_key` is the client key described below. |
| `submit(db, actor, submission) -> Receipt` | Deposit one message as `actor`. The sender is always the actor; another sender cannot be named, and a core-owned sender identity is refused. |
| `submit_many(db, actor, submissions) -> BatchResult` | Deposit up to 50 submissions in input order. Each item commits or is refused on its own; a refusal appears in its `SubmissionResult` and does not hide later outcomes. A larger batch is refused before anything is deposited. |
| `find(db, actor, recipient, idempotency_key) -> Receipt \| None` | The durable receipt already deposited under this `(actor, recipient, key)`, or `None`. |
| `history(db, actor, *, peer=None, after_id=None, limit=100, since_ts=None, until_ts=None, kind=None) -> HistoryPage` | Bounded history of the messages where `actor` is sender or recipient, oldest first by durable id, strictly after `after_id`. `peer` restricts the page to the two-party conversation with that agent; `kind` filters the nature; `since_ts`/`until_ts` bound the creation time. `limit` is between 1 and 200. |
| `Receipt(id, actor, recipient, kind, created_ts, reused)` | `id` is the durable mailbox row id, stable across retries; `request_id` is that id for a `request` and `None` otherwise; `reused` says this call returned an existing deposit. |
| `HistoryEntry` and `HistoryPage` | One message (`id`, `sender`, `recipient`, `body`, `kind`, `payload`, `work_item_id`, `created_ts`, `delivered_ts`) and a page (`entries`, `next_cursor`, `has_more`). The stored `payload` keeps the module object under `payload.module`; a 1.1-draft row is returned as stored, never rewritten. `next_cursor` is the last returned id, or the supplied cursor for an empty page; pass it back as `after_id`. `delivered_ts` is observation only and never a lease claim. |
| `SubmissionResult` and `BatchResult` | Ordered batch outcomes (`index`, `receipt`, `error`, `detail`) and the batch (`results`, `ok`). The value objects expose `as_dict()` for JSON clients. |
| Errors | `InvalidSubmission` (code `invalid`), `IdempotencyConflict` (`conflict`), `ScopeRefused` or `RecipientRefused` (`refused`), `UnsupportedStorage` (`unsupported`). A reserved sender identity raises `ScopeRefused` before any deposit. A batch reports the same codes in `SubmissionResult.error`. |

### Reserved sender identities

The public surface is for a module's own identity, never for the core's. The
following actors are refused before the declared-identity check and any deposit,
with `ScopeRefused` (code `refused`); the comparison is case-insensitive, and it
checks both the supplied spelling and its resolved primary:

* the system sender `ameesh` (`work.SYSTEM_SENDER`) — system events, CI
  notifications, review and deep-analysis requests;
* the watch sender `veille` (`watch.SENDER`) — periodic watch reports;
* the unbound actor `inconnu` (`work.UNKNOWN_ACTOR`);
* every Agent/Persona of the configured canon whose card declares a role: the
  core reads the titles from the canon and never writes a mesh's role names
  (#7637/#7660). This covers the governance identities of every mesh —
  orchestrator, auditor, bureau, pilot, composer, watch, reviewer — including a
  role added later or declared by another canon. `reserved_actors(db)` returns
  the resolved names;
* every `human:*` and `chat-*` name (the core's human and chat prefixes).

`all` (broadcast) remains `InvalidSubmission`. A module service with no declared
runner identity may submit under any other non-reserved service name; the
database credential remains its trust boundary. The exported `RESERVED_ACTORS`
and `RESERVED_ACTOR_PREFIXES` constants name the static names and the prefixes;
the canon part is read per deployment. When no canon is configured or it cannot
be read, only the static names and prefixes apply.

Idempotency is scoped to `(actor, recipient)`. The content is the body, the
kind, the module payload and the work item. The same key with the same content
returns the same `id` with `reused=True`; the same key with another content
raises `IdempotencyConflict`; concurrent retries end on one durable row. The
core stores the key in `payload.idempotency_key`; the unique index of
migration 0497 is the authority, so the guarantee holds across processes, not
only within one caller.

The module payload is confined to `payload.module`, a JSON object (at most
256 KiB serialized). The namespace belongs to the calling actor: the module
submits under its own identity, so `payload.module` is already scoped by that
name. Admission is an allow-list: the only admitted top-level
keys are `module` and `attribution`. Every other top-level key is core-owned and
refused before any deposit, so a core control marker added in a later release is
closed to modules by default instead of becoming open through a deny-list that
did not know it yet. The exported `ADMITTED_PAYLOAD_KEYS` states the list;
`RESERVED_PAYLOAD_KEYS` lists the known core markers for diagnostics only.
Core control markers — delivery and accounting (`idempotency_key`, `report`,
`report_fingerprint`, `urgent`, `ack`, `info`, `cc`, `broadcast`, `radar`,
`ci_passif`, `human`, `human_account`, `verdict`, `arbitration`, `reassigned`,
`request_expires_ts`), review (`review_designation`, `review_request`),
decisions (`decision`, `decision_answer`) and operations (`pass`, `ci`,
`staging_actif`) — are never available at the top level. A key with one of
those names is ordinary data when it is nested inside `payload.module`: it does
not select a core delivery, review, decision or accounting path.
`payload.attribution` is the one admitted core key: it is validated with the
closed caller-attribution schema and remains data, never admission, budget or
authorization.

Published bounds: body at most 64 KiB UTF-8, serialized payload at most
256 KiB, 50 submissions per batch, 200 history entries per page. Bodies must be
readable text (the thread rule). Signatures, request deadlines, urgent wake-up,
acknowledgements and copies are not part of this surface; a consumer that needs
them uses the governed command or HTTP boundary.

## Named SQL read views

For a trusted deployment that already has database access, these named views
are admitted read surfaces. Use explicit columns and existing grants. This
document grants no SQL role, bypasses no column ACL and adds no access for a
remote HTTP client. Prefer HTTP for a client without a database identity.

| View | Admitted columns / meaning |
| --- | --- |
| `agent_mesh_overview` | `name`, `host`, `harness`, `status`, `status_text`, `last_seen_ts`, `last_turn_ts`, `lease_expires_ts`, `unread`, `passive_unread`: current fleet and delivery observation, not a lease claim or permission to act. |
| `mesh_keys` | `name`, `holder`, `public_key`, `public_key_fingerprint`, `key_role`, `key_updated_at`, `key_revoked_at`: public-key lookup; retain conflict and revocation semantics. |
| `mesh_approvals_status` | `id`, `unconsumed`, `unexpired`, `key_known`, `key_live`, `key_matches`, `expires_ts`, `key_is_owner`: observation of an approval's conditions, never a substitute for core signature verification or consumption. |

Names and selected columns above are covered by the same compatibility rule.
Other columns on these views, base tables, triggers, SQL functions and joins
through `agent_mailbox`, `agent_registry`, `spend_pending` or budget tables are
internal. `SELECT *` makes additional internals a dependency. All writes to
core data go through an admitted command/API/callback operation, never through
an updatable view or a caller-built transaction on core tables.

## Consumer qualification and delivery

[#7665](https://github.com/manaty/ameesh/issues/5857) supplies the external
witness, public fixture interface and compatibility CI. Existing evidence is
in `test_l84_serve`, `test_l98_modules_registry`, `test_l104_bare_canon`,
`test_l2894_turn_observe`, and `test_l2894_turn_context`; it is not yet the complete
external-module qualification required by #5827. The exact follow-up coverage
is listed in the mapping. No test imports `tests.support` across the package
boundary: the forthcoming fixture kit or a consumer-owned process/database
fixture replaces that dependency.

Validate the same consumer unchanged against two consecutive stable versions.
A contract failure belongs in upstream CI before release. Delivery of an
extension is complete for the external request only when IMA removes its
temporary implementation and records that removal in #1803. This document
alone does not close the umbrella request.
