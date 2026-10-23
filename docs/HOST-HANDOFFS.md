# Durable host handoffs (L232a)

`ameesh handoff plan AGENT --repository REPOSITORY --dry-run` reports an
admitted destination using three fresh host readings, physical limits across
canons, source pressure or materially better spare capacity and a cooldown.
Remove `--dry-run` to reserve capacity for an agent with a live source lease.
Planning requires the agent's bound identity or a correction role.
`ameesh handoff show OPERATION` reads the durable plan, allowlisted evidence
and event journal. These commands perform no SSH or provisioning.

The runner's relocation option reserves a handoff instead of changing the
host or discarding a native session. The current lot continues until its
boundary. An atomic database fence then blocks source claims and new turns.
The source worktree is retained. Dirty or unpublished work holds the move.

The L232b host adapter drives `ameesh.handoffs.advance`, one bounded step
per call. Steps are idempotent by operation ID and are serialized against
other controllers, cancellation and explicit retries. Each controller
acquires a durable epoch; journal transactions check that epoch and the
original lock backend. Before each host call, a CAS commits its effect intent
under that epoch. An unfinished intent survives lock loss and prevents
another controller from starting a conflicting effect. Calls must use a
dedicated database connection per controller.

The adapter must enforce enrolled repository/host identities and approval
policy. `revalidate` checks current physical capacity including reservations,
canon revision, placement, pinned release, credential readiness and existing
executor/checkout/session effects. It must use timeouts and never return
credentials, OAuth caches or private diagnostics in evidence.
The destination host gate must be open when planning and before host effects;
a lending host without a fresh available gate holds the operation.
After committing the enable intent, the controller takes the destination
gate publication lock, rereads the latest gate, and holds the lock until
`enable` returns or the controller’s 60-second enable deadline expires.
Host reading publication uses the same lock, so a closure
published during intent creation is observed and a concurrent publication
cannot interleave with enable. The adapter must complete enable synchronously
inside this boundary; queued or delayed host jobs must enforce the current
local gate themselves before starting an executor.

The deadline releases the publication lock even if the Python adapter is
stalled. It leaves the durable enable intent unfinished and holds/fences the
handoff for explicit reconciliation, without restoring the source. The
daemon worker cannot change the journal, and its late return is discarded.
A timeout does not stop an external effect: reconciliation must revoke and
quiesce the old epoch, including the outstanding enable request, before a
retry can proceed. The adapter must not use the controller’s database
connection from its enable worker.

A controller process allows at most four outstanding enable workers;
`AMEESH_HANDOFF_ENABLE_WORKERS` configures 1–64 before startup. A worker
retains its permit after a timeout until its adapter call actually returns.
Each advance, including a held operation that a concurrent retry may resume,
reserves capacity before revalidation or any journal/effect
change. At saturation, it leaves the operation at its existing checkpoint
(a new handoff stays planned), creates no effect intent, changes no fence,
and logs `handoff-enable-capacity-exhausted` as an operator warning.
Progress resumes when a worker exits. Permanently stalled workers require
host reconciliation and controller recovery; ordinary retry cannot bypass
the process limit.

| State | Next action and proof |
| --- | --- |
| planned | Prepare tools, caps and credentials without enabling execution |
| prepared | Wait for the current lot and turn to finish; fence new claims |
| drained | Stop the source executor; verify a clean exact pushed Git commit |
| published | Transfer and verify the repository, branch, commit and clean worktree |
| transferred | Prepare a brief for Claude/Codex or a fresh secret-free dsh profile/session |
| session_ready | Switch placement, clear the source lease/session and increment its epoch |
| switched | Recheck destination admission and release the claim fence |
| activated | Idempotently enable, then verify an authenticated destination turn |
| verified | Close the operation and release the reservation |

Adapter methods are `prepare`, `stop_source`, `publish`, `transfer`,
`session`, `admit`, `enable`, `verify`, `revalidate`, `restore_source` and
`reconcile_effect`. Each effect receives `controller_epoch`; the host helper
must durably reject revoked epochs, including requests delayed in transit.
`revalidate` inspects prerequisites and never starts an executor.
Proofs are checked by the controller and only allowlisted metadata reaches
the journal. `verify` may return `{"ok": true, "pending": true}` while the
first authenticated turn is still running. This leaves the operation active.

