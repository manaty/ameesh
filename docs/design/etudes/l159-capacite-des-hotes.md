---
type: Study
title: "Study — host capacity lent to the mesh, common contract with Nexlink Compute"
description: "Lend the unused capacity of the organisation's VMs to ameesh's agents without disturbing their applications. A \"capacity guard\" contract common to ameesh and Nexlink Compute: offer, common measure, ameesh-host-state/1 extended without a new version, tiers for giving way, caps set by the kernel. Two modes on a VM (full runner under a capped slice, offloading of heavy suites); security; rollout on the test VM; estimated lots (L159a–L159i, N1–N3)."
status: draft
tags: [capacite, hotes, garde, porte-d-hote, cgroups, systemd, compute, deport, placement]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-11T04:09:00+02:00" }
sources:
  - { resource: "decisions/0028-ressources-des-hotes-et-repartition.md", title: "Decision 0028 (host resources, back-pressure, relocation)" }
  - { resource: "decisions/0031-plusieurs-canons.md", title: "Decision 0031 (physical limits: the strictest card)" }
  - { resource: "../../../src/ameesh/resources.py", title: "Host measurement and pressure (L31, L73, L106)" }
  - { resource: "../../../src/ameesh/hostcheck.py", title: "Host ready to run its agents (L106)" }
  - { resource: "../../../src/ameesh/placement.py", title: "Governed placement (C4, R18, L31)" }
  - { resource: "../../../src/ameesh/canon.py", title: "Host, HostPolicy, Placement cards" }
  - { resource: "../../../src/ameesh/relocation.py", title: "Relocation between admitted hosts (L31)" }
  - { resource: "branche lot/v170-voie-b : docs/IMAGE-EXECUTEUR.md, docs/EXECUTEUR-MEDIEE.md, src/ameesh/mediated_executor/", title: "Mediated runner, host gate, enrolment, image (L107–L115, not merged)" }
  - { resource: "nexlink:desktop/src/compute/ (develop)", title: "Idle guard (#93), offer, PROV-2, PROV-3, D13, native mode (#115–#117)" }
  - { resource: "nexlink:docs/specs/compute-agent-turn.md (branche 94b)", title: "Nexlink agent turn contract, v0.2 (#94, not merged)" }
---

# Context

## The need

The owner, on 10 and 11 October: use the unused capacity of the
organisation's VMs for ameesh's agents, **without disturbing the applications**
they serve. And: "we overlap with Nexlink Compute, we will have to make sure
that what we do in ameesh is reusable by Nexlink Compute".

Nexlink Compute lends the idle time of personal devices: builds today, ameesh
agent turns currently being integrated. An application VM that lends its
quiet periods poses the same problem, on the server side.

## The observation

The owner's workstation is the only host of eleven agents.

* **2026-10-10**: the load reached 43 for 12 CPUs, against a threshold of 24
  set by 0028. No turn started any more, orchestrator included. The delayed
  turns added up to about 80 minutes.
* **2026-10-11**: the workstation's `/tmp`, a 12 GB tmpfs, crossed 80 % at
  02:58 and filled up around 03:38. All turns were blocked.
* **2026-10-10, mesh VM** (4 vCPU, 8 GB, no swap): the `node` process of a
  harness reached 4.1 GB of resident memory, and the kernel OOM killed it.
  The runner's unit had no cap at all.

The heavy test suites (Postgres in a container, jest, e2e) account for most
of the CPU load and of `/tmp`. The harnesses account for most of the memory:
2 to 4 GB per active agent.

## The available capacity

Read-only inventory of 2026-10-11. Only aggregates are kept here. Method:

* memory margin: 50 % of the lowest `MemAvailable` over 7 days (sysstat);
* CPU margin: 50 % of the idle share, either over the busiest 10-min slot
  ("at peaks"), or on average ("off peaks").

Results:

* 23 running VMs: 73 vCPU, 182 GB of memory.
* **About 31 GB reusable, on two VMs only**:
  - a **test VM**: 8 dedicated vCPU, 32 GB. It lends ≈ 14 GB, ≈ 1 vCPU
    at peaks and ≈ 3 vCPU off peaks. It serves our test suites, nothing
    else, but its 10 GB root disk has only 1.9 GB free;
  - a **CI VM**: 16 vCPU, 64 GB. It lends ≈ 18 GB, ≈ 2 vCPU at peaks
    and ≈ 7 vCPU off peaks. Its jobs are concentrated in four hours of the
    afternoon. Its `runner` user is a member of the `docker` group.
* In total: ≈ 3 vCPU guaranteed at peaks, ≈ 10 off peaks, up to 12 at night.
  At 4 GB per agent, that makes **about 7 agents**.
* The twelve small accessible VMs together offer ≈ 6.5 GB, **scattered
  in slices of 0.2 to 1.3 GB**, i.e. less than one agent per VM. Almost
  all of them serve production: they are **unusable for agents**.
* The mesh VM has no margin.

The contract must therefore hold for any host. The pilot, however, plays out
on one or two large VMs, starting with the test VM.

# What exists

## In ameesh (`main`)

* **Measurement and back-pressure** (L31, decision 0028, L73, L106;
  `resources.py`):
  - Each runner samples its host: available memory, swap, 1-min load, CPU,
    free disk of the work directory, `/tmp` tmpfs, power supply and turns
    in progress. The samples go into `host_resources`, kept for 7 days.
  - The thresholds are in the canon (`policy.resources` of the `Host` card),
    with prudent defaults.
  - A crossed threshold blocks **new** turns. Under critical pressure, the
    lowest-priority agents are paused, **never in the middle of a turn**.
  - L31b makes the wait visible; an orchestrator gets through under
    non-critical pressure; an episode fits in two log lines.
* **Ready host** (L106, `hostcheck.py`): `ameesh doctor --harness` checks
  the harness binaries and the enabled runner units. It measures the
  availability of the tooling, not that of the capacity.
