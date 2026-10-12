---
type: Study
title: "L477 — effective CPU/I/O weights on runner and guest cgroups"
description: "Read-only audit on ameesh-agents-1: the guest CPU weight is in effect next to a hard CPU quota, its I/O weight is inert (no iocost device, BFQ not loaded), the runner units carry no weight, and DeepSeek tool scopes escape the runner's memory cap."
status: draft
tags: [pression, cgroup, cpu, io, audit, hote]
generated: { by: "mty-amh-impl-02", at: "2026-10-12" }
---

# Recommendation

- **Keep the guest's `CPUWeight=10`, and read it as a relative share.** Kernel
  `cpu.weight=10` is readable on `/ameesh.slice` and on
  `/ameesh.slice/ameesh-guest.slice`; the guest also carries a hard
  `CPUQuota=200%` (`cpu.max=200000 100000`, two CPUs). A weight distributes
  relative CPU shares only among eligible sibling workloads that compete for
  the same CPUs: it preempts nothing and caps nothing by itself, applied alone
  it does not slow the guest down. The quota is what hard-caps the guest; the
  weight decides how the guest's contended share is arbitrated against the
  weight-100 `system.slice` and `user.slice` at the root.
- **Stop presenting `IOWeight=10` as effective on this host.** `io.weight` is
  iocost's proportional-control interface, and no iocost device is configured
  (the root-only `io.cost.qos`/`io.cost.model` are empty); BFQ's cgroup-v2
  interface is a different file, `io.bfq.weight`, which does not even exist
  here (`bfq` is not loaded and has no module file installed); the active
  `[none] mq-deadline` has no weight arbitration. systemd 255 writes both files
  for `IOWeight=`; a successful property write is not a kernel effect (the
  limit already stated in [L460](l460-gel-cgroup.md)). Keep admission control,
  duration caps and memory protection as the I/O-pressure levers until a
  backend is enabled and calibrated.
- **Do not extend weights to the runner units as the first move.** The DeepSeek
  Harness runs each tool command in a `dsh-subprocess-*.scope` under
  `app.slice`, outside `ameesh-agents.slice` and outside the runner unit
  cgroup; weighting the runner units would not cover the tests and builds they
  are meant to shape. Claude Code's tool processes, by contrast, stay inside
  the runner cgroup. This scope escape is lot #490.
- **Moving those scopes under `ameesh-agents.slice` is not enough for the
  memory cap.** It restores the slice's aggregate ceiling (26.6 GiB observed),
  not the runner's `MemoryMax=6G`: a single agent's tool scope can still exceed
  6 GiB. Per-agent coverage needs a capped per-agent ancestor or delegated
  children inside the runner (§4, P4).
- **Any host change (enabling iocost, loading BFQ, moving the scopes, setting
  unit weights) needs the owner's separate authorization**, preceded by the
  controlled measurement proposed below. This audit changed nothing and
  generated no load: the only recurring read was one registry `SELECT` every
  ~5 s for ~3.6 min (§7).

# Scope and method

Read-only audit of **ameesh-agents-1** (the guest host). First pass (r1) on
2026-10-12 between 18:50 and 19:05 UTC; this r2 re-reads the live state between
20:42 and 20:53 UTC on the same kernel and systemd. Kernel
`6.8.0-142-generic`, systemd 255 (`255.4-1ubuntu8.17`), 8 CPUs, 31 GiB RAM.
Repository baseline of the branch: `origin/main` at `9ac74f0b` (r1); the code
line numbers quoted below are those of the L477 branch (base `ac61544e`, PR
head `2d93d4ff`).

Commands used: `systemctl show` and `systemctl --user show` (unit properties),
reads of `/sys/fs/cgroup/**` (`cgroup.controllers`, `cgroup.subtree_control`,
`cpu.weight`, `cpu.max`, `io.weight`, root `io.cost.qos`/`io.cost.model`),
`/sys/block/*/queue/scheduler`, `lsblk`, `docker ps`, `/proc/<pid>/cgroup`,
`/proc/pressure/*`, `lsmod`, `dpkg -L`, `strings`/byte reads of the installed
`libsystemd-core-255.so`, `ameesh list --json` (SELECTs on
`agent_mesh_overview` only), read-only `cat` of the `.heavy` slot files, and
`journalctl`. **`ameesh heavy --status` is deliberately not used**: it is not
passive (§6). No unit, cgroup, scheduler, mount or configuration was modified,
and no load was generated on purpose.

Not audited: **pc-smichea** and the mesh server. Neither is reachable from this
host (no `~/.ssh`, `pc-smichea` does not resolve), so the findings below cover
the guest host only. The exact read-only commands to fill that gap are at the
end of this study ("Rerunning it on pc-smichea and the mesh server").

