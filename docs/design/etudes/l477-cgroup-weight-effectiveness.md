---
type: Study
title: "L477 — effective CPU/I/O weights on runner and guest cgroups"
description: "Read-only audit on ameesh-agents-1: the guest CPU weight is effective, its I/O weight is inert (no iocost/BFQ), the runner units carry no weight, and DeepSeek tool scopes escape the runner cgroup."
status: draft
tags: [pression, cgroup, cpu, io, audit, hote]
generated: { by: "mty-amh-impl-02", at: "2026-10-12" }
---

# Recommendation

- **Keep the guest's `CPUWeight=10`.** It is set on the guest slice *and* on its
  parent `ameesh.slice`, and the kernel shows `cpu.weight=10` at both levels:
  the whole guest yields to the host's other work as intended.
- **Stop presenting `IOWeight=10` as effective on this host.** The storage path
  has neither iocost nor BFQ (`io.cost.qos` and `io.cost.model` are empty, every
  block device runs `[none] mq-deadline`), so `io.weight` cannot arbitrate I/O.
  A successful property write is not a kernel effect (the limit already stated
  in [L460](l460-gel-cgroup.md)). Keep admission control, duration caps and
  memory protection as the I/O-pressure levers until a backend is enabled and
  calibrated.
- **Do not extend weights to the runner units as the first move.** The
  DeepSeek Harness runs each tool command in a `dsh-subprocess-*.scope` under
  `app.slice`, outside `ameesh-agents.slice` and outside the runner unit
  cgroup; weighting the runner units would not cover the tests and builds they
  are meant to shape. Claude Code's tool processes, by contrast, stay inside
  the runner cgroup.
- **Any host change (enabling iocost, moving the scopes, setting unit weights)
  needs the owner's separate authorization**, preceded by the controlled
  measurement proposed below. This audit changed nothing and generated no load.

# Scope and method

Read-only audit of **ameesh-agents-1** (the guest host), on 2026-10-12 between
18:50 and 19:05 UTC. Kernel `6.8.0-142-generic`, systemd 255
(`255.4-1ubuntu8.17`), repository baseline `origin/main` at `9ac74f0b`.

Commands used: `systemctl show` and `systemctl --user show` (unit properties),
reads of `/sys/fs/cgroup/**` (`cgroup.controllers`, `cgroup.subtree_control`,
`cpu.weight`, `io.weight`, `io.cost.qos`, `io.cost.model`),
`/sys/block/*/queue/scheduler`, `findmnt`, `lsblk`, `docker ps`, `docker info`,
`systemctl show 'docker-*.scope'`, `/proc/<pid>/cgroup`, `/proc/pressure/*`,
`ameesh list`, a read-only `SELECT` on `agent_registry` for leases, and
`journalctl`. No unit, cgroup, scheduler, mount or configuration was modified,
and no load was generated on purpose.

Not audited: **pc-smichea** and the mesh server. Neither is reachable from this
host (no `~/.ssh`, `pc-smichea` does not resolve), so the findings below cover
the guest host only. The exact read-only commands to fill that gap are at the
end of this study ("Rerunning it on pc-smichea and the mesh server").

# 1. The hierarchy actually in place

Observed on ameesh-agents-1 (`cpu.weight` and `io.weight` read from the cgroup
files; `—` means the file does not exist because the controller is not enabled
at that level):

| cgroup (manager) | controllers | `cpu.weight` | `io.weight` |
|---|---|---|---|
| `/ameesh.slice` (system) | cpuset cpu io memory hugetlb pids rdma misc | **10** | **default 10** |
| `/ameesh.slice/ameesh-guest.slice` | cpu io memory pids | **10** | **default 10** |
| `…/ameesh-guest-runner@deepseek1,6.service` | flat (memory only) | — | — |
| `/system.slice` | all | 100 | default 100 |
| `/system.slice/docker-<id>.scope` (8 pgtests) | all | 100 | default 100 |
| `/user.slice/user-1001.slice` | cpu memory pids | — | — (io absent) |
| `…/user@1001.service` | cpu memory pids | — | — (io absent) |
| `…/app.slice` | cpu memory pids | 100 | — |
| `…/app.slice/dsh-subprocess-*.scope` | flat (memory only) | — | — |
| `…/ameesh.slice` (user) | cpu memory pids | 100 | — |
| `…/ameesh-agents.slice` | memory pids | — | — |
| `…/ameesh-runner-agent@<agent>.service` | flat (memory only) | — | — |

