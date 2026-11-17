# External module public contract, version 1

Contract revision: **1.0, 17 November 2026**. Scope: an installed external
module and its client processes. Reference: [#5827](https://github.com/manaty/ameesh/issues/5827),
first document tranche [#5856](https://github.com/manaty/ameesh/issues/5856).
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

The version of this document is independent of the package version. Its scope
is the surfaces listed below, not every importable Python name. A future
surface becomes available only in a released version that implements it and
lists it in this contract. Clients check that version before enabling it.
In particular, the baseline here is ameesh commit
`aeca7e2ebca4994d477cc9e2a7cb2970a4fdf007`; an open PR is not an installed feature.

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
| `ameesh modules --json` | Discovered module descriptions and refused entries. A refused entry is not an active provider. |
| `ameesh module list` and `ameesh module NAME …` | Installation evidence and dispatch to the named module's CLI. `list` is reserved. The module owns the arguments after its name. |
| `ameesh canon check`, `ameesh canon sync` | Validate and admit configured canon through the core's authority checks. These commands do not yet export IMA's complete directory/profile snapshot (mapping E1). |
| `agent-runner --agents-file FILE` | Dynamic admission filter; [format and withdrawal](../RUNNER-ADMISSION.md). It narrows canon admission, never grants authority. |
| `ameesh turn current --json [--expect-turn ID]` | Current turn's versioned authorization document, with identity and database recheck. `--local` omits the database recheck and must not be treated as proof of a live lease. |
| `ameesh turn observe (--turn ID \| --request ID) --json` | Read-only `ameesh-turn-observation/1`: request, turn, accounting and quiescence. Absence of a known cost is not zero cost. |
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
`GET /api/v1/openapi.json`. At the baseline there are 34 paths, including the
discovery document `/.well-known/ameesh`. The path count is an observation, not a
limit. Only the methods described there are public.

| Area | Public paths (prefix `/api/v1`) |
| --- | --- |
| Identity and fleet | `/me`, `/agents`, `/agents/{agent}`, `/hosts`, `/hosts/{host}`, `/projects` |
| Work and progress | `/lots`, `/lots/{id}`, `/lots/{id}/assign`, `/projects/{project}/progress`, `/projects/{project}/timeline` |
| Agent interaction | `/agents/{agent}/chat`, `/agents/{agent}/messages`, `/agents/{agent}/peek`, and the documented interrupt/pause/resume/stop methods |
| Observation | `/requests/{request}`, `/turns/{turn}`, `/sandbox/turns`, `/sandbox/turns/{turn}`, `/events` |
| Governance | `/budget`, `/budget/caps`, `/decisions`, `/alerts`, `/alerts/{key}/ack`, `/actions/{action}/approval-request` |
| Threads | `/threads/{project}`, `/threads/{project}/{lot}` |

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
| `descriptor(options=None)` | Returns exactly `types`, `schemas`, `rules`: unique type-name list, schema mapping, list of rule mappings. The baseline requires an external lifecycle module to have a type. |
| `CAPABILITIES` | Optional unique names among `lifecycle`, `cli`, `audit`, `workspaces`, backed by the corresponding implementation. |
| `main(db, argv, *, actor)` | Optional CLI callback. Receives the resolved actor and a core-owned connection for ordinary dispatch. Help can receive `db=None`, `actor=""`; do not query in that case. Return an exit status. |
| `audit(store, project)` | Optional callback for active project modules; returns findings through the existing module audit protocol. Host-only behavior is reserved to the core's always-active modules. |
| `ameesh.modules.registry.discover(*, builtin=True, canon=None)` | Discovery record with `modules`, `refused`, `names()` and `get(name)`; each Module offers `as_dict()`. Discovery errors never authorize an entry. |

The discovery record's public fields are `name`, `version`, `source`,
`capabilities`, `cli`, `storage`, `distribution`, `entry_point`,
`descriptor_sha256`, `card_ref`. `storage` on the Python record is a tuple of
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

IMA's per-document fresh-session policy, refusal diagnostics, prepared native
profile and lease-bound replay requirements are inventoried as E1–E3 in the
mapping. Their proposed schemas are not available runner hooks. Until those
gaps are delivered, IMA's existing adaptations remain tracked by its
[#1803](https://github.com/thepackengers/issue-manager-agent/issues/1803), rather
than acquiring compatibility guarantees for private methods.

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
