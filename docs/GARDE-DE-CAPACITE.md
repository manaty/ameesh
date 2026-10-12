# Capacity guard: the contract

A lending host lends its unused capacity to ameesh agents without slowing
down its own application. This contract is shared by ameesh and Nexlink
Compute. ameesh publishes it, along with its JSON schemas and its golden
sets, and both sides run the same golden sets in their tests. The reasons
behind each choice are in the study
[l159-capacite-des-hotes](design/etudes/l159-capacite-des-hotes.md), section 1.

| Piece | Where |
|---|---|
| this document | `docs/GARDE-DE-CAPACITE.md` |
| schema of the state | [`docs/schemas/ameesh-host-state-1.schema.json`](schemas/ameesh-host-state-1.schema.json) |
| schema of the acknowledgement | [`docs/schemas/ameesh-host-ack-1.schema.json`](schemas/ameesh-host-ack-1.schema.json) |
| golden sets | `tests/dore/capacity_guard/` (`state.json`, `ack.json`, `offer.json`, `lendable.json`, `freshness.json`) |
| offer parser, paths, names, freshness of the state | `src/ameesh/capacity_guard.py` |
| common measure | `resources.measure` (L159d) |
| VM guard | `ameesh guard` (L159e) |

## 1. Roles

| Role | On a VM of the organisation | On a lent device |
|---|---|---|
| **Lending host** | application VM, CI VM, test VM | laptop, desktop |
| **Guard** (privileged, host side) | `ameesh guard`, a system service | the Nexlink Compute runner (macOS, Windows); `ameesh guard` on Linux |
| **Guest** (unprivileged) | classic or mediated ameesh executor, or an offloaded job | mediated executor in the agent VM |
| **Mesh** | sees the offer, the state and the measure (`ameesh hosts`), places and moves agents | the same, through `PUT /host/availability` |

The guard measures, decides and **enforces**. The kernel (cgroups) or the
hypervisor (the size of the VM) enforces the caps; the guest's good will
never does. The guest obeys the state, gives back its leases and
acknowledges.

## 2. Principles

1. **The application always wins.** The guest runs in a low-priority slice
   under hard caps. When the application needs its resources again, the
   guest gives way without waiting for the end of its turn.
2. **One format.** `ameesh-host-state/1` is extended with **optional**
   fields only. A reader ignores the fields it does not know.
   - No state value is added in version 1. An unknown state is
     unreadable, and a classic host would then fall back to `available`.
   - The acknowledgement does not change. It is closed on purpose.
   - An incompatible change opens version 2.
3. **One measure.** The same definitions apply on a VM and on a device.
   The lendable capacity is computed from what the application consumes,
   **never from what the guest consumes**. Otherwise the guest would push
   itself out.
4. **Closed by default.** On a lending host, a gate that is unreadable,
   missing or expired counts as `stopped`, even for a classic executor
   (`host_gate_fallback: stopped`).
5. **Three levels, always decreasing:** declared offer ≥ granted caps
   (`caps`) ≥ consumption. On a VM, the host's responsible reviews the offer
   in the canon. On a device, its owner consents to it in Nexlink.

## 3. The offer: `policy.lend`

On a VM, the offer is declared in the `Host` fiche of the canon and reviewed
by PR. `canon check` validates it (`capacity_guard.parse_lend`). The guard
reads it from its local configuration, and `ameesh hosts` reports any gap
between the two.

```yaml
policy:
  lend:                       # absent: the host lends nothing
    cpu_max: 3                # CPUs at most (quota), number > 0      — required
    memory_max: 14GiB         # hard cap of the whole guest           — required
    disk_max: 80GiB           # dedicated volume of the guest         — required
    cpu_weight: 10            # weight against the application, 1 to 10000 (default 100)
    agent_memory_max: 4.5GiB  # hard cap per agent or per job, ≤ memory_max
    windows: ["mon-fri 18:00-14:00 UTC", "sat-sun"]   # absent: always
    reclaim:                  # the APPLICATION's thresholds at which the guest gives way
      memory_available_min: 6GiB
      cpu_pressure_max: 20%       # PSI cpu "some", avg60, outside the guest
      memory_pressure_max: 2%     # PSI memory "some", avg60
      busy_units: ["actions.runner.*"]   # systemd unit patterns (CI jobs)
      busy_max: 4                 # requires busy_units
    drain_seconds: 90         # deadline of the withdrawal (default 90, > 0)
    calm_seconds: 600         # hysteresis before lending again (default 600)
    restart_after_seconds: 600   # ameesh guard only: units it stopped start again (L159e4)
```