* **Cards and placement** (`canon.py`, `placement.py`, decisions 0014, 0029,
  0031):
  - The `Host` card carries a responsible person, administrators, labels
    and a policy: harnesses, providers, credential modes, `max_agents`,
    thresholds, work directories, cleanup.
  - An admission (`Placement`) names an agent's admitted hosts.
  - With several canons, the physical limits retained are the strictest
    of the cards.
* **Relocation** (0028 §5, `relocation.py`): between two turns, an agent
  leaves a host under pressure for an available admitted host. The choice
  goes to the most memory, then to the lowest load. The session follows if
  its storage is shared; otherwise, rotation with a summary.

## In ameesh (track B, branch `lot/v170-voie-b`, not merged)

The mediated runner (L107–L115) was designed for Nexlink Compute devices.
Three of its pieces serve here as they are.

* **The host gate** (L112, `mediated_executor/gate.py` and `host_gate.py`).
  An external process, the Compute runner, writes the
  `ameesh-host-state/1` state to a file (temporary file then `rename`)
  or pushes it on a socket. The ameesh runner answers with `ameesh-host-ack/1`.

  ```json
  {"schema": "ameesh-host-state/1", "state": "draining", "seq": 8,
   "until_ts": null, "drain_deadline_ts": 1791640090.12,
   "caps": {"max_concurrent": 1, "cpu_share": 0.5, "memory_mb": 4096},
   "reason": "user_active"}
  {"schema": "ameesh-host-ack/1", "seq": 8, "state": "draining",
   "in_turn": [], "held": [], "drained": true, "ts": 1791640040.12}
  ```

  Depending on the state, the runner:
  - in `available`, claims within the limit `min(caps.max_concurrent,
    max_agents)`;
  - in `draining`, no longer claims. Each turn stops at the safe point, with
    preemption at the deadline (90 s by default). The leases are returned,
    then the acknowledgement says `drained: true`;
  - in `stopped`, stops everything immediately.

  The return to `available` restarts claiming without human action.

  An unreadable state counts as `stopped` for a mediated host and `available`
  for a classic host (`host_gate_fallback`). A classic host can already have
  a gate (`AMEESH_HOST_GATE=file`). The runner only applies
  `caps.max_concurrent`: `cpu_share` and `memory_mb` circulate, but
  nobody enforces them on the ameesh side.
* **The volatile host**: `policy.volatile: true` gives a 90 s lease,
  renewed every 30 s; `policy.lease_ttl` sets it.
* **Enrolment** (L110):
  - an authorised human issues a single-use code, bound to the mesh, the host
    and a list of agents (`ameesh host enroll`);
  - the device enrols with a P-256 key (`ameesh device enroll
    --code-file`) and obtains short-lived tokens;
  - revocation releases the leases.

  Added to these are the `ameesh-executor/1` image (L114: uid 10001, no
  provider secret, exit codes 0 to 6) and the end-to-end trial (L115: seven
  green steps in a container). Together, they form the "device without
  database access" mode.

## In Nexlink Compute (`develop`, 2026-10-11)

