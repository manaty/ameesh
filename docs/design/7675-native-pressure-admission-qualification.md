# E3b — native pressure/admission qualification and bounded deferral observation

Design note for [#7675](https://github.com/manaty/ameesh/issues/5868), tranche
**E3b** of the [IMA public-contract mapping](ima-public-contract-mapping.md),
under [#5827](https://github.com/manaty/ameesh/issues/5827). The lot delivers
two things and deliberately refuses a third:

1. `ameesh-deferral/1`, a bounded structured block that says why a turn is
   held ([API contract](../api/deferral-observation-v1.md));
2. the native pressure and admission **qualification witness** for the two IMA
   overrides, with the missing declarative policy keys added only where the
   witness proves a gap;
3. **not** a replacement of `_tx_lock`, a `runner.Runner`/`AgentWorker`
   subclass, or a permanent document priority class.

The consumer is IMA `develop` at `23e0a944bc82647e929a2cb96b4af3245030354c`
(the frozen inventory SHA of the mapping); its chart values are what the
witness replays. The removal of IMA's private
`document-container-pressure.py`, `PriorityAdmission` and
`install_worker_diagnostics` remains tracked by
[IMA#1803](https://github.com/thepackengers/issue-manager-agent/issues/1803).

## Pressure: IMA's gate against the native `own_cgroup` measures

IMA's private `ContainerPressure` reads the container cgroup and computes:
CPU = usage rate / `min(cpu_limit, cpu.max quota)`; memory =
`memory.current / memory.max`; throttling = `nr_throttled / nr_periods` (the
share of **periods**, not of wall time). Its chart declares `cpu 0.90`,
`memory 0.85`, `throttled periods 0.25`, requests 500m/1Gi, limits 4 CPU/4Gi.

`tests/test_7675_qualification.py` replays those formulas on a synthetic cgroup
v2 tree and compares the native reading and verdict, without importing IMA.
The parity result at the tranche:

| IMA gate | Native measure before #7675 | Native threshold before | Verdict |
| --- | --- | --- | --- |
| CPU 0.90 | `cpu_used` (CPUs) only; **no ratio to the quota** | none | **gap**: same reading, no native ceiling |
| Memory 0.85 | `mem_used = memory.current / memory.max` | `max_cgroup_memory_used`, default 90 % | parity (declared 85 % gives IMA's verdict) |
| Throttled periods 0.25 | `throttled` = throttled **wall time** / elapsed | `max_cgroup_throttled`, default 50 % | **gap**: different fact, not a rename |
| Requests 500m/1Gi | not read | not a threshold | out of scope: requests are a scheduling input, and the native policy only names ceilings |

The wall-time vs period distinction is not cosmetic: one throttled microsecond
counts a whole period, so a container can show 10 % wall time and 30 % periods.
Reinterpreting `max_cgroup_throttled` as the period ratio would have silently
changed the meaning of every existing Host fiche, so the tranche adds keys
instead:

```yaml
policy:
  resources:
    pressure_scope: own_cgroup
    max_cgroup_cpu_used: 90%             # no default; absent = unwatched
    max_cgroup_throttled_periods: 25%    # no default; absent = unwatched
```

Both accept a fraction or a `%` text like the other cgroup ceilings, are
critical halfway to 1.0, and produce no breach on an unknown reading. The
ratio `cpu_used_ratio = cpu_used / cpu_quota` is `null` without a finite
`cpu.max` — never a guessed 100 %. A host that declares neither key keeps the
previous behaviour exactly (the `own_cgroup` fallback to `host`, with
`scope_fallback`, is unchanged and still visible).

## Admission: IMA's permanent class against the native lock

IMA's `PriorityAdmission` gives every document turn a **permanent** priority
class over role/background work. The native
`admission_lock.PriorityRLock` (shared-connection admission, on by default) has
no permanent class: a woken turn is `CONTROL`, ordinary work is `NORMAL`, FIFO
within a class, aging promotes an old waiter, and `BOOST_BURST` bounds how many
consecutive `CONTROL` grants may overtake a `NORMAL` one.

The witness pins the acceptance case the mapping asks for:

* an awakened document (`CONTROL`) passes before already-waiting role and
  background passes (`NORMAL`), and two awakened passes stay FIFO;
* background latency is bounded by `BOOST_BURST` even under a continuous
  stream of awakened work (no starvation, no permanent document class);
* aging lets an old background waiter go first once `AGING_S` elapses.

Conclusion: the native lock **qualifies as-is** for document-vs-role latency
and fairness. No document class, no lock pointer and no change to
`admission_lock` are added by this lot. IMA's own `PriorityAdmission` and its
`_tx_lock` attachment can be removed under IMA#1803 once it accepts this
witness.

## Deferral observation

The runner persists the reason a turn is held in `agent_registry.status` /
`status_text` (and the operator-pause columns); IMA's diagnostics re-read those
private strings and its ledger. `ameesh deferral_observation` maps the same
facts into **one** `ameesh-deferral/1` block with a closed `reason` enum and no
free text, no prompt and no payload. It is published by `ameesh list/show
--json`, `GET /api/v1/agents[/{agent}]` and `ameesh-turn-observation/1`
(`ameesh turn observe`). An unrecognised `blocked` text is `other`, never a
guessed cause; a new cause needs a contract revision, not a longer regex.

### Ambiguous accounting is retained

`document_scheduling.py` overrides `_compta_ecrit`, `budget_ok` and
`_held_by_pressure`; the mapping requires that ambiguous ledger evidence keep
the native reservation authoritative. The block therefore never restates
accounting: when the ledger is ambiguous, `accounting.state` stays `unknown`,
the native reservation is kept, and the deferral block only says why the turn
is held. It never claims the work failed, succeeded or was settled, and
`quiescent` stays false. The test inserts an open reservation and a request
with a secret payload, then checks that the observation keeps
`accounting.state == "unknown"`, carries the deferral block, and leaks neither
the body nor the payload.

## Non-goals and follow-ups

* No IMA code or production change in this lot (IMA removal: IMA#1803).
* No `runner.Runner` / `AgentWorker` subclass, no `_tx_lock` replacement, no
  arbitrary runner callback: the public boundary stays declarative
  configuration plus observation.
* The new keys are opt-in; no existing Host fiche changes verdict. If IMA
  wants its chart values, it declares them (or a later tranche compiles them
  into its profile); ameesh sets no IMA-specific default.
* Requests (`500m/1Gi`) are not thresholds and are not added to the resource
  policy: they belong to the consumer's scheduling profile.