Two different `ameesh.slice` units coexist: the **system** one (`/ameesh.slice`,
the guest, weight 10) and the **user** one (under `user@1001.service`, weight
100, the local DeepSeek/Claude runners). They compete at different levels of
the root.

# 2. Guest weights: CPU effective, I/O inert

The guest configuration is the one described in L460:
`deploy/guest/ameesh-guest.slice:24-25` sets `CPUWeight=10`/`IOWeight=10`, and
the parent drop-in `deploy/guest/ameesh.slice.d/guest-weight.conf:16-17` sets
the same on `ameesh.slice` so the whole guest yields to the application. The
offload installer does the same on the dedicated user's slice
(`deploy/guest/install.sh:330-331`). The live host confirms it:

```
$ cat /sys/fs/cgroup/ameesh.slice/cpu.weight
10
$ cat /sys/fs/cgroup/ameesh.slice/io.weight
default 10
$ cat /sys/fs/cgroup/ameesh.slice/cgroup.subtree_control
cpu io memory pids
```

So both controllers are enabled in the guest subtree, `cpu.weight=10` is in
effect at the guest and parent levels, and the guest runner units
(`ameesh-guest-runner@deepseek1/6.service`, `MemoryMax=4.5G`) share that slice
weight without a weight of their own.

The I/O half has no effect. On this host:

```
$ wc -c /sys/fs/cgroup/io.cost.qos /sys/fs/cgroup/io.cost.model
0 /sys/fs/cgroup/io.cost.qos
0 /sys/fs/cgroup/io.cost.model
$ for f in /sys/block/*/queue/scheduler; do echo -n "$f: "; cat "$f"; done
/sys/block/sda/queue/scheduler: [none] mq-deadline
/sys/block/vda/queue/scheduler: [none] mq-deadline
$ ls /sys/module/bfq
ls: cannot access '/sys/module/bfq': No such file or directory
```

`io.weight` is honored only by **iocost** or by **BFQ's cgroup support**
(kernel I/O controller documentation, pointed to by L460). Neither is active:
iocost has no device configured, BFQ is not loaded, and both storage paths
(`vda1` for the host root and `sda` for `/srv/ameesh-guest`) run
`[none] mq-deadline`. The guest's `io.weight=10` is therefore accepted, visible
and **inert**. Any future claim that the guest "yields on I/O" must first show
a calibrating measurement, not a property read-back.

# 3. Runner units: no weight, and no CPU controller in their subtree

On the user side, none of the 13 runner units carries a weight, and neither
does `ameesh-agents.slice` (which only sets `MemoryHigh=80%`/`MemoryMax=85%`,
`deploy/systemd/ameesh-agents.slice:19-20`; the unit itself sets
`MemoryHigh=5.4G`/`MemoryMax=6G`,
`deploy/systemd/ameesh-runner-agent@.service:67-69`):

```
$ systemctl --user show ameesh-runner-agent@mty-amh-impl-02 -p CPUWeight -p IOWeight
CPUWeight=[not set]
IOWeight=[not set]
$ cat …/user@1001.service/ameesh.slice/ameesh-agents.slice/cgroup.controllers
memory pids
```

`cpu` is not propagated into `ameesh-agents.slice`, so the runner units have no
`cpu.weight` file at all: for CPU they are flat members of the user
`ameesh.slice` (weight 100), exactly like every other slice at that level. A
per-runner `CPUWeight` would first have to make systemd enable the `cpu`
controller along that chain.

The `io` controller is not even **available** under `user@1001.service`:

```
$ cat …/user.slice/user-1001.slice/cgroup.controllers
cpu memory pids
```

so a user-level `IOWeight` cannot be in effect as installed. (systemd may
enable controllers on demand when a unit requests one, but today nothing does;
and the host-level I/O backend is missing anyway, see §2.)

# 4. Placement: DeepSeek tool scopes escape the runner cgroup

The most consequential finding. Each command of a DeepSeek Harness agent is
launched in its own transient scope:

```
$ cat /proc/self/cgroup
0::/user.slice/user-1001.slice/user@1001.service/app.slice/dsh-subprocess-2859202-….scope
$ systemctl --user show dsh-subprocess-…scope -p Slice -p CPUWeight -p IOWeight -p MemoryMax
Slice=app.slice
CPUWeight=[not set]
IOWeight=[not set]
MemoryMax=infinity
```

The scope's parent process is the runner unit
(`…/ameesh.slice/ameesh-agents.slice/ameesh-runner-agent@mty-amh-impl-02.service`),
but the scope itself is a **sibling** of `ameesh.slice` under `app.slice`,
which has no `MemoryMax` either. Eight such scopes were live during the audit
(four `mty-amh-impl-*`, `deepseek3/4/5`, one long test from `mty-amh-impl-04`).
This is a DeepSeek Harness behaviour (the scope's command is
`…/dsh-subprocess-local/lib/runner.js`), not ameesh runner code: `runner.py`
does not create scopes for commands.

Consequences today:

- the runner unit's `MemoryMax=6G` does **not** bound the agent's tests and
  builds, only the harness process itself;
- a future `CPUWeight`/`IOWeight` on the runner unit would not cover that work;
- the heavy work of all DeepSeek agents competes flat inside `app.slice` (which
  is a plain 100-weight child of `user@1001.service`).

Claude Code behaves differently: for `ameesh-dev3` and `ameesh-dev5` the tool
processes (`bash`, `timeout`, `python -m unittest`, `ugrep`, `psql`) were found
inside `ameesh-runner-agent@<agent>.service/cgroup.procs`. Their work **is**
covered by the unit's memory cap and by any weight set on the unit.

# 5. Docker and the database are outside both hierarchies

The eight disposable PostgreSQL containers run as `docker-*.scope` under
`/system.slice`, at `cpu.weight=100`/`io.weight=default 100`, outside
`ameesh.slice` and outside `user@1001.service`:

```
$ docker ps --format '{{.Names}}'
mty-amh-impl-04-pgtest … (8 containers, postgres:17)
$ cat /sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.weight
100
```

At the root, `system.slice` (100) competes with the guest `ameesh.slice` (10)
and with `user.slice` (100). The database containers are therefore never
lowered along with agent work — which matches L460's "do not lower the database
service's weight" — but there is also no cgroup lever that isolates the
database from a heavy local agent run at equal root weight: only the guest side
yields. Any weighting of the local user hierarchy must keep this in mind and be
validated against database latency.

# 6. Heavy suites are serialized and niced, not weighted

`ameesh heavy` takes an exclusive `flock` slot and runs the command under
`nice` (default 10); it sets no cgroup property (`src/ameesh/heavy.py`). Under
CPU contention `nice` does arbitrate; under I/O saturation it does not. There
is no `CPUWeight`/`io.weight` behind the heavy slots.

# 7. Contention and lease latency observed

The host was under real I/O pressure during the audit; PSI read at 18:55 UTC:

```
cpu   some avg10=8.51  avg60=10.51 avg300=12.50
io    some avg10=48.39 avg60=31.91 avg300=21.32   full avg10=33.07
memory some avg10=0.00
```

I/O is the contended resource, which is exactly where `io.weight` is inert.

Lease mechanism (read from `src/ameesh/runner.py` and `config.py`): the default
`lease_ttl` is 300 s; the heartbeat interval is `max(3, lease_ttl/3)` = **100 s**
during a turn; a lease is lost only when its deadline passes, and the runner
then stops the turn's group hard. A read-only sample of `agent_registry` took
40 points every ~5.1 s (≈200 s) for the 13 agents of this host:

- idle agents (`deepseek2`, `ameesh-dev6`): remaining TTL pinned at 295–300 s,
  renewed on each poll;
- agents in a turn (11 of them): sawtooth 300 → 200 s; **minimum remaining TTL
  observed 199.6–202.0 s**, i.e. the renewal landed at the nominal 200 s floor
  within one 5 s sample; observed cadence ≈102 s against 100 s nominal;
- `last_seen` was up to ≈100 s for busy agents (updated on heartbeat) and ≤5 s
  for idle ones;
- no `lease lost` / `deadline` incident in the runner journals over the past
  6 hours.

