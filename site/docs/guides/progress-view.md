# Progress view

`ameesh progress` shows the real-time state of a project: **lots** on a
timeline (requested → frozen → verdict → merged), **agents** (harness, model,
effort, state, current task), **milestones** and **budget**. It is fed by what
ameesh records itself (lot transitions, gated actions, turns and leases,
the cost ledger); neither git nor any issue tracker is read.

```bash
ameesh progress [--project P] [--since 24h]            # text, fits a narrow terminal
ameesh progress --json [--project P] [--since 24h]     # schema ameesh-progress/1
ameesh progress --html FILE [--project P] [--since 24h]
```

- `--since`: start of the window, a duration (`90m`, `16h`, `2d`) or an ISO
  8601 date (`2026-10-05`, `2026-10-05T08:00Z`; without a time zone = local
  time). Default `24h`.
- `--project P`: lots whose app or workstream is `P`, the project's actions,
  agents whose project or team is `P`, and their costs.
- `--html FILE`: a **standalone** static page (inline CSS and script, embedded
  data, `default-src 'none'` policy: no network request, no font, no CDN),
  readable on a phone. No server: open the file, or copy it anywhere. Written
  atomically; run the command again to refresh it.

Exit codes: 0; 1 (database unreachable, schema missing, SQL error); 2 (unreadable
option).

## Lot milestones

Milestones are **declared**, not guessed. `requested` and `merged` are
recorded automatically; `frozen` (branch frozen for review) and `verdict`
(`ok` or `blocked`, with the reviewed commit) are declared:

```bash
ameesh work milestone <id> frozen --sha <commit>
ameesh work milestone <id> verdict ok --sha <commit> [--note …]
ameesh work list         # column DÉLAI: phase and age
ameesh work show <id>    # the full timeline: requested → frozen → review → merged
```

When a lot has no declared freeze or verdict, they are deduced from its
transitions (first entry into `qa`, exit from `qa`, approval of its `git-merge`
action…).

## The JSON schema `ameesh-progress/1`

| Key | Meaning |
|---|---|
| `schema` | `"ameesh-progress/1"` |
| `generated_ts`, `generated_at` | time of the snapshot |
| `host`, `project`, `window` | who produced it, the filter, the time window |
| `lots[]` | id, title, `state` (`active`, `review`, `blocked`, `approved`, `merged`), raw `work_state`, `milestones` `{requested, frozen, verdict, merged}`, `verdict`, `blocked_verdicts`, `blocks`, `assignee`, `reviewer`, `qa_loops`, budget, linked `actions` |
| `agents[]` | name, harness, model, `effort`, `state` (`working`, `idle`, `paused`, `stopped`), `since_ts`, current `turn` `{started_ts, duration_s, label, task}`, turns, unread, lease |
| `milestones[]` | project milestones declared in the canon (always empty in v1: the v1 profile declares none) |
| `actions[]` | actions changed in the window or not terminal |
| `budget` | `spend` `{window, 1h, 24h}`, `by_agent`, subscription gauges `plans`, `hourly_cap_usd`, `paid_harnesses` |
| `truncated` | render bounds reached (500 lots, 500 actions) |
| `missing` | what the view cannot read (yet), in plain words |

Conventions: instants are UTC epoch seconds (`_ts`) or `null`; durations in
seconds (`_s`); amounts in US dollars (`_usd`). Data ameesh does not have is
`null` or an empty list, **never guessed**. Fields can be added within
version 1; removing a field or changing its meaning moves to
`ameesh-progress/2`.

!!! note "Gauges are local"
    Subscription gauges are read from the harness logs **of the host that
    produces the snapshot**, without history in the database.