The numbers (#93, #94, #115 to #117) are those of the Nexlink mesh tasks,
not GitHub issues.

* **Idle guard** (#93, merged; `desktop/src/compute/state.ts`,
  `idle-guard.ts`, `resources.ts`, `conditions.ts`). It writes
  `ameesh-host-state/1` "exactly"; the initial plan's own format
  (`status.json`, five states) was abandoned at the freeze.
  - **Signals**: five minutes without input, mains power, unmetered network,
    time window of the offer. Before claiming only, these are joined by a
    CPU at most 30 % busy and 2 GiB of free memory. During the work,
    nothing is measured ("the VM is the load"). There is no runner under
    Linux.
  - **Writing**: a probe every 2 s, and the state is only rewritten if it
    changes. `seq` starts from the clock. The `caps` are
    `max_concurrent: 1`; `cpu_share` and `memory_mb` come from the offer
    (free memory at the time of the offer, multiplied by the consented share).
  - **Reasons**: `idle`, `user_active`, `battery`, `window_end`,
    `metered`. The owner's pause, the lost lease and the shutdown all
    become `revoked`.
  - **Actions**: without work in progress, immediate preemption. With
    work, `draining`, then shutdown of the VM. The owner's pause gives
    `stopped`. No freeze, no pause-resume, no priority lowering.
  - **Acknowledgement**: the hardened reader only admits the keys `schema`,
    `seq`, `drained` and `agents_in_turn`. **It therefore rejects ameesh's
    real acknowledgement** (`state`, `in_turn`, `held`, `ts`). The runner is
    never recognised, and the withdrawal falls to 15 s instead of 90 s. The
    #94 branch fixes this reader.
* **Agent turn** (#94; branches `agent/compute-agent-turn` and `…-94b`,
  not merged). The `compute-agent-turn` v0.2 contract has converged with
  track B. The `COMPUTE_AGENT_TURN_ENABLED` flag is off, and `develop`
  only admits the `build` class.
  - An `agent` class job reserves the device's sandbox for a window
    (4 h proposed).
  - It launches there the `ameesh-executor/1` image of a single mesh, which
    enrols (L110).
  - The Compute lease, of 120 s (renewed every 40 s), covers the 90 s ameesh
    lease.
  - `window.json` accounts for the window at its end.
* **PROV-2 and PROV-3** (merged, activation closed):
  - Mac networking (vzNAT);
  - one VM per (device, mesh) pair: Lima on Mac, WSL on Windows;
  - the runner runs in a rootless Podman container (`--cpus=2
    --memory=3g --pids-limit=512 --read-only --cap-drop=ALL
    --stop-timeout=100`), and only goes out to the mesh server, through a
    proxy.
  - **The size of the agent VM is fixed** (2 vCPU, 4 GiB, 40 GiB) and does
    not depend on the offer. D13, on the other hand, sizes the build VM
    according to the offer (`nexlink-compute-sizing/1`), as an allocation
    and not as a usage cap.
* **Native mode** (#115 to #117: admission, consent and erasure written as
  pure functions, with no production wiring): desktop builds outside a VM,
  under a standard account, on macOS and Windows x64. **No agent is
  admitted there.** Its caps (`cpuPercent`, `memoryBytes`, `outputBytes`,
  `durationSeconds`) go through an `enforceResources` port that no OS
  implements yet.
* **Offer**: `ComputeOffer` v1, signed, sent to the hub and not to the mesh.
  It carries the cores, the memory, the disk, conditions (`idle`, `ac`,
  `wifi`), a window in local time, the consented shares and the egress per
  day. Defaults: from 22:00 to 07:00, 25 % of CPU and memory, 20 % of
  disk.
  The parent specification plans more, none of which is built: a
  `when` / `caps` / `on_context_change` profile (`pause`, `finish_current`
  or `return`), continuous adaptation and a shared budget.

## What is missing

1. **The measurement does not distinguish the application from the guest.**
   On a lent VM, the load and memory sampled by ameesh include those of the
   agents themselves. On the Nexlink side, nothing is measured during work.
2. **Nothing caps an ameesh agent.** The `ameesh-runner-agent@` unit has
   no `MemoryMax`, no `CPUWeight`, no slice: this is the cause of the OOM on
   the mesh VM.
3. **Giving way is too slow.**
   - Back-pressure only stops new turns.
   - A turn in progress lasts up to 15 min, and work launched in the
     background keeps 20 min of grace.
   - Stopping by signal drains for up to 30 min.
4. **The `caps` do not state the real envelope.** On the ameesh side,
   nothing enforces them. On the Nexlink side, they come from the offer,
   whereas the agent VM is fixed (2 vCPU, 4 GiB): they can exceed reality
   or fall short of it. Disk, I/O and duration do not appear in them.
5. **The windows are poor.** `until_ts` ignores the job's maximum duration,
   and nothing says when the next window opens.
6. **The real acknowledgement is rejected** by the Nexlink reader on
   `develop`.
7. **The paths diverge in the ameesh documentation** (track B).
   `EXECUTEUR-MEDIEE.md` and the `gate.py` default say
   `/run/ameesh-gate/state.json`. `IMAGE-EXECUTEUR.md` and Nexlink say
   `/run/ameesh-gate/state/state.json`. The directory form is the right one:
   a `rename` is only visible through a mount if the directory is
   mounted.
8. **Nexlink has three cap vocabularies** (offer, sizing, native mode),
   unrelated to the gate's `caps`.

# 1. The common contract: the capacity guard

## 1.1 Roles

| Role | On an organisation VM | On a lent device |
|---|---|---|
| **Lending host** | application VM, CI VM, test VM | laptop, desktop |
| **Guard** (privileged, host side) | `ameesh guard`, system service (to be created, L159e) | the Nexlink Compute runner (macOS, Windows); under Linux, `ameesh guard` could serve |
| **Guest** (unprivileged) | classic or mediated ameesh runner, or offloaded job | mediated runner in the agent VM (PROV-3) |
| **Mesh** | sees the offer, the state and the measure (`ameesh hosts`), places and relocates | same, via `PUT /host/availability` |

The guard measures, decides and **enforces**. The caps are set by the
kernel (cgroups) or by the hypervisor (VM size), never left to the guest's
goodwill. The guest obeys the state, returns its leases and acknowledges.
This is already Nexlink's split: the Compute runner writes the state and
holds the VM, the ameesh runner obeys.

## 1.2 Principles

1. **The application always wins.** The guest lives in a low-priority
   slice, under hard caps. When the application picks up again, it gives way
   **without waiting for the end of its turn**.
2. **A single format.** `ameesh-host-state/1` is extended with
   **optional** fields, which the ameesh reader ignores if it does not know
   them (`GateState.from_json`).
   - **No state value is added** in version 1: an unknown state is
     unreadable, and a classic host would then fall back to `available`.
   - **The acknowledgement does not change**, because the Nexlink reader
     only admits a closed list of keys, and that is intended.
   - An incompatible change opens a version 2.
3. **A single measure.** The definitions are the same on a VM and on
   a device. The lendable capacity is computed on the application's
   consumption, **never on the guest's**: otherwise, the guest would chase
   itself away.
4. **Closed by default.** On a lending host, an unreadable, missing or
   stale gate counts as `stopped`, even for a classic runner
   (`host_gate_fallback: stopped`).
5. **Three levels, always decreasing**: declared offer ≥ granted caps
   (`caps`) ≥ consumption.
   - On a VM, the offer is reviewed by the person responsible for the host,
     in the canon.
   - On a device, it is consented by its holder, in Nexlink.

## 1.3 The offer: what the host lends

On a VM, the offer is **declarative**. It lives in the `Host` card of the
canon and is reviewed by PR, like the thresholds of 0028. The guard reads it
from its local configuration; `ameesh hosts` flags any discrepancy between
the two.

```yaml
---
type: Host
title: vm-exemple
responsible: human:…
tags: [pret]
policy:
  volatile: true            # 90 s lease: a killed guest is quickly picked up elsewhere
  max_agents: 3
  credential_modes: [api-key]
  work_root: /srv/ameesh-guest/work
  lend:                     # new (L159a); absent = the host lends nothing
    cpu_max: 3              # vCPU at most (quota)
    cpu_weight: 10          # weight against the application (100 by default)
    memory_max: 14GiB       # hard cap for the whole guest
    agent_memory_max: 4.5GiB  # hard cap per agent or per job
    disk_max: 80GiB         # dedicated volume of the guest
    windows: ["mon-fri 18:00-14:00 UTC", "sat-sun"]   # absent = always
    reclaim:                # when the application picks up again (thresholds of THE APPLICATION)
      memory_available_min: 6GiB
      cpu_pressure_max: 20%       # PSI cpu "some", 60 s average, excluding guest
      memory_pressure_max: 2%     # PSI memory "some", 60 s average
      busy_units: ["actions.runner.*"]   # CI VM: jobs in progress
      busy_max: 4
    drain_seconds: 90       # withdrawal deadline
    calm_seconds: 600       # hysteresis before lending again
---
```

On a device, the offer is the `ComputeOffer` that the holder has
consented to. The device's `Host` card keeps `policy.volatile` and
`max_agents` (often 1), and the mesh refuses any claim beyond that.
A mapping table is also needed from the offer to the `caps`, and
from the `caps` to the VM and the container. It would replace Nexlink's
three current vocabularies (N2).

## 1.4 The common measure

The guard publishes in the state a `measure` object, defined the same way
everywhere. The fields that `resources.sample()` already samples keep their
name; the others are new.

| Field | Definition |
|---|---|
| `ts`, `window_s` | time of the sample; window of the averages (60 s) |
| `cpu_count` | logical CPUs of the host (on a device: those of the machine, not those of the VM) |
| `app_cpu` | CPUs consumed **outside the guest**, averaged over the window: the total minus the guest (`cpu.stat` of its slice, or VM processes on a device) |
| `guest_cpu` | CPUs consumed by the guest |
| `mem_total_bytes`, `mem_available_bytes`, `swap_used_bytes` | the host's `/proc/meminfo`, as today |
| `guest_mem_bytes` | `memory.current` of the guest's slice, or memory of the VM |
| `psi` | `{cpu_some, mem_some, mem_full, io_some}` in %, 60 s average, for the host; `app_psi`, same keys for the application's group when it is readable |
| `disk_free_bytes` | guest volume; `app_disk_free_bytes`: the lowest of the application's file systems |
| `tmp_*` | occupancy of `/tmp` if it is a tmpfs (L73) |
| `on_ac`, `battery_percent`, `metered` | power supply (L106) and metered network; `null` on a VM |
| `user_idle_s` | seconds since the last input; `null` on a VM |
| `busy` | number of application activity units in progress (CI jobs); `null` without `busy_units` |
| `lendable` | `{cpu, memory_bytes, disk_bytes}`: lendable capacity, according to the formula below |

**Formula.** It is the inventory's formula, made continuous.

* **History.** The guard keeps 7 days of local history, in 10-min steps.
  On first start, it starts from sysstat if it exists, otherwise from a
  prudent value (25 % of memory, 1 CPU).
* **Memory.** `lendable.memory_bytes = min(memory_max, h × M7)`. M7 is the
  lowest, over 7 days, of `mem_available + guest_mem`: the memory that
  the application left free at its worst moment. The margin `h` is 0.5.
* **CPU.** `lendable.cpu = min(cpu_max, h × (cpu_count − A7))`. A7 is the
  95th percentile of `app_cpu` per 10-min slot, over 7 days.
* **Disk.** `lendable.disk_bytes = min(disk_max, disk_free − 2 GiB)`.

The same measure feeds `ameesh hosts` and relocation (0028 §5). A lending
host is ranked there by `lendable.memory_bytes`, and not by its raw free
memory. On the workstation too, PSI provides what the load does not say: a
load of 43 does not indicate how much the user suffers, PSI does (L159d).

## 1.5 The state: `ameesh-host-state/1` extended

The current fields do not change: `schema`, `state`, `seq`, `until_ts`,
`drain_deadline_ts`, `caps` and `reason`. The state is written by the guard
and read by the guest, whose reader ignores unknown fields. All added fields
are therefore optional.

| Field | Meaning | Old reader |
|---|---|---|
| `expires_ts` | the state is no longer valid after this instant. The guard rewrites it at a third of its lifetime (30 s by default). A stale state counts as unreadable, hence `stopped` | ignores it and keeps the last state; the kernel caps remain in place |
| `until_ts` | clarified: the nearer of the two ends, that of the window and that of the job (`max_seconds`) | unchanged |
| `caps.cpu_share` | clarified: share of the CPUs **of the host** (quota `cpu_share × cpu_count`) | unchanged |
| `caps.memory_mb` | clarified: hard cap of the **whole** guest, i.e. the real envelope and not the offer | unchanged |
| `caps.cpu_weight`, `caps.io_weight` | weight against the application (1 to 10,000) | ignored |
| `caps.memory_high_mb` | soft cap: beyond it, the kernel reclaims the guest's memory first | ignored |
| `caps.agent_memory_mb` | hard cap per agent or per offloaded job | ignored |
| `caps.disk_mb`, `caps.tmp_mb`, `caps.max_job_s` | disk, `/tmp` and duration of the guest | ignored |
| `caps.enforced` | the caps that the guard enforces itself (kernel, hypervisor); the others are indicative | ignored |
| `freeze` | `{since_ts, until_ts}`: turns frozen by the guard (tier 2). The runner does not count this time in its turn timeouts | ignored; the freeze is bounded to 60 s, below the timeouts |
| `windows` | next windows `[{start_ts, end_ts}]`, for planning | ignored |
| `offer` | the offer in force (section 1.3), for display and discrepancy checking | ignored |
| `measure` | the common measure (section 1.4) | ignored |
| `guard` | `{kind: "ameesh-guard" or "nexlink-compute", version, host_kind: "vm" or "device"}` | ignored |

**Reasons.** Free text is already admitted. The following values are fixed:

* from Nexlink, the existing reasons and `metered`, plus three that
  replace a misleading `revoked`: `owner_pause`, `lease_lost`,
  `shutdown`;
* from the VMs: `app_memory`, `app_cpu`, `app_busy` (CI jobs), `disk_low`,
  `maintenance`, `guard_stopping`.

**Paths.** Those of the image and of Nexlink are kept:

* `/run/ameesh-gate/state/state.json`, directory read-only for
  the guest;
* `/run/ameesh-gate/ack/ack.json`, directory writable by the guest.

The `gate.py` default and `EXECUTEUR-MEDIEE.md` (track B) align with them at
the merge (L159f).

**Relay to the mesh.** `ameesh-exec-availability/1` relays the same
additions to the server. It is a contract addition (1.2), without breakage:
the server already normalises the body by discarding unknown fields.

## 1.6 Giving way: the tiers

The guard evaluates its signals every 5 s, with averages over 10 s to
react and over 60 s to decide. It moves up a tier as soon as a reclaim
threshold is crossed. It only comes back down after `calm_seconds` spent
below all thresholds (hysteresis), incrementing `seq`.

| Tier | State written | What the guard does | What the guest does |
|---|---|---|---|
| **0 — ready** | `available`, `caps` = lendable | low-weight slice (`CPUWeight=10`, `IOWeight=10`), `CPUQuota`, `MemoryHigh`, `MemoryMax`; `MemoryMax` per agent | claims within the limit of `max_concurrent` |
| **1 — reduced** | `available`, `caps` lowered | brings `max_concurrent` down to the number of turns in progress; reduces the CPU quota; brings `MemoryHigh` down to usage, and the kernel reclaims the guest's cache | no longer starts a turn beyond that |
| **2 — freeze** (CPU or I/O only) | `available` + `freeze` | freezes the agents' units, runner included, and the jobs (`systemctl freeze`), 60 s at most. A freeze frees no memory: it is never used for memory | nothing during the freeze; the 90 s lease, renewed every 30 s, survives it; on thaw, the frozen time does not count in the turn timeouts |
| **3 — withdrawal** | `draining`, deadline at `drain_seconds` | thaws, waits for the acknowledgement | stops its turn at the safe point, background work included; returns its leases; acknowledges `drained: true` |
| **4 — stop** | `stopped` | 30 s after the deadline without `drained`: `systemctl kill`, then stop of the slice | immediate stop |

These tiers give a precise meaning to the `on_context_change` of the
Nexlink specification:

* `pause` corresponds to tier 2;
* `finish_current` to tier 3, with a long deadline;
* `return` to tier 4.

**Safety nets independent of the guard**:

* the slice's `MemoryMax` confines the OOM to the guest;
* `OOMScoreAdjust=500` on the guest's processes: the global OOM picks
  them first;
* systemd-oomd watches the slice, if present.

A stopped guard leaves its last caps in place (they are properties of the
slice), and its state goes stale at `expires_ts`.

**Reclaim signals**:

* on a VM: available memory of the application, PSI excluding the guest,
  `busy_units` (CI jobs), application disk, end of window;
* on a device: return of the user, battery, metered network, end of
  window (#93). During work, per-resource measurement (N1) will make it
  possible to reduce before stopping everything.

The reaction is counted in seconds at tiers 0 to 2, then in
`drain_seconds` at tier 3. The application never sees the guest take
more than its `caps`.

## 1.7 Caps: mappings

| Cap | Linux, cgroups v2 | systemd | Device (agent VM, PROV-3) |
|---|---|---|---|
| `cpu_share` | `cpu.max` | `CPUQuota=` (share × CPU × 100 %) | vCPUs of the VM and `--cpus` of the container (fixed today: 2) |
| `cpu_weight` | `cpu.weight` (or `cpu.idle` if the kernel and systemd allow it) | `CPUWeight=` | priority of the VM process (to be studied) |
| `memory_mb` | `memory.max` of the slice | `MemoryMax=` | memory of the VM and `--memory` of the container (today 4 GiB and 3 GiB) |
| `memory_high_mb` | `memory.high` | `MemoryHigh=` | — |
| `agent_memory_mb` | `memory.max` of the agent's unit | `MemoryMax=` on `ameesh-guest-runner@` | `--memory` of the container (one agent at a time) |
| `io_weight` | `io.weight` | `IOWeight=` | — |
| `disk_mb` | size of the dedicated volume | `ReadWritePaths=` limited to the volume | disk of the VM (today 40 GiB) |
| `tmp_mb` | private `/tmp` on the volume | `PrivateTmp=yes`, `TMPDIR` | container tmpfs (256 MiB) |
| freeze | `cgroup.freeze` | `systemctl freeze` / `thaw` | pause of the VM if the hypervisor allows it (optional) |
| forced withdrawal | `cgroup.kill` | `systemctl kill`, `stop` | stop of the VM (`limactl stop`, `wsl --terminate`) |

## 1.8 The acknowledgement

`ameesh-host-ack/1` does not change: `seq`, `state`, `in_turn`, `held`,
`drained` and `ts`. The Nexlink reader simply has to admit these keys
(N2).

Offloaded jobs (mode b) do not acknowledge. The guard sees them as
scopes (`ameesh-job-*.scope`) in the guest's slice, and stops them
itself at the deadline.

## 1.9 Who implements what, and how to stay in agreement

* **ameesh publishes the contract**: `docs/GARDE-DE-CAPACITE.md`, the JSON
  schemas of the two messages and **golden sets** (L159a). A golden set
  associates raw inputs (`/proc/meminfo`, PSI, `cpu.stat`, offer, history)
  with the expected outputs: `lendable`, tier, state written. They live under
  `tests/dore/capacity_guard/`, like those of the mediated runner.
* **ameesh provides** the guard for Linux VMs (`ameesh guard`, L159e) and
  the guest: classic or mediated runner, and the VM side of offloading. As
  Nexlink has no runner under Linux, the same guard could serve a
  Linux device.
* **Nexlink keeps its runner** as the guard of devices. It writes the
  added fields and runs the same golden sets in its tests (N1).
  It also maps the `caps` to the real envelope of the agent
  VM (N2).
* **In (a2), a VM reuses PROV-3's launch recipe**: same
  container, same options, same gate mounts. The container then
  behaves the same way on a device and on a VM.
* **A new field** is proposed by PR on the ameesh contract, with its
  golden set. The ameesh reader ignores it as long as it does not know it.

# 2. Two modes on a VM

## 2.1 Mode (a): a full ameesh runner under a capped slice

The lending host becomes a mesh host like any other, admitted for a few
agents. The relocation of 0028 sends an agent there when the workstation is
under pressure and the VM is `available`.

* **Units**: `ameesh-guest-runner@<agent>.service`. These are **system**
  units, which avoids depending on delegation of cgroups to the user
  manager.
  - Identity and caps: `User=ameesh-guest`, `Slice=ameesh-guest.slice`,
    `MemoryHigh` and `MemoryMax` per agent, `OOMScoreAdjust=500`, plus the
    hardening of section 3.
  - Gate: `AMEESH_HOST_GATE=file`, `AMEESH_HOST_GATE_FALLBACK=stopped`,
    with the paths of section 1.5.
  - Stop: `AMEESH_DRAIN_SECONDS=120`, short `turn_grace_seconds`,
    `TimeoutStopSec=180`.
* **Two variants**:
  - **(a1) classic**: direct access to the mesh database. It requires a
    WireGuard peer, a limited Postgres role (L56) and the agent's secrets on
    the VM (harness account, forge token). Possible on `main` as soon as
    the gate is there (L159f).
  - **(a2) mediated** (track B): no database access, no provider key
    (model relay), a revocable enrolment. It is the `ameesh-executor/1`
    image, launched like PROV-3, and the guard writes the gate. It
    requires track B to be merged and the mesh server to be reachable over
    HTTPS. Plan-based harnesses do not go there.
* **Card and admission**:
  - `Host` card with `policy.lend`, `volatile`, `max_agents`, and a
    `work_root` on the guest's volume;
  - admission `hosts: [poste, vm]` for the chosen agents;
  - relocation must also consult the gate state of the destination
    host (L159f).

## 2.2 Mode (b): offload only the heavy suites

The agent stays on the workstation. Its heavy commands (full suite, e2e,
SQL tests with Postgres in a container, builds) go to the VM. This
mode settles the CPU and `/tmp` part of the problem, without putting any of
the agent's secrets on the VM.

**(b1) over SSH**, feasible right away on the test VM:

* **Workstation side**, `ameesh offload run [--profile …] -- <command>`:
  - takes an **exact snapshot of the working tree**, via a temporary
    index, `git write-tree` and `git commit-tree`. Neither the agent's index
    nor its references move. Untracked files are included, ignored ones
    excluded;
  - sends it as a **differential git bundle** to a mirror of the repository
    on the VM;
  - runs the command there in a disposable job directory. The output and
    the exit code come back as is.
* **VM side**, an `ameesh-guest` user, without sudo or `docker` group:
  - its SSH key is restricted (`restrict`, forced command
    `ameesh-offload-shell`) to four verbs: `put`, `run`, `status` and
    `cancel`. No shell, no port forwarding;
  - test containers go through **rootless Podman**, in the user's
    slice;
  - dependencies are cached, by hash of the lock file;
  - each job is a scope (`systemd-run --user --scope`) capped at
    `agent_memory_mb`;
  - concurrency tokens (`flock`; three on the test VM) form the
    queue, and the client displays the wait.
* **Obeying the gate**:
  - no new job outside `available`;
  - in `draining`, jobs finish until the deadline, then the guard kills
    them;
  - in `stopped`, they are killed immediately.
* **Fallback**: when the VM refuses or stops a job, the client reruns the
  command on the workstation (`--fallback local`). Without
  `AMEESH_OFFLOAD_HOST`, or if the VM is unreachable, the command also runs
  on the workstation. The agents' prompt says which commands go through
  `ameesh offload`.

**(b2) via a runner**: a labelled GitHub Actions runner on the VM. The agent
triggers it (`workflow_dispatch`) on its pushed branch, then follows the run
with `gh run watch`.

* Advantages: the logs stay in the forge, and there is no SSH key
  to manage.
* Cost: a push and 20 to 60 s of latency.
* On a **public repository**, a runner group limited to the chosen workflows
  is needed, as well as approval of outside contributions.

It is kept for repositories whose CI already runs on our VMs.

## 2.3 Choice

| | (b1) SSH offload | (a1) classic runner | (a2) mediated runner |
|---|---|---|---|
| What leaves the workstation | CPU and `/tmp` of the suites | whole agents (memory and CPU) | whole agents |
| Agent secrets on the VM | none | harness account, forge token, database access | a revocable runner key |
| Depends on | nothing new | gate on `main` (L159f), L56, WireGuard | track B merged, HTTPS server |
| Lead time | 1 to 2 days | about a week | after track B |
| Eligible VMs | test VM; CI VM after decision | test VM | all, CI VM included |

**Recommendation**:

1. (b1) first, on the test VM, to relieve the workstation this week;
2. then (a1) on the same VM, for two or three agents;
3. (a2) last, for the VMs we do not fully control, starting
   with the CI VM.

If the VM dedicated to agents of L163 is created, it receives (a1) before the
test VM. It has no application to protect, and the test VM keeps the
offload (b1). The contract applies there all the same: per-agent caps
(L159c), measure (L159d), and a guard reduced to the host's floors.

# 3. Security and isolation

* **Dedicated user** `ameesh-guest`: no sudo, no group of the
  application, no `docker`. Its home and its work are on a dedicated volume
  (`/srv/ameesh-guest`, `0700`).
  - The system units set `NoNewPrivileges`, `ProtectSystem=strict`,
    `ReadWritePaths=/srv/ameesh-guest`, `ProtectHome=tmpfs`, `PrivateTmp`,
    `PrivateDevices`, `ProtectKernelTunables` and `RestrictSUIDSGID`.
  - `ProtectProc=invisible` hides the processes of other users, whose
    command lines sometimes carry secrets.
  - `ProtectControlGroups` prevents the guest from raising its own
    caps.
  - Mode (b) leaves user namespaces open, because Podman needs
    them.
* **Application secrets**: the guest reads neither its files nor its
  environment. `InaccessiblePaths=` covers its configuration and data
  directories, as well as `/run/docker.sock`. An installation check,
  `ameesh guard check`, looks under the guest's identity for any file of
  the application that it could read.
* **Agent secrets** (a1): home `0700`, or systemd's `LoadCredential=`.
  The trust boundary remains the VM's root: anything that is root, or
  equivalent, reads these secrets. A VM therefore only hosts an agent
  with secrets if everyone who has root access there is worthy of those
  secrets.
* **CI VM: `runner` in the `docker` group.** This group is equivalent to
  root, because `docker run -v /:/h` reads the whole disk.
  - Any CI job, including the code of a PR, could read the harness
    accounts, the forge tokens and the database password of an agent
    (a1). It could also kill the guest or raise its caps.
  - **No agent with secrets (a1) on the CI VM** as long as this group is
    given to the jobs.
  - To lend there anyway, two ways:
    1. mode (a2), which leaves only a revocable runner key there. The
       worst case becomes impersonation of that runner, bounded to its
       admitted agents and its relay caps;
    2. a CI in rootless Docker, with one user per runner. This is a
       project for the CI maintainers.
  - Mode (b1) remains possible there: offloaded jobs carry no
    secret, and only bring back test logs.
* **Limited Postgres role**:
  - (a1): the `ameesh_executeur` role of L56 (PR #23: data yes, schema
    no) and one login role per host, which `pg_hba` only admits from
    the VM's WireGuard address. Known limit: all runners
    share the same rights on the data;
  - (a2): no Postgres role;
  - (b): disposable databases, specific to the job (Podman, local socket),
    never the application's database nor the mesh's.
* **Network**:
  - inbound: nothing in mode (a). In mode (b), SSH only, from the
    workstation and the mesh VM (security group or WireGuard);
  - outbound: nftables rules per guest uid (`meta skuid`)
    refuse the application's local ports (database, cache, API on the
    loopback) and the internal network, except the mesh database in (a1).
    Outbound HTTPS remains allowed for the providers and the forge. In
    (a2), only the mesh server is reachable, as in PROV-3.
* **What remains**: the guest shares the application's kernel.
  The isolation is that of Linux between users, not that of a VM.
  Lending a customer's production VM would require a micro-VM, or not
  lending. The inventory has already settled it: these VMs are too small.

# 4. Rollout

## 4.1 Pilot VM: the test VM

* **Why this one**:
  - it belongs to our project: no customer data, no
    production, and only the owner's key is installed on it;
  - it keeps more than 28 GB free during the suites, and it is
    idle the rest of the time;
  - its "application" is our test suites. Offloading the agents' suites
    there extends its use, under better control: a dedicated user
    rather than root, caps, a queue.
* **Condition**: a dedicated 100 GB volume, mounted on `/srv/ameesh-guest`,
  rather than an enlarged root. A guest that fills its volume does not reach
  the system. Also growing the root to 20 GB would give Docker some air,
  already tight.

## 4.2 Steps

1. **Preparation** (owner): volume, SSH access from the workstation,
   choice of modes.
   - A versioned and reviewed installation script (`deploy/guest/install.sh`,
     L159b) prepares the VM: user, volume, caps of the user slice,
     rootless Podman, restricted key, nftables rules.
   - The owner runs it himself on the VM: no agent modifies the
     persistent configuration of a VM.
2. **Offload (b1)**: `ameesh offload` on the workstation, and an agent
   prompt that offloads the full and e2e suites. One week of measurement.
3. **Caps everywhere** (L159c): `MemoryMax` per agent, on the workstation and
   the mesh VM, with the `ameesh-agents.slice` slice. This step depends on
   nothing else, and it fixes the OOM on the mesh VM.
4. **Guard** (L159e) on the test VM, first in observation: it writes
   the state and the measure, without dynamic caps.
5. **Runner (a1)** on the test VM, or first on the dedicated VM of
   L163 if it exists:
   - two agents first, under DeepSeek via `dsh` (no plan-based
     account), with `[poste, vm]` admissions;
   - one week of measurement;
   - then three agents.
6. **CI VM**: only after the decision on isolation (section 3).
   It is lent only outside CI hours, in (b1) or in (a2).

## 4.3 Rollback

* **(b1)**: remove `AMEESH_OFFLOAD_HOST`, and the agents test again
  on the workstation. Then remove the key from `authorized_keys`, delete
  the user and detach the volume. The application was never touched.
* **(a)**:
  1. `ameesh guard drain` (or write `stopped`): the agents return their
     leases;
  2. a PR on the canon removes the admission: the agents go back to the
     workstation at the next turn;
  3. disable the units;
  4. delete the host's login role (a1), or run `ameesh host
     revoke` (a2);
  5. delete the user and the slice.
* **The guard**: `systemctl disable --now ameesh-guard`, then reset the
  slice's properties. Nothing else has changed on the VM.

## 4.4 Success measures

Each step is measured one week before and one week after. The sources
are the runner's logs (start and end lines of pressure episodes,
L31b) and `host_resources` (7 days).

| Measure | Before (10 and 11 October) | Target |
|---|---|---|
| Turns delayed by workstation pressure | ≈ 80 min over the day of the 10th | less than 10 min per day |
| Workstation load | up to 43 for 12 CPUs | 95th percentile below 18, no critical episode |
| Workstation `/tmp` | full on the 11th around 03:38 | never above 80 % |
| Median duration of a turn with a test suite | to be sampled (step 2) | −30 % |
| Lent VM's own suites | to be sampled | 90th percentile degraded by 10 % at most |
| Lent VM's application | — | no OOM; memory PSI excluding guest below 1 % outside withdrawals |
| Withdrawals | — | 99 % acknowledged `drained` before the deadline |
| Usage | 0 | hours of turns or jobs hosted per day |

# 5. Lots

Durations are in agent work hours, review included. Dates
start from Monday 12 October. The guard (L159e) waits for the contract
(L159a); the rest proceeds in parallel.

## 5.1 ameesh

| Lot | Content | Depends on | Duration | Date |
|---|---|---|---|---|
| **L159a** — contract | `docs/GARDE-DE-CAPACITE.md`; JSON schemas of the extended state and of the acknowledgement; `policy.lend` in the canon (`canon check`); golden sets `tests/dore/capacity_guard/`; `state/` and `ack/` paths | this study | 5 h | 12/10 |
| **L159b** — offload (b1) | `deploy/guest/install.sh` (run by the owner); `ameesh-offload-shell` (`put`, `run`, `status`, `cancel`); `ameesh offload run` (snapshot, differential bundle, concurrency tokens, local fallback); agents' prompt | — | 8 h | 12–13/10 |
| **L159c** — per-agent caps | `ameesh-agents.slice`; `MemoryHigh` and `MemoryMax` per agent unit, on the workstation and the mesh VM; `OOMScoreAdjust`; check in `ameesh doctor` | — | 3 h | 12/10 |
| **L159d** — common measure | in `resources.py`: PSI, application and guest shares, `lendable`; `host_resources` columns (migration); `ameesh hosts`; `max_cpu_pressure` threshold on the workstation; relocation ranked by `lendable` | L159a | 6 h | 13/10 |
| **L159e** — `ameesh guard` | system service for Linux VMs: offer, history, tiers 0 to 4, caps via `systemctl set-property --runtime`, freeze, `expires_ts`, waiting for the acknowledgement, `guard check`, `guard drain`; golden sets | L159a, L159d | 12 h | 14–15/10 |
| **L159f** — gate on `main` | L112 gate for the classic runner (merge of track B, or extraction of the gate alone); directory paths; `expires_ts`, `freeze`; a withdrawal also stops background work (end-of-turn grace); defaults for a lending host; relocation that consults the destination gate | decision on track B | 6 h | 15/10 |
| **L159g** — pilot (a1) | `ameesh-guest-runner@` units; PR on the canon (`Host` with `lend`, admissions); two agents, then three; measurement report | L159c, L159e, L159f, L56 | 4 h + 7 days | from 16/10 to the report of 23/10 |
| **L159h** — CI VM | depending on the decision: window and `busy_units` for (b1), or (a2) image with the guard | owner's decision, L159g report | 6 h | from 26/10 |
| **L159i** — (a2) on a VM | `ameesh-executor/1` container launched by the guard like PROV-3 (volume, gate, enrolment code, stop in 100 s, exit codes) | track B merged, mesh server over HTTPS | 4 h | from 28/10 |

Total on the ameesh side: about 54 h, plus one week of observation.

**Neighbouring lots**, already open:

* **L161**: tests chosen according to impact, errors reported in real time.
  Fewer heavy suites to offload, and the L159b client can relay
  errors as they come.
* **L162**: nothing heavy in tmpfs, cleanup of abandoned `/tmp`s. It is
  the complement, on the workstation, of the guest's private `/tmp`.
* **L163**: a VM dedicated to agents. It is the first host of (a1) if it
  exists (section 2.3).

## 5.2 Nexlink: what falls to the authors of #93 (claude1) and #94 (claude2)

| Lot | Who | Content | Depends on | Duration | Date |
|---|---|---|---|---|---|
| **N1** — idle guard | author of #93 | write `expires_ts` and rewrite the state at a third of its lifetime; distinct reasons (`owner_pause`, `lease_lost`, `shutdown`); `windows`; `measure` according to the common definitions, host CPU and memory excluding the VM **during** work; `guard`; ameesh golden sets in its tests | L159a | 6 h | 14–15/10 |
| **N2** — agent turn | author of #94 | merge the acknowledgement reader of 94b (real keys `state`, `in_turn`, `held`, `ts`), without which the withdrawal stays at 15 s; `until_ts` = the nearer of the window end and `max_seconds`; `caps` equal to the real envelope (agent VM and container), or VM sized according to the offer like D13; `caps.enforced`; a single table offer → `caps` → VM | N1 | 8 h | 16–17/10 |
| **N3** — written contract | author of #94 | `compute-agent-turn`: `in_turn` and `held` (and not `agents_in_turn`); `state/` and `ack/` paths; reference to the guard contract | L159a | 0.5 h | 14/10 |

None of these lots blocks the pilot on a VM. They make the same
contract true on devices.

## 5.3 Owner actions

| Action | For | Duration | When |
|---|---|---|---|
| Decide: pilot VM; order (b1), then (a1), then (a2) | everything | 10 min | 11 or 12/10 |
| Add a 100 GB volume to the test VM (and grow the root to 20 GB) | L159b | 15 min | 12/10 |
| Open SSH to the test VM from the workstation and the mesh VM | L159b | 10 min | 12/10 |
| Run `deploy/guest/install.sh` on the VM (with `!`) | L159b, L159e | 15 min | 12–13/10 |
| Decide on the merge of track B (or the extraction of the gate) | L159f, L159i | 10 min | 14/10 |
| WireGuard peer of the VM and Postgres role (merge of L56, PR #23) | L159g | 20 min | 15/10 |
| Accounts admitted on a VM (DeepSeek alone first; plans afterwards?) | L159g | 10 min | 15/10 |
| Review the canon PR (`Host` with `lend`, admissions) | L159g | 10 min | 16/10 |
| CI VM: CI speed or lending; isolation of the `docker` group (with the CI maintainers) | L159h | 20 min | 24/10 |

# 6. Questions for the owner

1. **Order of the modes**: (b1) this week on the test VM, then (a1)?
   Recommendation: yes.
2. **Volume**: 100 GB dedicated to the guest rather than an enlarged root?
   Recommendation: the volume, plus 10 GB of root for Docker.
3. **CI VM**: lend only outside CI hours, in (b1) or in
   (a2), and never in (a1) as long as `runner` is in `docker`?
   Recommendation: yes. CI speed remains the priority; tiers 1 to 3 are
   triggered on `busy_units`.
4. **Plans on a VM**: can a plan-based Claude or Codex account
   serve an (a1) agent on the test VM? Recommendation: DeepSeek
   first, plans after accounts are separated by organisation.
5. **Field name**: `policy.lend` in the `Host` card?
6. **First host of (a1)**: the dedicated VM of L163, if it is created, or the
   test VM? Recommendation: the dedicated VM.
