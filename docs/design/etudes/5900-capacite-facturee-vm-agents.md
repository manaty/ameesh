---
type: Study
title: "#5900 — billed capacity of the agent VMs, fitted to the work that actually runs"
description: "Owner request of 05/11/2026 relayed by bureau (manaty/ameesh#4489): a process that fits the cost of ameesh-agents-1/2/3 to what is needed, without slowing Nexlink. Seven-day diagnostic from the mesh database, state of #4147/#4148/#4150/#4151 (release path inert end to end), sourced Scaleway prices including disks and IP, cost per state, options compared (no resize fits the load of 05/11, the only out-of-rule saving is archiving agents-3, −139 €/month at a high risk), hysteresis rules, proposed lots C1–C6, one owner question and two operator questions. No code, no machine gesture."
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

1. **Expect little from shutting VMs down when they go idle, today.** Over
   the last seven days, agents-1 had 1 whole hour without a turn, agents-2
   none and agents-3 8 (about one a day). The night carries as much work as
   the day. An hour without a turn is not yet an idle hour: mail, a lot wake
   or a lease can still keep the host busy. Even if all 8 hours of agents-3
   were idle, the ceiling is about 35 h a month × 0.19074 € ≈ **6.6 €/month**.
   That is before the 60-minute billing minimum per start/stop period and
   the minutes it takes to archive and restore a local disk. The 30-minute
   drain rule of 28/10 is still worth repairing and running (point 4). Its
   saving today is small.
2. **The lever is the size of the fleet, not its schedule.** Summed minute by
   minute, the work of the three VMs fits on two VMs 98.7 % of the time with
   a 20 % margin, but not on one (memory p99 37 GB, max 45.7 GB). agents-3 is
   the least loaded over the week (mean concurrency 0.95 turn, memory max
   12.4 GB). It hosts no elected role.