So under the current load a lease would need ≈200 s of missed heartbeats to be
lost, and the observed lateness is at most about one sample (≈5 s). The lease
is not the fragile part today; the heartbeat's database round-trip is the thing
to watch if weights are ever enabled, because it shares the DB/I/O path.

# What would have to be true before weighting anything

1. **Placement** — the work to be weighted must be inside the cgroup carrying
   the weight. For DeepSeek agents that means placing the
   `dsh-subprocess-*.scope` inside the runner cgroup (or a dedicated weighted
   slice), not under `app.slice`.
2. **Controller** — `cpu` enabled along `ameesh-agents.slice` (or the chosen
   parent); `io` enabled under `user@1001.service` if user units are to carry
   `IOWeight`.
3. **Backend** — iocost or BFQ enabled and calibrated on the relevant devices
   (`vda1`, `sda`); verified by a differential read/write measurement, never by
   a property read-back.
4. **Measurement** — lease-renewal jitter, database round-trip, mail wakeups
   and test/build wall time, before/after, on a canary agent.
5. **Authorization** — owner decision; this audit authorizes nothing.

# Policy proposal

- **P1 — Guest CPU.** Keep `CPUWeight=10` on `ameesh.slice` and
  `ameesh-guest.slice`. No change; it is effective and matches the offer model.
- **P2 — I/O claims.** Treat `IOWeight=10` as *accepted but inert* on this
  host. Either enable iocost/BFQ and calibrate it (P5), or state in the
  capacity story that I/O prioritization currently relies on admission control
  and duration caps only. Do not use the guest `IOWeight` as evidence of I/O
  protection in a host report.
- **P3 — Runner weights.** Do not set `CPUWeight`/`IOWeight` on the runner
  units before P4 (placement) and P1 of "what would have to be true". If a
  cheap first step is wanted, the CPU side already yields through the guest
  slice; nothing needs to be added to the local runners to protect the host
  from the guest.
- **P4 — Scope placement (harness).** Ask the DeepSeek Harness to let the
  command scope be created in the caller's cgroup, or with an explicit slice;
  then place it under `ameesh-agents.slice` / `ameesh-guest.slice`. This also
  restores the `MemoryMax` coverage of tool work. This is a harness change, not
  a host change, and it should be measured (does dsh's isolation or cleanup
  depend on the scope?).
- **P5 — I/O backend.** If I/O prioritization is wanted, enable iocost (or
  BFQ) on the two devices, calibrate it, and start with the guest only (weight
  already 10). Never include the PostgreSQL containers in a lowering; measure
  database latency before/after.
- **P6 — Measurement protocol.** On a canary agent: record baseline lease
  jitter, DB round-trip and heavy-test wall time; then run a CPU hog and an I/O
  hog with and without the weights; decide on the delta. This generates load
  and therefore needs explicit authorization.
- **P7 — Observability.** Add a read-only check (doctor / audit) reporting per
  host: the controllers present in the relevant subtrees, the io
  scheduler/iocost state, and the effective `cpu.weight`/`io.weight`. That
  makes the accepted-versus-effective gap visible without a new study. Propose
  as a follow-up lot only.