# 1. The hierarchy actually in place

Two read-only facts decide how to read the table. `cgroup.controllers` lists
the controllers *available* to a cgroup (delegated by its parent); it does not
say they are enabled. `cgroup.subtree_control` lists the controllers the cgroup
has enabled *for its children*. A cgroup exposes `cpu.weight`/`io.weight` only
when its parent enabled the controller for it; the weight files are non-root
files (kernel cgroup-v2 documentation), so the root cgroup has none. `—` in the
table means the file does not exist (controller not enabled at that level, or
nothing enabled for children).

Observed on ameesh-agents-1, r2 reads of 2026-10-12 20:42–20:43 UTC:

| cgroup (manager) | `cgroup.controllers` | `cgroup.subtree_control` | `cpu.weight` | `io.weight` |
|---|---|---|---|---|
| `/ameesh.slice` (system) | cpuset cpu io memory hugetlb pids rdma misc | cpu io memory pids | **10** | **default 10** |
| `/ameesh.slice/ameesh-guest.slice` | cpu io memory pids | memory pids | **10** | **default 10** |
| `…/ameesh-guest-runner@deepseek1.service` | memory pids | — | — | — |
| `/system.slice` | cpuset cpu io memory hugetlb pids rdma misc | cpuset cpu io memory hugetlb pids rdma misc | 100 | default 100 |
| `/system.slice/docker-<id>.scope` (8 pgtests) | cpuset cpu io memory hugetlb pids rdma misc | — | 100 | default 100 |
| `/user.slice` | cpuset cpu io memory hugetlb pids rdma misc | cpu memory pids | 100 | default 100 |
| `/user.slice/user-1001.slice` | cpu memory pids | cpu memory pids | **100** | — (io absent) |
| `…/user@1001.service` | cpu memory pids | cpu memory pids | **100** | — (io absent) |
| `…/app.slice` | cpu memory pids | memory pids | 100 | — (io absent) |
| `…/app.slice/dsh-subprocess-*.scope` | memory pids | — | — | — |
| `…/ameesh.slice` (user) | cpu memory pids | memory pids | 100 | — (io absent) |
| `…/ameesh-agents.slice` | memory pids | memory pids | — | — |
| `…/ameesh-runner-agent@<agent>.service` | memory pids | — | — | — |

Corrections and reading notes:

- r1's table marked `cpu.weight` as absent on `/user.slice/user-1001.slice`
  and `user@1001.service`. That was wrong: both advertise `cpu` in
  `cgroup.controllers` (so their parent enabled it for them) and both expose
  `cpu.weight=100`. The table above is rebuilt from fresh reads with
  `cgroup.controllers` and `cgroup.subtree_control` kept apart.
- `systemctl show -p CPUWeight` reports `[not set]` when the unit did not set
  the property, even though the kernel default is 100 (`user-1001.slice`, user
  `ameesh.slice`). The table shows kernel values, not property values. Where a
  value is policy, it is visible in the unit: `/usr/lib/systemd/user/app.slice`
  sets `CPUWeight=100`; `deploy/guest/ameesh.slice.d/guest-weight.conf:16-17`
  and `deploy/guest/ameesh-guest.slice:24-25` set 10.
- Two different `ameesh.slice` units coexist: the **system** one
  (`/ameesh.slice`, the guest, weight 10) and the **user** one (under
  `user@1001.service`, weight 100, the local DeepSeek/Claude runners). They
  compete at different levels of the root.
- Two ancestors also carry a hard CPU quota: `/user.slice/user-1001.slice` has
  `cpu.max=700000 100000` (`CPUQuota=700%`, 7 of the 8 CPUs) and
  `/ameesh.slice/ameesh-guest.slice` has `cpu.max=200000 100000`
  (`CPUQuota=200%`).

# 2. Guest weights: CPU effective as a share, I/O inert

The guest configuration is the one described in L460:
`deploy/guest/ameesh-guest.slice:24-25` sets `CPUWeight=10`/`IOWeight=10`, and
the parent drop-in `deploy/guest/ameesh.slice.d/guest-weight.conf:16-17` sets
the same on `ameesh.slice`, so the guest competes at the application's level
rather than only between its own children. The offload installer does the same
on the dedicated user's slice (`deploy/guest/install.sh:330-331`). The live
host confirms the full chain:

```
$ cat /sys/fs/cgroup/ameesh.slice/cpu.weight \
      /sys/fs/cgroup/ameesh.slice/cgroup.subtree_control \
      /sys/fs/cgroup/ameesh.slice/ameesh-guest.slice/cpu.weight \
      /sys/fs/cgroup/ameesh.slice/ameesh-guest.slice/cgroup.subtree_control \
      /sys/fs/cgroup/ameesh.slice/ameesh-guest.slice/cpu.max
10
cpu io memory pids
10
memory pids
200000 100000
$ systemctl show ameesh-guest.slice -p CPUWeight -p IOWeight -p CPUQuotaPerSecUSec
CPUWeight=10
IOWeight=10
CPUQuotaPerSecUSec=2s
```

So `cpu` is enabled into the guest subtree, `cpu.weight=10` is in effect at
both the guest and parent levels, and the guest runner units
(`ameesh-guest-runner@deepseek1/6.service`, `MemoryMax=4.5G`) share that slice
weight and quota without a weight of their own.

What that does and does not mean: a CPU weight is a *relative share*, and only
among eligible sibling workloads competing for the same CPUs. With no sibling
runnable, a weight-10 cgroup is not slowed down; the weight neither preempts
nor caps. What actually bounds this guest is `cpu.max=200000 100000`
(`CPUQuota=200%`, two CPUs). The claim "the whole guest yields" is therefore
qualified: guest tasks are accounted under `/ameesh.slice` for CPU (its
`cgroup.subtree_control` enables `cpu` for its children), and that slice
competes at the root against `system.slice` and `user.slice`, both weight 100.
Within the guest, `ameesh-guest.slice` carries the same weight 10 and no
`CPUQuota` of its own, so its runners share the parent's cap and weight.

The I/O half has no effect. On this host (r2 reads are identical to r1):

```
$ cat /sys/fs/cgroup/io.cost.qos /sys/fs/cgroup/io.cost.model   # root-only files
(empty)
$ cat /sys/block/vda/queue/scheduler /sys/block/sda/queue/scheduler
[none] mq-deadline
[none] mq-deadline
$ lsmod | grep -w bfq || echo 'bfq: not loaded'
bfq: not loaded
$ ls /sys/module/bfq
ls: cannot access '/sys/module/bfq': No such file or directory
```