Failures or expiration hold the operation. After draining, the claim fence
remains in place; it does not terminate an already running target turn.
Reservations remain counted while held, including after expiration, so a
second planner cannot spend capacity still occupied by external effects.
`retry` requires revalidation; an expired reservation additionally requires
explicit `renew_ttl=SECONDS` and fresh destination readings. Renewal keeps
the recorded host and checkpoint and never rolls back over target work.
`cancel` is available only before the switch and requires verified source
restoration. After switching, reconcile the destination and retry forward;
source restoration is refused because the destination may have new work.
Source cleanup requires a separate retention decision after verification.

An interrupted host call requires explicit `retry` reconciliation before
ordinary progress resumes. `reconcile_effect` must durably fence the old
epoch and quiesce all its host jobs, including delayed calls, then return
`{"ok": true, "quiesced": true, "epoch": PREVIOUS_EPOCH}`. A missing,
failed or mismatched proof leaves the intent blocking progress. Replaying
an uncertain effect without that proof is refused. Cancellation records
`cancel_requested` before restoring the source; reconciliation preserves
this decision, and only cancellation may then resume.

Reservation expiry is checked again after admission and immediately before
enabling a destination. Canon writes compare the lease epoch read during
synchronization, so a write from before the switch is refused even after
the handoff reaches `verified`.

A refused canon write reports the policy update as deferred and records
`canon-sync-incomplete`. The affected host's canon state becomes `invalid`,
closing new claims until a later synchronization reads the current placement
and lease epoch and successfully applies the policy. It never reports an
unapplied capability revocation as updated or marks that synchronization `ok`.

L232a is tested with simulated adapters and local Git repositories. It does
not install a real host adapter, enable production agents, copy secrets,
perform root operations or authorise spending. L232b/c and provider login
readiness supply those separate integrations.

## Host drain, return and loss recovery (L232e)

`ameesh host drain HOST [--for 8h]` closes new claims, turns and prompt
consumption while preserving the current turn and lease. The persistent
request takes precedence over later resource readings that say `available`.
The controller considers critical agents first, checks receiving-host
readiness and uses a handoff at a turn boundary. `ameesh host undrain HOST`
requests their return; an elapsed `--for` duration makes the same request.
Missing helper, secret requirements and host-scoped logins remain named
refusals. These paths currently use simulated adapters.

The runner detects a known host as `lost` after its latest resource sample
is older than the larger of 180 seconds and three resource intervals, and
none of its executing agents has a live lease. It rechecks both facts after
locking the host gate and agent rows. An unobserved host is not classified
as lost. An existing drain retains its generation and agent origins, but
its automatic return timer is removed. A new heartbeat does not reopen
claims on a lost host.
`undrain` refuses a lost host; the planned-return path cannot bypass the
recovery proof. A returning machine stays withdrawn until it is independently
reconciled. Lost-host recovery moves agents forward rather than restoring an
uncertain source executor.

Detection preserves agent placement, expired leases and running-turn state;
it is not proof that an executor has stopped. Critical agents become eligible
for recovery after 180 seconds without a resource sample; other agents wait
900 seconds. The detector's longer threshold still applies if three resource
intervals exceed these delays. A fresh, admitted receiving host with matching
canon, capacity and readiness remains mandatory.

Recovery reserves capacity and fences source claims, then prepares the target.
The enrolled adapter's `recover_source` receives the operation and controller
epoch. It must durably disable the source executor (including delayed calls)
and attest physical quiescence, absence of unpublished work and the exact
clean Git checkpoint on the enrolled remote. Its result names `source`,
`agent`, `lease_epoch` and lifecycle `generation`, with `source_fenced=true`,
`quiesced=true`, `unpublished_work=false`, plus the ordinary publication
fields (`repository`, `branch`, `commit`, `remote_commit`, `clean`). A stale
epoch, mismatched scope or missing proof holds the operation. Unreachable
source files are not evidence of cleanliness; an older remote checkpoint
alone does not prove that the most recent work was published.

After this proof, a transaction revokes the expired source epoch and records
the published checkpoint before transfer/session/switch/admission/enable.
The old runner cannot renew, finish a turn or reclaim the agent when the
source returns. A helper timeout leaves a pending effect requiring explicit
reconciliation; ordinary cancellation cannot restore a lost source.
Heartbeat absence alone never enables a second executor.

If a source is lost during an existing pre-switch drain, the controller
converts that same operation to loss recovery while preserving its destination
reservation. It keeps the source fenced, discards stale publication/session
proofs and obtains the scoped recovery proof before proceeding. An unknown
effect must first be explicitly reconciled. Restoration checks the source's
current lifecycle under its gate lock, so a planned drain cannot restore a
source that has since become lost.
Restoration intent stays committed on the independent journal connection.
Its completion and the cancellation fence release use the transaction owning
the source gate lock. If that backend disconnects during restoration or before
commit, both writes roll back and the pending intent still requires explicit
quiescence reconciliation.

