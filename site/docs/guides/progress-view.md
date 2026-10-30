# Progress view

`ameesh progress` shows the real-time state of a project: **work items** on a
timeline with development milestones (requested → frozen → verdict → merged), **agents** (harness, model,
effort, state, current task), **milestones** and **budget**. It is fed by what
ameesh records itself (work item transitions, gated actions, turns and leases,
the cost ledger); neither git nor any issue tracker is read.

```bash
ameesh progress [--project P] [--since 24h] [--no-gantt] [--no-versions]
ameesh progress --json [--project P] [--since 24h]     # schema ameesh-progress/1
ameesh progress --html FILE [--project P] [--since 24h]
ameesh progress --serve [--port 8765] [--project P] [--since 24h]
```

- `--since`: start of the window, a duration (`90m`, `16h`, `2d`) or an ISO
  8601 date (`2026-10-05`, `2026-10-05T08:00Z`; without a time zone = local
  time). Default `24h`.
- `--project P`: work items whose app or workstream is `P`, the project's actions,
  agents whose project or team is `P`, and their costs.
- `--html FILE`: a **standalone** static page (inline CSS and script, embedded
  data, `default-src 'none'` policy: no network request, no font, no CDN),
  readable on a phone. No server: open the file, or copy it anywhere. Written
  atomically; run the command again to refresh it.
- `--serve` (L304): the same page served on `127.0.0.1` only, read-only, with
  its JSON at `/api/progress.json` and a **Refresh** button. No network
  resource, no external request.
- `--no-versions` (L160f): skip the GitHub reads of the **versions** tab.

Exit codes: 0; 1 (database unreachable, schema missing, SQL error); 2 (unreadable
option).

## Roadmap (Gantt)

Since L96 the view opens on the **roadmap**: plan milestones and epics,
tasks, dated commitments and pending decisions on a day scale, planned dates
(dashed) against the milestones actually reached (requested, in progress,
submitted, verdict, delivered), today's line, delays, and the source of each
element (canon card, commitment, decision). It is shown by default; `ameesh
progress --no-gantt` drops it and the page has a hide button. The same
data is `ameesh plan show [--json]` (schema `ameesh-roadmap/1`).

## Versions tab

Since L160f the page also has a **versions** tab (the `versions` key of the
JSON, schema `ameesh-versions/1`): for each project and product (one per
version-tag stream of its repository), the **last published** version and its
date, and the **next release** — its open release lots, its planned date (the
plan of the lots, otherwise the p80 of the L157 reference table), its state
and its content (the release notes of the release branch, otherwise the lots
merged since the last tag, grouped by theme, with their pull request).

GitHub is read **read-only** with the host's token (`GH_TOKEN`/`GITHUB_TOKEN`;
`gh` only as a local fallback, never on the server), cached and shared by
`--serve`. Every datum says its source and read time; what cannot be read is
listed in `versions.missing`, **never guessed**. `--no-versions` skips the
GitHub reads altogether.

The timeline milestones below belong to the development workflow. Core states
follow requested → in_progress → to_validate → validated → delivered;
integration (`merged`) and deployment (`promoted`) are separate development
stages within the delivered core. See [Work lifecycle](../concepts/work-lifecycle.md).
The compatibility JSON key `lots[]` is retained.

## Lot milestones

Milestones are **declared**, not guessed. `requested` and `merged` are
recorded automatically; `frozen` (branch frozen for review) and `verdict`
(`ok` or `blocked`, with the reviewed commit) are declared:

```bash
ameesh work milestone <id> frozen --sha <commit>
ameesh work milestone <id> verdict ok --sha <commit> [--note …]
ameesh work list         # column DELAY: phase and age
ameesh work show <id>    # the full timeline: requested → frozen → review → merged
```

A freeze names an immutable commit: the full 40-hex id, or a 12+ hex
abbreviation resolved in the author's repository and written full. `HEAD` and
branch names are refused (#2786): each reader would resolve them in its own
clone, and a legacy non-full freeze is a doubt, never a merge. The target is
read published and refreshed, so a stale clone cannot make an already-delivered
freeze look delivered; an unreadable forge writes no absence mark.

When a lot has no declared freeze or verdict, they are deduced from its
transitions (first entry into `qa`, exit from `qa`, approval of its `git-merge`
action…).

## The JSON schema `ameesh-progress/1`

| Key | Meaning |
|---|---|
| `schema` | `"ameesh-progress/1"` |
| `generated_ts`, `generated_at` | time of the snapshot |
| `host`, `project`, `window` | who produced it, the filter, the time window |
| `projects`, `overview`, `decisions` | the `ameesh projects` view, the overview cards and the pending decisions (L62, L304) |
| `roadmap` | the roadmap (schema `ameesh-roadmap/1`), `null` with `--no-gantt` |
| `versions` | published versions and next releases per product (L160f), `null` with `--no-versions` |
| `lots[]` | id, title, `state` (`active`, `review`, `blocked`, `approved`, `merged`), raw `work_state`, `milestones` `{requested, frozen, verdict, merged}`, `verdict`, `blocked_verdicts`, `blocks`, `assignee`, `reviewer`, `qa_loops`, budget, linked `actions`, `waiting_for` (what and whom it waits for, L29), `stale`, `epic`, plan entry |
| `agents[]` | name, harness, model, `effort`, `state` (`working`, `idle`, `paused`, `stopped`), `since_ts`, current `turn` `{started_ts, duration_s, label, task}`, turns, unread, lease, `paused_on_budget` (`true` for a budget/pace pause, `false` for another `blocked` cause; #1070) |
| `epics[]`, `milestones[]` | lots grouped by plan epic with progress, and the canon's `WorkPackage` milestones; nothing is invented |
| `actions[]` | actions changed in the window or not terminal |
| `budget` | `spend` `{window, 1h, 24h}`, `by_agent`, subscription gauges `plans`, `hourly_cap_usd`, `paid_harnesses`, `limits` |
| `stale_after_s` | staleness threshold in force (`--stale-after`, default 6 h) |
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
