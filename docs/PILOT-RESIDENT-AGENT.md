# Pilot: the resident agent (L76, #285)

This is the measurement plan of the L76 pilot. It says what is measured, how,
and how the pilot is rolled back. It carries no result: every figure is filled
in during the pilot with its source (log line, ledger row, thread message);
none is extrapolated or announced in advance.

The study [`design/etudes/agent-resident.md`](design/etudes/agent-resident.md)
(§ Cost and latency) leaves three points unmeasured; the pilot exists to
answer them:

1. the real cost of a resident orchestrator over a day;
2. the effect of Claude's one-hour cache on a spaced-out conversation;
3. the memory of an idle resident process.

Decision [0035](design/decisions/0035-agent-resident.md) §5 (sleep and
wake-up) and the study's risks (stuck resident, host memory, zombie process)
are what the pilot is expected to exercise.

## Scope

* **one agent**, on staging: `ameesh set <agent> resident=on`. The study
  proposes the orchestrator; the agent is named at activation, once T1 then T2
  are merged and deployed (canon §5: a merged feature behind an off switch is
  not done).
* no other resident agent during the window;
* `resident_sleep` left at its default (1800 s) unless a deviation is noted;
* the pilot adds no input queue, no authority and no conversation: those are
  L77 and later.

## What is recorded, and where it comes from

| Point | Measurement | Source |
|---|---|---|
| Memory, awake | RSS of the resident process (`VmRSS` in `/proc/<pid>/status`) just after a turn, then at +15 min and +30 min idle; host headroom | host journal, `ps` |
| Memory, asleep | host headroom and the `dormant:` status at the moment the process is closed by sleep | `ameesh list`, host journal |
| Cost over a day | cost accumulated on the agent's ledger rows over 24 h of pilot, and the number of turns served; the same figures for the 24 h of one-turn-per-event before it, for scale | ledger (`turn_costs`) |
| Cache on spaced turns | `cache_read_input_tokens` / `cache_creation_input_tokens` of the first turn after a gap over an hour, and the gap | ledger, events |
| Latency | time between an input and its answer, read against the startup cost the study measured (4–12 s per turn, per harness) | thread, events |
| Liveness and silence | answered probes; `resident silent: no answer to the liveness probe` and `silent between turns` lines; sleeps, wakes, resident restarts | host journal |
| Lease | the resident group dies with the lease (stop, epoch change, host withdrawal), checked once on purpose | host journal, `turn_resources` |

The study's own warning is kept: the token cost follows the size of the
session, not the mode. A cost difference between two windows is not read as an
effect of the resident mode unless traffic and session size are comparable —
otherwise record both and conclude nothing.

## Return (rollback)

* **Nominal return**: `ameesh set <agent> resident=off`. Between two turns the
  resident is stopped, the lease is kept, and the next turn is served one per
  event. No migration, nothing to restore: the process never outlives the
  lease.
* **Immediate stop of the pilot** if any of these is observed, keeping the
  evidence (log line, timestamp):
  * a resident group survives its lease or a host withdrawal;
  * a silent resident serves a turn up to the cap instead of being caught by
    the bounded probe — once is enough to reopen the code;
  * the memory is not given back at sleep, or the host is under pressure with
    no sleep;
  * a probe-failure series keeps growing (the backoff reached its ceiling)
    without the cause being understood.
* The return itself is recorded: date, command, log lines.

## State

T1 (PR #417) and T2 (PR #422) are not merged at the time of writing; the pilot
starts at their activation, not before. T3 prepares the bounded probe, the
silent-resident handling and this plan — the measurements belong to the pilot,
not to the preparation.
