---
type: Study
title: "L498 — delegated cgroup v2 per turn: stable ownership and bounded cleanup"
description: "Design study: one delegated cgroup per turn under the runner unit, entered before the harness executes, recorded as the turn's owner, closed by cgroup.kill; fallbacks, carve-outs and the authorizations it needs."
status: draft
tags: [pression, cgroup, harnais, nettoyage, hote]
generated: { by: "mty-amh-impl-02", at: "2026-10-13" }
---

# Recommendation

**Give every turn its own cgroup under its runner unit, enter it before the
harness executes anything, record that cgroup as the turn's owner, and close
the turn by killing it.** Kernel membership follows `fork`/`exec`, so
`setsid`, double `fork` and reparenting cannot escape it, and cleanup stops
enumerating PIDs. The existing PID/PGID + subreaper + pidfd machinery stays as
the exit-code reader and the fallback; this study does not remove it.

This is the follow-up #490 names: its targeted fix keeps DeepSeek tool commands
in the runner's cgroup but "does not provide that per-turn attribution"
(`docs/EXPLOITATION.md`, the residual risk tracked since #472/#717). The scope
listed by the #490 design is the one kept here: user and system runner units,
guest/mediated execution, double fork, `setsid`, and hand-off between live
turns.

The study is read-only. Unit settings and any on-host activation need the
owner's agreement; on-VM measurements need mesh-design's agreement. No freeze
(L460 stands) and no weight change (L477 P3/P8 stand) are proposed.

# Baseline: how a turn is owned today

- The harness is launched with `subprocess.Popen(..., start_new_session=True)`
  (`src/ameesh/runner.py:3878`): its pid is its process-group id.
- `_open_turn_resource(turn_id, pgid)` records the group
  (`src/ameesh/runner.py:3484`); `_close_turn_resource`
  (`src/ameesh/runner.py:3497`) marks the resource `orphan` while the group
  still runs, from two strict read-only snapshots
  (`src/ameesh/turn_processes.py:30`).
- At the end of a normal turn, `Tracker.survivors` decides what survives
  (`src/ameesh/background.py:282`), `Tracker.start` opens the grace period
  (`:338`), `Tracker.tick` applies the deadline (`:377`). On Linux the runner
  becomes a child subreaper (`src/ameesh/platform/process.py:480`) and keeps
  pidfds on the targets it observed.
- The residual is written in `docs/EXPLOITATION.md` (~line 512): a live member
  can fork — or `setsid` — past the last snapshot, so snapshot-based ownership
  cannot be complete. "L'attribution et l'arrêt exhaustif de ces descendants
  demandent un cgroup délégué par tour, à traiter dans le suivi #498."
- #490 (PR #470, open at the time of writing) removes dsh's native
  `systemd-run --scope` so tool commands inherit the caller's cgroup; it fixes
  runner-level placement and leaves per-turn attribution and cleanup here.

# What the hosts expose today (read-only, ameesh-agents-1, 2026-10-13)

- Kernel 6.8.0-142, systemd 255, cgroup v2 unified; `cgroup.kill`,
  `cgroup.events` and `cgroup.freeze` are present in the runner unit's
  directory.
- `user@1001.service` has `cpu memory pids` in `cgroup.subtree_control`, with
  its own processes in `init.scope`: systemd's delegation layout.
- `ameesh.slice` and `ameesh-agents.slice` enable `memory pids` only. The
  runner unit therefore has `memory.max` (6 GiB) and `pids.max`, but **no
  `cpu.weight`**: a per-turn cgroup can own and kill, it cannot carry a CPU
  weight without a host change (L477, "what would have to be true", point 2).
  This study asks for none.
- The unit is `Delegate=no`; its directory belongs to `ameesh` (created by the
  user manager) and `cgroup.subtree_control` is empty. On a **system** unit
  (mesh server, guest) that directory is created by the system manager:
  delegation is what makes it writable by the service's user.
  `deploy/guest/ameesh-guest-runner@.service:73` sets `ProtectControlGroups=yes`,
  which mounts `/sys/fs/cgroup` read-only for the unit and is incompatible
  with delegation as is.
- systemd: with `Delegate=yes` "the control group tree below the unit's
  control group is owned and managed by the unit itself", and all supported
  controllers are enabled for it; `DelegateSubgroup=` (systemd ≥ 254) places
  the unit's own processes in a subgroup, which is how the no-internal-process
  constraint is respected while the delegated root stays free for children.
- Kernel: writing `1` to `cgroup.kill` kills the cgroup and all descendants
  with SIGKILL, "deal[s] with concurrent forks appropriately and is protected
  against migrations"; delegation keeps the parent's restrictions
  hierarchical ("nothing can escape the resource restrictions imposed by the
  parent").

# Design

## 1. Unit layout

On each runner unit (user, server, guest):

```
Delegate=yes
DelegateSubgroup=runner
```

The runner's own process lands in `<unit>/runner/`; turn cgroups are siblings:
`<unit>/turn-<id>/`. The guest unit additionally needs `ProtectControlGroups=no`
on the runner only. `MemoryMax`/`MemoryHigh` stay on the unit: per-turn
children are accounted inside it, and `systemctl set-property --runtime` (the
L159 guard) still caps the unit and its whole subtree.

Where delegation is unavailable — older host, macOS, Windows, a cgroup
directory that is not writable, a mediated server that refuses the operation —
the runner logs once and keeps today's path. Off those hosts the trampoline
below is not used at all.

## 2. Creating and entering the turn cgroup

- Before `Popen`, the runner resolves its own cgroup
  (`src/ameesh/platform/host.py:648`), checks it is a delegated, writable
  directory, and creates `turn-<id>/`.
- Placement must happen **before the harness executes anything**. Route: a
  trampoline module (`src/ameesh/turn_cgroup.py`, modelled on
  `src/ameesh/platform/audit_guard.py`), launched as
  `python -m ameesh.turn_cgroup --cgroup <path> --ready-fd <n> -- <harness argv>`:
  1. writes its own pid to `<path>/cgroup.procs`;
  2. re-reads `/proc/self/cgroup` and refuses unless the path is exactly the
     expected one, inside the runner's delegated root, with no symlink escape;
  3. writes `READY` to the inherited descriptor and closes it;
  4. `os.execvpe`s the harness (same pid, same session and group leader).
- The runner waits for `READY` with an independent bound, as the audit route
  already does for its scope (`src/ameesh/runner.py:3896`), and fails the turn
  closed (consign re-queued, nothing delivered) if placement is refused or the
  signal is missing. A dedicated descriptor keeps the harness's own
  stdout/stderr untouched.
- A failed placement is a refused launch, not a silent fallback: where
  delegation is expected, the harness must not run outside its turn cgroup.

## 3. End of turn: hand-off, kill, verification

The decision logic does not change; the **unit** of ownership does.

- A normal end with surviving work keeps the turn cgroup open as the
  background owner until the tracker's deadline: the tree stays where it is.
  No pid migration — that is precisely the racy operation this replaces.
  `turn_resources` holds the cgroup path and the deadline.
- An urgent end, a lost lease or an expired deadline: write `1` to
  `<path>/cgroup.kill`, wait (bounded) for `populated 0` in
  `<path>/cgroup.events`, then `rmdir`. A cgroup that cannot be removed is
  reported `orphan` with its path.
- The subreaper keeps reaping adopted zombies and recording exit codes; pidfds
  stay for already-observed jobs. They become telemetry and fallback, not the
  proof of cleanup.

## 4. Persisted attribution and reconciliation

- `turn_resources` carries the turn's cgroup path next to the pgid; the path
  is stable where a pid is reusable.
- At runner start, and periodically, enumerate the `turn-*` children of its
  own delegated cgroup: any child without an open turn resource for this
  runner belongs to a dead turn — kill and remove it. On unit stop, systemd's
  own cgroup cleanup remains the backstop.
- Containers keep their label route (`src/ameesh/containers.py`): docker
  scopes live under `system.slice`, outside the runner's cgroup, and the
  cgroup cannot own them.
- `ameesh doctor` gains a read-only check: unit delegated, `cgroup.kill`
  available, delegated root free of the runner's own processes.

# Coverage, limits, carve-outs

Fixed: double `fork`, `setsid`, reparenting to init or a subreaper, pid reuse,
the fork race of the last snapshot; per-turn memory accounting inside the
unit's cap; turn liveness decided from `cgroup.events` without a scan.

Not fixed, or deliberately out of the first slice:

- A tool that **deliberately migrates out** (`systemd-run`, `docker run`)
  keeps today's behaviour: the cgroup is an ownership boundary, not a sandbox.
  This is the same accepted limit as #490's.
- **Containers** keep the label route.
- The **exempt/auditor turn** runs in its own `systemd-run --scope`, a sibling
  of the unit under `app.slice`, which cannot be nested under the service. It
  keeps its memory scope and control-group kill and stays out of the per-turn
  cgroup until a later slice.
- A **resident harness** (the L76 parent) is one long-lived process: a
  per-turn cgroup then describes the agent/lease, not the turn, unless the
  harness itself moves its tool commands per turn. That is a decision for the
  resident slice; this design does not pretend to it.
- **Mediated execution**: the cgroup lives on the host that forks the turn, so
  the same code applies to the local runner; a remote process-control
  operation stays as it is (`background` already handles `NotSupportedRemotely`)
  and is extended with #472 if needed.
- **Non-Linux hosts** keep PID/PGID.

# Alternatives considered

- `systemd-run --user --scope` per turn: a scope is a child of a slice, not of
  the runner service, so it would escape the unit's `MemoryMax` — the #477
  placement finding; rejected.
- Moving survivors into a per-agent background cgroup at hand-off: moving pids
  is racy against `fork` and a child stays behind; keeping the turn cgroup
  until its deadline is stable; rejected.
- `clone3(CLONE_INTO_CGROUP)`: atomic placement with no window at all, but it
  needs a native helper; the self-move trampoline before `exec` is equivalent
  for a harness that does nothing before its checks. Keep as a possible
  refinement, not a requirement.
- Freeze/thaw recovery service: out of scope (L460's conclusion).

# Verification before any activation

- **Read-only, on a canary unit**: `Delegate=yes` + `DelegateSubgroup=runner`
  gives a writable root and enables `memory`/`pids` in its subtree, leaves
  `MemoryMax` and the guard's `set-property` effective, and systemd removes
  the tree on unit stop. No change is made by this study.
- **Witness tests** (hermetic where a plain cgroup fixture can simulate
  delegation, skipped otherwise): the trampoline refuses a foreign or missing
  cgroup; a double-fork + `setsid` descendant dies with `cgroup.kill` while
  the old snapshot route misses it; a grace-period job survives to its
  deadline; a reused pid cannot make a closed turn look alive; the
  no-delegation path is bit-for-bit today's.
- **Measurements** (mesh-design + owner agreement): orphan rate, cleanup
  latency, lease jitter, per-turn memory accounting.

# Decisions needed

1. **mesh-design**: is this the route expected for L76c, and are the two
   carve-outs (exempt/auditor turns, resident harness) the right first-slice
   boundary?
2. **owner**: authorization for the unit settings on a canary host
   (`Delegate=yes`, `DelegateSubgroup=runner`; guest: `ProtectControlGroups=no`
   on the runner unit) and for the measurement window. No unit, weight or
   freeze change is part of this study.
3. **First slice**: user runners on ameesh-agents-1 only, then the mesh server
   and the guest.