All L232e host effects are currently tested with simulated adapters. The
runner records `helper:L232b` when no enrolled implementation is supplied;
real SSH, secret delivery and provider login integration depend on L232b/c/d.

## Elected roles across candidate hosts (L165/EP1)

A governance role can be declared on its Agent card as:

```yaml
releve: election
candidats: [ameesh-agents-1, ameesh-agents-2, manaty-mesh-1, pc-smichea]
```

`releve` defaults to `hote` (today's behaviour: the claim only happens on the
host of the registry row). With `election`, every host in `candidats` must be
covered by the same Placement admission. `ameesh canon sync` evaluates and
stores the placement verdict of each candidate host in `agent_candidates`
(host policy and visibility included, with its evaluated profile), or refuses
that candidate without blocking the others. A host policy that refuses the
agent closes only that candidate (`agent_candidates.placement_ok=false`): it
does not invalidate the host, and a pinned agent of the same host stays
claimable. A card-level error (unresolved responsible, missing admission,
invalid `releve`/`candidats`) closes **every** candidate, whichever candidate
host read it. Removing the Agent or Placement
card is noticed from any candidate host: the standby verdicts are deleted and
the holder's own row is stopped on its host, guarded by its lease epoch so a
concurrent claim is not killed.

The lease is still the only permission to run: the first candidate whose
runner finds the lease free or expired claims it. The claim moves the row to
that host, with the candidate's work directory and placement verdict, and
increments `lease_epoch`; the former holder is fenced from renewing, ending
its turn or reclaiming. The holder is the one that renews, so only it takes
turns.

These roles are outside L232e's handoff: loss detection and handoff planning
skip them, and a pending `handoff_fenced` does not close their claim. There is
nothing to publish from a local worktree — the pilot's state lives in the mesh
database, the canon and GitHub — so the L232e source proof does not apply. A
planned `host drain` still returns the holder's lease immediately, in the same
transaction as the drain and with `lease_epoch` incremented: a standby can
claim without waiting for the TTL, the drained host cannot claim again, and the
former holder is fenced from renewing, ending its turn or reclaiming. An
unplanned stop loses the role when its lease expires (the L232e timing, 180 s
by default); a returning host goes back to standby until the lease is free
again. This lot only makes the cross-host claim and the planned-drain release
possible; EP2 decides which candidate wins and when (next section).

## Candidate score, rank delay and hysteresis (L165/EP2)

Each candidate host publishes, once a minute, a standing for every elected
role it may hold (`role_standings`, migration 0476): seven-day availability
from `host_resources`, a capacity tie-break, and **only the local part** of
readiness — harness present, no known failing credential, and
`account_turn.turn_hold` empty (account pause/ceiling, mesh and agent budget).
Placement, host gate (`host_lifecycle`, `host_holds`, the gate state of the
latest reading), canon state and freshness are recomputed by every reader
from the database; a host's own word never covers them.

**Availability** is the share of the last seven days covered by a fresh
reading. A reading stands for the host's own staleness threshold —
`host_lifecycle.staleness_s`: `max(180 s, 3 × its reading cadence)` — never a
fixed window, and readings or standings dated in the future are refused. The
denominator is always seven days, even for a host that appeared an hour ago:
a newcomer has to prove itself before it beats an old VM. **Capacity** (load
and memory headroom, 0..1) only orders equal availabilities; it never enters
the five-point margin.

Ranking keeps only fresh, fully ready candidates: a dead better candidate
does not make the second wait. The best ready candidate claims with no delay,
the second waits 20 s, the third 40 s… from the first time its runner saw the
lease free. The delay is client-side and polite — `registry.claim` remains
the only arbiter, so two hosts that try together still produce one holder.

A holder keeps the role while it renews. A **planned handover** happens only
when a challenger has stayed at least 5 availability points above the holder
for a full hour (standing history, no hole beyond the freshness threshold)
and both are fresh and ready: the holder releases at the end of its turn
(epoch++), and does not reclaim for the challenger's rank delay plus 60 s, so
a challenger that dies after the release does not leave the role free. A
workstation that boots therefore does not take the role back from a VM.
`ameesh role status [ROLE] [--json]` prints the holder, the epoch and the
ranked candidates with the diagnostic of each refusal.
