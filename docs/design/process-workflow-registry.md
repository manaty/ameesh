# Native Process workflow registry (#1915 slice B, lot #1922)

Consumer contract: `manaty/ameesh#1379` (body and comment 6207021753).
Slice A (#1918): the strict `process_workflow` compiler. Slice C: finite
verifiers and installed types. This document publishes the **seam** C plugs
into and the invariants B guarantees.

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
  default.
* `Receipt`: `accepted`, `replayed`, `event_id`/`version`, `state`,
  `workflow_ref`, `workflow_stage`, `workflow_version`, `intent_sha256`.
* `read_sources(context) -> Mapping`: the source read for C (which is pure and
  SQL-free), **inside the transaction and under lock**. The mapping declares
  only what is really collected (`SOURCE_KEYS`: `milestones`, `receipts`,
  `decisions`, `deliveries`); `observations` is not installed and is **not**
  declared: a verifier that requires it refuses `source-unavailable`
  (fail-closed). `receipts` carries `{accepted, guard, event_id, command_id,
  workflow_stage, workflow_version, sha256, evidence, facts}` — `guard` is the
  Process guard or the reserved verifier name (`hold`, `revision_transfer`).

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
