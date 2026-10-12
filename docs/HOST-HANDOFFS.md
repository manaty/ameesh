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
other controllers, cancellation and explicit retries. Each journal change
commits before the next external effect. A controller crash releases its
lock; its successor inspects existing external effects and resumes the
checkpoint. Calls must use a dedicated database connection per controller.

The adapter must enforce enrolled repository/host identities and approval
policy. `revalidate` checks current physical capacity including reservations,
canon revision, placement, pinned release, credential readiness and existing
executor/checkout/session effects. It must use timeouts and never return
credentials, OAuth caches or private diagnostics in evidence.

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
`session`, `admit`, `enable`, `verify`, `revalidate` and `restore_source`.
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

L232a is tested with simulated adapters and local Git repositories. It does
not install a real host adapter, enable production agents, copy secrets,
perform root operations or authorise spending. L232b/c and provider login
readiness supply those separate integrations.
