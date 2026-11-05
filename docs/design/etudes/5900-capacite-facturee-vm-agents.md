---
type: Study
title: "#5900 — billed capacity of the agent VMs, fitted to the work that actually runs"
description: "Owner request of 05/11/2026 relayed by bureau (manaty/ameesh#4489): a process that fits the cost of ameesh-agents-1/2/3 to what is needed, without slowing Nexlink. Seven-day diagnostic from the mesh database, state of #4147/#4148/#4150/#4151 (release path inert end to end), sourced Scaleway prices including disks and IP, cost per state, options compared, a first bounded target (agents-3 GP1-S → GP1-XS, −71 €/month), hysteresis rules, proposed lots C1–C6 and owner questions Q1–Q4. No code, no machine gesture."
status: draft
tags: [capacite, hotes, cout, scaleway, drainage, placement, burst, baseline]
generated: { by: "mty-amh-impl-18/claude-opus-5-5", at: "2026-11-05T08:30:00Z" }
sources:
  - { resource: "manaty/ameesh#4489", title: "Owner request, 05/11/2026 (lot #5900)" }
  - { resource: "home:processus/lots.md", title: "Canon, « Capacité allumée au plus juste » (owner, 28/10/2026)" }
  - { resource: "../3167-placement-liberation.md", title: "Spec #3167: footprint, idle, baseline/burst, lifecycle hook, adapters" }
  - { resource: "../../../src/ameesh/host_idle.py", title: "Footprint and idle state (#4147, #4150)" }
  - { resource: "../../../src/ameesh/host_release.py", title: "Transitions, hook, auto-drain and release blockers (#4148)" }
  - { resource: "../../../src/ameesh/host_capacity_alerts.py", title: "host_idle_reserved, baseline_oversized (#4151)" }
  - { resource: "../../../src/ameesh/storage/postgres/host_lifecycle.py", title: "Loss detection (detect_losses, #5869 not fixed)" }
  - { resource: "../../../src/ameesh/storage/postgres/hosts.py", title: "Retention of host measurements (7 days)" }
  - { resource: "home:hotes/ameesh-agents-1.md, ameesh-agents-2.md, ameesh-agents-3.md; home:placements/*.md", title: "Host cards and placements (canon origin/main, 05/11)" }
  - { resource: "nexlink:scripts/ci/autoscale-reserve.sh", title: "ci-autoscale of nexlink-ci-1 (scw start/stop precedent)" }
  - { resource: "https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?zone=fr-par-1", title: "Scaleway public product catalog, read 05/11/2026" }
  - { resource: "github.com/scaleway/docs-content@c361a512aa (04/11/2026)", title: "Scaleway docs: instances FAQ, power-off, standby, resize, migrate local storage, billing FAQ" }
---

# Recommendation

1. **Do not expect savings from shutting VMs down when they go idle, today.**
   Over the last seven days the three agent VMs had 0 to 4 whole hours a day
   without a turn, and the night carries as much work as the day. The idle
   periods come in minutes. Scaleway bills at least 60 minutes per start/stop
   period, and powering a local-storage VM off and on takes minutes. So the
   30-minute drain rule of 28/10 would save almost nothing here, even once it
   works.
2. **The lever is the size of the fleet, not its schedule.** Summed minute by
   minute, the work of the three VMs fits on two VMs 98.7 % of the time with
   a 20 % margin, but not on one (memory p99 37 GB, max 45.7 GB). agents-3 is
   the least loaded: mean concurrency 0.95 turn, memory max 12.4 GB, CPU max
   5.7 cores. It hosts no elected role.
3. **First target, bounded and reversible: resize agents-3 from GP1-S
   (8 vCPU/32 GB) to GP1-XS (4 vCPU/16 GB).** Same family, same local disk
   (GP1-XS takes up to 150 GB), same agents, same IP. Saving: **≈ 71.5 €/month
   excl. VAT, 16 % of the fleet**. The power-off that the resize requires also
   gives the first real measurement of the wake-up delay, which the drain
   policy needs. It runs after #5869, so the power-off does not turn the
   host `lost` (§6).