- Sizes are bytes or texts such as `14GiB` or `20GB`. Percentages go
  from 0 to 100, with or without `%`.
- A window is `<days> [HH:MM-HH:MM] [zone]`. The days are `mon` to
  `sun`, as ranges (`fri-mon` wraps around) or lists (`mon,wed`). An end
  before the start runs past midnight. The zone is `UTC` (the default) or
  an IANA zone (`Europe/Paris`).
- **Fail closed.** Any unreadable key, or one of the three caps
  missing, is a `host-policy-invalid` error, and the whole offer is
  dropped. A number counts as unreadable if it is `.inf`, `.nan` or has
  more digits than a float holds, `reclaim` included: the host lends nothing until its fiche is fixed. An unknown key
  is a `host-lend-unknown` warning, and the key is ignored.
- With several fiches for one host (several canons), the lowest cap of
  each key applies.

On a device, the offer is the `ComputeOffer` its owner consented to. The
device's `Host` fiche keeps `policy.volatile` and `max_agents`.

## 4. The common measure

The guard publishes a `measure` object in the state. Every key is always
present: the schema requires each of them, and those of `psi`, `app_psi`
and `lendable` when these are objects. A value that is not measured is
`null`, never a guess, and a missing key makes the state invalid.

| Key | Definition |
|---|---|
| `ts`, `window_s` | time of the reading (epoch s); window of the CPU averages (time since the previous reading, 60 s by default). PSI values are always the kernel's avg60 |
| `cpu_count` | logical CPUs of the host (on a device: the machine's, not the VM's) |
| `app_cpu` | CPUs consumed **outside the guest**, averaged over the window: the total minus the guest |
| `guest_cpu` | CPUs consumed by the guest (`cpu.stat` of its slices, or the VM's processes) |
| `mem_total_bytes`, `mem_available_bytes`, `swap_used_bytes` | `/proc/meminfo` of the host |
| `guest_mem_bytes` | `memory.current` of the guest's slices, or the VM's memory |
| `psi` | `{cpu_some, mem_some, mem_full, io_some}` in %, for the host; `null` without PSI |
| `app_psi` | the same keys for the application's group, when it can be read |
| `disk_free_bytes` | free space of the guest's volume |
| `app_disk_free_bytes` | the lowest free space of the application's file systems |
| `tmp_path`, `tmp_fstype`, `tmp_size_bytes`, `tmp_used_bytes` | `/tmp` and its use (L73) |
| `on_ac`, `battery_percent`, `metered` | power supply and metered network; `null` on a VM |
| `user_idle_s` | seconds since the last input; `null` on a VM |
| `busy` | application activity units in progress (CI jobs); `null` without `busy_units` |
| `lendable` | `{cpu, memory_bytes, disk_bytes}`, capped by the offer; `null` when nothing could be computed |

On a VM, the guest is the cgroups `ameesh-guest.slice` and
`ameesh-agents.slice`, under the system manager and the user managers.
`AMEESH_GUEST_CGROUP` (paths separated by `:`, empty for no guest)
replaces them.

**Lendable capacity**, with the margin `h = 0.5`:

- **memory:** `min(memory_max, h × M7)`. M7 is the lowest value over
  7 days of `mem_available + guest_mem`, the current reading included;
- **CPU:** `min(cpu_max, h × (cpu_count − A7))`. A7 is the 95th
  percentile of `app_cpu` per 10-minute slot over 7 days. Without history,
  the current `app_cpu` is used. The result is rounded to 2 decimals;
- **disk:** `min(disk_max, disk_free − 2 GiB)`;
- **warm-up:** under one day of history, the guard lends at most 25 % of
  the memory and 1 CPU;
- the guard never lends a negative amount: it floors at 0;
- `guest_mem_bytes` is 0 when there is no guest and `null` when it cannot
  be read. In that case the current free memory is unknown, and only the
  history counts toward M7;
- without an offer, `lendable` is zero: the host lends nothing. The
  capacity before any offer stays visible in `ameesh hosts`.

`tests/dore/capacity_guard/lendable.json` gives the inputs (a reading, the
history `{min_free_mem_bytes, app_cpu_p95, span_s}`, the offer) and the
expected `lendable`.

## 5. The state: `ameesh-host-state/1`

The guard writes the state, and the guest reads it. `schema`, `state`
(`available`, `draining` or `stopped`) and `seq` (an integer that grows at
each write) are required. A capacity guard (a state with `guard`) also
always writes `expires_ts` (section 5.3). Every other field below is
optional, and a reader ignores those it does not know. The sizes in `caps`
(`*_mb`) are in MiB (section 5.4).

| Field | Meaning | An older reader |
|---|---|---|
| `until_ts` | the nearer of two ends: the end of the window and the end of the job (`max_job_s`) | unchanged |
| `drain_deadline_ts` | in `draining`, turns are preempted past it | unchanged |
| `reason` | why (section 5.1) | unchanged |
| `caps.max_concurrent` | the guest claims within `min(max_concurrent, max_agents)` | unchanged |
| `caps.cpu_share` | share of the **host's** CPUs (0 < x ≤ 1): quota `cpu_share × cpu_count` | unchanged |
| `caps.memory_mb` | hard cap of the **whole** guest: the actual envelope, not the offer | unchanged |
| `expires_ts` | the state is no longer valid from this instant. Required from a capacity guard, at most 300 s ahead; the guard rewrites it every third of its life (30 s by default). On a lending host, an expired, missing or null `expires_ts` counts as `stopped` (section 5.3) | ignores it; the kernel caps stay in place |
| `caps.cpu_weight`, `caps.io_weight` | weight against the application (1 to 10 000) | ignored |
| `caps.memory_high_mb` | soft cap: above it, the kernel reclaims the guest's memory first | ignored |
| `caps.agent_memory_mb` | hard cap per agent or per offloaded job | ignored |
| `caps.disk_mb`, `caps.tmp_mb`, `caps.max_job_s` | disk, `/tmp` and duration of the guest | ignored |
| `caps.enforced` | the caps the guard enforces itself (kernel, hypervisor); the others are indicative | ignored |
| `freeze` | `{since_ts, until_ts}`: turns frozen by the guard (tier 2), 60 s at most. The executor does not count this time in its turn deadlines | ignored; the freeze stays below the deadlines |
| `windows` | the next lending windows, `[{start_ts, end_ts}]`, for planning | ignored |
| `offer` | the offer in force (section 3): `cpu_max`, `memory_max`, `disk_max` in bytes, … | ignored |
| `measure` | the common measure (section 4) | ignored |
| `guard` | `{kind: "ameesh-guard" or "nexlink-compute", version, host_kind: "vm" or "device"}` | ignored |

`ameesh-exec-availability/1` relays the same additions to the server, which
already drops unknown fields.

### 5.1 Reasons

Free text is admitted. The following values have a fixed meaning:

- devices: `idle`, `user_active`, `battery`, `window_end`, `metered`,
  `owner_pause`, `lease_lost`, `shutdown`. The last three replace a
  misleading `revoked`;
- VMs: `app_memory`, `app_cpu`, `app_busy` (CI jobs), `disk_low`,
  `maintenance`, `guard_stopping`.

### 5.2 Paths

- `/run/ameesh-gate/state/state.json`: the state. Its directory is
  read-only for the guest;
- `/run/ameesh-gate/ack/ack.json`: the acknowledgement. Its directory is
  writable by the guest. The guard reads `ack.json` only: the guest may
  keep its own files there (the classic executor keeps `slots/`, which
  counts `caps.max_concurrent` across all its executors on the host, and
  `parts/`, one part of the ack per executor, merged into `ack.json`).

Both files are written to a temporary file, then renamed.

### 5.3 Freshness

A guard that dies must not leave behind a state the guest keeps obeying.
On a **lending host** (`host_gate_fallback: stopped`), the guest obeys the
state it read at `now` only if all of these hold, and counts it as
`stopped` otherwise:

- the document is readable, of schema `ameesh-host-state/1`, with a known
  `state` and an integer `seq`;
- `expires_ts` is a finite number: **missing or `null` is stale**, and so
  is a number with more digits than a float holds;
- `now < expires_ts`: a state expiring at `now` is expired;
- `expires_ts ≤ now + 300 s`: further ahead, a dead guard's `available`
  would outlive it. A guard writes `now + 90 s` by default.

On a **classic host** (no capacity guard), an absent or `null`
`expires_ts` is admitted, because the gate predates it; a state that is
otherwise unreadable or stale falls back to `available`, as before.
`capacity_guard.gate_state` implements this rule, and
`tests/dore/capacity_guard/freshness.json` gives its cases. The rule
compares the guard's instants with the guest's clock: on a device, the
agent VM's clock must follow the host's, since a skew of more than a few
seconds misjudges freshness.

### 5.4 Units of the caps

`memory_mb`, `memory_high_mb`, `agent_memory_mb`, `disk_mb` and `tmp_mb`
are **MiB** (2^20 bytes): `memory_mb: 4096` is 4 294 967 296 bytes. A guard
converts bytes to these caps rounding **down** (`bytes // 2^20`), so a cap
never exceeds the capacity it stands for. systemd reads them with the
suffix `M`, which is also 2^20 bytes (`MemoryMax=4096M`).

## 6. Giving way: the tiers

The guard evaluates its signals every 5 s. It reacts on 10-second averages
and decides on 60-second ones. It goes up one tier as soon as a `reclaim`
threshold is crossed. It comes back down only after `calm_seconds` below
every threshold, and increments `seq` each time.

| Tier | State written | What the guard does | What the guest does |
|---|---|---|---|
| **0 — lend** | `available`, `caps` = lendable | low-weight slice, CPU quota, `MemoryHigh`, `MemoryMax`, `MemoryMax` per agent | claims within `max_concurrent` |
| **1 — reduce** | `available`, `caps` lowered | `max_concurrent` brought down to the turns in progress; quota lowered; `MemoryHigh` brought down to current use | starts no turn beyond |
| **2 — freeze** (CPU or I/O only) | `available` + `freeze` | freezes the agents' units and jobs, 60 s at most. A freeze frees no memory, so it is never used for memory | nothing during the freeze; the 90 s lease survives it; frozen time is not counted |
| **3 — withdraw** | `draining`, deadline at `drain_seconds` | thaws, then waits for the acknowledgement | stops its turn at a safe point, background work included; gives back its leases; acknowledges `drained: true` |
| **4 — stop** | `stopped` | 30 s after the deadline without `drained`: kills, then stops the slice | stops at once |

A stopped guard leaves its last caps in place (they are properties of the
slice), and its state expires at `expires_ts`. Safety nets that do not
depend on the guard: the slice's `MemoryMax` keeps OOM kills inside the
guest, and `OOMScoreAdjust=500` makes the global OOM killer pick the
guest's processes first.

## 7. Caps: how they are enforced

| Cap | Linux cgroups v2 | systemd | Device (agent VM) |
|---|---|---|---|
| `cpu_share` | `cpu.max` | `CPUQuota=` (share × CPUs × 100 %) | vCPUs of the VM, `--cpus` of the container |
| `cpu_weight` | `cpu.weight` | `CPUWeight=` | priority of the VM process |
| `memory_mb` | `memory.max` of the slice | `MemoryMax=` | memory of the VM, `--memory` of the container |
| `memory_high_mb` | `memory.high` | `MemoryHigh=` | — |
| `agent_memory_mb` | `memory.max` of the agent's unit | `MemoryMax=` on the agent's unit | `--memory` of the container |
| `io_weight` | `io.weight` | `IOWeight=` | — |
| `disk_mb` | size of the dedicated volume | `ReadWritePaths=` limited to the volume | disk of the VM |
| `tmp_mb` | private `/tmp` on the volume | `PrivateTmp=yes`, `TMPDIR` | tmpfs of the container |
| freeze | `cgroup.freeze` | `systemctl freeze` / `thaw` | pause of the VM (optional) |
| forced withdrawal | `cgroup.kill` | `systemctl kill`, `stop` | stop of the VM |

## 8. The acknowledgement: `ameesh-host-ack/1`

The guest writes it. It does not change, and it is **closed**: a reader
admits exactly these keys, all required.

| Key | Meaning |
|---|---|
| `schema` | `ameesh-host-ack/1` |
| `seq` | the `seq` of the state acknowledged |
| `state` | that state |
| `in_turn` | agents still in a turn |
| `held` | agents still holding a lease |
| `drained` | no turn and no lease left |
| `ts` | time of the acknowledgement |

The device reader that admitted only `schema`, `seq`, `drained` and
`agents_in_turn` refused this acknowledgement. `ack.json` records both
shapes, so the readers converge. Offloaded jobs do not acknowledge. The
guard sees them as scopes in the guest's slice and stops them itself at the
deadline.

### 8.1 The ameesh guest

The classic ameesh executor obeys the gate (`ameesh/host_gate.py`, L159f;
settings in [EXPLOITATION.md](EXPLOITATION.md#host-gate-l159f)):

- its freshness rule is `capacity_guard.state_problem`, the rule of
  section 5.3, checked against `freshness.json`;
- a guest runner on a lending host (Host fiche with `policy.lend`) has a
  gate by default, closed when the state is absent, unreadable or stale;
  native runners exclude that guest gate by default (#501), preserving
  explicit overrides; EACCES/EPERM on ack removes a native gate but keeps
  a guest closed until a successful ack;
- in `draining`, a turn stops at its next safe point, and at the latest at
  `drain_deadline_ts`; the background work of finished turns stops at that
  deadline too, and `drained` is `true` only once no lease and no such work
  is left;
- `freeze` is read as an interval: the guard should keep it in the state
  until its next change, since the executor may only read it once thawed;
- each resource reading records the state obeyed, and relocation never
  chooses a destination that is not `available`.

## 9. Golden sets, and changing the contract

Each file of `tests/dore/capacity_guard/` carries `"schema":
"ameesh-capacity-golden/1"` and its `family`:

- `state.json`, `ack.json`: `valid` and `invalid` documents, each with
  its `why`. Every valid document matches its schema, and every invalid
  one does not;
- `offer.json`: an offer as written in a fiche (`lend`), and the offer read
  from it, or the problems that drop it;
- `lendable.json`: the constants of section 4, then the inputs and the
  expected `lendable` of each case;
- `freshness.json`: `max_state_ttl_s`, then a state document (or `null`),
  the instant it is read, whether the host lends, and the state the guest
  obeys (section 5.3).

`tests/test_l159a_capacity_guard.py` checks them in ameesh against the
schemas, the offer parser, the freshness rule and the formula of section 4, which it computes
independently of `resources`. Its JSON Schema validator implements only
the keywords the schemas use, so a new keyword fails the test until the
validator learns it. Nexlink Compute runs the same files in its tests.

To add a field, open a PR on this contract with the field, its schema and
its golden case. ameesh's reader ignores the field until it knows it. A
field that changes the meaning of an existing one, a new state value, or a
change to the acknowledgement opens version 2 (`ameesh-host-state/2`) and
needs the trailer `ameesh-breaking:` (CONTRIBUTING.md, version numbering).
