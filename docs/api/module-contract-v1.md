# External module public contract, version 1

Contract revision: **1.13, 28 November 2026**. Scope: an
installed external module and its client processes. Reference:
[#5827](https://github.com/manaty/ameesh/issues/5827),
first document tranche [#5856](https://github.com/manaty/ameesh/issues/5856).
<!-- The revision number and date above are assigned by the packet composer at
composition; a lot that changes the contract adds a fragment under
docs/api/module-contract-v1.d/<lot>.md and does not edit this header or the
revision list. See docs/design/contract-revision-fragments.md. -->
Revision 1.1 adds the E1a canon/directory snapshot and the read-only agent
observation (tranche E1b; CLI `ameesh agent observe`, HTTP
`/api/v1/agents/{agent}/observation`); it is additive to revision 1.0.
Revision 1.2 adds the Python mail submission and history surface (tranche E2a,
#7671) — with the #7706 security correction that reserves the core's sender
identities and confines module data to the imposed `payload.module` namespace —
the declarative per-agent session policy (tranche E3a, #7674), the bounded
deferral observation and native qualification (tranche E3b, #7675) and the
module schema/migrations snapshot; all are additive to revision 1.1.
Revision 1.3 adds the lease-fenced pending request and correlated reply
operations to the same Python mail surface (tranche E2b, #7672); it is additive
to revision 1.2.
Revision 1.4 adds no name: the core puts the requests of a turn it interrupted
back in the queue, under the captured lease (tranche E2c, #7673); it is
additive to revision 1.3.
Revision 1.5 adds `ameesh.public.fixture` (tranche F2, #7701): an installed
consumer creates, migrates, observes and drops its own disposable database
without `tests.support` or a private path; it is additive to revision 1.4.
Revision 1.6 adds `ameesh.public.database` (#6573): the production connection
for the same public Python surfaces, opened from the usual configuration with
an immediate schema check and no internal import; it is additive to revision
1.5.
Revision 1.7 adds the fixture's agent qualification states
(`Fixture.agent`/`Fixture.agent_state`, #8341, manaty/ameesh#6581): a module
test puts an agent in a lease, canon, placement, spend-marker, reservation or
cost state through the core's own code, without writing a core table; it is
additive to revision 1.6.
Revision 1.8 adds `ameesh.public.connectors` (#8342, manaty/ameesh#6582): an
installed module declares its action connectors under `ACTION_CONNECTORS` and
imports the protocol objects (`Action`, `Outcome`, `NotSentError`) from the
public surface instead of `ameesh.connectors`; the governed gate, its policy
and the connector protocol are unchanged, so the revision is additive to 1.7.
Revision 1.9 adds `ameesh accounts profiles --json` (#8337,
manaty/ameesh#6577): the redacted list of the host's declared account
profiles, so a consumer checks its account layout without importing
`accounts.parse`; it is additive to 1.8.
Revision 1.10 adds `ameesh migrate --dry-run --json` (#8339,
manaty/ameesh#6579): one versioned `ameesh-migrate-dry-run/1` object on
standard output with the pending core and module migrations (checksum and
non-additive reasons), the applied migrations modified since application, and
the database versions unknown to this code. It is additive to 1.9: the text
dry-run, the application path and the 0/3/1 exit codes are unchanged.
Revision 1.11 adds `ameesh reservations --json` and
`GET /api/v1/reservations` (#8343, manaty/ameesh#6583): the bounded,
read-only fleet view of the account reservations still counted (`open` and
`kept`), so a consumer stops querying `account_reservations` directly; it is
additive to 1.10 and changes no reservation, envelope or admission path.
Revision 1.12 adds no Python name: it makes the governed canon-refusal write
public as the CLI `ameesh canon refuse` (#8340, manaty/ameesh#6580), so an
installed module that has refused a canon no longer imports
`ameesh.canon_sync`; it is additive to 1.11.
Revision 1.13 adds `ameesh prices --json` (#8338, manaty/ameesh#6578): the
read-only `ameesh-prices/1` document of the effective price schedule (per
model, the per-harness ceiling of an unknown model, and the schedule's source),
so a consumer stops importing `cost.load_prices`/`cost.max_price_of`; it is
additive to 1.12 and changes no pricing, reservation or accounting path.

The [IMA mapping](../design/ima-public-contract-mapping.md) records the concrete
consumer and the missing surfaces. This document defines the supported boundary;
it does not claim that the remaining adapters have already been implemented.

Amendment to revision 1.9 (#8457, manaty/ameesh#6666): in
`ameesh-account-profiles/1`, `env`/`key_env` may be `null` when the name is not
trusted (a harness descriptor's default, that default with a short `_` suffix,
or a profile declaring `public_names: true`), and a refused unknown harness is
never echoed; both fields were already nullable, so no field or reason changes.

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
Every revision later than 1.0, as listed above, is available only in a
released version that carries the lots it names; at the 1.0 baseline those
elements are absent, and a consumer must still treat their absence as
"unknown", never as permission. The frozen
[surface](module-contract-v1.surface.json) and its
[coverage table](module-contract-v1-coverage.md) carry the elements of every
revision up to the one they freeze, from the release that implements them, and
freeze the latest revision listed above; the surface keeps the 1.0 commit as
its comparison point.

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
| `ameesh canon refuse --reason TEXT [--commit SHA] [--canon ID] [--json]` | Revision 1.12 (#8340): record a refused canon state for the host that runs the command. The reason is one line, bounded and secret-free; a secret-looking reason is refused, never stored. `--commit` pins the full commit the module inspected; `--canon` selects a configured canon (default otherwise). Exit 0 means the refusal is recorded (or already recorded identically); `--json` reports the resulting state. It has no `--host` option and never writes another host. |
| `ameesh canon snapshot --expect-commit SHA --json` | [E1a immutable canon/directory snapshot](canon-snapshot-v1.md), available in the release containing #7669 / #5862. No database access or registry admission; profile/lease preparation remains E1b. |
| `agent-runner --agents-file FILE` | Dynamic admission filter; [format and withdrawal](../RUNNER-ADMISSION.md). It narrows canon admission, never grants authority. |
| `ameesh turn current --json [--expect-turn ID]` | Current turn's versioned authorization document, with identity and database recheck. `--local` omits the database recheck and must not be treated as proof of a live lease. |
| `ameesh turn observe (--turn ID \| --request ID) --json` | Read-only `ameesh-turn-observation/1`: request, turn, accounting, bounded deferral and quiescence. Absence of a known cost is not zero cost. |
| `ameesh agent observe AGENT [--account-profile NAME] [--json]` | Read-only `ameesh-agent-observation/1`: admitted canon/placement references, responsible, live lease owner/epoch, declared and effective bounds, redacted provider budget and native turn/sandbox references. One repeatable-read snapshot; no claim, reservation, settlement, dispatch or canon synchronization. A missing fact is `null` plus a code in `missing`, never permission or zero cost. |
| `ameesh accounts profiles [--json]` | Read-only `ameesh-account-profiles/1` (revision 1.9, #8337): every profile declared in this host's configuration, sorted by `harness` then `name`, with `kind` (`api_key_env`/`config_dir`), `env` and `key_env` (variable **names**, `null` when the name is not trusted, #8457: trusted means the shape `^[A-Z_][A-Z0-9_]*$` and either a harness descriptor's default key variable, that default followed by `_` and a suffix `[A-Z0-9_]{1,32}`, or a profile declaring `public_names: true`; the profile stays listed and `valid` is unchanged), `key_source` (`env`, `file` or `null`), `reserve_usd` and `billing_account`. Never a key value read by ameesh, a key file's path or content, nor a configuration directory; no database read and no credential read (a key's value is never read from the environment or a file). A value written in `env`/`key_env` is echoed only under the structural trust above: the guarantee covers a key pasted by mistake (it never carries a default variable prefix nor an attestation), not an operator who deliberately writes a key under a trusted name. `valid` combines the runner's admission of the whole `accounts` configuration with the completeness of this read: it is false as soon as any profile is refused (a non-empty `refused` implies `valid` false). An unreadable profile does not hide the others: it is listed under `refused` with `harness`/`index`/`name` (`null` unless a valid identifier; `harness` is `null` unless it is a known harness id, #8457) and a closed `reason` (`invalid_accounts`, `unknown_harness`, `invalid_profile_list`, `not_an_object`, `invalid_name`, `duplicate_name`, `invalid_profile`, `shared_configuration_dir`, `unpublishable_variable_name` — kept in the closed list, no longer emitted since #8457), never the error text. Threat model: a key pasted in place of a variable name by mistake, not an adversary (whoever writes the configuration already holds the keys); no shape test tells a key from a chosen name, hence a structural trust. A synthetic `(default)` profile is never declared, so never listed. Exit 0 whenever the document is printed. |
| `ameesh reservations [--json] [--limit N] [--account A] [--agent A] [--state S]` | Read-only `ameesh-reservations/1` (revision 1.11, #8343): the fleet's account reservations still counted against a billing envelope — `open` (a turn is in flight, returned whatever its period) and `kept` (a turn never settled; still counted until a human release or the end of its calendar day, so only a `kept` whose period is the envelope's current calendar day is returned, with the same timezone and instant as the envelope usage) — oldest first, at most `--limit` rows (default 100, maximum 500; `truncated` says older ones were not returned). Each row carries `id`, `agent`, `agent_known` (`false` when the agent no longer exists; the row is still listed because it occupies the envelope), `account_profile` (the profile name recorded for the agent, or `null`; never a key), `billing_account`, `usd`, `state`, `period`, `created_ts`, `age_s`, `account_envelope` and `period_reset_ts` (the end of the envelope's calendar day, `null` without an envelope). `observed_ts` is one instant and every age derives from it. The read opens one `REPEATABLE READ, READ ONLY` transaction and never opens, settles or releases a reservation. A row that cannot be rendered faithfully (non-finite amount, unreadable timestamp, unknown state or period) is listed under `unreadable` with `id`, `agent`, `billing_account`, `state` and a closed `reason` (`invalid_id`, `invalid_identity`, `invalid_state`, `invalid_amount`, `invalid_timestamp`, `invalid_period`); it never hides the other rows and never becomes a zero reservation. No `spend_key`, credential, path or DSN is selected. `--state` is `all` (default), `open` or `kept`; `--account` and `--agent` are exact filters. The same document is `GET /api/v1/reservations`. |
| `ameesh prices [--json]` | Read-only `ameesh-prices/1` (revision 1.13, #8338): the price schedule the accounting reads now, in USD per million tokens. `source` says where it comes from: `path` and `path_origin` (`AMEESH_PRICES` or `default`, i.e. `~/.config/ameesh/prices.json`), `status` (`absent`, `loaded`, `unreadable`, `malformed`), `kind` (`file` when at least one local entry is in force, else `defaults`), `entries` and `rejected_entries` (counts only: an invalid entry's key and value are never copied), and `sha256`/`mtime` of the content read — the schedule is re-read on every turn, so these date the version observed at `observed_ts`. `models[]`, sorted by `model`: `input`, `cached_input`, `output`, `harness` (the price family, or `null`), `origin` (`default` or `file`), `overrides_default`, and `prompt_tier`/`off_peak` (`null` unless that model is billed by prompt length or by hour at the official price). `harnesses[]`: `max_price` (component-wise maximum of the family's known prices, the reservation and accounting ceiling of a model absent from the schedule, `null` if the family has none), `unknown_model_price` with `unknown_model_basis` (`family_max`, or `schedule_max` with `fallback: true` when the family has no price — never a zero price), `default_entry` and, for `codex`, `service_tier_multipliers`. `schedule_max` is the whole schedule's maximum. An unreadable or malformed file is reported, never fatal: the defaults stay in force. No database read, no credential; exit 0 whenever the document is printed. |
| `ameesh list --json`, `ameesh show --json` | `ameesh-agents/1`: the same bounded `deferral` block (revision 1.2, #7675), closed reasons; its absence or `null` is not permission to run. |
| `agent-mail send`, `agent-mail show` | Ordinary governed mail. The help documents flags and stdin. These commands alone do not provide IMA's atomic request/reply/deduplication protocol (E2). |
| `ameesh migrate --dry-run [--additive-only] [--json]` | Core and module migration classification; `--json` returns the read-only `ameesh-migrate-dry-run/1` report, the text forms stay diagnostic. The module does not import or invoke the migration implementation; actual application retains its operator and staging gates. |
| `ameesh action propose\|approve\|execute\|reconcile` | Governed action lifecycle. From revision 1.8 an installed module supplies external action connectors through `ACTION_CONNECTORS` and `ameesh.public.connectors`, below. |

Use JSON only where the command offers it. Human tables, log text, Python
tracebacks and private exit details are not machine schemas. A successful exit
means the documented command completed, not that a deferred request is already
delivered or that a turn is authorized. Preserve the command's refusal and retry
semantics; do not convert an unavailable observation into permission.

### Migration dry-run report (revision 1.10, #8339)

When the migration state is readable, `ameesh migrate --dry-run --json` writes
exactly one JSON object on standard output and no other text; human diagnostics
stay on standard error. `--json` requires `--dry-run`: applying migrations
keeps its text journal and its operator gates. The call executes no DDL, not
even the migration registry, and its exit code keeps the existing meaning: `0`,
the report was produced (non-additive pending migrations do not fail a plain
`--dry-run`); `3`, `--additive-only` refused because at least one pending
migration is non-additive; `1`, an applied migration was modified (the object
still lists it), or the state could not be read (no object is then written, the
reason is on standard error).

The object uses schema `ameesh-migrate-dry-run/1`:

| Field | Type and meaning |
| --- | --- |
| `schema` | constant `ameesh-migrate-dry-run/1`. |
| `dry_run` | boolean, `true`. |
| `additive_only` | boolean, the flag given. |
| `pending` | array of core then module migrations not recorded as applied, in application order. Each entry: `module` (string, or `null` for the core), `version` (integer; module-local for a module migration), `label`, `checksum` (SHA-256 of the SQL file) and `non_additive` (array of reasons; empty when the migration only adds). |
| `modified` | array of applied migrations whose recorded checksum differs from this code's. Each entry has the same fields plus `applied_checksum` (the recorded value). A non-empty array is exit `1`. |
| `unknown` | array of database versions this code does not know: `module` (string or `null`) and `version`. Informational; it does not by itself change the exit code. |

A consumer checks `schema` before reading and ignores unknown response fields.
A module-local `version` is not comparable with a core version: read `module`
and `label` together.

### Public canon refusal (1.12, #8340)

`ameesh canon refuse` is the supported replacement for an installed module
calling `canon_sync.record_state(..., "invalid", reason)`. It writes the
`canon_state` row of the host that runs it (there is no `--host`), for a
configured canon, and blocks every new claim on that canon until a later
successful `ameesh canon sync` replaces the state. A running lease, session or
turn is untouched.

- The reason is ordinary one-line text, at most 1000 characters after
  whitespace is collapsed. A control character, an empty or oversized reason,
  or a reason that looks like a secret is refused before any write; the
  rejected text is never stored or echoed back. `--reason` is required.
- `--commit SHA` is optional. When present it must be a full 40- or 64-hex
  commit; it is stored beside the refusal, never as the audited
  `last_good_commit`. Without it the refusal is not pinned.
- `--canon ID` selects a configured canon by id or root; otherwise the default
  canon is used. An unconfigured root is refused (`unknown-canon`).
- The call is idempotent: repeating the same (host, canon, reason, commit)
  changes nothing and reports `written: false`. A different reason or commit
  replaces the recorded refusal; an `ameesh canon sync` that reads a valid
  canon replaces it with `ok` and clears the refused commit.
- `--json` returns `ok`, `host`, `canon`, `canon_id`, `commit`, `status`,
  `diagnostic`, `written`, `checked_ts`, `last_good_commit`, `code` and
  `detail`. A refusal exits 1 and carries a closed `code` (`no-host`,
  `no-canon`, `unknown-canon`, `ambiguous-canon`, `invalid-commit`,
  `empty-reason`, `reason-too-long`, `reason-unreadable`, `reason-secret`,
  `canon-error`, `changed`) plus a secret-free `detail`; it writes nothing.
- This command only denies admission. It does not sync, fetch, validate the
  canon, or replace any other state. Use `ameesh canon sync` for that.

The stored diagnostic begins with `module refusal: ` so `ameesh canon check`
and `ameesh canon check --json` (under `registry_state`) distinguish it from a
sync failure; the refused commit is the `refused_commit` field of the same
state.

## HTTP boundary

The normative method, path, input and response schemas are the checked-in
[OpenAPI v1 document](openapi-v1.json), also served at
`GET /api/v1/openapi.json`. At this revision there are 36 paths, including the
discovery document `/.well-known/ameesh`. Revision 1.0 had 34; revision 1.1
adds `/api/v1/agents/{agent}/observation`; revision 1.11 adds
`/api/v1/reservations`. The path count is an observation, not a limit. Only the
methods described there are public.

| Area | Public paths (prefix `/api/v1`) |
| --- | --- |
| Identity and fleet | `/me`, `/agents`, `/agents/{agent}`, `/hosts`, `/hosts/{host}`, `/projects` |
| Work and progress | `/lots`, `/lots/{id}`, `/lots/{id}/assign`, `/projects/{project}/progress`, `/projects/{project}/timeline` |
| Agent interaction | `/agents/{agent}/chat`, `/agents/{agent}/messages`, `/agents/{agent}/peek`, and the documented interrupt/pause/resume/stop methods |
| Observation | `/agents/{agent}/observation`, `/requests/{request}`, `/turns/{turn}`, `/sandbox/turns`, `/sandbox/turns/{turn}`, `/events`; `/agents` and `/agents/{agent}` also carry the bounded `deferral` block |
| Governance | `/budget`, `/budget/caps`, `/reservations`, `/decisions`, `/alerts`, `/alerts/{key}/ack`, `/actions/{action}/approval-request` |
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

`/reservations` is built inside one `REPEATABLE READ, READ ONLY` transaction
and answers the same `ameesh-reservations/1` document as `ameesh reservations
--json`; `observed_ts` is the transaction's single instant. It never opens,
keeps, settles or releases a reservation, and it is not an admission: a missing
or truncated list is never permission to spend. Its closed projection and the
meaning of `open`/`kept`, `truncated` and `unreadable` are the ones documented
for the CLI entry above. The same bounds apply (`limit` 1..500, default 100);
`account`, `agent` and `state` filter exactly.

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
| `CAPABILITIES` | Optional unique names among `lifecycle`, `cli`, `audit`, `workspaces`, backed by the corresponding implementation. `release` (with `RELEASE_MODELS`) is reserved to built-in modules: an installed module declaring it is refused. |
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

External action connectors from an installed module are part of this contract
from revision 1.8: a module declares them under `ACTION_CONNECTORS` and imports
the protocol objects from `ameesh.public.connectors`; see [action
connectors](#action-connectors-declared-by-an-installed-module-18-8342) below.
At the 1.0 baseline they were absent, and the historical design note
[`ACTION_CONNECTORS`, operation classes and test bench](https://github.com/manaty/ameesh/blob/9755b4f3265a19afe6105e687be88edaab4ab459/docs/design/module-action-connectors.md)
remains the protocol rationale, not a second surface.

## Action connectors declared by an installed module (1.8, #8342)

Revision 1.8 adds `ameesh.public.connectors`, the supported Python surface for
a module that supplies action connectors (#7628 / #5818, consumer IMA#1803,
manaty/ameesh#6582). It is additive: the connector protocol, the governed gate
(`ameesh.actions`), the canon `action_classes` policy and the database schema
do not change.

A module declares the connectors it offers in a module-level mapping named
`ACTION_CONNECTORS`, from connector name to constructor. The module is
installed through its own `ameesh.modules` entry point like any other module;
a connector-only module returns an empty `types` list in its descriptor.

```python
from ameesh.public.connectors import Outcome

class PublishConnector:
    name = "example-publish"
    dedupe = "guaranteed"

    def classify(self, operation, args):
        return "reversible"

    def execute(self, action, idempotency_key):
        ...  # perform the effect; the key is always action.action_id
        return Outcome.confirmed("external-id")

    def reconcile(self, action):
        ...  # read the external system; None when it cannot tell
        return Outcome.unknown("external read timed out")

ACTION_CONNECTORS = {"example-publish": PublishConnector}
```

The core registry reads `ACTION_CONNECTORS` on discovery and validates it
before the module becomes an action provider: the declaration is a mapping;
each name is a unique module-style identifier; each value is a callable
constructor; and a name owned by the core (`shell-noop`, `git-merge`) or
already declared by another module is a collision. A refused module is reported
with its error in `ameesh modules --json` (`refused`) and never serves a
connector. Instantiation checks the returned object's `name`, `dedupe`, and
callable `classify`/`execute`/`reconcile`, and requires its `name` to equal the
registry key. The core imports only the module entry point it discovered; no
path named by the canon is imported.

| Element | Contract |
| --- | --- |
| `ACTION_CONNECTORS` | Module-level `{name: constructor}` mapping. `ameesh.public.connectors.ACTION_CONNECTORS` freezes the attribute spelling; the mapping is code, never permission. |
| `name`, `dedupe`, `timeout` | `name` is the registry key and the value of `actions.connector`. `dedupe` is `guaranteed` only when the external system really deduplicates on the idempotency key, `none` otherwise (`DEDUPE`). `timeout` is an optional positive number of seconds (default 60) after which the gate treats the attempt as `unknown`. |
| `classify(operation, args) -> str` | The default class of an operation, one of `ACTION_CLASSES` (`read`, `reversible`, `irreversible`, `costly`). The class declared in the trusted canon's `action_classes` is authoritative; the gate may skip this call. |
| `execute(action, idempotency_key) -> Outcome` | The effect. The gate calls it only after writing `launched`; the key is always `action.action_id`. A `dedupe="guaranteed"` connector actually deduplicates that key. |
| `reconcile(action) -> Outcome \| None` | A read-only observation: `None` when the external record cannot be found, `Outcome.confirmed` on proof, `Outcome.failed` on certain failure. It never grants authorization. |
| `Action` | Frozen record: `action_id`, `project`, `connector`, `operation`, `target`, `args`, `action_class`, `amount`, `currency`, `policy_version`, `attempt`, `work_item`, `recurrence`, `spend_provider`, `spend_purpose`. |
| `Outcome` | Frozen record: `state` (`confirmed`, `failed` or `unknown`), `external_ref`, `detail`, `extra`. Construct with `Outcome.confirmed(external_ref=None, detail="")`, `Outcome.failed(detail, external_ref=None)` or `Outcome.unknown(detail, external_ref=None)`; another state is refused. |
| `NotSentError` (code `not-sent`) | Raised only for a certain failure before the first byte of the request was written (name resolution, refused or timed-out connection, TLS handshake). The gate settles the attempt `failed` (`not sent`) and permits a retry under the same `action_id`; never raise it after sending. |

`unknown` is the safe answer whenever an effect may have happened: a read
timeout after the request was sent, a lost response, or any other exception.
Only a certain no-effect failure is `failed`; in doubt, return `unknown`.

Activation and the gate remain the only path to an effect. A connector is
usable in a project only when the trusted canon selects its module and declares
the operation under `action_classes`; the gate rechecks activation and class
before approval and execution. Declaration is not permission, and a
connector's `classify` never overrides the canon.

Accordingly, a module never writes the core's action log, approval,
reservation or ledger tables; never calls `ameesh.connectors.get`, the internal
registry or its own `execute` outside the gate; never treats its database
credential, `ACTION_CONNECTORS` or a previous approval as authorization; and
never imports `ameesh.connectors`, `ameesh.actions`, `ameesh.modules.registry`
or another private path. A direct write or a bypassed gate is an unreviewed
action, not an extension point.

`ameesh.public.connectors` exports `CONTRACT_VERSION` (`"1.8"`),
`ACTION_CONNECTORS`, `OUTCOMES`, `ACTION_CLASSES`, `DEDUPE`, `DEFAULT_TIMEOUT`,
`Action`, `Outcome` and `NotSentError`. It re-exports the exact objects the
gate uses, so an outcome is not translated between two vocabularies. Enable the
surface only when its `CONTRACT_VERSION` is `"1.8"` or a compatible later
minor; the package revision `ameesh.public.CONTRACT_VERSION` is then at least
`"1.8"`. `ameesh.public.mail` stays at `"1.4"` and `ameesh.public.fixture` at
`"1.7"`.

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
`session_policy` is a card error, so the agent is not claimable. An avatar card
(`avatar:` block) declares `session_policy: jamais` without writing it; a
rotating policy or `fresh_session: true` on it is a card error (#8204). The wait after
a failure is interrupted by shutdown and draining; graceful stop remains the
core's responsibility. The optional `harness` argument of the internal
`context_max_tokens()` helper is not frozen by this contract: a declared cap
applies to every harness.

IMA's per-document fresh-session policy is covered by this declarative
surface. The lease-bound replay requirement (tranche E2c) is a core behavior,
not a runner hook: see [interrupted requests](#requests-of-an-interrupted-turn-e2c-7673)
(revision 1.4). IMA's removal of its shutdown subclass stays tracked by its
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

## Mail submission, history, pending requests and replies (Python)

Contract 1.2 adds the first write surface for a module that already holds the
deployment's database identity: `ameesh.public.mail`. This is the replacement
for direct `agent_mailbox` submission, history, pending-request and reply SQL
(mapping E2a/E2b); the governed `agent-mail` commands and the HTTP boundary
above remain the public surfaces for an ordinary caller. Contract 1.3 adds the
lease-fenced pending request and correlated reply operations to the same
surface (tranche E2b, #7672). This Python surface does not authenticate a
remote client: the database credential remains the trust boundary. A process
that runs under a mesh runner must use the identity that runner verified; a
module service with no declared runner identity keeps the actor it names, as
long as that name is not one the core reserves (below). Mediated executor
connections are refused — their contract is separate.

Enable the surface only when `ameesh.public.CONTRACT_VERSION` is `"1.3"` or a
compatible later minor; rely on the requeue of interrupted requests only from
`"1.4"`. A consumer written against the 1.1 draft applies the
`payload.module` migration above before treating 1.2 as compatible.

| Element | Contract |
| --- | --- |
| `Submission(recipient, body, kind="notify", payload=None, work_item_id=None, thread=True, idempotency_key=None)` | One typed message. `recipient` is a registered agent; `kind` is `request`, `reply`, `notify` or `event`; `payload` is the module-owned JSON object carried under `payload.module`; `work_item_id` optionally links a lot; `thread=False` keeps the content out of the readable project thread (the mailbox row and this history remain); `idempotency_key` is the client key described below. |
| `submit(db, actor, submission) -> Receipt` | Deposit one message as `actor`. The sender is always the actor; another sender cannot be named, and a core-owned sender identity is refused. |
| `submit_many(db, actor, submissions) -> BatchResult` | Deposit up to 50 submissions in input order. Each item commits or is refused on its own; a refusal appears in its `SubmissionResult` and does not hide later outcomes. A larger batch is refused before anything is deposited. |
| `find(db, actor, recipient, idempotency_key) -> Receipt \| None` | The durable receipt already deposited under this `(actor, recipient, key)`, or `None`. |
| `history(db, actor, *, peer=None, after_id=None, limit=100, since_ts=None, until_ts=None, kind=None) -> HistoryPage` | Bounded history of the messages where `actor` is sender or recipient, oldest first by durable id, strictly after `after_id`. `peer` restricts the page to the two-party conversation with that agent; `kind` filters the nature; `since_ts`/`until_ts` bound the creation time. `limit` is between 1 and 200. |
| `Lease(owner, epoch)` | The native lease the caller acts under: the runner id and epoch read from `ameesh turn current --json` (`lease.owner`, `lease.epoch`). It is a claim the core rechecks against the registry; it never grants authority by itself. |
| `pending(db, actor, lease, *, sender=None, limit=50) -> PendingPage` | The requests held by `actor`'s live lease and current native turn, oldest first. Only a request reserved as the current turn's prompt (`meta.consigne`) under the same live `(owner, epoch)` lease and the current accounting marker (`spend_pending`, same `turn_id`, delivery in its window) is pending; an unanswered request delivered in an earlier turn is not current and never appears. A stale, replaced or expired lease raises `ScopeRefused`; an absent current-turn marker yields an empty page. `sender` restricts the page to one sender; `limit` is between 1 and 200. This call never reserves, settles or spends. |
| `reply(db, actor, request_id, reply, *, lease) -> Receipt` | Answer one pending request under the same lease fence. The request must be addressed to `actor` and be the current native turn's prompt; the lease row is locked and rechecked in the transaction that reads the request and writes the reply, so an expired or replaced owner/epoch refuses. The reply is deposited to the request's sender (never a caller-named recipient) and linked to the request's lot. One request accepts one reply; an identical retry returns the durable receipt with `reused=True`, another body or payload raises `ReplyConflict`. |
| `Reply(body, payload=None, thread=True)` | The content of one answer. The payload is validated like a submission's; `thread=False` keeps the answer out of the readable project thread. The recipient and the lot are the request's, not chosen here. |
| `PendingRequest` and `PendingPage` | One request (`id`, `sender`, `recipient`, `body`, `payload`, `work_item_id`, `created_ts`, `delivered_ts`, `turn_id`) and a page (`entries`, `has_more`). `turn_id` is the native turn that reserved it; `delivered_ts` is observation only and never a lease claim. `has_more` says another pending request exists; there is no cursor because pending is a live set. The value objects expose `as_dict()` for JSON clients. |
| `Receipt(id, actor, recipient, kind, created_ts, reused, in_reply_to=None)` | `id` is the durable mailbox row id, stable across retries; `request_id` is that id for a `request` and `None` otherwise; `in_reply_to` is the answered request id for a `reply` and `None` otherwise; `reused` says this call returned an existing deposit. |
| `HistoryEntry` and `HistoryPage` | One message (`id`, `sender`, `recipient`, `body`, `kind`, `payload`, `work_item_id`, `created_ts`, `delivered_ts`, `in_reply_to`) and a page (`entries`, `next_cursor`, `has_more`). The stored `payload` keeps the module object under `payload.module`; a 1.1-draft row is returned as stored, never rewritten. `in_reply_to` is the answered request id for a reply and `None` otherwise. `next_cursor` is the last returned id, or the supplied cursor for an empty page; pass it back as `after_id`. `delivered_ts` is observation only and never a lease claim. |
| `SubmissionResult` and `BatchResult` | Ordered batch outcomes (`index`, `receipt`, `error`, `detail`) and the batch (`results`, `ok`). The value objects expose `as_dict()` for JSON clients. |
| Errors | `InvalidSubmission` (code `invalid`), `IdempotencyConflict` or `ReplyConflict` (`conflict`), `ScopeRefused` or `RecipientRefused` (`refused`), `UnsupportedStorage` (`unsupported`). A reserved sender identity raises `ScopeRefused` before any deposit; so does a configured canon whose complete inventory cannot be read. A batch reports the same codes in `SubmissionResult.error`. |

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
the canon part is read per deployment. **No configured canon** is an explicit
absence: only the static names and prefixes apply. A canon that *is* configured
but unreadable, only partially loaded, or carrying load errors is not an absence
of roles: the call is refused with `ScopeRefused` before any deposit, rather
than admitting an omitted governance name under a guessed empty inventory;
`reserved_actors(db)` raises the same refusal instead of returning an inventory
it cannot establish completely.

Idempotency is scoped to `(actor, recipient)`. The content is the body, the
kind, the module payload and the work item. The same key with the same content
returns the same `id` with `reused=True`; the same key with another content
raises `IdempotencyConflict`; concurrent retries end on one durable row. The
core stores the key in `payload.idempotency_key`; the unique index of
migration 0497 is the authority, so the guarantee holds across processes, not
only within one caller.

A request is pending only for the native turn that holds it. The
`spend_pending` marker and the request's reserved prompt (`meta.consigne`:
`runner`, `epoch`, `turn`) are the authority; this surface reads them, never
writes them. A request reserved for an older turn, an unanswered request that
arrived outside the turn's prompt, a withdrawn request, or one already answered
is not pending. The actor must be the request's recipient. An absent marker
makes the page empty: absence is never permission to answer.

The lease is not minted by this surface. `pending` and `reply` take the
`owner`/`epoch` of the native turn (`ameesh turn current --json`) and the core
rechecks them against the live registry row. `reply` locks that row before it
reads the request and writes the answer, so a lease that expired, changed
hands or advanced to another epoch refuses instead of writing under a stale
identity. It also holds the current accounting marker `FOR SHARE` until commit,
so a concurrent clear or replacement of `spend_pending` waits instead of ending
the turn between the decision and the write. Pending rechecks the same facts
inside its read query; a lease that looks live in an earlier read cannot make
an old request current.

Reply correlation is held by `agent_mailbox.in_reply_to` (migration 0594,
additive). The unique partial index `agent_mailbox_in_reply_to_idx` is the
authority for one reply per request, so concurrent retries end on one durable
row. An identical retry (same body and payload) returns the durable receipt
with `reused=True`; a different body or payload raises `ReplyConflict`; the
request row itself is not rewritten. The reply is ordinary two-party mail:
visible in the history of the request's sender and of the actor, in the
readable project thread unless `thread=False`, and nowhere else through this
surface. It inherits the request's lot and never names its own recipient.

Native accounting stays authoritative: neither `pending` nor `reply` reserves,
settles, spends or changes a turn, its marker or its reservation. The
`/requests/{request}` and `turn observe` observations remain the read-only
surfaces for turn, accounting and quiescence.

### Requests of an interrupted turn (E2c, #7673)

From revision 1.4 the core, not the module, puts back in the queue the
requests of a turn it interrupted. A mail turn marks its messages delivered
when the harness starts; if the core then cuts the turn — runner shutdown or
drain, host withdrawal (outside the safe point), `ameesh restart`, priority
preemption, or a lost lease — each `request` that turn carried and that has no
correlated reply becomes unread again. It is delivered to a later turn of the
same agent, under that turn's own reservation, flagged as possibly redelivered,
and appears in `pending` for that turn only. A module therefore needs no
shutdown subclass and writes no restored prompt.

| Guarantee | Contract |
| --- | --- |
| Exact captured set | Only the requests of the interrupted turn: the ids it served, still reserved under its runner, epoch, reservation token and native turn id. Another turn's request, a message reserved since by another reservation, and other kinds (`notify`, `event`, `reply`) are not touched. Human messages keep their own end-of-turn settlement. |
| Lease guard | The registry row is locked first. Nothing is written once another holder has taken the lease since the captured epoch; the requeue proceeds only while the lease is still `(owner, epoch)` (live or expired but not reclaimed) or after that owner released it with no claim since. A refused requeue is logged; the request then stays delivered. |
| Already-replied exclusion | A request with a correlated reply (`in_reply_to`) stays answered and is not delivered again. |
| Race with `reply` | `reply` and the requeue lock the same registry row, so they serialize. A reply committed first is seen by the requeue, which leaves that request answered; a reply that comes after the requeue finds a request that is no longer the current turn's prompt and raises `ScopeRefused`. One request never ends both answered and queued again. |
| Pending prompt | `pending_prompt` and its identity are never modified: an unrelated instruction queued during the turn stays as it was. |
| No duplicate | The requeue removes the turn's reservation in the same write, so the next turn reserves the request once; no second reply is possible (unique `in_reply_to`), and no reservation, spend marker or ledger row is created or changed. |

A turn that ends because its harness failed is not a core interruption: its
requests are not delivered again, so that a request that makes the harness fail
cannot loop. A mediated executor does not requeue (its behavior is unchanged).

The module payload supports role, document and Memoria data, and is confined
to `payload.module`, a JSON object (at most 256 KiB serialized). The namespace
belongs to the calling actor: the module submits under its own identity, so
`payload.module` is already scoped by that name. Admission is an allow-list:
the only admitted top-level keys are `module` and `attribution`. Every other
top-level key is core-owned and refused before any deposit, so a core control
marker added in a later release is closed to modules by default instead of
becoming open through a deny-list that did not know it yet. The exported
`ADMITTED_PAYLOAD_KEYS` states the list; `RESERVED_PAYLOAD_KEYS` lists the
known core markers for diagnostics only. Core control markers — delivery and
accounting (`idempotency_key`, `report`, `report_fingerprint`, `urgent`, `ack`,
`info`, `cc`, `broadcast`, `radar`, `ci_passif`, `human`, `human_account`,
`verdict`, `arbitration`, `reassigned`, `request_expires_ts`), review
(`review_designation`, `review_request`), decisions (`decision`,
`decision_answer`) and operations (`pass`, `ci`, `staging_actif`) — are never
available at the top level. A key with one of those names is ordinary data when
it is nested inside `payload.module`: it does not select a core delivery,
review, decision or accounting path. This applies to a submission and to a
reply alike. `payload.attribution` is the one admitted core key: it is
validated with the closed caller-attribution schema and remains data, never
admission, budget or authorization.

Published bounds: body at most 64 KiB UTF-8, serialized payload at most
256 KiB, 50 submissions per batch, 200 history entries per page, 200 pending
requests per page (default 50). Bodies must be readable text (the thread rule).
Signatures, request deadlines, urgent wake-up, acknowledgements and copies are
not part of this surface; a consumer that needs them uses the governed command
or HTTP boundary.

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

## Public fixture for a consumer-owned database (1.5, #7701)

Revision 1.5 adds `ameesh.public.fixture`, the qualification fixture for an
installed module. A consumer uses it to run its own Process, usage, refusal and
mail witnesses on a **consumer-owned disposable database**, without importing
`tests.support`, a private ameesh path or a core table. Enable the surface when
`ameesh.public.fixture.CONTRACT_VERSION` is `"1.5"` or a compatible later
minor; the package revision `ameesh.public.CONTRACT_VERSION` is then at least
`"1.5"` (`"1.6"` once the production connection is present).

| Element | Contract |
| --- | --- |
| `Fixture(admin_dsn, *, executable=None, mail_executable=None, root=None, prefix="ameesh_fixture_", keep=False)` | A fixture bound to a PostgreSQL server the consumer controls. `admin_dsn` must name an existing database whose role may `CREATE DATABASE`; it is a secret, kept out of `repr()`. `executable` and `mail_executable` default to the installed `ameesh` and `agent-mail` commands. `root` selects a private directory (a fresh temporary one otherwise); `prefix` must stay in the `ameesh_fixture_` namespace. `keep=True` leaves the database for inspection. |
| `create() -> Fixture`, `close()`, `with Fixture(...) as fixture` | `create()` creates `ameesh_fixture_<random>`, derives its DSN and applies the released schema through the installed `ameesh migrate` command (never an import of the migrator). The context manager creates and closes. `close()` is idempotent, drops only a database this instance created and removes only a root it created. |
| `database`, `dsn`, `root` | The disposable database name, its DSN (carrying the consumer's credentials) and the private root. They refuse before `create()`. |
| `env(**extra) -> dict` | The isolated child environment. Every inherited `AMEESH_*`, `AGENT_MESH_*` and `AGENT_MAIL_*` variable is removed first; the fixture then sets `AMEESH_DSN` (the disposable database), an empty `AMEESH_CONFIG`, private `AMEESH_STATE`/`AGENT_MAIL_STATE`/`AGENT_MAIL_CONFIG`, and `AMEESH_SCHEMA=public`. After `canon()`, `AMEESH_CANON` and `AMEESH_CANON_REF` name the private repository. `extra` is applied last and must be strings. |
| `cli(*args, env=None, stdin=None, timeout=120)`, `mail(*args, ...)`, `run(command, *args, ...)` | Run an installed executable as a subprocess with `env()`, the private root as working directory and a bounded timeout. `cli` and `mail` use the installed commands; `run` takes a command prefix. They return `subprocess.CompletedProcess` and never import a dispatcher. |
| `canon(files, *, message="fixture canon", ref="main") -> str` | Write a fresh private git repository from `files` (relative path → text), commit it and return the commit. `refs/remotes/origin/<ref>` is set so the core's authenticator check sees the canonical branch after a fetch, with no network remote. `git(*args)` runs git in that repository. |
| `connect()`, `read_view(view, columns) -> list` | `connect()` opens one opaque core-owned connection to the disposable database, for the public Python surfaces (`ameesh.public.mail`) and one connection per racing thread; use it for those surfaces, not for SQL on core tables. `read_view` is the only SQL read surface: one of `agent_mesh_overview`, `mesh_keys` or `mesh_approvals_status`, with an explicit column list (never `*`). |
| `path(relative)`, `write(relative, text)`, `read(relative)` | Files under the private root. Absolute paths and `..` escapes are refused. |
| Errors | `FixtureError` (code `fixture`) when creation, migration or a read fails; `FixtureRefused` (code `refused`) for an unsafe target, an uncreated fixture or an out-of-protocol call. |

The fixture's safety boundary is part of the contract:

- it creates and drops only `ameesh_fixture_*` names; `close()` refuses any
  other name, so a wrong string cannot drop a consumer's database;
- `prefix` is bounded so the generated name (prefix plus 16 random bytes) stays
  within PostgreSQL's 63-byte identifier limit and is never silently truncated;
- the admin DSN is never printed and reaches only the disposable database; the
  child environment never inherits the host's canon, DSN or runner identity,
  and the `psql` fallback passes a password through `PGPASSWORD`, never argv;
- the schema is applied only by the documented `ameesh migrate` command, and
  core data is written only through admitted commands, HTTP or callbacks;
- the named read views, with explicit columns, are the only SQL the fixture
  offers; base tables, triggers and joins through them remain internal.

The fixture does not probe whether the target is production: the consumer must
point `admin_dsn` at a server and role whose disposable databases are
acceptable. A consumer that needs a different PostgreSQL server provides its
own DSN; no host default is read. The witness that exercises this surface is
[public-fixture-witness](../../examples/fixtures/public-fixture-witness);
`tests/test_7701_public_fixture.py` runs it on `AMEESH_TEST_DSN` and proves that
the reported `ameesh_fixture_*` database is gone afterwards.

### Agent qualification states (1.7, #8341)

Revision 1.7 raises `ameesh.public.fixture.CONTRACT_VERSION` to `"1.7"` and
adds the states a module test needs an agent in (manaty/ameesh#6581,
IMA#1803): an expired or foreign lease, a stale canon, a refused placement, an
ambiguous spend marker, an open or settled reservation, a cost row. A consumer
asks the fixture for the state instead of writing `agent_registry`,
`canon_state`, `spend_pending`, `account_reservations` or `turn_costs`; the
fixture reaches it through the core's own code on its disposable database and
proves it with the read-only agent observation (`ameesh agent observe`). Enable
it when the fixture's `CONTRACT_VERSION` is `"1.7"` or a compatible later
minor; `ameesh.public.CONTRACT_VERSION` is then at least `"1.7"`.

| Element | Contract |
| --- | --- |
| `agent(name, *, host=None, harness="claude", model="fixture-model", team="fixture", responsible="human:fixture", governed=True) -> dict` | Register `name` in the fixture registry and return its state report. A governed agent pins the fixture canon commit (the one `canon()` returned, else a fixed placeholder), has a fresh canon state on `host`, an admitted placement and no lease: it is claimable. `host` defaults to the agent's own `fixture-host-<name>`; agents given the same explicit host share its canon state. `governed=False` registers a row with no canon reference and no placement verdict. Calling it again rewrites the declared profile; it refuses while the agent holds a live lease and releases an expired one. |
| `agent_state(name, state, **options) -> dict` | Put the registered agent into `state`, one of `AGENT_STATES`, then read it back. Options are strings or numbers, and only the options listed for that state are accepted. The report is described below. |
| `AGENT_STATES` | The closed list of states, below. A later minor may add a state; it never changes what an existing one means. |
| `FixtureStateError` (code `state`) | The state was not reached or not observed: agent not registered, claim refused (the report's reasons explain why, e.g. a refused placement), reservation over its envelope. Its message carries no DSN or password. An unknown state, a non-plain agent name or an option that is neither a string nor a number is `FixtureRefused` (code `refused`), as is an option the state does not take. |

| State | Reached by | Observed as | Options |
| --- | --- | --- | --- |
| `lease_live` | the core's claim, owner `fixture-runner` | `lease.owner` set, `lease.live` true | `owner`, `ttl` (seconds, default 300) |
| `lease_foreign` | the same claim, owner `fixture-foreign` | the same, another owner | `owner`, `ttl` |
| `lease_expired` | the same claim with a past expiry (a runner that died without releasing) | `lease.owner` set, `lease.live` false | `owner`, `ttl` (negative, default -60) |
| `lease_free` | the core's release | `lease.owner` null | — |
| `canon_fresh` | the canon synchronisation's state write, pinned commit | `canon.fresh` true | — |
| `canon_stale` | the same write, then its check aged past `canon.max_age_s` (a host whose synchronisation stopped) | `canon.fresh` false, reason `canon_stale` | `age_s` (default `max_age_s` + 60) |
| `placement_admitted` | the canon synchronisation's placement verdict, current profile | `placement.ok` true | — |
| `placement_refused` | the same verdict, refused | `placement.ok` false, reason `placement_refused` | `diagnostic` |
| `spend_ambiguous` | the turn's spend marker, its ledger key lost, beside an open reservation of another turn at the same index | `spend.ledger_ambiguous` true | `start_index`, `account` |
| `spend_clear` | the marker cleared and this agent's open or kept reservations released | `spend.marker` false | — |
| `reservation_open` | the core's admission on the billing account (an envelope of 100 USD per day is set when the account has none) | `reservation.state` `open` | `account`, `usd`, `spend_key`, `limit_usd` |
| `reservation_settled` | the agent's open reservation (or `spend_key`, or a new one) settled after its cost row, like a turn | `reservation.state` `settled`, `budget.settled` | `account`, `usd`, `spend_key`, `cost_usd`, `limit_usd` |
| `cost` | one row in the cost ledger | `cost.rows` ≥ 1 | `usd`, `input_tokens`, `output_tokens`, `spend_key`, `account` |

`reservation_settled` accepts an existing `spend_key` only for the named
agent and, when supplied, the requested `account`. A foreign key is refused
with `FixtureStateError` before any cost or reservation write. Without a key,
the fixture selects this agent's latest open reservation on the requested
account (or any account when omitted), creating one if none matches.

Every report carries `agent`, `lease` (`owner`, `epoch`, `live`, `expires_ts`),
`canon` (`state`, `last_good_commit`, `age_s`, `max_age_s`, `fresh`,
`ref_matches`), `placement` (`ok`, `ref`, `diagnostic`), `reasons` (the
observation's admission reasons) and `spend` (`marker`, `spend_key`,
`ledger_ambiguous`); `agent_state` adds `state`. The budget states add
`account`, `spend_key`, `reservation` (`state`, `usd`) and `budget` (`usd`,
`settled`, `reserved`, `kept`); `cost` adds `cost` (`spend_key`, `turn`,
`usd`, `rows`, `recorded_usd`). The observation is taken from the fixture
environment's host, not the agent's, so `responsible_policy_unavailable` may
appear among the reasons; it never means a state was missed.

States are isolated and reversible, on the fixture's own database only. A
state touches the named agent's rows and the named billing account. The
exception is the canon states: the core keeps one canon state per host, so
`canon_stale` and `canon_fresh` change the canon state of the agent's host.
That is why each agent gets its own host by default. Agents registered on one
explicit host share its canon state. Four reverse states undo the
others: `lease_free` undoes the lease states, `canon_fresh` undoes
`canon_stale`, `placement_admitted` undoes `placement_refused`, and
`spend_clear` undoes the spend marker and the open or kept reservations. A
cost row stays in the ledger, as it would in production. A lease state
releases the previous lease first. States compose with the core's claim
rules: a refused placement makes the next lease state refuse, exactly as a
runner's claim would. The claim checks the canon state's status, not its age:
after `canon_stale`, a lease state still succeeds and `canon_stale` stays in
`reasons`. The age gates a new turn, not the lease.
`canon_stale` and `spend_ambiguous` have no production write path, since time
and a lost key produce them; the fixture writes those two facts itself, inside
the core package and only on its disposable database. The consumer still
writes no core table.

## Public production connection (1.6, #6573)

Revision 1.6 adds :mod:`ameesh.public.database` and its re-export
`ameesh.public.connect`: the production counterpart of the fixture's
`connect()`. A module that already holds the deployment's database identity
opens its connection from the same environment and configuration as an
installed core command (`AMEESH_DSN` or the configured DSN, `AMEESH_SCHEMA`,
driver and backend) instead of importing `ameesh.config`, `ameesh.db` and
`require_schema()`. Enable the surface when `ameesh.public.CONTRACT_VERSION` is
`"1.6"` or a compatible later minor; `ameesh.public.mail` stays at `"1.4"` and
`ameesh.public.fixture` at `"1.5"` (`"1.7"` from revision 1.7).

| Element | Contract |
| --- | --- |
| `connect() -> Db` | Open one connection from the process's usual configuration, with no DSN, credential or connection argument. The agent-mesh schema is checked immediately with the core's probe, never deferred: a missing or incompatible schema refuses before the caller can write. Return the core connection object, opaque for this contract: pass it to `ameesh.public.mail` and close it with `db.close()`, never use it as an SQL escape hatch on the core's tables (their names, columns and views other than the admitted named views are internal). One connection per process or racing thread; the caller closes it. |
| `DatabaseUnavailable` (code `unavailable`) | The configuration cannot be read, the database cannot be opened, or the connection is lost while the schema is checked. |
| `SchemaRefused` (code `schema`) | The connection opened but the agent-mesh schema is absent, incompatible or cannot be verified. Nothing is written by this surface before the refusal. |
| `UnsupportedStorage` (code `unsupported`) | A mediated or remote executor connection is outside this surface; it is refused before any mail operation. `DatabaseError` is the common base of the three. |
| Redaction and opacity | `connect()` adds no attribute returning the DSN or a password. No published error message carries the DSN, a password or its encoded form, and `repr()` of the returned connection does not expose them. |
| Configuration and identity | The surface reads the usual configuration exactly like an installed core command and does not authenticate a remote client: the database credential remains the trust boundary. A process running under a mesh runner still submits under the identity that runner verified; `connect()` mints, widens or verifies no identity. |

The surface is additive: `Fixture.connect()` keeps its 1.5 behavior and
isolation, and `public.mail` keeps its 1.4 behavior. Where the fixture
constructs a disposable database for qualification, `public.connect()` opens
the deployment's configured database; a consumer chooses the appropriate one,
it is not inferred from the other.

The external witness
[public-fixture-witness](../../examples/fixtures/public-fixture-witness) runs
this surface in a clean child process of the fixture environment: it connects
with no argument, submits and reads through `ameesh.public.mail`, closes, and
observes the documented schema, storage and unavailable refusals without
importing an internal module. `tests/test_8333_public_connect.py` covers the
same elements in-process and in a subprocess; the
[coverage table](module-contract-v1-coverage.md) cites both.

<!-- contract-revision-sections: the packet composer folds the sections of the
pending revision fragments here, in manifest order. Do not remove this anchor
or add a revision section after it by hand; see
docs/design/contract-revision-fragments.md. -->

## Consumer qualification and delivery

[#7665](https://github.com/manaty/ameesh/issues/5857) supplies the external
witness ([contract-witness](../../examples/modules/contract-witness)), the
frozen [surface](module-contract-v1.surface.json) and the compatibility CI; the
element → test table is [module-contract-v1-coverage.md](module-contract-v1-coverage.md).
[#7701](https://github.com/manaty/ameesh/issues/5902) supplies the Python
fixture surface and its external witness
([public-fixture-witness](../../examples/fixtures/public-fixture-witness)),
frozen as revision 1.5; [#8341](https://github.com/manaty/ameesh/issues/6581)
adds its agent qualification states as revision 1.7, with the same witness.
[#6573](https://github.com/manaty/ameesh/issues/6573) supplies the Python
production connection, frozen as revision 1.6; its witness runs in the same
external distribution and in `tests/test_8333_public_connect.py`. [#8342 /
#6582](https://github.com/manaty/ameesh/issues/6582) adds the public
action-connector surface as revision 1.8; the offline
[module-connector bench](../../tests/fixtures/module_connector_bench) is a real
external distribution that imports only `ameesh.public.connectors`, and
`tests/test_7628_module_connectors.py` discovers it and refuses invalid
declarations. Existing evidence is
in `test_l84_serve`, `test_l98_modules_registry`, `test_l104_bare_canon`,
`test_l2894_turn_observe`, and `test_l2894_turn_context`; the fixture witness
covers the process, usage, refusal, mail and production-connection paths that
previously required `tests.support` or internal imports. No test imports
`tests.support` across the package boundary: a consumer-owned process/database
fixture replaces that dependency.

Validate the same consumer unchanged against two consecutive stable versions.
A contract failure belongs in upstream CI before release. Delivery of an
extension is complete for the external request only when IMA removes its
temporary implementation and records that removal in #1803. This document
alone does not close the umbrella request.
