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

## Enrolled handoff factory (H1)

`ameesh.handoff_contract` binds what a controller may call on an adapter:
`inventory`, `repository`, `revalidate`, then the effects `prepare`,
`stop_source`, `publish`, `transfer`, `session`, `admit`, `enable`, `verify`,
`restore_source`, `recover_source` and `reconcile_effect`. Every effect
request carries the operation id, the source lease owner and its epoch, its
repository identity, the durable evidence and the controller epoch. The
controller refuses an absent method or a request outside that identity before
the adapter is entered; proofs stay checked by the controller.

`deploy/mise-a-jour/fabrique-handoff.py` is the single pinned factory. A
controller loads that exact file, never a module named by a job, a message or
a document. It reads five exact private documents under
`/etc/ameesh-handoff` — `enrollment.json` (version and controller host),
`hosts.json` (admitted host/harness/account/tool/secret-alias identities),
`repositories.json` (project to enrolled repository identity),
`secrets.json` (preflight alias to private `fichier#CLE` reference) and
`transports.json` (one private SSH endpoint per other admitted host) — the
fixed `hote-handoff.py` companion and the fixed `pont-handoff.py` bridge next
to it. `requirements_for(agent, host)` returns the exact
`host_preflight.Requirements`: host, harness, exact account, tools and alias
names; the private reference mapping is available separately for the L232c
bridge and never enters `Requirements` or an error. A missing or unsafe
document, an unenrolled host, harness or account, an unknown project
repository, a credential-bearing repository URL, an endpoint outside the
private enrollment or a missing adapter method refuses admission. Only the SSH
userinfo `git@` is admitted, in URL or scp-like form; HTTP(S)/`git+https`
userinfo is refused as a credential. Documents must be regular files: the
final type is checked before opening, the open is non-blocking and the
descriptor is checked again, so a FIFO or a device cannot hang admission.

H1 binds no host effect. The companion's capabilities refuse by name until H2
(preparation and Git) and H3 (activation, health and recovery) replace them;
a present-but-unimplemented method refuses at call time and never returns a
simulated success. This factory is source only: it installs no service,
performs no SSH, root, credential or production operation.

## Preparation and Git effects (H2)

H2 implements `inventory`, `revalidate`, `prepare`, `stop_source`, `publish`,
`transfer` and `session`. `publish` proves the source branch is clean, on its
enrolled remote, at the exact commit; `transfer` checks out that exact commit
on the destination with the account's own Git credentials and refuses a dirty
or occupied worktree; `session` prepares a new private marker with no copied
home, harness cache or OAuth token. `prepare` reads the destination's pinned
release receipt, the enrolled tools and secret aliases, and the unit's finite
memory caps; `stop_source` stops only the enrolled unit and proves its cgroup
is quiescent. None of these steps starts or enables the destination executor.

The controller-side companion checks the durable operation `pending_effect`,
the placement/lease, both canon states, the destination gate and the existing
`host_preflight` readiness before an effect. The host executor is the pinned
`hote-handoff.py` (its `HostEffects` class and stdin dispatcher). It keeps a
private journal under the host execution enrollment: the step order, the
source lease owner and its epoch, the last controller epoch, a durable intent
before the effect and an exact allowlisted receipt. The lease is bound once
per operation; a changed owner or lease epoch is refused. A separate durable
fence, shared by every operation of one agent and unit on the host, records
the highest lease epoch observed: a newer lease advances it, and any delayed
operation from an older lease is refused before it can act — including a
`stop_source` replayed from an old receipt, which must not stop the executor
of a later lease. A stale controller epoch, a changed intent or an
out-of-order step is refused; a repeated step returns its stored receipt
without repeating the effect (a stored `stop_source` is re-observed, and
re-stopped only while its lease is still current); a receipt written before
its state mark is repaired on replay without replaying the effect; an
interrupted intent is resumed only with its exact original identity and
payload, otherwise the call raises and the controller keeps its own durable
intent for explicit reconciliation. Revalidation and effects share the agent
lock and the fence, and the durable controller epoch is monotonic: a
revalidation observed during an effect can only raise it. Every SSH exchange
is bounded by the enrolled endpoint; the host dispatcher's alarm covers the
effect, not only the stdin read, and a timed-out child process group is killed
before the uncertain intent is left for reconciliation. Expiry during an
effect is uncertain, never a definitive refusal: it keeps the durable intent,
fails the transport, and leaves reconciliation to the controller.