`io.weight` is iocost's proportional-control interface: the kernel
cgroup-v2 documentation states that the `io.cost.qos`/`io.cost.model` files
(which exist **only on the root cgroup**) configure "the IO cost model based
controller (`CONFIG_BLK_CGROUP_IOCOST`) which currently implements `io.weight`
proportional control"
([Linux v6.8, cgroup-v2.rst, io.cost.qos](https://github.com/torvalds/linux/blob/v6.8/Documentation/admin-guide/cgroup-v2.rst)).
iocost is compiled in here (`CONFIG_BLK_CGROUP_IOCOST=y`), but both root files
are empty: no device is configured, so nothing reads the per-cgroup weights.
The observed `io.weight=default 10` is systemd's accepted write — the v255
[`set_io_weight()`](https://github.com/systemd/systemd/blob/v255/src/core/cgroup.c#L1620-L1630)
writes the literal `default <weight>` — not an arbitration result.

BFQ is a different interface: per the kernel BFQ documentation, "the group
parameter to set the weight of a group with BFQ is `blkio.bfq.weight` or
`io.bfq.weight`" in cgroup v2, with a 1..1000 range
([Linux v6.8, bfq-iosched.rst](https://github.com/torvalds/linux/blob/v6.8/Documentation/block/bfq-iosched.rst)).
That file does not exist here: `CONFIG_IOSCHED_BFQ=m` and
`CONFIG_BFQ_GROUP_IOSCHED=y`, but `bfq` is not loaded and **no `bfq.ko` file is
installed**: `/lib/modules/6.8.0-142-generic/kernel` has no `block/` directory,
`dpkg -L linux-modules-6.8.0-142-generic` contains no bfq file, and no
`linux-modules-extra-*` package is installed. Enabling BFQ would first require
a module package installation on the host (P5).

systemd 255 writes both files for `IOWeight=`: its `set_io_weight()` calls
`set_bfq_weight(…, "io", …)` (writing `io.bfq.weight`, scaled to 1..1000,
[source](https://github.com/systemd/systemd/blob/v255/src/core/cgroup.c#L1321-L1346))
and then writes `io.weight` as `default N`; both calls ignore failures. The
source matches the installed binary (`set_bfq_weight`, `Failed to set
'io[.bfq].weight' attribute …` in `libsystemd-core-255.so`). On this host the
`io.bfq.weight` write cannot land and the `io.weight` write lands in a
controller with no device. Both are **accepted but inert**: the active
`[none] mq-deadline` has no cgroup weight arbitration. Any future claim that
the guest "yields on I/O" must first show a calibrating measurement, not a
property read-back.

# 3. Runner units: no weight, and no CPU controller in their subtree

On the user side, no runner unit carries a weight, and neither does
`ameesh-agents.slice` (which only sets `MemoryHigh=80%`/`MemoryMax=85%`,
`deploy/systemd/ameesh-agents.slice:19-20`; the unit itself sets
`MemoryHigh=5.4G`/`MemoryMax=6G`,
`deploy/systemd/ameesh-runner-agent@.service:67-69`). r2 reads:

```
$ systemctl --user show ameesh-runner-agent@mty-amh-impl-02 -p CPUWeight -p IOWeight -p Delegate -p KillMode
CPUWeight=[not set]
IOWeight=[not set]
Delegate=no
KillMode=mixed
$ cat /sys/fs/cgroup/user.slice/user-1001.slice/cgroup.controllers \
      /sys/fs/cgroup/user.slice/user-1001.slice/cpu.weight \
      /sys/fs/cgroup/user.slice/user-1001.slice/cpu.max
cpu memory pids
100
700000 100000
$ cat …/user@1001.service/ameesh.slice/ameesh-agents.slice/cgroup.controllers
memory pids
```

`cpu` is not propagated into `ameesh-agents.slice`, so the runner units have no
`cpu.weight` file at all: for CPU they are flat members of the user
`ameesh.slice` (weight 100), exactly like every other slice at that level. A
per-runner `CPUWeight` would first have to make systemd enable the `cpu`
controller along that chain.

The `io` controller is not even **available** under `user@1001.service`:
`user.slice`'s `cgroup.subtree_control` is `cpu memory pids`, so no user unit
can carry an effective `IOWeight` as installed today. (systemd may enable
controllers on demand when a unit requests one, but nothing does; and the
host-level I/O backend is missing anyway, see §2.)

Also note that the whole user hierarchy is already hard-capped:
`user-1001.slice` has `CPUQuota=700%` (`cpu.max=700000 100000`). Weights
inside it could only redistribute work *within* that 7-CPU quota.

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
which has no `MemoryMax` either. Several such scopes were live during the audit
(eight at r1: four `mty-amh-impl-*`, `deepseek3/4/5`, one long test from
`mty-amh-impl-04`; four at the r2 read of 20:43Z). This is a DeepSeek Harness
behaviour, not ameesh runner code: `runner.py` does not create scopes for
commands.

The harness code confirms the placement is a default, not a choice it exposes:
in `@deepseek-ai/dsh` 0.2.0-rc.2,
`node_modules/@deepseek-ai/dsh-subprocess-local/lib/index.js:383` builds
`systemd-run --user --scope --quiet --collect --expand-environment=no --unit=dsh-subprocess-… -- …`
with **no `--slice=`**, and the observed scope carries `Slice=app.slice`. There
is no slice option in that code path.

Consequences today:

- the runner unit's `MemoryMax=6G` does **not** bound the agent's tests and
  builds, only the harness process itself;
- a future `CPUWeight`/`IOWeight` on the runner unit would not cover that work;
- the heavy work of all DeepSeek agents competes flat inside `app.slice` (which
  is a plain 100-weight child of `user@1001.service`).

**What fixing the placement can and cannot restore.** A `systemd-run` scope's
`Slice=` names a slice, so manager-created scopes cannot simply be nested under
a service: putting them under `ameesh-agents.slice` would restore the slice's
aggregate ceiling only (`MemoryMax=85%`, 26.6 GiB observed), not the runner's
6 GiB — a sibling tool scope could still exceed 6 GiB. Two routes give
per-agent coverage, both harness-side:

1. **A capped per-agent ancestor.** A slice per agent (for example
   `ameesh-agent-<name>.slice` with `MemoryMax=6G`), containing the runner
   service and its scopes. This needs the runner unit's `Slice=` to move and
   the harness to pass the matching `--slice=` (or an equivalent option).
2. **Delegated children.** `Delegate=yes` on the runner unit, with the harness
   creating its scope cgroups itself inside the unit's cgroup; the unit's
   `MemoryMax=6G` and any unit weight then apply to them. This is not another
   `systemd-run` call: it is harness-side cgroup creation, and it changes the
   unit (`Delegate=`, today `no`) as well as the harness.

Claude Code behaves differently: for `ameesh-dev3` and `ameesh-dev5` the tool
processes (`bash`, `timeout`, `python -m unittest`, `ugrep`, `psql`) were found
inside `ameesh-runner-agent@<agent>.service/cgroup.procs` (r1). Their work
**is** covered by the unit's memory cap and by any weight set on the unit.

# 5. Docker and the database are outside both hierarchies

The eight disposable PostgreSQL containers run as `docker-*.scope` under
`/system.slice`, outside `ameesh.slice` and outside `user@1001.service`:

```
$ docker ps --format '{{.Names}}' | wc -l
8
$ cat /sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.weight \
      /sys/fs/cgroup/system.slice/docker-<id>.scope/io.weight \
      /sys/fs/cgroup/system.slice/docker-<id>.scope/memory.max
100
default 100
max
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

**`ameesh heavy --status` is not passive.** `status()` goes through
`_slot_paths()`, which calls `os.makedirs(<state_dir>/.heavy)` (`heavy.py:76`),
and through `_try()`, which opens each slot file with `open(path, "a+")` and
takes `flock(LOCK_EX|LOCK_NB)` (`heavy.py:82-88`, called at `heavy.py:102`). On
a fresh host it creates the directory and the slot files; sampled while a suite
starts, it can make a free slot look busy for the instant it holds the lock. An
audit must read the slot files instead:

```
st="${AMEESH_STATE_DIR:-$HOME/.local/state/ameesh}/.heavy"
ls -la "$st" 2>&1
for f in "$st"/slot-*; do [ -e "$f" ] || continue; printf '%s: ' "$f"; cat "$f"; echo; done
```

Read-only output at 20:42Z (two busy slots, holder and command):

```
slot-0: {"pid": 4057157, …, "agent": "mty-amh-impl-04", "command": "bash .run-tests-147-464.sh", …}
slot-1: {"pid": 4178413, …, "agent": "deepseek3", "command": "bash /tmp/ds3-394-concurrency.sh", …}
```

# 7. Contention and lease latency observed

The host was under real I/O pressure during the r1 audit; PSI read at 18:55 UTC:

```
cpu   some avg10=8.51  avg60=10.51 avg300=12.50
io    some avg10=48.39 avg60=31.91 avg300=21.32   full avg10=33.07
memory some avg10=0.00
```

The r2 window was lighter: `io some avg10` between 7.10 and 11.66
(`full avg10` 6.72–8.36) at 20:42:38 and 20:47:28 UTC. I/O is the contended
resource on both readings, which is exactly where `io.weight` is inert.

**Lease mechanism (read from `src/ameesh/runner.py`, `storage/postgres/registry.py`
and `config.py`, L477 branch base `ac61544e`).** The default `lease_ttl` is
300 s (`config.py:171`) and the heartbeat interval is
`max(3, lease_ttl/3)` = **100 s** during a turn (`runner.py:940`). A late but
eventually successful renewal therefore has up to ~200 s of room (TTL minus
cadence). That ~200 s is **not** a general grace period: the runner stops the
turn's group immediately on four other paths:

1. `deadline_passed()` before the call (`runner.py:590-595`);
2. the renewal call unanswered past `call_budget()` — the remaining lease, with
   no floor (`runner.py:540-551`, `596-601`);
3. a database error: `Unavailable` with a known deadline waits for the deadline
   (`runner.py:604-614`), any other error stops after
   `MAX_RENEW_FAILURES=3` consecutive failures (`runner.py:315`, `602-634`);
4. `registry.renew` returns `None` (`runner.py:635-638`), which happens on an
   owner/epoch mismatch **or when the row's deadline has already passed when
   the renewal write lands** (`registry.py:530-552`): the turn is stopped at
   once ("lease lost (epoch N)").

A read-only sample of `agent_registry` took 40 points every ~5.1 s
(2026-10-12 20:44:54Z → 20:48:30Z, ~216 s) for the 13 agents of this host
holding a lease. The exact command is block 6 of the rerun section below
(`ameesh list --json` issues SELECTs only); first and last points:

```
=== 2026-10-12T20:44:54Z sample 1
  deepseek5          ttl= 289.4 last_seen_age=  10.6
  mty-amh-impl-02    ttl= 280.0 last_seen_age=  20.0
  deepseek4          ttl= 240.9 last_seen_age=  59.1
  …
=== 2026-10-12T20:48:30Z sample 40
  mty-amh-impl-01    ttl= 211.0 last_seen_age=  89.0
  …
```

Summary of the 40 points:

- six agents stayed pinned at 295–300 s for the whole window (idle agents,
  renewed on each poll);
- seven agents ran a turn during the window and showed the sawtooth
  300 → 200 s; the **minimum remaining TTL observed was 200.3 s** (deepseek5;
  202.0 s for deepseek4), i.e. the renewal landed at the nominal 200 s floor
  within one ~5 s sample, while the slowest of the seven bottomed at 213.3 s;
  measured cadence **100.0 s** on the three agents that completed two renewal
  cycles inside the window, against 100 s nominal;
- `last_seen` was up to 99.7 s old for busy agents (updated on heartbeat) and
  ≤5 s for idle ones;
- no remaining TTL below zero: no lease expired during the sample. (The
  timestamps are server epochs compared to the local clock; a clock skew would
  shift all absolute values equally and leaves the cadence unchanged.)

Journal check over the last 6 hours (the sample window and before), with the
exact filter for the runner's own stop lines (format
`[<agent>] harness stopped (<reason>)`, `runner.py:818`):

```
$ journalctl --user -u 'ameesh-runner-agent@*' --since '6 hours ago' --no-pager \
  | grep -cE 'harness stopped \((lease deadline reached|renewal unanswered past the lease|database unreachable|lease lost)'
0
$ journalctl --user -u 'ameesh-runner-agent@*' --since '6 hours ago' --no-pager \
  | grep -cE '\] heartbeat:'
0
```

The journal does contain agent turn output quoting text (mail, commit messages);
the filter above targets the runner's own stop/heartbeat lines, so those quoted
texts are not counted.

So, under this load and with this database, a lease is lost only through the
paths above; the observed lateness is at most about one sample (≈5 s), and the
≈200 s margin is conditional on renewals eventually succeeding. The heartbeat's
database round-trip is the thing to watch if weights are ever enabled, because
it shares the DB/I/O path.

# What would have to be true before weighting anything

1. **Placement** — the work to be weighted must be inside the cgroup carrying
   the weight. For DeepSeek agents that means a capped per-agent ancestor or
   delegated children inside the runner unit (§4), not merely a shared slice.
2. **Controller** — `cpu` enabled along `ameesh-agents.slice` (or the chosen
   parent); `io` enabled under `user@1001.service` if user units are to carry
   `IOWeight`. This is not a neutral read: enabling a controller rewrites the
   `cgroup.subtree_control` of ancestors and can move which cgroup owns the
   CPU scheduling group of existing processes. Enabling `cpu` under
   `ameesh-agents.slice`, for instance, makes all runner units one sibling
   group at that level instead of flat members of the user `ameesh.slice`;
   that changes the effective hierarchy for live workloads and must be
   measured before/after. cgroup v2 also forbids a cgroup with member
   processes from enabling controllers for its children (systemd moves its own
   processes when it needs to); a write can therefore fail with `EBUSY`.
3. **Privileges** — system-manager changes (`/ameesh.slice`,
   `/ameesh-guest.slice`, guest runner units, system `io.cost.*`, scheduler
   selection) require root on the host; user-manager changes
   (`ameesh-agents.slice`, the runner units) require the owning user (uid 1001)
   and its user manager, and a `Slice=`/`Delegate=` change is a unit change,
   not a runtime tweak. This audit runs as a tenant and authorizes nothing.
   Check the RT case too: with `CONFIG_RT_GROUP_SCHED` (not set here:
   `# CONFIG_RT_GROUP_SCHED is not set`), non-root realtime tasks in a subtree
   can prevent CPU-controller activation.
4. **Backend** — iocost or BFQ enabled and calibrated on the relevant devices
   (`vda`, `sda`); verified by a differential read/write measurement, never by
   a property read-back or a non-empty file.
5. **Measurement** — lease-renewal jitter, database round-trip, mail wakeups
   and test/build wall time, before/after, on a canary agent.
6. **Authorization** — owner decision; this audit authorizes nothing.

# Policy proposal

- **P1 — Guest CPU.** Keep `CPUWeight=10` on `ameesh.slice` and
  `ameesh-guest.slice`. It is effective as a relative share, and it sits next
  to the `CPUQuota=200%` that hard-caps the guest. No change.
- **P2 — I/O claims.** Treat `IOWeight=10` as *accepted but inert* on this
  host: no iocost device configured, no BFQ module loaded, `[none] mq-deadline`
  active. Either enable a backend and calibrate it (P5), or state in the
  capacity story that I/O prioritization currently relies on admission control
  and duration caps only. Do not use the guest `IOWeight`, nor a non-empty
  `io.cost.*` file, as evidence of I/O protection in a host report.
- **P3 — Runner weights.** Do not set `CPUWeight`/`IOWeight` on the runner
  units before P4 (placement) and step 1 of "what would have to be true". If a
  cheap first step is wanted, the CPU side already yields through the guest
  slice; nothing needs to be added to the local runners to protect the host
  from the guest.
- **P4 — Scope placement (harness).** Ask the DeepSeek Harness to place the
  command scope in a per-agent capped ancestor, or to create it as a delegated
  child of the caller's runner unit. The current placement under `app.slice`
  escapes both the runner's `MemoryMax=6G` and any future unit weight. Be
  explicit about coverage: moving the scopes under `ameesh-agents.slice` alone
  restores only the slice's aggregate cap (26.6 GiB observed), not the 6 GiB
  per agent; a per-agent slice or `Delegate=yes` children are needed for that.
  This is a harness change (plus, for the delegated route, a unit change
  requiring authorization), and it should be measured: does dsh's isolation or
  cleanup depend on the current scope? Lot #490 tracks the escape.
- **P5 — I/O backend.** If I/O prioritization is wanted, first establish
  feasibility and impact, then enable iocost (or BFQ) on the device, calibrate
  it, and start with the guest only (weight already 10). Known constraints on
  this host: iocost is compiled in but unconfigured; BFQ has **no module file
  installed** (`CONFIG_IOSCHED_BFQ=m`, no `bfq.ko` under
  `/lib/modules/6.8.0-142-generic`, no `linux-modules-extra-*` package), so
  enabling it means installing a kernel-modules package as root. Both backends
  work at the **whole block device**: calibrating `vda1` means configuring
  `vda` (which carries `/`, `/boot` and every local workload, including the
  PostgreSQL containers) for every workload on that device; `sda` carries
  `/srv/ameesh-guest`. iocost/BFQ can reduce virtio throughput and cannot
  arbitrate the physical I/O of competing guests. This is a privileged,
  host-wide change: require explicit host authorization, a before/after
  database-latency and throughput measurement, and a written rollback plan
  (restore `mq-deadline`, clear `io.cost.*`) before touching a device. Never
  include the PostgreSQL containers in a lowering.
- **P6 — Measurement protocol.** On a canary agent: record baseline lease
  jitter, DB round-trip and heavy-test wall time; then run a CPU hog and an I/O
  hog with and without the weights; decide on the delta. This generates load
  and therefore needs explicit authorization.
- **P7 — Observability.** Add a read-only check (doctor / audit) reporting per
  host: the controllers available *and* enabled in the relevant subtrees, the
  io scheduler/iocost state and enablement (not just file sizes), and the
  effective `cpu.weight`/`io.weight` with the unit properties next to them.
  That makes the accepted-versus-effective gap visible without a new study.
  Propose as a follow-up lot only.
- **P8 — Freezing.** Unchanged: no arbitrary tool freezing (L460 conclusion,
  PR #435); weights, admission control, memory protection and duration caps
  (PR #423) remain the direction.

# Limits and open questions

- One host, one moment (plus a lighter r2 window); no benchmark. The audit
  shows what is in effect, not the performance delta a weight would produce.
- pc-smichea and the mesh server were not audited; their runner hierarchies
  (Claude/Codex agents) may differ. The r1 table error on the user ancestors
  is a reminder to re-read rather than copy a hierarchy on a new host.
- Does the DeepSeek Harness accept an upstream change to choose the slice of
  its command scopes, and does it rely on the current placement for cleanup or
  isolation? The installed 0.2.0-rc.2 exposes no slice option on that path
  (§4).
- `ameesh-guard.service` runs in `system.slice` without a weight; its own
  helper slice and its runtime `set-property` calls on the guest were not
  exercised during this read-only audit.
- The lease sample measures remaining TTL against the local clock; a skew
  between the DB host and ameesh-agents-1 shifts the absolute values (not the
  cadence). The observed floor is consistent with the 300 s TTL / 100 s
  cadence, so any skew is small.

## Rerunning it on pc-smichea and the mesh server

Read-only: nothing below writes a property, a cgroup or a scheduler, and no
load is generated. Run it on the target host, note the UTC time, and paste the
output here or in a comment. Run it while the host is under load — PSI and
lease jitter say nothing on an idle host. Except for the lease sampler (one
`SELECT` every 5 s), no command creates or locks anything.

```bash
# 0 — host, kernel, systemd, CPU/memory, RT-group-sched support
hostname; uname -r; systemd --version | head -1; nproc; free -g | head -2
(zcat /proc/config.gz 2>/dev/null || cat /boot/config-$(uname -r) 2>/dev/null) \
  | grep -E 'CONFIG_(RT_GROUP_SCHED|BLK_CGROUP_IOCOST|IOSCHED_BFQ|BFQ_GROUP_IOSCHED|MQ_IOSCHED)'
```

```bash
# 1 — live cgroup of each ameesh slice: available vs enabled controllers,
#     weights, quotas. (system manager and, on pc-smichea, the user manager)
for mgr in "" "--user"; do
  for u in ameesh.slice ameesh-guest.slice ameesh-agents.slice; do
    cg=$(systemctl $mgr show "$u" -p ControlGroup --value 2>/dev/null); [ -n "$cg" ] || continue
    echo "== systemctl $mgr show $u -> /sys/fs/cgroup$cg"
    for f in cgroup.controllers cgroup.subtree_control cpu.weight cpu.max io.weight io.bfq.weight; do
      printf '   %-24s: ' "$f"
      [ -e "/sys/fs/cgroup$cg/$f" ] && cat "/sys/fs/cgroup$cg/$f" | tr '\n' ' ' || printf '<absent>'
      echo
    done
    systemctl $mgr show "$u" -p Id -p CPUWeight -p IOWeight -p CPUQuotaPerSecUSec -p MemoryHigh -p MemoryMax
  done
done
```

```bash
# 2 — runner units and their caps. On pc-smichea the runners are user units:
#     run this block as the user that owns them (its XDG_RUNTIME_DIR is needed);
#     on the mesh server they are system units (second command, no --user).
systemctl --user show 'ameesh-runner-agent@*.service' \
  -p Id -p Slice -p Delegate -p CPUWeight -p IOWeight -p MemoryHigh -p MemoryMax 2>&1
systemctl show 'ameesh-runner@*.service' \
  -p Id -p Slice -p Delegate -p CPUWeight -p IOWeight -p MemoryHigh -p MemoryMax 2>&1
# controllers delegated to the user manager (pc-smichea)
U=$(id -u)
for d in "/sys/fs/cgroup/user.slice/user-$U.slice" \
         "/sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service"; do
  echo "== $d"; echo "  controllers: $(cat "$d/cgroup.controllers" 2>/dev/null)"; \
  echo "  subtree:     $(cat "$d/cgroup.subtree_control" 2>/dev/null)"; \
  echo "  cpu.weight:  $(cat "$d/cpu.weight" 2>/dev/null || echo '<absent>')"; \
  echo "  cpu.max:     $(cat "$d/cpu.max" 2>/dev/null || echo '<absent>')"
done
```

```bash
# 3 — I/O backend: iocost configuration and enablement, BFQ, scheduler, devices.
#     A non-empty io.cost.* file is NOT proof of arbitration: read dev= and
#     enable=0/1. Use `cat`, never `wc -c`.
cat /sys/fs/cgroup/io.cost.qos /sys/fs/cgroup/io.cost.model 2>&1
ls /sys/fs/cgroup/io.bfq.weight 2>&1
for f in /sys/block/*/queue/scheduler; do printf '%s: ' "$f"; cat "$f"; done
lsmod | grep -w bfq || echo 'bfq: not loaded'
ls -d /sys/module/bfq 2>&1
ls /lib/modules/$(uname -r)/kernel/block/ 2>&1 | head
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
# 6 — pressure, heavy slots and leases, sampled as in §7.
#     `ameesh heavy --status` is NOT read-only: it recreates the slot files and
#     briefly locks them (heavy.py:76,82-88,102). Read them instead:
cat /proc/pressure/cpu /proc/pressure/io /proc/pressure/memory
st="${AMEESH_STATE_DIR:-$HOME/.local/state/ameesh}/.heavy"
ls -la "$st" 2>&1
for f in "$st"/slot-*; do [ -e "$f" ] || continue; printf '%s: ' "$f"; cat "$f"; echo; done
for i in $(seq 1 40); do
  date -u +%T
  ameesh list --json | python3 -c '
import json, sys, time
now = time.time()
for a in json.load(sys.stdin):
    if a["lease_owner"]:
        print("  %-18s ttl=%6.1fs last_seen_age=%5.1fs" % (
            a["name"], a["lease_expires_ts"] - now, now - a["last_seen_ts"]))'
  sleep 5
done
```

```bash
# 7 — runner journal: any stop caused by the lease? (§7 filter)
journalctl --user -u 'ameesh-runner-agent@*' --since '6 hours ago' --no-pager \
  | grep -E 'harness stopped \((lease deadline reached|renewal unanswered past the lease|database unreachable|lease lost)|\] heartbeat:'
```

The comparison frame is the table in §1: a host whose root `io.cost.qos` /
`io.cost.model` **configure a device with `enable=1`**, whose scheduler shows
`[bfq]` **and whose `io.bfq.weight` exists**, or whose
`dsh-subprocess-*.scope` sits inside the runner cgroup needs its row corrected
before any weight policy is decided for it.