4. **Repair the release path before relying on it.** The release half of
   #4148 is inert end to end (§2). #5869 must land first. Then the lots C2–C4
   below make the existing policy usable for a future host that really goes
   idle in blocks.
5. The bigger option, taking agents-3 out and keeping it powered off as a
   burst reserve (≈ 139 €/month), is not recommended before C4 has measured
   per-turn memory. agents-2 alone already saturated its memory on 30/10.
   Moving six more agents onto agents-1/2 would put Nexlink throughput at
   risk.

# 1. The request and its limits

The owner, 05/11, through bureau: « la marge VM coûte inutilement ». The
owner wants a process that fits the cost of the agent VMs to what is
needed, without slowing Nexlink. The owner does not want an improvised
shutdown. The constraints of the lot:

- count compute **and** disks and IP, which are billed even when the VM is
  off, plus the cost and delay of a wake-up;
- low average CPU or an idle alert alone is not enough;
- keep a governance and recovery base, avoid oscillation, explain every VM
  that stays on;
- no cloud rights for agents, no new VM, no budget increase, nothing on IMA,
  Memoria or a personal session;
- one track, lots only on an owner decision.

The canon decision exists (lots.md, « Capacité allumée au plus juste »,
28/10): `baseline_hosts` = 2, a burst host idle for 30 min is drained and
given back, and it returns by itself when work arrives.

# 2. What exists, and what does not work

Read on `origin/main` `da75ae89a` and the canon `origin/main` of 05/11.