3. **No resize fits today's load.** The smaller type in the same family,
   GP1-XS (4 vCPU/16 GB), would save 71.48 €/month on one VM, but neither
   candidate fits it (§3.6):
   - claude1 and claude2 moved to agents-3 on 05/11 (#5841). With their
     measured memory added minute by minute, agents-3 would have peaked at
     18.1 GB, and spent 441 minutes above 80 % of 16 GB in a week.
   - agents-1 without them still peaks at 16.3 GB, with 238 minutes above
     4 cores, and it is the failover host of the elected roles.
   - Between GP1-XS and GP1-S there is no other size. The other 8 vCPU/32 GB
     x86 types cost more than GP1-S (§4).
4. **Repair and run the existing rule now, without waiting.** The release
   half of #4148 is inert end to end (§2). The lots that apply the rule of
   28/10 (C2–C5) need no new owner decision, as the pilot noted. They start
   now in the order of §8. Only option D (C1) is outside the rule.
5. **The one large saving is outside the rule, and risky: option D.**
   Archiving agents-3 as a burst reserve and moving its agents to
   agents-1/2 saves ≈ 139 €/month (31 % of the fleet). agents-2 alone
   already saturated its memory on 30/10. Moving eight more agents,
   including three Nexlink reviewers, onto agents-1/2 could slow Nexlink.
   This study does not recommend it at today's load. The owner can choose
   it (Q1). Otherwise C6 re-runs the sizing on C4's per-turn peaks when the
   load falls, and nothing waits for it.

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
- one track, with no automatic sub-lots. A gesture outside the rule of
  28/10 (a resize, a new expense) goes to the owner. The lots that apply
  that rule follow this study once reviewed (pilot's note, 05/11 06:50,
  #192699).

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
  admitted there (41 turns in the week, mostly at night). They have run
  there since 05/11 01:19Z and 04:11Z (#5841, §3.6).
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

## 3.6 Load added on agents-3 on 05/11

claude1 and claude2, two Claude agents of Nexlink, moved from agents-1 to
agents-3 on 05/11, at 01:19Z and 04:11Z (#5841, note of the pilot from
bureau, #192812). The week above contains almost none of their work on
agents-3.

- **Since 01:19Z on agents-3:** CPU max 2.2 cores, memory max 7.8 GB, 51
  turns for the two of them. Only 254 of the 365 minutes have a reading.
  That window is too short to size a host on.
- **Projection.** Each runner's memory (`mem_current_bytes`) is read from
  the `cgroup` field of `host_resources`. Adding claude1 + claude2 (p95
  3.3 GB, max 7.8 GB) to agents-3 minute by minute over the week gives:
  memory p95 12.6 GB, max **18.1 GB**, and **441 minutes above 12.8 GB**,
  which is 80 % of a GP1-XS. CPU per runner is not recorded, so the CPU
  projection is missing. It can only be higher than §5's 73 minutes above
  4 cores.
- **agents-1 without the two.** Memory p95 10.9 GB, max 16.3 GB,
  179 minutes above 12.8 GB. `app_cpu` was above 4 cores for 238 minutes,
  an upper bound because it still counts the two agents.

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
that GP1 does not include it (Q3).

# 5. Options

| Option | Saving €/month | Risk to throughput | Reversible | Verdict |
|---|---|---|---|---|
| A. Idle drain of burst hosts (canon of 28/10), repaired | ≤ 6.6 on agents-3 (8 h without a turn in 7 days, upper bound), ≈ 0 on agents-1/2 | none | yes | apply the rule now (C2–C5); small saving today, larger if the load keeps falling |
| B. Night shutdown of a VM | ≈ 0, would cut work that runs | high | yes | rejected: no night trough |
| C. agents-3 GP1-S → GP1-XS | 71.48 | high since 05/11: projected memory max 18.1 GB of 16 with claude1/2 (§3.6), CPU above 4 cores 73 min a week without them | yes, the same resize back | rejected at today's load |
| C'. agents-1 GP1-S → GP1-XS | 71.48 | high: max 16.3 GB, 238 min above 4 cores, failover host of the elected roles | yes | rejected |
| D. agents-3 archived as a burst reserve, its 8 agents moved to agents-1/2 | 139.24 (keeps 9.02) | high: agents-2 already saturated; all 41 agents on two VMs, three Nexlink reviewers among the movers | yes, power on again | owner decision (Q1), not recommended at today's load |
| E. agents-3 deleted | 148.26 | as D, and no reserve | no | rejected |
| F. agents-1/2 smaller | — | high (both under memory pressure) | — | rejected |
| G. Move to POP2/PRO2 | negative (more expensive) | — | — | rejected |
| H. BASIC2-A8C-32G (8 vCPU/32 GB, 100.59 €/month) | 38.65 per VM | arm64: a new platform (images, binaries, Playwright), not a resize | rebuild | out of scope (new VM) |

**Why no resize, and why D is not recommended.**

- Neither C nor C' fits (§3.6). A host at 18 GB on 16 GB swaps or throttles
  its turns, and three Nexlink reviewers run on agents-3.
- D saves the most, but it adds eight agents to two hosts that have already
  reached PSI mem above 96 %. The figure of 98.7 % of minutes that fit on
  two VMs comes from spot readings, before claude1/2 moved. Nexlink
  reviewers would share a VM with the Nexlink orchestrator.
- D and a resize become sound once C4 has stored per-turn peaks and the
  load falls. The daily turn count went from 3 120 on 30/10 to 1 237 on
  04/11 (§3.1). C6 re-runs the sizing then. The owner can still take D
  earlier (Q1).

**Why each VM stays on.**

- agents-1 and agents-2 carry the elected roles and their failover base. In
  the canon's terms they are the `baseline_hosts` = 2.
- agents-3 is a burst host that carries work 58 % of the time, at night as
  well, and since 05/11 three Nexlink reviewers. It is drained and powered
  off by the repaired rule (C2–C5) when it is idle, and it stays on
  otherwise.

**CPU on agents-3 against 4 cores (for C, and for any later resize).**
Over 7 days (9 611 minutes, 29/10 07:13 → 05/11 07:12 UTC, a window one
hour later than §3.2), agents-3 had `app_cpu` above 4 cores for 73 minutes
(0.76 %), above 3.2 cores for 174 minutes, and a maximum of 6.07 cores,
before claude1/2 arrived.

# 6. Runbook for an out-of-rule gesture (D, or a later resize)

This is a runbook for bureau, the only one with the Scaleway rights.
Agents hold no cloud rights. The owner's go is required because it is a
change on billed infrastructure, even though it lowers the cost. It is
written for agents-3. For D, step 4 stops after the power-off, and the
agents are moved first, with `ameesh agent move` and a canon placement PR.

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
   go through `ameesh host reconcile`, as on 05/11 for agents-1. The
   gesture therefore runs after #5869 is delivered. Before that, it is possible
   only if the owner accepts this loss and reconciliation path.
4. **Archive (D) or resize.** `scw instance server stop <id> --wait`
   archives the host; D stops here. A resize goes on with
   `scw instance server update <id> commercial-type=<type>`, then
   `scw instance server start <id> --wait`. Record the timestamps of each
   step: they are the first measurement of an archive and a wake-up.
5. **Health before reopening.**
   - The host is healthy: its runners are alive, `ameesh doctor` is green,
     a mail is delivered and the readings are fresh (canon §14).
   - Release the hold, then `ameesh host undrain`, then reconcile.
6. **Canon.**
   - The agents-3 card gets its new state (type and `tarif_eur_h`, or
     archived) and the disk and IP lines.
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
- **No power-off on spot averages.** The decision uses the idle state of
  #4147 (turns, leases, runs, mail, wakes), never one reading or the
  average CPU. Sizing decisions (D, a resize) use measured peaks: today the
  per-minute maximum of `host_resources`, and the per-turn peaks once C4
  stores them.
- **Wake-up delay before it is measured.** C5 starts with a default of
  15 minutes for the anticipation and the hysteresis. It replaces the
  default with the delay measured at its own first wake-up.

# 8. Proposed lots

The pilot's note of 05/11 06:50 on the lot, agreed with bureau (#192699),
says the rule of 28/10 needs no new decision. The lots that apply it, C2–C5,
follow this study once reviewed, with no owner round trip. C1 (option D) is
not in the rule, so it goes to the owner (Q1). No resize is proposed
(§5).

| Lot | Content | Class | Estimate |
|---|---|---|---|
| (#5869, exists, P1) | A drained or held host does not turn `lost` on a reading gap | sensitive | — |
| C1 | Only on the owner's go for D (Q1): move the 8 agents of agents-3 to agents-1/2 (placement PR), then the runbook §6 run by bureau (archive, wake-up measured), canon cards | release-like gesture, bureau | 3 h |
| C2 | Declared runs: a host card or mesh config can say `runs_interface: none`, so `runs = 0` counts as known. This unblocks `auto_drain_blocker` and `release_blocker` (§2) | sensitive (gate of release) | 0.5 d |
| C3 | Cost per state: card fields for compute, disk and IP, and `capacity: baseline` on agents-1/2; `ameesh hosts` shows the on/off cost; `host_idle_reserved` and `baseline_oversized` are journaled as events, so they have a history | light | 0.5 d |
| C4 | Sizing history: store the per-turn cgroup `memory.peak` and CPU in `turn_resources`; keep a daily per-host aggregate (p95/max memory, `app_cpu`, concurrency, PSI) for 90 days; one additive migration | sensitive (SQL) | 1 d |
| C5 | Scaleway VM adapter on one host (§7): request in the database, systemd unit with the key, fresh reading before `busy`, hysteresis and anticipated wake-up, default wake-up delay until measured | sensitive (infra, credentials) | 1 d |
| C6 | Sizing review on C4's per-turn peaks, CPU per runner included, once the daily turn count stays below the week of §3.1 for a week, or on the owner's request: resize or D, with figures | study | 2 h |

**Order, with no wait on a duration.**

1. **Now, in parallel:** #5869 (already P1), C2, C3 and C4. They do not
   depend on each other.
2. **C5** follows as soon as C2 and #5869 are merged. The release path must
   reach `idle-drained`, and a powered-off host must not turn `lost`. C5
   does not wait for C4 or C6.
3. **C1** exists only if the owner chooses D (Q1). It needs #5869 (§6,
   step 3) and is independent of C2–C5.
4. **C6** blocks nothing. It is triggered by measured load, not by a fixed
   delay.

# 9. Questions

For the owner (only the target, its cost, its risk and its interruption,
as the pilot asked):

- **Q1.** Option D: archive agents-3 and move its 8 agents (three of them
  Nexlink reviewers) to agents-1/2, for −139.24 €/month excl. VAT. The
  risk is slower Nexlink turns on hosts that have already saturated their
  memory, and the interruption is 15–45 min for those agents. This study
  recommends **no** at today's load. The alternative is to apply the
  existing rule now (C2–C5, a few €/month today) and let C6 re-run the
  sizing when the load falls.

For the operator (bureau), not for the owner:

- **Q2.** Is bureau the one who runs the Scaleway gesture with the existing
  key, as for ci-autoscale? And for C5, which host holds the adapter key?
- **Q3.** Can someone check one Scaleway invoice line: is the local SSD of
  the GP1 billed apart (5.37 €/month per VM) or included?
