# Native Process workflow registry (#1915 slice B, lot #1922)

Consumer contract: `manaty/ameesh#1379` (body and comment 6207021753).
Slice A (#1918): the strict `process_workflow` compiler. Slice C: finite
verifiers and installed types. This document publishes the **seam** C plugs
into and the invariants B guarantees.

Slice A accepts several **distinct targets under one guard from the same
stage** — the reference card has `draft -> backlog` and `draft -> sprint`
under `planning`. The command carries `to_stage`, and the registry checks the
exact `(to_stage, guard)` edge from `from_stage`; only an exact duplicate
`(from, to, guard)` is refused (`edge-duplicate`).

## What B adds to the repository

* additive migration `0452_process_workflow_registry`:

  | object | addition |
  |---|---|
  | `work_items` | `workflow_ref` text NULL, `workflow_stage` text NULL, `workflow_version` bigint NOT NULL default 0 |
  | `work_item_events` | `command_id` uuid NULL, `workflow_stage` text, `workflow_version` bigint, `evidence` jsonb |
  | index | `work_item_events_command_uidx` partial unique `(work_item_id, command_id) WHERE command_id IS NOT NULL` |
  | constraints | a complete receipt is required `IS TRUE` (stage, version, `process-receipt/1` `evidence` with a 64-hex `intent.sha256`, known `action`): any missing key or nested `null` is refused; non-negative version; a stage without a reference is forbidden |

  The existing history is preserved: rows written before the migration have
  `command_id IS NULL` and are not concerned by the receipt constraint.

* `ameesh.process_registry`: the only native write of a workflow transition.
  No new CLI or HTTP route; lots without a workflow do not go through this
  module (`workflow-absent`) and keep `work.move`.

## Two distinct versions

* **Process revision** — `workflow_ref` + `workflow_version` on the lot and on
  the receipt. It changes only through `transfer_revision`, explicit,
  journalled and without regression; no automatic adoption of a new revision.
* **Optimistic transition version** — a command's `expected_version` and
  `Receipt.version`: the id of the last `work_item_events` row of the lot
  (0 if the journal is empty), across all writers (registry commands as well
  as the historical `work.move`). Any concurrent write of the journal makes a
  command still on the old version fail (`version-conflict`).

## Published API (slice C)

```python
from ameesh import process_registry as pr

pr.install_verifier("qa_started", verifier)      # guard → server callable
pr.install_verifier(pr.HOLD_VERIFIER, verifier)  # holds: entry AND exit

receipt = pr.apply_command(db, pr.Command(
    work_item_id=1915,
    command_id="…-uuid…",
    expected_version=12,                          # lot journal
    authority=pr.Authority(actor="…", source="runner", epoch=7, runner_id="…"),
    workflow_ref="canon/…@sha", workflow_version=3,
    action="advance",                             # advance | hold | release
    from_stage="review", to_stage="tests", guard="qa_started",
    evidence={"pr": "…", "merge_sha": "…"},       # structured facts, never a boolean
), spec=compiled_workflow)                        # slice A artifact

state = pr.registry_state(db, 1915)
stored = pr.receipt_of(db, 1915, command_id)
sources = pr.read_sources(context)                # real collections, in the tx
```

* `Verifier(context) -> Verification`: called **inside the transaction**, after
  the lot lock and the authority check. `context` carries `db` (the
  transaction), the locked lot row (C's snapshot: `id`, `title`, `type`,
  `assignee`, `reviewer` through `to_jsonb`, `closed_at`, `state`, `loops`,
  `workflow_*`), the `Command`, the compiled artifact and
  `from_stage`/`to_stage`. An uninstalled verifier (`verifier-uninstalled`), a
  return value that is not a `Verification` (`verifier-invalid`, a boolean is
  refused), an exception (`verifier-error`) or a refusal (`verifier-refused`)
  cancel the transaction. Verifiers come from the **installed** registry:
  `apply_command` and `transfer_revision` accept no `verifiers=` from the
  caller (the test bench goes through the private `_apply`/`_transfer`). Two
  reserved names: `revision_transfer`, and `HOLD_VERIFIER` (`hold`, slice C:
  the `verify_hold` callable dispatches on the command `action`) for both the
  entry into and the exit from a hold (`from_stage = to_stage` = the kept
  stage; `work_item.state` carries the hold being left).
* `Command`: `action` ∈ `advance` (guarded `from`→`to` edge; refused from a
  hold, `held`), `hold` (`blocked`/`waiting_human`, the stage is kept;
  `waiting_human` demands an open native decision request, L233), `release`
  (exit from the hold, verified like the entry; a still-open decision refuses
  it, `decision-pending`).
  An `advance` into a **released core** (`merged`, `promoted`, `closed`)
  carries the side effects the storage now refuses to a pinned lot (#1922/N8):
  the migration reservation is given back (#956) with its journal line, and
  the dependants are woken — once — under the dependency graph lock (L385). A
  `closed` core demands the closure in the evidence: `close_reason` among
  `abandoned`, `superseded` (with `superseded_by`, an existing lot other than
  itself) or `delivered` (with `sha`, the reviewed head, 7 to 64 hex), and
  writes `close_reason`/`superseded_by` plus the `closed`/`delivered`
  milestones (`close-reason`, `superseded-unknown`, `delivered-sha`).
* `transfer_revision(..., spec=…)`: first adoption (the stage becomes the
  artifact's initial one, a hold is kept; the initial stage must project the
  current core, otherwise `stage-mismatch`, and a terminal lot is never
  reopened, `terminal-reopened`) or a transfer (the stage is kept, it must
  exist in the new artifact and project the current core outside a hold). It
  requires the reserved `revision_transfer` verifier: nothing is possible by
  default. Slice C installs it (#4007, contract `revision-transfer/1`, see
  [Revision transfer](#revision-transfer-4007-a-of-2846)).
* `Receipt`: `accepted`, `replayed`, `event_id`/`version`, `state`,
  `workflow_ref`, `workflow_stage`, `workflow_version`, `intent_sha256`.
* `read_sources(context) -> Mapping`: the source read for C (which is pure and
  SQL-free), **inside the transaction and under lock**. The mapping declares
  only what is really collected (`SOURCE_KEYS`: `milestones`, `receipts`,
  `observations`, `decisions`, `deliveries`, plus the conditional `approvals`
  of #4006 and the conditional `observations` of #4425); a collection absent
  from the host is not declared: a verifier that requires it refuses
  `source-unavailable` (fail-closed). `approvals` is declared only when the
  host has a fresh, valid IMA #704 projection; `observations` is declared only
  when the host has a fresh, valid IMA observations projection (#4425).
  `receipts` carries `{accepted, guard, event_id, command_id, workflow_stage,
  workflow_version, sha256, evidence, facts}` — `guard` is the Process guard
  or the reserved verifier name (`hold`, `revision_transfer`).

## Transaction invariants

1. `SELECT … FOR UPDATE` on the lot before any check: two commands of the same
   lot never double.
2. Authority checked inside the transaction, `agent_registry` row in
   `FOR SHARE`: known actor, neither `stopped` nor `dead`; `runner` source with
   a live lease held by that runner at that epoch (`authority-lease`,
   `authority-epoch`, `authority-expired`). `explicit`/`session`: epoch 0. The
   check runs **before** reading the receipt: a revoked actor never recovers a
   result authorised in the past.
3. Lot revision = command revision = artifact revision (`ref-mismatch`,
   `revision-mismatch`, `spec-mismatch`).
4. Canonical intent: JCS + SHA-256 (`intent_sha256`) over the explicit inputs
   (action, key, expected version, revision, authority, facts;
   `from`/`to`/`guard` or `hold`). Replay of the same intent → accepted receipt
   (`replayed=True`); same key, other intent → `intent-conflict`.
5. Core, stage, `loops` (qa → build repair loop bounded by `work.MAX_QA_LOOPS`)
   and receipt in the same transaction: any error cancels everything
   (`rollback without half an event`).
6. Terminal cores (`promoted`, `closed`) are never reopened
   (`terminal-reopened`), including through workflow adoption; entering
   `waiting_human` demands an open native decision request and locks its row;
   its exit waits for the answer (`decision-pending`). A hold is never crossed
   (`held`): only a verified `release` leaves it.
7. A pinned lot (non-NULL `workflow_ref`) is written only by this module:
   `work.move`, `work.close` and `work.close_merged` refuse it — the merge
   sighting alone does not set `merged`. The filter is also **in the SQL** of
   those historical writes (and of the #1668 `release_unassigned` reopening):
   a concurrent adoption, which keeps the state, no longer lets a legacy move
   slip between the read and the UPDATE, and a registry `blocked` hold does
   not fall when a prerequisite closes (it is lifted only by a verified
   `release` command). Reading `workflow_ref` goes through the storage
   interface (`work.get` carries it like `reviewer`, `''` on a pre-0452
   database, and therefore through the mediated contract): commands also run
   on a mediated executor connection (VM session), where a raw SELECT is
   refused (B5).
8. **Lock order (N12)**: for `hold`/`release`, the lot row (`work_items`) is
   locked first, then the open decision requests (`agent_mailbox`) — the order
   of the single L233 statement (`work.move(held=True)` locks the lot, then
   the requests in its `EXISTS … FOR UPDATE`); the reverse order could
   deadlock with it on the same request, and `decisions.answer` closes the
   request before moving the lot in two separate transactions. The lock keeps
   the request closed-proof until commit and `_pending_decision` is read after
   both locks, so a request committed while the lot lock was awaited is seen.
   An INSERT committed after that read is not blocked by a row lock
   (PostgreSQL has no gap lock): the `ask` path must take the lot lock before
   inserting its request for a full serialization. An `advance` into a
   released core takes the dependency graph lock **before** the lot, like
   `work.close`/`close_merged`.
9. **Core before the hold (N10)**: adopting or transferring a held lot is
   accepted only when the journal proves the core before the hold (the last
   line that is not a hold) and the adopted/kept step projects it; otherwise
   `hold-core-unproven` (nothing proven) or `stage-mismatch` (divergence).
   The resume rewrites that proven core: the check is made at adoption, the
   control is at release.
10. **Artifact trust boundary (N5)**: `spec` is supplied by the caller, the
    server component that compiled the revision. The registry checks
    `workflow_ref`/`workflow_version` against the command and the lot
    (`spec-mismatch`), never the artifact content: the ref names the commit
    and slice A publishes no digest. If a later artifact publishes one, the
    seam must bind it.

## Consumer admission matrix (#1915 C-admission)

Contract: `manaty/ameesh#1379` (owner handoff 02:58Z, consumer qualification
08:45Z). This section is **documentation only**: it publishes what the installed
native seam decides today, what it refuses and which consumer-policy facts are
still missing. It adds no verifier and no admission code. "Today" means: no production
caller invokes `install_verifiers` or `apply_command` yet; the seam is
reachable from the Python API and the tests only.

Q1/Q2/Q3 below are a first **capability/refusal** deliverable. They do **not**
qualify `planning`/`start_work` against the business contract: those two guards
accept today on the lot title and on the assignee alone, and
`capability_report()` lists them in `executable_guards` (installed, no declared
source). "Executable" there means "callable", **not** "qualified": until the
missing facts below are published, a consumer must treat them as not admitted
(a refuse-by-default for these two guards is a possible later change, not made
here).

### Requirement / native source / refusal / missing

| # | requirement (consumer contract) | native source read today | refusal today | still missing |
|---|---|---|---|---|
| 1a | `planning` **and** `start_work`: current spec approved | none: `_check_planning` reads only `title`; the locked row also carries `app`, `workstream`, `type`, `state`, `reviewer`, `workflow_*` | `evidence-missing` (no title); `type-unsupported` (type outside `INSTALLED_TYPES`) | "current spec": artifact/digest, approving actor, validity window — **consumer policy, not published** |
| 1b | `planning`/`start_work`: budget | three native envelopes, none read by C: lot `work_items.budget_usd numeric(12,4)` (migration 0004, **not projected** by `_lock_lot`); account `account_budgets` (migration 0443, daily per billing account); agent `budget_usd` (canon declarative field, `canon_sync.DECLARATIVE`) | none: no native budget check | which envelope (lot vs agent vs account), threshold, period — **missing**. Shape matters: a lot field added to `_lock_lot` is a plain projection and **escapes** `source-unavailable` (an absent field reads as empty, not as a missing source); an account/agent envelope would be one more declared collection in `SOURCE_KEYS`, fail-closed when absent |
| 1c | reviewed governance authority | no generic native object; only candidates: native decision request L233 (open requests only), module proof `work_module_milestones` (`schemas`/`rules`, `actor_role`, `separation_of_duties`), `reviewer` designation | nothing generic | source role/actor and review rule — **missing**; any acceptance would be invented |
| 2 | `revision_transfer`: admission/transfer explicitly reviewed, bounded to lot/project/revision/actor, refused on absent or stale proof | reserved name `TRANSFER_VERIFIER = "revision_transfer"` (`process_registry` and `process_verifiers`), called by `transfer_revision`; **installed** by `install_verifiers` (#4007) and published in `RESERVED_VERIFIERS`; source: the conditional `approvals` collection (#4006) | contract `revision-transfer/1` (#4007): see [Revision transfer](#revision-transfer-4007-a-of-2846) | approver identity is a GitHub login compared to agent names (see the named limit there) |
| 3 | `docs`, `investigation`, `governance-tracking`, `promotion`, `human_operation` admitted by an **active project module**, without bypassing the SQL guard | `INSTALLED_TYPES` carries the five (+ `bug`, `evolution`); completion is per type; SQL guard `ameesh_module_requirements` (migration 0363): `bug`/`evolution`/`improvement` stay admitted, any other type needs `project_modules.types` for the lot's project; modules are written only by canon sync | SQL `AMEESH_MODULE_RULE: work item type % has no active module in project %` (every write of the lot — insert, state change or any other update — and module-milestone insert; trigger of 0363, `ameesh_module_requirements` redefined by 0427); C `type-unsupported` | the active module is canon-declared: `extensions.ameesh.projects.<project>.modules` names **installed** descriptors only (`modules.installed()`), whose types must be disjoint within a project; the installed registry also discovers external descriptors installed in the environment through the `ameesh.modules` entry point group (#2847); a private descriptor (for example ThePackengers', name chosen by its owner, not in this repository) declares the five, so a project admits them by selecting that module in its canon, while a project without it is still refused by the SQL guard; since #5001 ameesh also ships the built-in `work-types` module, which declares the same five with no schema, rule or behaviour, is never always-active and is selected per project like any module (`modules: [work-types]`), so a standard install admits them without a private descriptor and the SQL guard is unchanged; the exact external descriptor revision and project/canon admission revisions remain to be supplied by its owner; `improvement` is admitted by the legacy SQL but absent from `INSTALLED_TYPES`, so C refuses its `advance` `type-unsupported` (`hold`/`release` do not check the type and pass) |
| 4 | `observations` staging/preprod: reliable native source, fail-closed | conditional `observations` collection (#4425): the local `ameesh-observations/1` projection derived from the bureau's append-only journal; `deployed` records from native staging attestations, QA `pass`/`fail` from the IMA contract #796 when it exists, human acceptances from #4006 | absent projection → `source-unavailable` for `merged_and_staging_deployed`, `staging_pass`/`staging_failure`, `preprod_*`, `repair_started`; revoked/superseded/expired/future record → `evidence-missing`; stale artifact/environment/kind → `revision-mismatch` / `observation-result-mismatch`; the guard still requires an accepted receipt referencing the observation | QA envelope (external IMA #796) — named missing; the reader and the witness are ready and nothing is invented |

### Refusals: two levels (B and C)

The refusal codes live at two levels, and only B's reach a caller of
`apply_command`:

* **B (`apply_command`)** raises `RegistryRefusal` with its own code. For a
  guard, it is `verifier-uninstalled` (no callable installed) or
  `verifier-refused` (C answered `ok=False`); a refusal writes no journal and
  no receipt (rollback). B carries only C's message (`process_registry._verify`):
  C's code is **not** propagated.
* **C (the verifier callable, `server_verifier`)** returns a `Verification`
  whose `facts["code"]` is the precise code (`source-unavailable`,
  `type-unsupported`, `evidence-missing`, …). It is observable only by calling
  the callable (or `verify(command, snapshot)`) directly.

So through B, `source-unavailable`, `type-unsupported` and `evidence-missing`
are all `verifier-refused`; telling them apart needs C's callable. Propagating
C's code through B is a **missing seam point**, named here, not added.

C checks, in order: `stage-stale`, `terminal-reopening`, `hold-advance`,
`guard-unsupported`, `type-unsupported`, `source-unavailable`, then the
reference checks (`evidence-invalid`, `evidence-missing`) and the guard
itself. For an `advance`, B refuses first, before calling C:
`stage-mismatch`, `terminal-reopened`, `held`, `edge-undeclared`. For `hold`/`release`,
C checks neither type nor guard sources (except `decisions` for
`waiting_human`).

* `verifier-uninstalled` (B) — B knows the guard name, no server callable is
  installed for it (`revision_transfer` too, on a host that did not call
  `install_verifiers`);
* `source-unavailable` (C) — a collection the guard declares is absent from
  the snapshot; fail-closed, after the earlier refusals above;
* `type-unsupported` (C) — the lot type is outside `INSTALLED_TYPES`; the SQL gate
  (`AMEESH_MODULE_RULE`) is **separate** and applies at every write of the lot
  (insert, state change, any other update), so the same lot can be refused by
  either gate for different reasons
  (`improvement`: SQL admits, C refuses; a type no module declares: SQL refuses).

### Reserved names

| name | installed by `install_verifiers` | in the code matrix | note |
|---|---|---|---|
| `hold` | yes | yes | `hold`/`release`; `waiting_human` additionally needs `decisions` |
| `revision_transfer` | yes (#4007) | yes (`RESERVED_VERIFIERS` = `("hold", "revision_transfer")`; requires the `approvals` source, so `capability_report().refusal` → `source-unavailable` without a fresh projection) | `transfer_revision`; contract `revision-transfer/1`; `installation_receipt()` |

### First deliverable admitted now (Q1/Q2/Q3)

* **Q1** — describe the existing active-module mechanism (SQL guard + per-type
  completion) and qualify it with discriminating SQL witnesses (no module: the
  lot is refused at insert; a type an active module declares: admitted). The
  generic external mechanism is witnessed by the test-only `sample` descriptor
  declaring the five (#2847); it adds no milestone schema or lifecycle rule, and
  selecting a descriptor stays a per-project canon decision. The built-in
  `work-types` module (#5001) declares the same five for a standard install,
  inactive until a project selects it. ameesh discovers
  external descriptors through the `ameesh.modules` entry point group, validates
  them fail-closed, and exposes installation evidence with `ameesh module
  --list`; the production private descriptor revision and project admissions
  remain with its owner. No consumer policy is invented.
* **Q2** — (superseded by #4007, which publishes the contract below)
  document `revision_transfer` as absent (then
  `verifier-uninstalled`). Publishing the name stays documentation for now; no
  `install_verifier("revision_transfer", …)` is added without a published
  contract (lot/project binding, identity of the reviewed revision and actor,
  proof source, staleness rule, independence rule).
* **Q3** — (superseded by #4425, see [Observations journal](#observations-journal-ima-4425-b-of-2846))
  the source is now conditional: without a fresh projection, C still refuses
  `source-unavailable` (witnessed on C's callable; through B it is
  `verifier-refused`); with a fresh projection, the guarded collection is
  present and a revoked/superseded/expired record is absent from it.

`planning`/`start_work` stay on today's subset (title, assignee, installed
type); the matrix publishes "not admitted" for the missing facts below.

### Missing consumer-policy facts (named, with expected source)

| # | fact | expected source |
|---|---|---|
| 1 | current approved spec (for `planning` and `start_work`): artifact/digest, approving actor, validity window | consumer policy |
| 2 | budget: envelope (lot `budget_usd`, account `account_budgets`, agent `budget_usd`), threshold, period | consumer policy (+ a native declared collection, or a plain `_lock_lot` projection that escapes `source-unavailable`) |
| 3 | governance: source role/actor and review rule | consumer policy / module rules |
| 4 | transfer proof: binding to lot (`work_item_id`) and project (`app`/`workstream`, native), reviewed revision identity and reviewing actor, proof source, staleness rule, independence rule, refusal codes | consumer policy; codes published with the contract |
| 5 | module declaring the five types (name, content) | pilot decision |
| 6 | reliable observation source: shape, attestation, freshness | #4425 publishes the `ameesh-observations/1` projection, the native staging attestations, the supersession/freshness rules and the fail-closed reader; the QA envelope waits for the external IMA #796 contract |

### Seam unchanged

`process_registry.install_verifier(name, callable)` and `read_sources(context)`
remain **the** generic seam: the source read happens inside the transaction,
under the lot lock and after the authority check; the collections are declared
(`SOURCE_KEYS`: `milestones`, `receipts`, `observations`, `decisions`,
`deliveries`, and the conditional `approvals` of #4006 / `observations` of
#4425); the verifier returns a `Verification`; no
caller-supplied verifier and no client boolean. A field that needs more asks
for **one more declared collection**, read in the same transaction; absent ⇒
`source-unavailable`, never a private SQL read by C.

## Approval bridge IMA/GitHub (#4006, C of #2846)

Owner decision **#143364** (option a, signed 27/10 06:58Z, approval #132): no
new token. The bureau reuses an existing GitHub token that already reads the
IMA repository's issues, keeps it in the bureau vault on `bureau-goal` (0600)
and records its name and real scopes on lot **#4005**, never its value. There
is **one reader**, on `bureau-goal`; agent hosts read a local read-only
projection and never call GitHub themselves.

### Source and authority

The source is the IMA-owned record: issue
`thepackengers/issue-manager-agent#704`, its comments and state. IMA remains
the decision authority; this bridge only *reads* the records and normalises
them. `source=api` is never an authority (it is refused by
`require_approval(..., source="api")`). The authority's name is not a code
constant (#4259): it is `approvals.authority` in the configuration, mandatory
and without default (`ima` for the TPK mesh); the reader writes it into the
projection, and a projection whose `authority` differs from the host's
configured one is refused (`projection-source`). No IMA database, API or private content is copied into
this repository.

An owner approval is a comment that:

* is authored by a configured approver (for #704: `smichea`), of GitHub type
  `User` — a bot comment is never an approval;
* starts with an explicit marker (`Owner approval` / `Owner approval
  recorded`), or carries the machine-readable envelope
  `<!-- ameesh-approval:v1 {...} -->` (schema `ameesh-approval/1`);
* names at least one exact artifact: a `blob/<revision>/<path>` URL, an
  `owner/repo@<revision>:<path>` pin, or a strict prose reference
  (`Reviewed artifact: <sha>, knowledge/...`). A branch or tag name is
  refused (`mutable-revision`); a short revision is resolved from a full SHA
  in the same body, otherwise through the GitHub commits API, and the
  resolved SHA must match the requested prefix. When the real reader runs,
  even a 40-hex revision cited in the body is re-read against the commits
  API, so a branch named like a SHA or an unrelated full SHA cannot pass.
  A revocation by scope ignores the revision: a branch URL or an unresolved
  short SHA still revokes its repository + path (it never becomes a silent
  no-op). An edited revocation still revokes; an edited replacement approval
  fences **every** repository + path it names, regardless of each reference's
  revision, so the previous approval cannot become current again. A
  recognised reference that cannot be bounded (out-of-scope repository,
  invalid path, malformed envelope entry) refuses the whole projection: a
  non-empty partial list is never taken as proof that the old approval is
  fully covered.

A comment edited after creation (`updated_at != created_at`) is refused
(`approval-edited`): the projection never accepts a decision whose text can
have changed after the fact.

### Supersession and revocation

* **Supersession** is per `(repository, path)`: the last approval of that
  path replaces every earlier one, even when it approves another revision.
  A superseded approval is never resurrected if the newer one is later
  revoked — the path then has no current approval (fail-closed).
* **Revocation** is an explicit later comment by an approver whose first line
  is `Owner approval revoked|withdrawn|superseded` (or a structured envelope
  with `decision: revoked`). It targets `#issuecomment-<id>` references, or,
  when it names none, the artifacts it pins (same repository + path). It
  applies only to approvals created strictly before it; an approval posted
  after a scope revocation is a new decision. A revocation of an unknown or
  later comment is witnessed (`revocation-time`), never ignored silently.
* A `revocation` record is published for audit; approvals carry
  `state: current|superseded|revoked`, `superseded_by`, `revoked_by`.

### Projection (`ameesh-approvals/1`)

`bureau-goal` writes the projection atomically (0644, parent not
group/other-writable, `.lock` serialisation, temp file + `os.replace` +
fsync). It contains no secret: repo/issue/state, `source_digest` (SHA-256 of
the issue state and every comment id/body hash), `generated_at`,
`expires_at`, approvers, normalised approvals, revocations, refusal
witnesses (comment id + stable code, never the body), reader identity and a
`digest` (JCS + SHA-256 of the rest). A host rejects a projection that is
missing, unreadable, a symlink, group/other-writable, too large, of another
schema/authority, digest-tampered, in the future, older than its TTL or its
consumer's `max_age_s`. `owner_uid` can pin the expected owner of the file
and its directory (0 for a root-synchronised copy).

**The file does not prove its provenance.** The local configuration therefore
carries the expected identity in `approvals.authority` and
`approvals.source`: `repo`, `issue`, `approvers` and, when the approvals cover
another artifact repository, `artifact_repos`. `load_projection` compares the
projection's `authority`, `source.repo`, `source.issue`, `approvers` and every
approval/revocation artifact with that identity; a mismatch, or an absent identity, is `projection-source` and the
approvals source is omitted from `read_sources` (fail-closed). The bureau and
each host must declare the same identity; only the bureau also needs
`token_file`. A valid 0644 projection produced from another repository, issue
or approver therefore cannot be substituted for the expected one.

### Configuration

```json
{
  "approvals": {
    "authority": "ima",
    "projection": "/var/lib/ameesh/approvals/ima-704.json",
    "max_age_s": 300,
    "owner_uid": 0,
    "source": {
      "repo": "thepackengers/issue-manager-agent",
      "issue": 704,
      "approvers": ["smichea"],
      "artifact_repos": ["thepackengers/home"],
      "token_file": "/etc/ameesh/secrets/ima-github.token",
      "api_base": "https://api.github.com",
      "ttl_s": 300,
      "timeout_s": 10,
      "total_timeout_s": 30,
      "reader": "bureau-goal"
    }
  }
}
```

`authority` is mandatory, without default (#4259): absent or malformed,
`refresh` refuses `reader-config` and the local read refuses
`projection-source`. `token_file` names a 0600 file; a token *value* in the
configuration is refused. `api_base` must be https outside the loopback. The bureau runs:

```
ameesh process-approvals refresh     # reads #704, writes the projection
ameesh process-approvals refresh --dry-run
ameesh process-approvals check       # freshness, counts, digests
ameesh process-approvals show --repo R --revision <full-sha> --path P
```

A host only uses `check` / `show`; it performs no network access. Its
`approvals` section must keep the **identity** fields (`authority`,
`source.repo`, `source.issue`, `source.approvers`, optional
`source.artifact_repos`) and the projection path; it may keep `source.token_file` (never read by `check` /
`show`) or omit it. Omitting the identity is a refusal (`projection-source`),
not a default.

### Seam and refusal codes

`process_registry.SOURCE_KEYS` gains the **conditional** `approvals`
collection. `read_sources` adds it only when `local_projection()` succeeds
(fresh file **and** local identity match); otherwise the key is absent, so a
verifier that requires approvals refuses `source-unavailable` (fail-closed),
exactly like `observations`. The
`source=` argument of `require_approval` is the **approval source** (only
`github` is accepted); it is not the command's authority source
(`runner`/`explicit`/`session`), which B checks separately. The value is
`{"projection": {generated_at, expires_at, digest, source_digest, repo,
issue, approvers, authority}, "records": [...]}`. `process_verifiers.SOURCE_COLLECTIONS` and
`Snapshot.approvals` carry it; `approval_records(snapshot)` returns the
normalised records. A C verifier applies the exact rules without reading a
file or trusting a client: `process_approvals.projection_from_source(...)` /
`approval_for_source(...)` rebuild the projection from the snapshot mapping
and refuse with the same bridge codes. This lot publishes **no new Process
refusal code**: #4007 (`revision_transfer`) owns the public contract and maps
these bridge codes to it (see [Revision transfer](#revision-transfer-4007-a-of-2846)).

Bridge codes (stable, module-level; `ApprovalError.code`):

| family | codes |
|---|---|
| reader | `reader-config`, `reader-token`, `reader-timeout`, `reader-unavailable`, `reader-http`, `reader-invalid`, `reader-bounds` |
| projection | `projection-missing`, `projection-unreadable`, `projection-permissions`, `projection-owner`, `projection-bounds`, `projection-invalid`, `projection-digest`, `projection-stale`, `projection-future`, `projection-source` |
| approval | `approval-missing`, `approval-stale`, `approval-revoked`, `approval-superseded`, `approval-ambiguous`, `approval-expired`, `approval-api-not-authority`, `approval-source` |
| normalisation witnesses | `author-bot`, `author-not-approver`, `approval-edited`, `approval-no-artifact`, `approval-envelope-invalid`, `mutable-revision`, `revision-unresolved`, `path-invalid`, `out-of-scope`, `revocation-unscoped`, `revocation-time` |

### Qualification

`tests/test_process_approvals.py` (57 pure/fixture cases, no database and no
network in the core, one loopback HTTP case) covers every refusal witness
requested by the lot: absent, revoked, stale-head, expired approval, stale
projection and unavailable reader. `tests/test_process_approvals_704.py`
runs only from `bureau-goal` with the real #704 fixture
(`AMEESH_APPROVALS_704_FIXTURE`); it is skipped by default and becomes the
real qualification once the #4005 token is recorded. The fixture file stays
in the bureau vault, never in Git. A read-only pre-check from an existing
host token on 27/10 normalised the two owner approvals cited by the Process
contract (P1 `d918149…:knowledge/architecture/ima-ameesh-work-contract.md`,
Process `6c771e2…:knowledge/architecture/ima-ameesh-process-contract.md`);
it is not the official bureau qualification and does not replace #4005.

### Discriminating cases proposed for review (no code)

Witnesses name the level they observe: **C** = the verifier callable
(`facts["code"]`), **B** = `apply_command` (`RegistryRefusal.code`).

| control | witness |
|---|---|
| `planning` | empty title → C `evidence-missing`, B `verifier-refused`; otherwise accepted, with spec/budget/governance **not** checked (the witness must show it) |
| `start_work` | no assignee → C `evidence-missing`, B `verifier-refused` |
| type | `docs` lot, no module → SQL `AMEESH_MODULE_RULE` at insert; `release` lot with the `it` module active → SQL admitted; `improvement` lot → SQL admitted, then `advance` C `type-unsupported` / B `verifier-refused`, while `hold`/`release` pass |
| `revision_transfer` | #4007: see the refusal contract in [Revision transfer](#revision-transfer-4007-a-of-2846) (existing codes only, the bridge code in `facts["detail"]`) |
| `observations` | without a fresh projection: any staging/preprod advance → C `source-unavailable`, B `verifier-refused`, no effect (rollback). With a fresh projection: a valid record referenced by an accepted receipt is accepted; a revoked, superseded, expired or future record is absent → C `evidence-missing`; a bad revision or environment → C `revision-mismatch` / `observation-result-mismatch` |

## Observations journal IMA (#4425, B of #2846)

Owner decision #155437 (option a, signed 29/10 00:05Z, approval #146) makes
IMA the trusted collector: deployments, QA results and human acceptances are
native observations, written through a local journal API and projected
read-only to the hosts. Lot B implements the collector and the fail-closed
reader; the real qualification runs from `bureau-goal` with the #4424
credential, and the QA envelope waits for the external IMA #796 contract
(nothing is invented).

### Journal and projection are distinct

The **journal** is an append-only JSONL file on `bureau-goal` (0600, directory
0700). Only `process_observations.Journal` writes it:

* `record(observation)` appends a normalised record; the same id with the same
  `source_digest` is idempotent, another digest is `observation-edited` (the
  journal is never rewritten);
* `supersede(id, by, reason)` appends the replacement;
* `revoke(id, reason, actor)` appends a terminal revocation (`rev-N`).

Until the external QA contract #796 exists, the writer refuses any `pass` or
`fail` observation in `staging`/`preprod` (`observation-invalid`): the config
refusal is not the only boundary. Only a human acceptance (`environment
=acceptance`, provenance `github-comment`) can be a `pass` today.

Each entry carries `seq`, `prev` (the previous entry's digest) and its own
JCS+SHA-256 digest, so a tampered or truncated line is `journal-invalid`. The
history of corrections and revocations survives a GitHub comment edit or
deletion: a change in the source digest revokes the old record, it never
rewrites it. **Named limit**: the chain proves order and content, not
completeness; a truncation at an entry boundary (removing whole trailing
lines) is not detectable from the file alone. The journal is owner-only and
must be backed up; the projection digest is integrity, not a signature. A
host-side "last seen" anchor is not implemented.

The **projection** `ameesh-observations/1` is derived from the journal at each
refresh: `schema`, `authority`, `source` (`journal_id`, `repos`, `issues`,
`source_digest`, `entries`), `generated_at`, `expires_at`, `collector` (host,
identity, version, reader), `observations`, `revocations`, `refusals`, and
`digest` (JCS+SHA-256 of the rest). The projection carries every current
record plus the most recent `MAX_HISTORY` (1000) superseded/revoked records;
the journal keeps the full history. `show --all` reads that bounded window,
older history stays bureau-only. Each observation carries `id`, `kind`
(`deployed`/`pass`/`fail`), `environment` (`staging`/`preprod`/`acceptance`),
`work_item_id` (null for a global acceptance), `app`, `artifact` (the full
revision), `source_revision`, `actor`, `observed_at`, `state`
(`current`/`superseded`/`revoked`), `superseded_by`, `revoked_by`,
`provenance` and `digest`.

### Collected sources and kind mapping

* **Native staging attestations**: `ameesh-staging-actif/1` under
  `mise-a-jour/actif-*.json`, and cut-off reports `ameesh-release-cutoff/1`
  under `release/reports/*.json` → `deployed` (environment `staging`). A
  report is used only when its project is `staging.app`, its result is
  `released` or `reconciled`, `staging.exit == 0`, and the embedded
  attestation carries the same full `release_sha`. `staging.hosts` is
  **required** and names the expected completeness: every named host must be
  present (active or reported). Every active host's `actif` must be a 7–40 hex
  prefix of the attested `sha` (the rule already used by
  `staging_pending._merge_attestation`); a reported host must carry a
  non-empty `raison`, must not also be active, and must appear in
  `staging.reportable_hosts` (a required agent VM is never reportable). A
  mismatched, empty or contradictory host proof is `source-invalid`: the name
  alone never proves that this commit is active on that host.
  The deployment timestamp is the attestation's own `date`, never the release
  cut `completed_at`: a later cut for an older commit cannot supersede a newer
  deployment. `completed_at` stays in the digest and the provenance.
  The observation id is derived from a digest of the attestation (or report)
  and the lot, so the attestation's notification bookkeeping can be rewritten
  without changing the id.
* **Human acceptances**: the local #4006 projection
  (`process-approvals`), with its revocation and its expiry → `pass`,
  environment `acceptance`, no lot and no app (global acceptance), provenance
  `github-comment`. No freshness threshold is added to #4006's own. These
  observations are **audit-only today**: no Process guard consumes
  `environment=acceptance` (the revision-transfer guard reads #4006 directly);
  wiring a consumer is a separate decision, not inferred here.
* **QA IMA**: the envelope is the external IMA #796 contract. Until it exists,
  a configured `qa` key is refused `reader-config` and no QA `pass`/`fail` is
  collected; the reader and the witness are ready for the contract.

Kind mapping of the relay: `merged_and_staging_deployed` and
`preprod_deployed` → `deployed`; `*_pass` → `pass`; `*_failure` → `fail`.
`repair_started` is mapped to nothing: it is not a deployment proof and no
guard accepts it as one.

### Freshness, supersession and revocation

The projection TTL is 300 s by default (`source.ttl_s`, minimum 120 s =
two refresh periods), refreshed every minute, with a 60 s clock skew.

* `deployed` never expires by age. The latest non-revoked deployment of the
  same `(app, work_item_id, environment)` supersedes the previous ones
  (`next-deployment`); a newer record in another environment does not.
* QA `pass`/`fail` expire after `QA_TTL_S` (7 days).
* Human acceptances follow the #4006 projection and the approval's own
  `expires_at`; an expired approval is revoked `acceptance-expired`.
* An approval that disappears from a fresh #4006 projection is revoked
  `source-disappeared`; a changed source digest is revoked `source-edited`,
  never rewritten. Revocation is terminal.

### Identity and fail-closed reading

Hosts declare `observations.authority` and `observations.source`
(`journal_id`, `repos`, `issues`, even empty). A projection whose authority,
journal, repositories or issues differ is `projection-source`; a
GitHub-provenance observation whose repository is outside the identity is
also `projection-source`. The host refuses a projection that is missing,
unreadable, group/other writable, owned by another uid, over 4 MiB, with a
bad digest, an unknown schema, an expired TTL, older than `max_age_s`, or in
the future. The expected owner defaults to the reading uid; a copy
synchronised by root declares `owner_uid: 0`. The journal lives in a
0700 directory and the published projection in a separate 0755 directory
(never group/other writable), so a different-uid host can read the copy. The projection is written atomically
(0644, owner-writable only); the journal is 0600 in a 0700 directory.

### Seam and refusal codes

`process_registry.SOURCE_KEYS` gains the **conditional** `observations`
collection. `read_sources` adds it only when
`process_observations.local_projection()` succeeds; the value is the list of
current, non-expired, non-future records for the lot and app (global
acceptances included), in the shape the guards already read:
`{id, kind, env, sha, state, lot, app, actor, observed_at, source_revision,
provenance}`. A revoked, superseded, expired or future record is absent, so
an existing Process guard refuses `evidence-missing`; a missing projection
removes the key and the guard refuses `source-unavailable`. No network read
happens on a host, and this lot publishes **no new Process refusal code**.

Observation and journal codes (stable, module-level; `ObservationError.code`):

| family | codes |
|---|---|
| collector | `reader-config`, `reader-unavailable`, `reader-bounds`, `acceptances-unavailable` |
| journal | `journal-missing`, `journal-unreadable`, `journal-permissions`, `journal-bounds`, `journal-invalid` |
| projection | `projection-missing`, `projection-unreadable`, `projection-permissions`, `projection-owner`, `projection-bounds`, `projection-invalid`, `projection-digest`, `projection-stale`, `projection-future`, `projection-source` |
| observation | `observation-missing`, `observation-invalid`, `observation-edited`, `observation-revoked`, `observation-superseded`, `observation-expired`, `observation-future`, `observation-stale`, `observation-source` |
| collection witnesses | `source-invalid`, `source-bounds` |

### Qualification

`tests/test_process_observations.py` covers the journal (append-only chain,
idempotence, edited source, revocation), the derived states, the projection
(digest, permissions, owner, stale, future, identity), the reader (lot/app
filter, QA age, deployed never expiring, stable refusals).
`tests/test_process_observations_collector.py` covers the staging fixtures,
the native supersession, the acceptance source with revocation, supersession,
disappearance, expiry and edit, the CLI, and the absence of writes on a
dry run or an unavailable source. `tests/test_process_observations_registry.py`
proves the B/C seam and the guard acceptance/refusal on the real shape.
`tests/test_process_observations_real.py` is skipped by default; on
`bureau-goal` it reads `AMEESH_OBSERVATIONS_REAL_CONFIG`, runs a dry refresh
on the real attestations and the #4006 projection, and never writes.

## Revision transfer (#4007, A of #2846)

`process_verifiers` installs the reserved `revision_transfer` verifier
(`install_verifiers`, after the 14 guards and `hold`). B calls it from
`transfer_revision`, for a first adoption as for a later transfer, inside the
transaction and after the lot lock, the authority check and B's own refusals
(`terminal-reopened`, `stage-mismatch`, `revision-regression`,
`hold-core-unproven`…). C stays pure: the proof comes from `read_sources`, the
`approvals` collection of #4006, never from a file, a network read or the
client.

### Contract `revision-transfer/1`

* **Target revision**: the command's `workflow_ref` is an exact pin
  `owner/repo@<40-hex sha>:<path>`; a branch, a tag, a short sha or any other
  form binds to no exact approval and is refused.
* **Proof source**: the `approvals` projection (IMA #704, `source=github`,
  authority read from the projection, i.e. the configured
  `approvals.authority`, #4259). The approval source and the contract version are module
  constants (`TRANSFER_APPROVAL_SOURCE`, `TRANSFER_CONTRACT`): no caller, test
  included, chooses another source or clock (`verify`, `server_verifier` and
  `install_verifiers` take neither `source` nor `now`).
* **Approval**: `process_approvals.require_approval` must return the single
  **current** approval of that exact artifact (repo, sha, path), not expired;
  the projection must still be fresh when C reads it.
* **References** (`evidence`, exactly these keys, any other is refused):
  `approval` (the approval `id` or its `comment_id`; it must be the current
  one), `lot` (the locked lot id), `project` (the locked lot's `app`).
* **Independence**: the approver (GitHub login) is in the projection's
  approvers and is neither the command actor nor the lot assignee
  (case-insensitive).
* **Terminal lot**: refused even if B would let a pinned terminal lot through.

Accepted: the verification facts carry `proof` — contract, approval source,
authority, lot, project, target `workflow_ref`/`workflow_version`, the
approval (`id`, `comment_id`, `author`, `created_at`, `url`, `body_sha256`,
`artifact`) and the projection (`digest`, `source_digest`, `generated_at`,
`expires_at`, `repo`, `issue`). B writes them into the receipt
(`work_item_events.evidence.verification.facts.proof`).

### Refusal contract (existing codes only)

No new Process refusal code is published (a new code goes through the pilot
first). The bridge code, when there is one, stays readable in
`facts["detail"]` and in the message (through B: `verifier-refused`, with the
message).

| case | C code | `detail` |
|---|---|---|
| projection absent, unconfigured, stale (dropped by `read_sources`) | `source-unavailable` | — |
| projection expired or unreadable when C reads it | `source-unavailable` | `projection-stale`, `projection-future`, `projection-invalid` |
| no approval for the artifact | `evidence-missing` | `approval-missing` |
| approval of another revision (head changed, other target revision) | `revision-mismatch` | `approval-stale` |
| approval superseded by a later one for the same path | `revision-mismatch` | `approval-superseded` |
| approval revoked | `evidence-invalid` | `approval-revoked` |
| approval expired, ambiguous, invalid, non-github source | `evidence-invalid` | `approval-expired`, `approval-ambiguous`, `approval-invalid`, `approval-source`, `approval-api-not-authority` |
| cited approval is not the current one; approver outside the projection's approvers | `evidence-invalid` | — |
| `workflow_ref` not an exact pin | `evidence-invalid` | — |
| unknown reference key / missing reference | `evidence-invalid` / `evidence-missing` | — |
| other lot / other project (or lot without project) | `item-mismatch` | — |
| approver is the actor or the assignee | `reviewer-not-independent` | — |
| terminal lot | `terminal-reopening` | — |

### Installation receipt

`installation_receipt(registry)` (schema `process-verifier-install/1`) says
what is **really** installed: `installed` is true only when the callable under
`revision_transfer` is C's own; it carries the contract, the approval source,
the required references and source, the ameesh version and the
SHA-256 of the two deciding sources (`process_verifiers`,
`process_approvals`). A foreign callable under the name reads
`installed: false`. It no longer carries the authority (#4259): the authority
is configuration, not code, and is recorded in each accepted proof
(`verification.facts.proof.authority`).

### Named limit

The independence rule compares a GitHub login (the approver) with agent names
(actor, assignee). If an agent ever posted on IMA #704 under the approver's
GitHub account, C could not tell; the bridge's author rule (`User`, configured
approvers) and IMA remain the authority on who approved.

### Proofs

`tests/test_4007_revision_transfer.py`: on C's callable, acceptance with its
proof and every refusal above; on the real B (`transfer_revision`, public
API, verifiers installed by `install_verifiers`, projection read by
`read_sources`), the first adoption of a test lot is accepted with its
receipt, and a revoked approval or a missing projection leaves no event and
no pinned revision.

## TPK admission (#4134, E of #2846)

First native admission of the TPK Process revision (entry 1 of the #2846
breakdown) and the version reconciliation. A qualification, not product code:
the admitted revision is pinned on a dedicated test lot; no consumer mapping is
activated and no IMA upgrade is performed.

### Admitted revision and authority

| object | value |
|---|---|
| publication pin | `thepackengers/home@83525afb03d4c70e23bdb70f80d9545c518dc15a:ameesh/processus/tpk-work-delivery.md` (merge of home#253, 2026-10-20T23:51:08Z; blob `6d677fe09e678d1e2ba1a11cf0e492585f574b45`) |
| card | 14 751 bytes, SHA-256 `1db5cd93bbd2cb231e7ccfb47dd55f8e7a0872e00c5f767bc431bc2ad01734f0`; `updated: 2026-10-16` |
| workflow block | 42 lines, SHA-256 `4a39217ed5a2a31014a5ecb6bc7917d89c9e6e1208cc22ddbef17aac9c9f0b91`; byte-identical to the approved contract block |
| approved contract | `thepackengers/issue-manager-agent@6c771e2e16bc04e8ce3b04a55df67ad4aca77cb8:knowledge/architecture/ima-ameesh-process-contract.md`; approval #704/6206927953 |
| compiled | 14 stages, 46 edges, 7 types, initial `draft`, projection `thepackengers/3` |

The pin is the publication of the approved contract; the block comparison is
raw (not a YAML re-normalisation). The card adds only presentation keys
(`description`, `status`, `tags`, `generated`, `sources`).

**Exact-artifact approval.** `revision_transfer` needs a current approval of
the exact artifact named by `workflow_ref`. The #704 approval 6206927953
approves the IMA contract, not the home card; the `APPROVED` review of home#253
is a publication approval and C reads only #704 issue comments. With the real
#704 projection (bureau G1, 2026-10-28T02:54Z: 4 approvals, 0 refusals,
digest `5c3ce5e276db7e933ef47506450c8f02982bb691890485bf7d109effb4a49a2c`, source `a92afbd92236d3d516f2d9c3638dde031cd775aca33432d529d63447f1a49996`), the bridge answers
`approval-missing` for the home pin; `transfer_revision` refuses
`verifier-refused` / `evidence-missing`. The owner approval of the exact pin
must be posted on #704; the proposed text is in decision #154308 (owner
`smichea`, no new authority). The projection must also declare
`thepackengers/home` in `approvals.source.artifact_repos`, otherwise the whole
projection is refused as out of scope.

### Version reconciliation

| version | value | role |
|---|---|---|
| Process pin | home `83525afb03d4c70e23bdb70f80d9545c518dc15a` + card path | `workflow_ref` |
| Process revision content | IMA `6c771e2e16bc04e8ce3b04a55df67ad4aca77cb8` | approved contract; approval 6206927953 |
| native `workflow_version` | `1` | first admission sequence; **not** a TPK/IMA version (no numeric TPK version is published) |
| native journal version | max `work_item_events.id` | `expected_version`; not a Process version |
| IMA serving at publication | 1.11.0 @ `44a42555406f30939285316119c7c66e60d39c13` | card text; Dockerfile at `954cebc1ac8c97dda6993c545a46099d93871cdd` |
| IMA committed qualification | 1.11.3 | historical relay 6293775913 |
| bureau qualification | 1.11.4 @ `e15f3ef3a65a4b1f0d9ce995a45a5f5b857b4d8f` | historical relay 6293775913 |
| IMA develop at 2026-10-28 | ameesh 1.16.0 + 8 fixes @ `92ae35324062957d5865606bf1332a47c36b3adf` | Dockerfile at `c7b0a82e36438e85ba9957b254036b2df0ae71e6`; not an admission authority |
| ameesh publication acceptance | 1.16.0 @ `fba44801e250b88aacd9fab7166115bead75935d` | past publication check |
| ameesh installed / staging | 1.26.0 @ `904cefc74acf6b18c2e5133a776e6e18719bc5ac` (three VMs) | code of the admission; A and C installed |

The admission authority is the installed 1.26.0/904cefc (A and C active, with
`process-verifier-install/1` `installed: true`), the exact pin and the exact
owner approval of that pin. The IMA serving version is not upgraded by this
admission; the card's "serving pin remains 1.11.0" is a publication-time
statement. A new card or contract revision is a new explicit transfer, version
+1, never a silent adoption. `0` remains the unpinned default.

### Receipt and witnesses

* Card pin/content: `tests/test_4134_process_admission.TpkCardPinTest` on the
  real card fixture (file SHA-256, compiled shape, 42-line block SHA-256).
* Deterministic refusal: `TpkAdmissionTest.test_refusal_without_pin_approval`
  on the real #704 fixture and the real B registry; no event, no pinned
  revision, `approval-missing` in the `verifier-refused` message.
* Configuration prerequisite:
  `test_home_artifact_requires_artifact_repos` (the projection is refused
  when `artifact_repos` omits `thepackengers/home`).
* Head change and revocation: `test_head_change_is_refused` and
  `test_revoked_approval_is_refused` (synthetic approvals, real card/registry).
* Installation: `test_revision_transfer_installation_receipt` (contract,
  source, authority, references, source hashes).
* Accepted first adoption: `test_accepted_adoption_with_pin_approval`, run once
  the owner approval exists. Its proof carries the contract, approval source,
  authority, lot/project, `workflow_ref`/`workflow_version`, the approval
  (`comment_id`, author, artifact) and the projection (`digest`,
  `source_digest`); the lot moves to `draft`/`intake` with version 1 and one
  receipt.
* G1: bureau receipt on #4006 (2026-10-28T02:54Z): `test_process_approvals_704`
  3 tests OK; `refresh`/`check` 4 approvals, 0 refusals; token absent from the
  projection; `show` on the home artifact `approval-missing`.

The test fixtures (card and #704 comments) stay in the bureau vault; the
repository carries only the pin, digests, IDs and counts. No live mapping is
added to `github.projects`; no sidecar is stopped; `planning` and `start_work`
are not qualified by this admission. The accepted adoption is the only step
still gated by the owner approval.

## Proofs

`tests/test_process_registry.py` (77 cases, two drivers — 2 skipped while
slice C is absent from `main`): replay after a fresh connection, concurrent
same key, two commands at the same version, payload conflict, unknown/stopped/
`dead` actor, lease held by another runner, stale epoch, expired lease, race
with a revocation (the `FOR SHARE` lock is held), holds and exit through the
reserved verifier (`verifier-uninstalled` without it, refusal of a boolean,
advance from a hold refused, answer before the exit, concurrent answer without
deadlock), terminal never reopened (including through adoption), adoption
without core regression, transfer/replay/regression, public API without
verifier injection, `work.move`/`close`/`close_merged` refused on a pinned lot
(including the storage layer called with the snapshot from before the
adoption: the `workflow_ref` filter is in the SQL), `read_sources`
fail-closed, cancellation by trigger, intact history, migration constraints
(`IS TRUE`, including re-applying the file, which reinstalls the hardened
definition), and **pre-0452 support**: registry columns absent, `work.get`
returns an empty `workflow_ref` and `work.move`/`work.close` remain the
historical commands (no column probe). The four review findings have their own
witnesses:

* **N9** — an advance into a released core gives the migration reservation
  back (with its journal line, the receipt staying the last journal id), wakes
  the dependants once (`merged`, `promoted`, `closed` with its reason; an
  unassigned `blocked` dependant is reopened in the same transaction, #1668),
  writes `close_reason`/`superseded_by` and the `closed`/`delivered`
  milestones, refuses an unproven closure (`close-reason`,
  `superseded-unknown`, `delivered-sha`) and rolls everything back together
  when a milestone or the wake fails;
* **N12** — the concurrent witness probes the decision request while the
  command waits for the lot: the command never holds the request before the
  lot, and the exit refuses `decision-pending` once the ask transaction
  commits;
* **N10** — adoption/transfer of a held lot without a journal proof refuses
  (`hold-core-unproven`), with a proof it keeps the hold and resumes into the
  proven core, and a revision whose kept step projects another core refuses
  (`stage-mismatch`);
* **N5** — the trust boundary of the caller-supplied artifact is documented
  here and in the module.

One **real seam with slice C** case
(`process_verifiers.install_verifiers` on the public registry: guarded
`planning` advance, hold, exit, `waiting_human` through `read_sources`) runs
as soon as C is present, otherwise it is skipped — C is not yet in `main`.
`tests/test_l385_work_dependencies.py` guards the #1668 reopening: a pinned,
`blocked`-held, unassigned dependant is not reopened when its last
prerequisite closes (B4) — its hold falls only through a verified `release`.
The mediated read of the guard is guarded by `tests/test_l109_mediated_mode.py`
(B5): `work note`/`move` on an unpinned lot, and the refusal of a pinned one,
make no raw SELECT — the VM session would only send `work.get`, never
`work.move`. The module is declared in `tests/parts/pilote-psql.txt`
(transactions, locks, concurrency: both drivers' behaviour).

## Slice D: opt-in projection of a pinned lot onto its single issue (#2130)

Slice D is **opt-in** and concerns only lots pinned to a Process revision
(`workflow_ref` non-empty). Outside the opt-in — no admitted mapping for the
project, or an unpinned lot — the L126 projection is unchanged, bit for bit.

**Authoritative source.** For a pinned lot, the issue state, its board labels
and the closing comment follow the pinned revision: `workflow_ref` +
`workflow_stage` + `workflow_version`, read through the **admitted, compiled
artifact**. `work_items.state` (the current core, or a hold) is never the
projection source of a pinned lot. A hold (`blocked`, `waiting_human`) keeps the
stage; since the row does not carry the pre-hold core (`_pre_hold_core`), the
core comes from the artifact (`stage.core`).

**Mapping (data only).** The opt-in is the `github.process` key of the host
configuration:

```json
"process": {"<project>": {"ref": "canon@<sha>:<path>", "version": 3,
                          "document": {"schema": "process-workflow/1", "...": "..."},
                          "labels": {"<stage>": "<board label>"}}}
```

The document is compiled by `process_workflow.compile_workflow(document,
ref=…, version=…)`; its `(ref, version)` are the ones lots must carry, and
`labels` must cover exactly the artifact's stages. A project may admit a list of
mappings (a transfer admits two revisions at once). Unknown keys, a missing
`ref`/`version`, an empty mapping, a duplicate `(ref, version)` or an incomplete
label map raise `SettingsError`, as does a project that is not declared in
`github.projects`; the projection stops with the message, as it does today for
an invalid `github` key. No executable expression and no import path ever come
from the document; `compile_workflow` proves the document is well formed, not
where it came from — the admitted `ref` and the label map are data the host
configuration owns.

**Issue state.** The artifact's `projection` classifies cores: `issue_open` →
`open` (so `merged` — staging and preprod — stays **open**), `issue_completed` →
`closed/completed`, `issue_not_planned` → `closed/not_planned`. Two native
exceptions are read from the lot's own facts:

* a core `closed` with `close_reason=delivered` (#988) is `completed`: a real
  delivery outside the target, not an abandonment;
* a pinned lot closed by the native path (`work close`: the row is `closed`
  while the stage's core is not) keeps its stage and its board label, and the
  **native reason decides** — `delivered` → `completed`, `abandoned`/
  `superseded` → `not_planned`. An absent or unknown reason refuses
  (`projection-classification`) instead of guessing.

**Refusals (fail-closed, no legacy fallback).** A pinned lot is never projected
through `lot_issue_state`. The lot is left untouched — no issue created, no
field written, no closure comment — and reported once with a stable code:

| code | cause |
|---|---|
| `projection-unmapped` | no admitted mapping for the lot's `(ref, version)` |
| `projection-stage-missing` | pinned lot without `workflow_stage` |
| `projection-stage-undeclared` | stage absent from the admitted artifact |
| `projection-core-mismatch` | `work_items.state` (a core outside a hold) diverges from the artifact's core for the stage |
| `projection-classification` | core classified nowhere, or native `closed` without a usable `close_reason` |
| `projection-label-missing` | no admitted label for the stage |
| `projection-ambiguous` | the configuration admits `(ref, version)` twice (load time) |

A refusal writes nothing at all: not even the native `issue_ref` adoption of an
issue found by its marker (`_remember_ref` is skipped for a refused lot), and a
`--dry-run` pass is read-only. It is distinct from a title refusal and from the
`self.skip` cache: a corrected mapping, revision or stage is picked up on the
next pass without touching the work item's title.

**Managed labels.** The admitted mappings' labels join the managed label set of
their project (the union of every admitted revision, so a transfer's old board
label is replaced by the new one instead of lingering as a foreign label).
`_own_labels`, `_fields`, the fingerprints, `_three_way`, `_restamp` and
`_foreign_labels` use that set; every other label, and every human edit, stays
untouched, and a lot outside the opt-in keeps the historical `ameesh`/
`ameesh:*` fingerprints.

**Issue already closed.** When the projection wants `closed` and the issue is
already closed, a divergent `state_reason` (a legacy close for the wrong reason)
is corrected without adding a comment: state and reason stay coherent.

**Reused unchanged.** Adoption (`issue_ref`, `ameesh:work=` marker), one issue
per lot, compare-and-set `_remember_ref` and duplicate detection, shared read
quota, unknown-outcome reconciliation (the marker records what ameesh last
wrote), the three-way merge that keeps human edits, and the content guard. No
receipt `evidence` and no private payload enters the issue. `issue_feed` carries
the three workflow columns (and its filter keeps a pinned `merged` lot in the
flow), and `work.get` carries them too: `project_lots` uses it for older lots
found by their marker, so a healthy pinned orphan is not misread as
`projection-stage-missing`. Both reads use `to_jsonb`, with a pre-0452 database
returning `''`/NULL/0 — no column probe.

**Condition.** The consumer stops its own issue writer on the opted-in project
(the sidecar is not a qualified writer, 6211681303): native is the single
writer. This slice does not activate the writer.

**Witnesses** (`tests/test_2130_d_projection.py`, both drivers): unpinned lot
unchanged; `implementation`, `staging`, `preprod_passed` → open; `done` →
closed/completed; `stopped` → closed/not_planned, `delivered` → completed;
native close outside the stage: `delivered` → completed, `abandoned`/
`superseded` → not_planned, absent/unknown reason → named refusal; `blocked`
hold on `review` → open with the `review` label; pinned lot without mapping →
named refusal, no write, and an issue the legacy rule would have closed stays
intact; a refusal does not adopt (`issue_ref` stays empty, no GitHub write);
undeclared stage / divergent core / missing label → named refusals; a pinned
`merged` lot without an issue gets an open one, a pinned `done` lot without an
issue gets none; an issue closed for the wrong reason by the legacy is
corrected; human title/body/foreign labels kept, no private evidence in the
body; convergence after a GitHub write error; two admitted revisions and a
transfer replaces the old board label; the `Projector` passes the mapping;
`work.get` and `issue_feed` carry the three fields, including pre-0452
(''/NULL/0); ambiguous configuration → `SettingsError`.