- **P8 — Freezing.** Unchanged: no arbitrary tool freezing (L460 conclusion,
  PR #435); weights, admission control, memory protection and duration caps
  (PR #423) remain the direction.

# Limits and open questions

- One host, one moment; no benchmark. The audit shows what is in effect, not
  the performance delta a weight would produce.
- pc-smichea and the mesh server were not audited; their runner hierarchies
  (Claude/Codex agents) may differ.
- Does the DeepSeek Harness document a way to choose the slice of its command
  scopes, and does it rely on the current placement for cleanup or isolation?
- `ameesh-guard.service` runs in `system.slice` without a weight; its own
  helper slice and its runtime `set-property` calls on the guest were not
  exercised during this read-only audit.

## Rerunning it on pc-smichea and the mesh server

Read-only: nothing below writes a property, a cgroup or a scheduler, and no
load is generated. Run it on the target host, note the UTC time, and paste the
output here or in a comment. No root is needed: `/sys/fs/cgroup` and
`/proc/<pid>/cgroup` are world-readable. Run it while the host is under load —
PSI and lease jitter say nothing on an idle host.

```bash
# 0 — host, kernel, systemd
hostname; uname -r; systemd --version | head -1
```

```bash
# 1 — live cgroup of each ameesh slice: controllers, subtree, weights
#     (system manager and, on pc-smichea, the user manager)
for mgr in "" "--user"; do
  for u in ameesh.slice ameesh-guest.slice ameesh-agents.slice; do
    cg=$(systemctl $mgr show "$u" -p ControlGroup --value 2>/dev/null); [ -n "$cg" ] || continue
    echo "== systemctl $mgr show $u -> /sys/fs/cgroup$cg"
    cat "/sys/fs/cgroup$cg/cgroup.controllers" "/sys/fs/cgroup$cg/cgroup.subtree_control" \
        "/sys/fs/cgroup$cg/cpu.weight" "/sys/fs/cgroup$cg/io.weight" 2>&1
  done
done
```

```bash
# 2 — runner units and their caps. On pc-smichea the runners are user units:
#     run this block as the user that owns them (its XDG_RUNTIME_DIR is needed);
#     on the mesh server they are system units (second command, no --user).
systemctl --user show 'ameesh-runner-agent@*.service' \
  -p Id -p Slice -p CPUWeight -p IOWeight -p MemoryHigh -p MemoryMax 2>&1
systemctl show 'ameesh-runner@*.service' \
  -p Id -p Slice -p CPUWeight -p IOWeight -p MemoryHigh -p MemoryMax 2>&1
# controllers delegated to the user manager (pc-smichea)
U=$(id -u)
for d in "/sys/fs/cgroup/user.slice/user-$U.slice" \
         "/sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service"; do
  echo "== $d"; cat "$d/cgroup.controllers" "$d/cgroup.subtree_control" 2>&1
done
```

```bash
# 3 — I/O backend: iocost, BFQ, scheduler, backing devices
wc -c /sys/fs/cgroup/io.cost.qos /sys/fs/cgroup/io.cost.model 2>&1
for f in /sys/block/*/queue/scheduler; do printf '%s: ' "$f"; cat "$f"; done
lsmod | grep -w bfq || echo 'bfq: not loaded'
findmnt -rno TARGET,SOURCE,FSTYPE
lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS
```

```bash
# 4 — where the harness's tool commands really run (§4)
systemctl --user list-units --type=scope --all --no-legend 'dsh-subprocess-*.scope' 2>&1
systemctl --user show 'dsh-subprocess-*.scope' \
  -p Id -p Slice -p CPUWeight -p IOWeight -p MemoryMax 2>&1
ps -eo pid,user,cgroup:90,args --sort=pid |
  grep -E 'dsh-subprocess|runner\.js|unittest|pytest|cargo' | cut -c1-240
# Claude runners: are the tool processes inside the unit's cgroup.procs?
for u in $(systemctl --user list-units --all --no-legend --plain \
             'ameesh-runner-agent@*.service' | awk '{print $1}'); do
  cg=$(systemctl --user show "$u" -p ControlGroup --value)
  echo "== $u -> /sys/fs/cgroup$cg"
  for p in $(cat "/sys/fs/cgroup$cg/cgroup.procs" 2>/dev/null); do
    printf '%s ' "$p"; tr '\0' ' ' < "/proc/$p/cmdline" | cut -c1-100; echo
  done
done
```

```bash
# 5 — database containers and other docker scopes
docker ps --format '{{.Names}}' 2>&1
systemctl show 'docker-*.scope' -p Id -p Slice -p CPUWeight -p IOWeight 2>&1
```

```bash
# 6 — pressure, heavy slots and leases, sampled as in §7
cat /proc/pressure/cpu /proc/pressure/io /proc/pressure/memory
ameesh heavy --status 2>&1
for i in $(seq 1 40); do
  date -u +%T
  ameesh list --json | python3 -c '
import json, sys, time
now = time.time()
for a in json.load(sys.stdin):
    if a["lease_owner"]:
        print("  %-18s ttl=%6.1fs last_seen=%5.1fs" % (
            a["name"], a["lease_expires_ts"] - now, now - a["last_seen_ts"]))'
  sleep 5
done
```

The comparison frame is the table in §1: a host whose `io.cost.qos`/`io.cost.model`
are non-empty, whose scheduler shows `[bfq]`, or whose `dsh-subprocess-*.scope`
sits inside the runner cgroup needs its row corrected before any weight policy
is decided for it.