The host execution enrollment's `account` is the **Unix** account that owns
the home and runner unit. The harness/provider account in a request is a
different identity: the controller validates it against the enrolled
`hosts.json` accounts, and the host executor keeps it only as session metadata.
The two are never compared as if they were the same login.

`pont-handoff.py` is the explicit bridge to admitted helpers. It reuses only
the hardened SSH substrate of `transport-release.py` — fixed client options,
the private key and known-hosts files with their pinned digest, the bounded
pipe exchange and duplicate-key refusal — and never loads the release
controller, its operations, its database or its restart authority. The
destination release receipt is read through `environnement-versionne.py`;
neither module gains a new authority from the bridge. H7 installs the host
execution enrollment (`execution.json`: host, state root, configuration path,
release store, per-agent account/home/unit and allowed work roots) and the
private transport endpoints; H2 ships source only and performs no installation.

## Host drain, return and loss recovery (L232e)

`ameesh host drain HOST [--for 8h] [--actor human:<id>]` closes new claims,
turns and prompt consumption while preserving the current turn and lease. The
persistent request takes precedence over later resource readings that say
`available`. The controller considers critical agents first, checks
receiving-host readiness and uses a handoff at a turn boundary.
`ameesh host undrain HOST [--actor human:<id>]` requests their return; an
elapsed `--for` duration makes the same request. `ameesh host reconcile HOST
[--actor human:<id>]` reopens a lost host on the verified proof below. From a
terminal without an agent identity, `--actor` names the human who acts;
without it the command refuses instead of guessing. Missing helper, secret
requirements and host-scoped logins remain named refusals. These paths
currently use simulated adapters.

The runner detects a known host as `lost` after its latest resource sample
is older than the larger of 180 seconds and three resource intervals, and
none of its executing agents has a live lease. It rechecks both facts after
locking the host gate and agent rows. An unobserved host is not classified
as lost. An existing drain retains its generation and agent origins, but
its automatic return timer is removed. A new heartbeat does not reopen
claims on a lost host.
`undrain` refuses a lost host; the planned-return path cannot bypass the
recovery proof. A returning machine stays withdrawn until a verified
reconciliation proves a sustained fresh reading history in database time
(covering one detection window without a hole, and no reading dated in the
future), every origin agent still home and unfenced, no active handoff, and no
`recover_source` attempt since the loss. A live lease of an agent that is home
does not block the return: it is the returning host's own runner taking its
lease back, and a recovery that moved the agent elsewhere still closes the
proof. The next lifecycle pass of a healthy runner, or `ameesh host reconcile
HOST`, then records `lost -> returning -> available` with its actor and
evidence in the event log. A single fresh heartbeat does not reopen claims,
and an agent moved elsewhere by a recovery keeps the automatic proof closed.
`ameesh_host_gate_state` names a lost host `lost` (the generic `draining` it
used to return hid the bound). Lost-host recovery moves agents forward rather
than restoring an uncertain source executor.

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
a challenger that dies after the release does not leave the role free. The
release mark is durable (`role_handover`, migration 0478): an executor restart
does not let the resumed holder reclaim early or hand the role again to a
challenger that did not claim — the failed challenger stays out of the
handovers for an hour, even across the restart. A runner publishes standings
only for the agents it can claim (`agents_filter`): it never advertises a
readiness it will not act on. A workstation that boots therefore does not take
the role back from a VM.
`ameesh role status [ROLE] [--json]` prints the holder, the epoch and the
ranked candidates with the diagnostic of each refusal.

## Standby executors and the election briefing (L165/EP3)

Every candidate host runs the role's runner, and none of them takes a turn
before the claim: the runner only creates a worker after `registry.claim`
returns a live lease, the worker renews it before each turn, and a lost lease
kills the harness before any further turn. Standby costs no tokens and opens
no session.

