# Pilot: the resident agent (L76, #285)

This is the measurement plan of the L76 pilot and the result of its R1 run
(Codex resident, 2026-10-25/26). It says what is measured, how, and how the
pilot is rolled back. Every figure below has its source (log line, ledger row,
thread message); none is extrapolated or announced in advance.

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

The liveness probe of the table above is a second `initialize` on the control
channel, sent outside a turn and bounded (`probe(timeout)`). The Claude CLI
answers `success` to it without a model call, so a probe that brings back no
answer is a silent resident, not a refusal. The probe is Claude-specific; the
Codex run below uses the sampler and journal as its liveness evidence.

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

## Result — Codex resident, 2026-10-25/26 (R1, lot #3389)

**Window.** T0 = 2026-10-25T02:57:20Z. B6 was due at T0+24 h
(2026-10-26T02:57:20Z), sent at 02:58:42Z, and served at **04:44:45Z** after
the Codex account budget pause; B7 followed at 04:48:25Z. The 24 h window is
therefore nominal (25 h 47 min to B6); the pause is reported as a limit, not
imputed.

**Staging.** At T0, agents-2 `venv-courant -> venv-52c74fd` (mtime
2026-10-25T01:41:03Z), runner start 02:23:19Z, Codex 0.159.3, `ameesh doctor`
OK; the bureau confirmed `52c74fd1` on both VMs since 02:55Z. It moved to
`cc93c59` at 05:37Z, then to **`771e447` (h-19)** at 13:56Z (agents-2;
agents-1 13:56:45Z); h-20 merged at 14:35:31Z (main `47bf1fe68`). The pilot
executor was epoch 3 at 14:40Z and reached epoch 5 at 15:07Z after the restart
pass; it was **epoch 11** at B6/B7.

**Coverage.** Codex-only, per the pilot decision of 2026-10-25T06:39Z. The
Claude resident (`pilot-resident-3389`) stayed outside R1 and is
**non-conclusive** for the Claude one-hour cache and Claude cost; it is to be
requalified after 2026-10-28 06:00Z (backlog). The #3411 ephemeral exclusion
was active on agents-2; the late B-1b and the three delayed Claude turns
(10:23–10:49Z, $0.472091) are outside R1.

**Incidents counted, not imputed.** #3453 freeze-designation intrusions:
6 turns / $0.267857. #3489 auto-take: 5 turns / $0.535033. Operator pause
(drain #2152): ~44 min. Codex account budget pause: 17 h 03 min
(11:41:06Z→04:44:43Z). No overage. The pre-T0 contamination (6 turns,
including B0b setup and the #3456 auto-take) is outside the window.

### Measurements

| Point | Measurement | Source |
|---|---|---|
| Memory, awake | RSS n=239 samples: min 207 / median 215 / max 309 MB; host headroom 12.0–26.9 GiB (12.0 GiB minimum, host pressure; the resident was closed at 08:12:46Z) | `pilot-rss-3389` sampler summary, B6 |
| Memory, asleep | `pids=[]` 1,273 sampler lines; longest empty run 1,202 min (~20 h, the final dormancy); the last 12 samples 04:32:28→04:43:43Z show `MemAvailable` 24.5–25.7 GiB; `ameesh show` `dormant: asleep` at 10:56Z, lease kept | sampler log, `ameesh show` |
| Cost over a day | **1.192211 USD over 17 turns** (T0→B6 start; cached_input 5,230,848); B6 checkpoint 0.085922 USD (04:44:20→04:45:29) and B7 0.061491 USD (04:48:05→04:49:05); total agent 25 turns (6 pre-T0 + 17 window + B6 + B7); no same-size one-per-event 24 h baseline | `ameesh cost turns --since T0 --json` |
| Cache on spaced turns | Codex reports `cache_read` only, never `cache_creation`: B1 117,760; B4 171,136; B5 208,640; B5b 332,672; B6 (after 20 h 51 min) 85,504; B7 103,936; the 07:53 resume read 202,752 | ledger, events |
| Latency | all turns since T0: min 24 s, median 36 s, max 270 s (the #3489 `événements` turn); checkpoint turns B1–B6 31–111 s; B6 69 s, resident PID 4091683 at 04:44:43; B7 one-per-event 60 s | `cost turns` started/recorded |
| Liveness and silence | 1,273 empty-pid samples / 239 awake samples; 5 empty runs, longest 1,202 min; `resident process stopped (resident=off)` at 04:45:30Z; B7 exact-argv check: no app-server (the grep's exec/bash hits were textual); 0 `silent between turns` / `liveness probe failed` reported in the checkpoints | journal, sampler |
| Lease | B5b controlled restart: epoch 1→2→3, old resident PID 487699 dead, new 837696; at B6/B7 **epoch 11** (control-plane restarts during the pause; no resident process outlived its lease); B6 resident PID 4091683, no process at B7 | journal, sampler, B6/B7 |
| Return | `resident=off` at 04:45:30Z; `resident process stopped (resident=off)`; B7 at 04:48:25Z: resident=off, no app-server process, one-per-event turn 04:48:05→04:49:05 (60 s), epoch 11; sampler stopped and runner stopped after B7 | journal, `ameesh show`, B7 |

### Findings

- **Mechanism works.** The resident served the checkpoint turns in one session
  family (`01a170fb`→`01a17112`→`01a171df`→`01a17686`), slept/woke (timer
  05:42:05→06:00:25; host pressure 08:12:46; budget pause 11:41:06→04:44:43),
  stayed at 207–309 MB RSS, and no process outlived the lease (B5b epoch
  1→2→3; B6/B7 epoch 11). `resident=off` at 04:45:30Z; B7 one-per-event with
  no resident process. This is the first attested run of step 5 of #1780.
- **Cost and cache.** 1.192211 USD for 17 turns over the nominal 24 h
  (cached_input 5,230,848); B6/B7 0.085922/0.061491. B6, after 20 h 51 min,
  read 85,504 cache tokens with 0 creation. Codex subscription costs are not
  Claude costs; the Claude one-hour-cache question is not answered.
- **Claude part non-conclusive** (pilot 2026-10-25T06:39Z): R1 ran on Codex
  only; the Claude resident stayed outside R1 and is to be requalified after
  2026-10-28 06:00Z (backlog).
- **Incidents counted, not imputed:** #3453 intrusions 6 turns / $0.267857;
  #3489 auto-take 5 turns / $0.535033; operator pause 44 min; Codex budget
  pause 17 h 03 min; no overage.
- **Limits of the measurement:** one test agent, one VM, staging; cost
  comparable only to a same-size session; the study's warning is kept. The
  Codex account pace cap paused the resident from 11:41Z (auditor: ~11:43Z)
  until 04:44Z, so a large part of the 24 h window has no resident turn; cost,
  cache and latency cover the turns actually served, and the pause is a quota
  limit (no overage), not a resident failure. The bounded liveness probe is
  Claude-specific; on Codex the liveness evidence is the sampler and journal
  (no silent/failure line reported).

## State

T1/T2 are merged and active in staging (`771e447`, h-19; main `47bf1fe68` after
h-20). `resident=off`; the resident process is stopped (`resident process
stopped (resident=off)` at 2026-10-26T04:45:30Z); B7 proved one-per-event with
no resident process; the sampler is stopped and the test agent's runner is
stopped after B7 (the ephemeral expires 2026-10-27 02:18Z). No resident agent
remains.
