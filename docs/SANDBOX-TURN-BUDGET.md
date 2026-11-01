# Turn tranches

A tranche is a human-set shared USD envelope, in addition to the existing
provider billing account's calendar-day envelope. No tranche is created by a
migration, a runner or a canon sync. There is no compiled cap.

The only selector is `Agent.sandbox.tranche` in the admitted profile. Local
runner turns use `path=runner` (a resumed retry uses `retry`); agents without a
tranche keep the existing account admission. A declared tranche that is absent,
closed, outside its time window or awaiting overdraft reconciliation refuses the
turn before the harness launches. The runner reads the audited canon revision;
an environment variable, a Host setting or a CLI option cannot override it.

Authorized humans administer the records:

```sh
ameesh budget tranche create migration-envelope --project project-name \
  --usd 50 --starts-at 2026-11-01T00:00:00Z \
  --admitted-ref decision-with-owner-approval
ameesh budget tranche show migration-envelope --json
ameesh budget tranche close migration-envelope --why 'migration finished'
ameesh budget tranche release RESERVATION_ID --why 'uncertain spend checked externally'
ameesh budget tranche reconcile migration-envelope --why 'overdraft investigated'
```

The amount and dates above are examples. Creation records the human and the
admission reference. Close, release and reconciliation require an audit reason.
There is no automatic extension or cap increase. Reconciliation clears an
overdraft only when counted usage fits the original cap; it does not raise it.
Releasing an open/kept reservation releases both envelopes. A surviving sandbox
Job prevents release; observe its termination first.

`storage.postgres.turn_admission.ameesh_admit_turn` is the trusted runner
boundary. It reserves the complete bound on the tranche and account in one
transaction, or neither. Lock order is placement → retry family → admission key
→ tranche row → account. The admission-key lock also serializes response-loss
replays by local turns. Replay checks immutable identity and snapshots before
new-admission gates, returning the same reservation ids and, for a sandbox, the
same assigned attempt, scope/digest, Job name and deadline. A mismatch is refused.
Tranche window and sandbox lease are rechecked after waiting for the account.

For sandbox admission, the caller supplies the S2 snapshot, owner/epoch,
authorized request ids, scope without attempt, source, max attempts and placement
concurrency. The caller owns canon freshness and request authorization; no
external lifecycle write route is introduced. Admission writes the S1 intent,
retry-family attempt and both reservations together. The S4 supervisor must use
this boundary before creating a Job. This tranche implementation does not
activate the supervisor or create Jobs.

S1 completion/cancellation receives `turn_admission.sandbox_settle` as its
`(tx, admission, outcome)` callback. It commits the receipt, turn close and both
settlements in the caller's transaction. A sandbox result's reported cost is
informational: only relay ledger rows tagged with the exact turn and billing
account supply its actual cost. The relay derives the turn from its verified
token, not a request header; billing identity comes from the stored admission.
A durable marker precedes each provider request for an admitted sandbox turn.
Recording its cost and closing that marker is atomic and idempotent.

The runner issues an `amsb1.` bearer only after sandbox admission and only when
the relay URL is available (`AMEESH_RELAY_URL`, or the mediated service URL).
Only its SHA-256 digest is stored. The relay loads identity from the admission
and checks the current lease owner, epoch, host, cancellation, terminal state
and deadline against the database clock on every authentication. Token issuance,
verification and the pre-provider accounting gate lock the sandbox row before
the registry row, like completion and cancellation. Ordinary lease credentials
cannot replace a sandbox bearer while a sandbox turn is live on the same
(agent, owner, epoch); a terminal turn stops fencing, so the lease bearer is
usable again. A lease shared by several agents is supported: only the agent
holding the live sandbox turn is fenced (concurrent sandbox turns on one agent
are not: unique live index). A previously admitted sandbox agent refuses local
fallback when its canon cannot be read; the admission trace survives a runner
restart. These paths do not activate any sandbox card.

A closed turn cannot authenticate its bearer any more: the stored digest is
only a proof of issuance. The runner's daily housekeeping deletes the digests
of turns closed for longer than `sandbox_token_retention_days` days (default
30, `AMEESH_SANDBOX_TOKEN_RETENTION_DAYS`; 0 = never purged), in bounded
batches. The turn row, its result and its costs stay (migration 0530, #5217).

Each sandbox provider request has a durable id before the side effect. That id
is written as `turn_costs.relay_call_id`; a unique `(turn_id, relay_call_id)`
index for relay rows deduplicates ledger retries. Relay rows keep `spend_key`
NULL: their reservation is reached through `turn_id` and `sandbox_turns`.
The unique spend key of the runner's accounting marker remains unchanged.
Sandbox completion uses only relay costs and does not add a runner cost row;
two distinct model calls contribute two costs, and replaying either write
contributes no additional cost.

An absent measurement, an unsettled provider request or an uncertain result
keeps the full reserved amount. A known lower actual cost releases the unused
part. A higher actual cost is counted in full and marks the tranche overdrawn,
blocking further admissions until human reconciliation. The original bound is
retained separately for admission replay. Existing account settlement/release
entry points also settle/release both halves of a tranche admission.

The runner remembers the last trusted selector, including “no tranche”, per
(canon id, agent) in process memory. During an unreadable, missing, ambiguous or
untrusted canon revision, a remembered ordinary agent keeps account admission;
a remembered tranche participant refuses with `profile-refused`. Freshness is
required only for participants. A trusted read requires a nonempty pinned commit
and a source loaded at that commit; an unpinned read never replaces memory.
A recovered trusted profile replaces the memory.
On restart without memory, an unavailable canon refuses if any tranche row
exists, including a closed row. Only an entirely empty `tranche_limits` table
allows a logged fallback to account admission. The accepted risk is limited to
no tranche ever having been created. Once a tranche exists, restarting during a
canon incident refuses agents without memory until the canon recovers. Remembered
selectors take precedence over this fallback. Memory is lost on runner restart.