When the winner is a DIFFERENT host from the previous holder, no session
crosses hosts. The same claim resets the session, its account and its lot
(`session_reset_at` set durably) and persists the resume briefing built by
the winner in `agent_registry.election_brief`. The new holder consumes it as
the resume summary of its new session; the first turn is a short "you are
elected" instruction framed by that briefing. The briefing is deterministic
and built from the last persisted state (role card and canon, assigned and
takeable lots, unread mail, pending decisions, alerts, GitHub observation per
repository); it names the previous holder, the epoch and the session left
behind. A same-host re-claim (runner restart) keeps the local session: it is
not an election. A cross-host claim **without** the brief is refused (the CAS
writes nothing): a mediated executor, whose brief is forced to null, therefore
stays on standby. The brief is dropped with its session (`set_session`,
`clear_session`) so it never resurfaces in a later session. The first turn
after an election may open a lot session: the lot's minimal brief then
completes the election briefing, it never replaces it (the role state is not
lost when the first turn is a lot turn).
## Fencing the elected pilot's gestures outside the database (L165/EP4)

The epoch fences more than the runner's own writes. The effects a pilot
prepares outside the database check the epoch first:

| gesture | entry point |
|---|---|
| merge | `ameesh work fire` / `work merged` / `work merge-pr` / the automatic packet merge (`packet_automerge` arm and check) |
| merge queue | `ameesh work queue add\|remove` / `work land` |
| assignment | `ameesh work assign` / an assigned `work add` (and mail assignment through it) |
| packet | `ameesh work packet record` / the packet train fire |
| decision routing | `ameesh decide ask` |
| drill canary (no effect) | `ameesh role canary [--json]` (EP7 P3, #3433) |

The canary is the drill's fenced gesture. A real gesture played "same-value"
(`work assign` of a lot to its current assignee) is refused by its business
rule *before* the fence, for a stale epoch as for a live one, and so proves
nothing. `ameesh role canary` acts under the environment's identity like any
gesture — `AMEESH_ROLE` + `AMEESH_LEASE_EPOCH`, or a session bound by its
runner — and its only writes are the lease renewal and one `pilot.gesture`
event (`gesture` and `ref` both `canary`). Exit `0`: accepted, the event id
is printed (`--json`: `{ok, role, epoch, host, runner, event}`). Exit `1`:
refused, nothing journaled; for a stale or expired epoch (or a stopped role)
the message names the holder, its host and its epoch, while an earlier
refusal (no or invalid epoch, unknown role, unreadable registry) states its
own reason — the proof of a refusal is the exit code, the message and the
absence of the event. Exit `2`: no lease to fence (a human, the bureau, a host-pinned
agent). For the drill, clear the session identity so only the epoch speaks,
negative first:

```sh
env -u AGENT_MAIL_NAME -u AMEESH_RUNNER_ID -u AGENT_MESH_RUNNER_ID \
    -u AMEESH_LEASE_OWNER AMEESH_ROLE=<ROLE> AMEESH_LEASE_EPOCH=<OLD-EPOCH> \
  ameesh role canary          # exit 1, names the holder at <NEW-EPOCH>
env -u AGENT_MAIL_NAME -u AMEESH_RUNNER_ID -u AGENT_MESH_RUNNER_ID \
    -u AMEESH_LEASE_OWNER AMEESH_ROLE=<ROLE> AMEESH_LEASE_EPOCH=<NEW-EPOCH> \
  ameesh role canary --json   # exit 0, one canary event at <NEW-EPOCH>
```

For an agent whose card says `releve: election`, `pilot_gesture.gate` reads
the session's bound identity. A session that is not the live lease-holder at
the epoch it was opened with is refused, and the message names the current
holder, its host and its epoch. The gate renews the lease and writes a
`pilot.gesture` event in the same database transaction, so a stale epoch
cannot even journal the gesture; the effect follows. The event carries the
agent, gesture, ref, epoch and host — never a token or a secret. The renewal
never shortens the lease: it keeps at least the TTL of the agent's host
(`policy.lease_ttl` or `volatile` of its Host card, else `AMEESH_LEASE_TTL`
— the runner's own rule), and the locked renewal keeps the later of the
current expiry and the new one (`GREATEST`, not a pre-read), so a gesture
cannot make the lease expire before the next heartbeat and elect another
pilot (#3115). It also does not extend past that host rule: the 300 s
default of a gesture only applies to a host whose card rules no TTL. On a
volatile host (90 s), holding 300 s after a gesture would keep a disappeared
host's pilot alive for 300 s instead of 90 s and delay the election by as
much; an external call's margin still extends the lease when it needs to
(#3123).

An EP5 timer (`ameesh role run`) may carry no agent identity but passes
`AMEESH_ROLE` and the epoch it entered with. That pair is fenced too: a role
without `AMEESH_LEASE_EPOCH` is refused, and the transmitted epoch is
compared to the live lease in the same transaction (its owner is checked when
the wrapper declares one). A timer therefore never passes as a human.

For an external call — `work merge-pr`'s push, `land`'s push or `gh pr
merge`, the fire's merge, and the packet auto-merge's `POST /merges` — the
database transaction cannot cover GitHub. The pilot's lease is renewed just
before the call with a margin at least the call's worst case (push timeout
plus read-back), and the call is refused if the renewed lease does not have
that margin left. The residual race remains: a call that blocks beyond its
own timeout can outlive the renewed lease, and another candidate may then
take the role. The target lease, the compare-and-swap on the target and
GitHub's `--match-head-commit` are the last guards; a failed renewal refuses
the effect before it starts, and an uncertain outcome is reported and
reconciled, never silently retried.

The guard is never a committed pre-check followed by a later write. The long
preparation (proof reads, CI, packet checks) comes first; `Gate.hold` and the
database write are then in the same transaction: `work assign`, an assigned
`work add`, `work merged`, `decisions ask` (including its `--new-lot`
creation), `fire`, the train fire, the packet auto-merge. A refusal, or an
election that wins in between, rolls the preparation back — no orphan lot,
marker or decision is left behind.

Humans, the bureau, host-pinned agents (`releve: hote`) and runner-internal
sweeps without `AMEESH_ROLE` are unchanged. This does not replace the target
lease (`merge_locks`) or GitHub's compare-and-swap: it stops a pilot that
lost the election from starting the gesture at all. The packet composition
itself is local git work; what is fenced is the composition record the pilot
writes and the train fire that follows it.

## Drill: planned stop, network cut and standby on return (L165/EP7)

The election is proven by a real exercise on staging (canon §14: the mesh's
hosts, never a production), not by a green unit test. EP7 is that proof for
the governance roles — the ameesh pilot (`mesh-design`), the Nexlink pilot
(`orchestrateur`), the auditor — and, through them, for any elected role. It
runs against a *deployed* version: a merge, or a suite that passes on `main`,
does not replace it.

### Criteria

`T0` is the gesture time written down by the operator (the stop, or the
network cut), not the observer's first sample. Durations are measured in
database time; the observer's wall clock is only used to order samples.

| # | Criterion | Measurement | Target |
|---|---|---|---|
| P1 | New holder | `T_expiry` = last `lease_expires_ts` read before `T0`; `T_claim` = first sample with another `lease_owner` and a higher `lease_epoch`; `T_turn` = first `turn.started` of the new holder | `T_turn - T0 < 5 min`; `T_claim` follows `T_expiry` by at most one 60 s election pass plus the rank delay (100 s with three candidates) |
| P2 | Never two holders | every observer sample carries at most one `lease_owner`; `lease_epoch` only grows; the old holder's renew is refused; no turn of the old holder survives the claim | exactly 1 holder in every sample that has one; 0 during the expiry window is expected |
| P3 | Epoch fence | one no-op fenced gesture from the old holder (`ameesh role canary` at the old epoch, §Fencing) must exit 1, naming the current holder, host and epoch, with no `pilot.gesture` event; the same at the new epoch exits 0 with exactly one canary event | old epoch refused, new epoch accepted |
| P4 | Return to standby | after the host (or its executors) comes back while another holder is live: no claim, no turn, same holder and epoch for 5 min; the returning runner logs standby | yes |
| P5 | Continuity | the new holder starts a turn and consumes a mail addressed to the role; the first hourly packet after the switch, containing the witness lot, is composed and merged by the elected pilot with no gesture from the stopped host; when EP5 timers are installed, the next `ameesh role run` tick runs under its epoch | `T_turn - T0 < 5 min`; the witness packet merges at the first hourly cutoff after the switch |
| P6 | No double effect | one `turn.started` per transition; exactly one merge in the window (the witness lot); no duplicated mail, wake, `pilot.gesture`, packet or landing; no other lot merged | 0 duplicate; 1 witness merge |

"Never two holders" is not "never zero": during the lease TTL after `T0`
there is no holder, and that is the designed window.

### Scenarios

- **A — planned stop of the workstation.** The owner or the bureau stops the
  host that holds the role (or, when the host cannot be powered off, all of
  the role's executors on it; the report names which). The lease expires after
  its effective TTL (180 s in the study; verify `lease_ttl`, the code default
  is 300 s and can miss the 5-minute target) and a candidate claims. Restart
  the workstation only after the new holder is live.
- **B — network cut.** The holder stays alive but loses the database; it
  stops renewing and, after the lease TTL, another candidate claims. When the
  database path returns, the former holder is refused by epoch and goes back
  to standby. This is the scenario that tests P2 and P3 for real, because two
  processes are alive at once.
- **C — return of the workstation.** After A or B the workstation comes
  back: it takes no lease and produces no effect from its old epoch. Only a
  planned handover (its availability at least 5 points above the holder for
  a full hour, at the end of a turn) can give it the role back.

To run both A and B from the same starting holder, after A restart the
workstation, then hand the role back by stopping the VM that just won (a
planned stop, same procedure); the workstation claiming a free lease is the
normal rank-delay path, not a violation. Then run B with the workstation
holding again. Do not transfer the lease by hand at any point.

### Staging variants accepted for the first run

The workstation's owner uses `pc-smichea` during the day, and the bureau has
no root network filter. The first exercise therefore uses two named variants,
recorded as such in the report; they exercise the election and the fence, not
a hardware loss or a network partition:

- **A′ — executors only.** On the holder, at `T0`, stop the role's executor
  with an immediate kill — never a plain `systemctl stop`, which drains the
  running turn for up to 30 min and keeps renewing the lease:

  ```sh
  systemctl --user stop --no-block 'ameesh-runner-agent@<ROLE>'
  systemctl --user kill --signal=KILL 'ameesh-runner-agent@<ROLE>'
  systemctl --user stop 'ameesh-runner-agent@<ROLE>'
  systemctl --user is-active 'ameesh-runner-agent@<ROLE>'   # must be inactive
  ```

  The scope is the role's executor(s) on the holder. All executors of the
  host may be stopped instead only when every governance role they serve has
  a live standby on another host; otherwise the others would lose their
  holder for nothing. The report names the scope. The host stays up; the
  lease follows its TTL, exactly as in A. Never the `ameesh-runner-ima`/TPK
  units.
- **B′ — process freeze.** `SIGSTOP` the role's runners and hold them
  frozen until a **new holder with a higher epoch is observed**, not for a
  fixed sleep: `SIGCONT` must never precede the expiry, or no takeover
  happens and the exercise proves nothing. The wait is bounded by the
  effective TTL plus 180 s (one election pass, the rank delay and a margin);
  if the bound expires without a takeover, `SIGCONT` and stop the exercise
  (restore on a surviving host) — that is a failed run, not a drill. On
  resume the old process must be refused by epoch and go back to standby.
  This is not a network cut; it proves the same no-renewal and stale-epoch
  paths (P2, P3, P4).

  ```sh
  systemctl --user kill --kill-whom=all --signal=STOP 'ameesh-runner-agent@<ROLE>'
  # poll `ameesh role status <ROLE> --json` every 5 s; stop at the first
  # sample with another holder and a higher epoch, or at
  # effective_ttl + 180 s (the bound), whichever comes first
  systemctl --user kill --kill-whom=all --signal=CONT 'ameesh-runner-agent@<ROLE>'
  ```

The nominal A (whole host off) and B (root network cut) stay on the list for
a later window with the owner's agreement. The report says which variant ran
and does not present A′/B′ as a hardware-loss or network-partition proof.

### Preconditions

- EP1–EP6 are **deployed by a release**, not merely merged:
  `ameesh role status` names the role, its holder, its epoch and the ranked
  candidates on the deployed hosts.
- At least two candidates of the role are ready: placement admitted, host
  gate open, canon `ok`, fresh standing, and the role's **standby runner**
  installed and running on each (`systemctl --user enable --now
  ameesh-runner-agent@<ROLE>` on the candidate; never `agent-runner
  register` there, which would move the registry row to that host instead of
  standing by). A standby takes no turn and costs no token.
- The effective lease TTL is the 180 s of the study. `lease_ttl` is a
  parameter and the code default is **300 s**: a 300 s lease plus one 60 s
  election pass and the rank delay can exceed 5 minutes after `T0`, so the
  target may be missed. Verify the effective value before `T0`. If it exceeds
  180 s, either set it to 180 s first (decision A) or run at 300 s and record
  the drift and the missed target as a result (decision B); the criterion is
  never re-based on the expiry — the delay from expiry stays diagnostic.
- The drill starts from the holder named by `ameesh role status`, never from
  an assumption; if the target host is not the holder, either run the drill
  on the actual holder or wait for a planned handover.
- A quiet window: no real packet in CI, no release in progress. A witness
  lot (docs-only, light, no production effect) is created at drill start and
  assigned to an agent that runs on a candidate VM; it is frozen and reviewed
  **before the switch**, so the post-switch packet depends only on the elected
  pilot/composer, not on the stopped host. Its packet is the first hourly
  packet after the switch, composed by the elected pilot. It must merge with
  no gesture from the stopped host, and no other lot is merged in the
  window.
- Owner go for the stop or the network cut (bureau delegation, canon §10);
  the bureau executes the host gesture and writes `T0` on the host.

Baseline at 2026-10-23T15:40Z, kept here as the starting point of EP7 (it is
not a verdict): the deployed version is 1.19.0, which has no `ameesh role`
command, so EP1–EP4 are not in staging; `agent_registry` still carries
`releve='hote'` for `mesh-design`, `auditeur` and `orchestrateur`;
`agent_candidates` is empty; `ameesh role status` answers "no elected role
declared". The holders are all on `pc-smichea` (epochs 198, 143 and 174 at
the reading). Seven-day availability is 100 % on `ameesh-agents-1` and
`ameesh-agents-2`, 79.8 % on `pc-smichea` (8042 minutes of 10080). The
declared target is three candidates for the two pilots and the auditor,
`[ameesh-agents-1, ameesh-agents-2, pc-smichea]`, effective only after
`#3101` and `manaty/home#156` (the admission counted once) are merged and
deployed, and after the standby runners are started on the VMs. Until then
the exercise is blocked, not failed.

### Procedure

0. **Prepare.** Create the witness lot now (docs-only, light, estimate a
   few minutes), assign it to an agent that runs on a candidate VM, freeze
   it and get its review **before the switch**; pin the two candidate hosts;
   check no packet is in CI. Keep a shell on a **surviving** host (a VM that
   is not the holder) for the observer.
1. **Baseline.** On the surviving host, record `date -u`, `ameesh role
   status <ROLE> --json`, the role's row (`host`, `lease_owner`,
   `lease_epoch`, `lease_expires_ts`) and `ameesh list`. Note the witness
   lot id. Send a mail to the role so P5 has a witness, and note its id.
2. **Observer.** On the surviving host, in `$TMPDIR` or under
   `~/.local/state/ameesh/drills/`, sample the role every 5 s and prefix
   each line with the time:

   ```sh
   while :; do
     printf '%s ' "$(date -u +%FT%T.%3NZ)"
     ameesh role status <ROLE> --json
     sleep 5
   done | tee "$HOME/.local/state/ameesh/drills/ep7-<ROLE>-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
   ```

   Record the observer start and stop times. Also read the last
   `lease_expires_ts` before `T0`: it is `T_expiry` for P1.
3. **Gesture A or A′.** The bureau runs the A′ stop command above on the
   holder (or stops the whole host when that is the agreed variant) at `T0`,
   written down on the host and repeated on the lot thread. A plain
   `systemctl stop` is not a host loss: it drains the turn and keeps the lease
   alive for up to 30 min. Do not touch the database. Watch the observer: the
   lease must expire, then one candidate must claim with a new epoch.
4. **First turn (P1, P5).** As soon as the new holder is visible, send it a
   short mail (or check the one sent at step 1). `T_turn` is the first
   `turn.started` for the role after the claim, read from `ameesh events
   --json`. The new holder must process the mail within 5 min. Once it is
   live, the elected pilot (or the packet composer it delegates to) composes
   the first hourly packet with the witness lot; it must merge with no
   gesture from the stopped host. Record the merge time and the actor.
5. **Fence (P3).** From any surviving shell, present the old epoch the way
   an EP5 timer would and run the dedicated no-op canary (`ameesh role
   canary`, added to the gesture table of §Fencing by #3433):

   ```sh
   env -u AGENT_MAIL_NAME -u AMEESH_RUNNER_ID -u AGENT_MESH_RUNNER_ID \
       -u AMEESH_LEASE_OWNER \
       AMEESH_ROLE=<ROLE> AMEESH_LEASE_EPOCH=<OLD-EPOCH> \
       ameesh role canary
   ```

   It must exit 1, name the current holder, host and epoch, and write no
   `pilot.gesture` event. Then the positive control, same command with
   `<NEW-EPOCH>` and `--json`, must exit 0 and journal exactly one `canary`
   gesture at the new epoch — this proves the refusal was not vacuous. Exit 2
   means no live lease: that is not a fence result. The old `work assign
   <witness> <current-assignee>` canary is **invalid**: the business check
   ("already assigned") refuses it before the epoch gate, for both epochs.
   The real pass requires #3433 deployed on the candidate hosts. Never
   attempt `fire`, `land`, `merge`, `queue remove` or a decision creation as
   a drill gesture: an accepted one would be a real effect.
6. **Scenario B or B′.** Hand the role back (stop the VM holder, let the
   workstation claim on the free lease). Then either cut the workstation's
   database path — the tunnel or the DB endpoint only, never the LAN, with an
   automatic rollback timer — or, when root is unavailable, run the B′
   `SIGSTOP`/`SIGCONT` above. Record `T0`; check expiry, claim, the new epoch
   and, when the path or the process returns, that the old holder is refused
   and stays standby.
7. **Return (P4, P6).** Restart the stopped executors
   (`systemctl --user start 'ameesh-runner-agent@<ROLE>'`) or the host while
   the new holder is live. For 5 min the observer must show the same holder
   and epoch; the returning runner logs standby and opens no turn. Then compare the events
   of the window: one transition, exactly one merge (the witness lot), no
   duplicated mail, wake, `pilot.gesture`, packet or landing, and no other
   lot merged.
8. **Report.** Save the observer JSONL, the `ameesh events --json` slice,
   the two `journalctl --user -u ameesh-runner-agent@<role>` excerpts (old
   and new holder) and the operator timeline. The lot carries a three-line
   note: verdict per criterion, the measured `T_claim` / `T_turn`, and a
   `sha256` of each evidence file kept on the observer host. No process
   journal goes into the repository.

### Stop conditions

Stop the exercise, restore the host or the network and do not retry
blindly: two holders observed; an old-epoch gesture accepted; a duplicated
mail, wake, landing, fire or packet; any production effect; no new holder
5 min after `T0` on two consecutive attempts; go withdrawn. A failure is a
result: record it, leave the current holder in place (the lease decides) and
do not force the witness merge to "unblock" anything.

### Scope

This drill does not deploy, migrate, publish or spend. The only merge is
the witness lot, docs-only, through the normal packet path by the elected
pilot; no other lot is merged. The Nexlink pilot runs the same procedure
under lot `#3024` and `docs/ops/election-pilote-nexlink.md`; the two drills
may share one window but never one holder.