| Piece | Where | State |
|---|---|---|
| Footprint, idle state (#4147, #4150) | `host_idle.py:19`, `:211` | works, displayed in `ameesh hosts` |
| baseline/burst | `host_idle.py:155-169` | works; no card declares `capacity`, so every host is `burst` except the guard for elected roles |
| `tarif_eur_h` → `cost_eur_h` | `host_idle.py:336-358` | display only; no card declares it (agents-3 gives the price in its body text) |
| Alerts (#4151) | `host_capacity_alerts.py:5-40` | computed on the fly, **not journaled**: zero in the event log |
| Transitions and hook (#4148) | `host_release.py` | **inert**, see below |
| Scaleway or Kubernetes adapter | — | **none**; no card declares `lifecycle_hook` |
| Anticipated wake-up | `host_idle.py:91`, `host_release.py:293-323` | a `wake_ts` within 30 min keeps a host busy; a drained host returns only when it is the only admitted host for pending work; nothing reads the queue ahead of time |
| Sizing history | `host_resources` (60 s spot readings), `host_resource_slots` (10 min) | **7-day retention** (`storage/postgres/hosts.py:265-274`); the per-turn cgroup `memory.peak` is logged (`runner.py:7905-7917`) but not stored |
| Drained host vs. reading gap (#5869) | `storage/postgres/host_lifecycle.py:70-172` | **not fixed**: a `draining` host with no hook turns `lost` on a reading gap. The #5864/#5887 commits only repair recovery after a loss |

**The blocking gap.** `host_idle.project()` always sets `runs = 0` with
`runs_source = 'aucune interface de déclaration'` (`host_idle.py:23-24`).
`runs_evidence` treats that as unknown (`host_release.py:172-186`). As a
result, `auto_drain_blocker` always answers "declared runs unknown"
(`:245-246`), and `release_blocker` also refuses `idle-drained` on a manual
drain (`:255-259`). The code says so in its own `host_release_no_runs` alert.
`host_lifecycle_transitions` has zero rows in its whole history.

The pilot's suspected gaps (a)–(d) are all confirmed. This one comes on top
of them.

**Precedent for a VM adapter.** `ci-autoscale` on nexlink-ci-1
(`scripts/ci/autoscale-reserve.sh`, canon `processus/ci-nexlink.md`) runs a
root systemd timer every 2 minutes. It uses `scw instance server
start/stop --wait`, with credentials in `/etc/ci-autoscale/*.env` (0600) on
that one host. The ameesh hook runs on whichever runner holds the scan lock.
Copying ci-autoscale as is would therefore put cloud credentials on every
runner host, which the spec forbids (§3.3: only the adapter holds cloud
rights).

# 3. Diagnostic over the available history

Window: 2026-10-29 07:00 → 2026-11-05 06:50 UTC (7 days, the full retention
of `host_resources`). Read-only psql (`default_transaction_read_only=on`)
on `turn_resources`, `host_resources`, `agent_registry`, `ameesh_events`,
`host_lifecycle*`. Hours in Europe/Paris; night = 22:00–07:00.

## 3.1 Work

| Host | Turns | Time with ≥ 1 turn | Concurrency p50 / p95 / max | Mean | Whole hours without a turn (7 days) |
|---|---|---|---|---|---|
| agents-1 | 4 851 | 80.8 % | 2 / 7 / 15 | 2.50 | 1 |
| agents-2 | 8 224 | 87.1 % | 3 / 8 / 18 | 3.19 | 0 |
| agents-3 | 2 174 | 58.1 % | 1 / 3 / 8 | 0.95 | 8 |
| **Sum** | 15 249 | 97.2 % | 6 / 16 / 33 | 6.6 | — |

- **Night (22:00–07:00).** Busy time is 75 / 89 / 58 % at night against
  84 / 86 / 58 % by day. The summed concurrency peaks at 2 a.m. (8.8 turns
  on average) and is lowest at 4 p.m. (4.9). Between 28 % and 48 % of each
  agent's turns start at night, for a night that is 37.5 % of the day.
  There is no night trough.
- **Trend.** The daily turn count is falling: 3 120 on 30/10, 1 237 on
  04/11. One week is too short to say whether this lasts.

## 3.2 Resources (per-minute maximum)

| Host | `app_cpu` cores p50 / p95 / max | Memory used GB p50 / p95 / max | PSI mem max | Free disk min |
|---|---|---|---|---|
| agents-1 | 0.54 / 2.89 / 7.85 | 6.1 / 11.2 / 16.2 | 96.6 | **0.66 GB** (29/10) |
| agents-2 | 0.89 / 2.81 / 7.57 | 7.0 / 14.2 / **28.4** | **99.5** | 6.5 GB |
| agents-3 | 0.66 / 2.24 / 5.74 | 4.3 / 10.2 / 12.4 | 44 | 38 GB |
| **Sum, same minute** | 2.6 / 5.6 / 16.5 | 19.5 / 29.3 / 45.7 (p99 37) | — | — |

- **agents-2 saturated** on 30/10 around 05:21 Paris. Its memory reached
  28 GB with PSI mem at 99 % and load 121, while `app_cpu` was 0.6 core,
  with 9 turns running. It swapped 4 GB.
- **agents-1 was under memory pressure** twice: PSI mem 96 % on 31/10
  around 15:00 and 74 % on 29/10 at 21:00.
- **Disk.** `/tmp` shares the 134 GB ext4 volume. On 29/10 agents-1 had
  only 0.66 GB free, and it has had at least 21 GB free since 02/11.
- **`ameesh heavy` leaves no history.** It holds its slots with `flock` and
  records nothing, so heavy suites show only through the load and PSI peaks
  above.

## 3.3 Would the work fit on fewer VMs?

Capacity per VM is 8 cores and 31.3 GB. The share of minutes where the
summed work would have fit (9 515 common minutes):

| Criterion | 1 VM | 2 VM |
|---|---|---|
| `app_cpu` + memory at 100 % | 94.5 % | 100 % |
| idem with 20 % margin | 82.6 % | **98.7 %** |
| `load1` + memory at 100 % | 25.6 % | 83.7 % |
| turns, at most 8 per VM | 70.3 % | 95.9 % |

`app_cpu` underestimates the need: the load is far above it because of
memory and I/O waits. `load1` overestimates it. Counting turns says nothing
about resources, because DeepSeek and Codex turns are mostly API calls.

**Reading.** Two VMs carry the measured work. One does not.

## 3.4 Roles and placement

- **agents-1**: 15 primary placements (DeepSeek implementation, the
  reviewers claude1, claude2 and codex1, the composer mty-amh-impl-01). It
  is a candidate host for the elected roles.
- **agents-2**: 20 primary placements, including the three elected roles
  (orchestrator 10.6 turns/h, mesh-design, auditeur). It also hosts the
  Nexlink implementation, docs and release agents and the GitHub watch.
- **agents-3**: 6 primary placements (ameesh-cx3, mty-amh-impl-12/13/15/18,
  the Nexlink reviewer mty-nxl-rev-02). claude1 and claude2 are also
  admitted there (41 turns in a week, mostly at night).
- The elected roles move only between agents-1 and agents-2, which are their
  candidate hosts (`[ameesh-agents-2, ameesh-agents-1]`).
- manaty-mesh-1 (DEV1-L, the mesh database) runs no agent and is out of
  scope here. pc-smichea has been `lost` since 27/10 and costs nothing on
  Scaleway.

## 3.5 Host lifecycle in the period

- Zero transitions busy/draining/idle-drained.
- One `lost → available` reconciliation, on agents-1 on 05/11 at 05:47 UTC
  (#5864, #5887).
- Two agent moves.
- No `host_idle_reserved` or `baseline_oversized` alert can be counted,
  because they are not journaled.

# 4. Prices and cost per state

Scaleway public product catalog (fr-par-1 and fr-par-2, identical), read
05/11/2026. Docs from `scaleway/docs-content@c361a512aa`. Prices exclude
VAT. Monthly figures are 730 h × the hourly price; no monthly cap is
published.

| Item | €/h | ≈ €/month |
|---|---|---|
| GP1-S (8 vCPU, 32 GB, shared) | 0.19074 | 139.24 |
| GP1-XS (4 vCPU, 16 GB, local SSD ≤ 150 GB) | 0.09282 | 67.76 |
| GP1-M (16 vCPU, 64 GB) | 0.38352 | 279.97 |
| POP2-8C-32G (dedicated, block only) | 0.290 | 211.70 |
| PRO2-S (8 vCPU, 32 GB, block only) | 0.22338 | 163.07 |
| Local SSD | 0.000049 /GB | 0.036 /GB → 5.37 for 150 GB |
| Block Storage 5K IOPS | 0.000130 /GB | 0.095 /GB → 14.24 for 150 GB |
| Flexible IPv4 | 0.005 | 3.65 |

**Behaviour that matters for the policy (docs):**

- **Standby** (`stop_in_place`, which is also what `poweroff` from inside
  the guest does) is "charged as a running Instance". It saves nothing.
- **Power off** (archive) moves the local volume to a volume store and frees
  the node. Only the volumes and the reserved IP stay billed. The archive
  "may take some time, depending on the amount of data", and no wake-up time
  is published. It must be measured (§6).
- Billing is per hour, with **at least 60 minutes per uninterrupted
  start/stop period**.
- **Resize** needs a power-off. Same family (GP1-S → GP1-XS) changes in
  place if the local disk fits. Going to POP2 or PRO2 means first moving the
  local disk to Block Storage (snapshot, export, import), which then costs
  14–19 €/month instead of 5.37.

**Cost per state of one agents VM (GP1-S, 150 GB local, IPv4), per month:**

| State | Compute | Disk | IP | Total |
|---|---|---|---|---|
| On | 139.24 | 5.37 | 3.65 | **148.26** |
| Standby | 139.24 | 5.37 | 3.65 | 148.26 |
| Powered off (archived) | 0 | 5.37 | 3.65 | **9.02** |
| Deleted | 0 | 0 | 0 | 0 (its disk and identity are lost) |

**Fleet today: 3 × 148.26 ≈ 445 €/month excl. VAT.** One caveat: the
catalog prices local SSD as a separate line. Only an invoice can confirm
that GP1 does not include it (Q4).

# 5. Options

| Option | Saving €/month | Risk to throughput | Reversible | Verdict |
|---|---|---|---|---|
| A. Idle drain of burst hosts (canon of 28/10) as is | ≈ 0 today: no idle block of ≥ 60 min | none | yes | keep the policy, repair it (C2–C4), expect nothing now |
| B. Night shutdown of a VM | ≈ 0, would cut work that runs | high | yes | rejected: no night trough |
| **C. agents-3 GP1-S → GP1-XS** | **71.48** | low: observed max 12.4 GB of 16, 5.7 cores of 4 at the peak (short) | yes, the same resize back | **recommended first target** |
| D. agents-3 archived as a burst reserve, its 6 agents moved to agents-1/2 | 139.24 (keeps 9.02) | medium to high: agents-2 already saturated; 41 agents on two VMs | yes, power on again | after C4 measurements, owner decision |
| E. agents-3 deleted | 148.26 | as D, and no reserve | no | rejected |
| F. agents-1/2 smaller | — | high (both under memory pressure) | — | rejected |
| G. Move to POP2/PRO2 | negative (more expensive) | — | — | rejected |

**Why option C and not D.**

- C keeps the three hosts, the placements, the sessions and the elected
  base. The only thing that changes is the ceiling of one host whose
  measured peak fits under it.
- D saves twice as much, but it adds 6 agents to two hosts that have
  already reached PSI mem above 96 %. The figure of 98.7 % of minutes that
  fit on two VMs comes from spot readings, not from the per-turn peaks that
  C4 will store. The Nexlink reviewer mty-nxl-rev-02 would share a VM with
  the Nexlink orchestrator.
- D becomes a sound decision if two weeks of C4 measurements confirm the
  margin, or if the turn count keeps falling (§3.1).

**Why each VM stays on.**

- agents-1 and agents-2 carry the elected roles and their failover base. In
  the canon's terms they are the `baseline_hosts` = 2.
- agents-3 is a burst host that carries work 58 % of the time, at night as
  well. It goes down a size; it does not go off.

**The CPU under option C.** GP1-XS has 4 shared cores. On agents-3,
`app_cpu` was above 4 cores only in short peaks (p95 2.24). Above that, turns
would slow down: CPU PSI rises, no turn is killed. The host gate already
holds back new turns under pressure. `max_agents` on agents-3 stays at 10.
Per-agent memory caps (6 GB per agent) stay as they are, because the
kernel is what enforces the ceiling of the host.

# 6. How to apply the first target

This is a runbook for bureau, the only one with the Scaleway rights.
Agents hold no cloud rights. The owner's go is required because it is a
change on billed infrastructure, even though it lowers the cost.

1. **Window.** Avoid the 11:00 and 23:00 releases and any batch CI in
   progress. Tell the orchestrator, because mty-nxl-rev-02 reviews Nexlink.
   Expected unavailability is 15–45 minutes; the archive time is measured,
   not assumed.
2. **Drain.** `ameesh host drain ameesh-agents-3 --for 2h`. The running
   turns finish within the 35-minute bound, and leases are refused. Check
   `ameesh host status` before going on. Add a hold
   (`ameesh hosts ameesh-agents-3 --hold --until 2h --reason …`) so the
   alerts about its agents stay quiet.
3. **Guard against a false loss: wait for #5869.** A hold does not exempt
   a host from loss detection (`detect_losses` only spares `idle-drained`
   and returning hosts). Without #5869, the reading gap of the power-off
   turns agents-3 `lost`. Its agents admitted elsewhere (impl-13, impl-18)
   would then be relocated, which loses their sessions, and reopening would
   go through `ameesh host reconcile`, as on 05/11 for agents-1. C1
   therefore runs after #5869 is delivered. Before that, it is possible
   only if the owner accepts this loss and reconciliation path.
4. **Archive and resize.** `scw instance server stop <id> --wait`, then
   `scw instance server update <id> commercial-type=GP1-XS`, then
   `scw instance server start <id> --wait`. Record the timestamps of the
   three steps: they are the first measurement of a wake-up.
5. **Health before reopening.**
   - The host is healthy: its runners are alive, `ameesh doctor` is green,
     a mail is delivered and the readings are fresh (canon §14).
   - Release the hold, then `ameesh host undrain`, then reconcile.
6. **Canon.**
   - The agents-3 card gets the type, `tarif_eur_h: 0.09282` and the disk
     and IP lines.
   - agents-1 and agents-2 get `capacity: baseline`, which settles the
     baseline without relying on the guard.
   - This goes through a canon PR.
7. **Rollback.** If two days show sustained PSI mem above 50 % or turns
   held back by the host gate for more than 10 min a day, apply the same
   gesture back to GP1-S.

# 7. Policy rules for later (once C2–C4 are delivered)

- **Hysteresis.**
  - A burst VM is powered off only after it has been idle for at least
    `max(idle_after_s, 60 min billing minimum, 2 × measured wake-up)`.
  - Once on, it stays on for at least 60 minutes.
  - Without this, a VM woken for one turn costs a full hour, plus the
    archive and restore of its disk.
- **Anticipated wake-up.** Power on ahead by the measured wake-up delay for
  three cases: a lot wake due within that delay, an actionable lot assigned
  to an agent admitted only on drained hosts, and a release window (11:00,
  23:00) whose agents are there.
- **Adapter on one host.** The hook writes a request to the database. An
  adapter outside the runners carries it out: a systemd unit holding the
  Scaleway key, on one host, in the ci-autoscale pattern. It reports back
  `busy` only after the host's fresh reading. The runners hold no cloud
  credentials.
- **No power-off on spot averages.** The decision uses the per-turn memory
  peaks and the history of C4, never one reading or the average CPU.

# 8. Proposed lots (on owner decision)

| Lot | Content | Class | Estimate |
|---|---|---|---|
| (#5869, exists, P1) | A drained or held host does not turn `lost` on a reading gap | sensitive | — |
| **C1** | Runbook §6 run by bureau: agents-3 → GP1-XS, wake-up measured, canon cards (`capacity`, `tarif_eur_h`, disk and IP). Owner go | release-like gesture, bureau | 2 h |
| C2 | Declared runs: a host card or mesh config can say `runs_interface: none`, so `runs = 0` counts as known. This unblocks `auto_drain_blocker` and `release_blocker` (§2) | sensitive (gate of release) | 0.5 d |
| C3 | Cost per state: card fields for compute, disk and IP; `ameesh hosts` shows the on/off cost; `host_idle_reserved` and `baseline_oversized` are journaled as events, so they have a history | light | 0.5 d |
| C4 | Sizing history: store the per-turn cgroup `memory.peak` and CPU in `turn_resources`; keep a daily per-host aggregate (p95/max memory, `app_cpu`, concurrency, PSI) for 90 days; one additive migration | sensitive (SQL) | 1 d |
| C5 | Scaleway VM adapter on one host (§7): request in the database, systemd unit with the key, fresh reading before `busy`, hysteresis and anticipated wake-up | sensitive (infra, credentials) | 1 d |
| C6 | Review after two weeks of C4: decide option D (agents-3 archived as a reserve) with measured figures | study | 2 h |

C1 depends on the owner's go and on #5869 (§6, step 3). C5 is worth
building only if C4 shows a host that is idle in blocks of 60 minutes or
more, or if option D is chosen.

# 9. Questions for the owner

- **Q1.** Go for C1 (agents-3 → GP1-XS, −71.48 €/month, 15–45 min of
  unavailability for its 6 agents in a window set by bureau)? Or go
  directly to D (−139.24 €/month, higher risk)?
- **Q2.** Is bureau the one who runs the Scaleway gesture with the existing
  key, as for ci-autoscale? And for C5, which host holds the adapter key?
- **Q3.** Should C2–C5 be built now, when they bring no saving under today's
  load, or only after C6?
- **Q4.** Can someone check one Scaleway invoice line: is the local SSD of
  the GP1 billed apart (5.37 €/month per VM) or included?
