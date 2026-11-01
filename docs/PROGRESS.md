# `ameesh progress` — `ameesh-progress/1` schema

Real-time view of progress (lot L24, decision
[0024](design/decisions/0024-vue-temps-reel.md)): lots on a timeline, agents,
milestones and budget, **fed by what ameesh records itself** —
lot transitions (`work_items`, `work_item_events`), gated actions
(`actions`), turns and leases from the registry (`agent_registry`, `spend_pending`),
cost ledger (`turn_costs`). Neither git nor the board is read.

L78 adds `agents[].conversation`: the latest 20 detail entries, the durable
per-agent cursor and `ameesh-conversation/1` schema. See
[Conversation log](CONVERSATIONS.md) for producer APIs, retention, the R12
thread and subscriber cursor recovery. Text requires the explicit content
supervision role; the raw thread outbox is not exported.

```
ameesh progress [--project P] [--since 24h] [--stale-after 6h]   # text, narrow terminal
ameesh progress --json [--project P] [--since 24h]   # this schema
ameesh progress --html FICHIER [--project P] [--since 24h]
ameesh progress --serve [--port 8765] [--project P] [--since 24h]   # local page (L304)
ameesh progress … --no-gantt                         # without the roadmap (L96)
```

* **Roadmap** (L96): the timeline of epics, milestones, tasks,
  commitments and pending decisions, planned versus actual, is **shown by
  default** (text: FEUILLE DE ROUTE (ROADMAP) section after PROJETS; page: section
  "Feuille de route" (Roadmap) with a "masquer" (hide) button, remembered by the
  browser; JSON: `roadmap` key). `--no-gantt` removes it (`roadmap:
  null`). Its window is its own (days), not `--since`.

* `--since`: start of the window, a duration (`90m`, `16h`, `2d`/`2j`) or an
  ISO 8601 date (`2026-10-05`, `2026-10-05T08:00Z`; no time zone = local time).
  Default `24h`.
* `--stale-after D` (L29): an unmerged lot with no activity for `D` is
  stale (default `AMEESH_STALE_AFTER`, otherwise `6h`).
* `--project P`: lots whose `app` or `workstream` is `P`; actions of project
  `P` (and those linked to these lots); agents whose `chantier` or `team` is
  `P`; costs of these agents.
* `--html FICHIER`: **self-contained** static page (inline CSS and script,
  embedded data, `default-src 'none'` policy: no network
  request, no font or CDN), readable on a phone. No server:
  open the file, or copy it wherever you want. Atomic write (temporary
  file then rename); to regenerate, rerun the command. Its Refresh button is
  disabled ("open with --serve to refresh").

**The page** (L304, static or served) shows little per screen: tabs
**Overview** (one card per project, from `overview`: open lots,
lots merged in the window, pending decisions, 24 h spend, next milestone
with its progress bar, two warnings at most; then "Watch": overdue or
urgent decisions, blocked and stale lots), **Versions** (L160f, #497: per
product, the last published version and the next release with its lots,
its planned date, its content and the freshness of the GitHub read),
**Gantt** (the roadmap: tasks
grouped by epic, or by project when they have none, an epic without any
task keeping its own group, as does a planned epic whose tasks are all
finished and hidden, groups folded by default unless they hold a
late task (in progress alone is not enough), with a summary bar and their open, delivered, blocked and late
counts; delivered and abandoned tasks hidden unless ticked; week zoom fits
the window on screen, day zoom gives each day a column and scrolls to
today, `z=day` in the URL; a milestone can narrow the window to the span of all its dates, planned
and actual, in calendar days; a task opens
its lot window),
**Work items**, **Agents** (tables with a text filter and sortable columns;
merged and closed lots, stopped agents hidden by default), **Budget** and
**Decisions**. A project selector sits on top (served: the snapshot is read
again with `?project=`; static: the page filters lots, agents, cards and
decisions and Gantt tasks, and says that budget, milestones and epics
cover all projects). Details open
in **windows** on click (lot: dated milestones, waiting_for, cost, its
decisions, thread command; agent; milestone: epics and open lots; decision:
options, recommendation, answer command). Light, dark or system theme
(remembered by the browser). Open tab, project and auto-refresh live in
the URL (`#tab=lots&p=demo&auto=1`, `#tab=gantt&z=day`).

* `--serve [--port 8765]` (L304): serves the same page and this JSON from
  a local server, **read-only**, until Ctrl-C. See "Local server" below.
* `--no-versions` (L160f, #497): read no GitHub at all; the `versions` key
  stays and says why it is empty.

Exit codes: 0, 1 (database unreachable, schema missing, SQL error),
2 (unreadable option, `--port` without `--serve`, `--serve` with `--html` or
`--json`, port unavailable).

## Local server (`--serve`, L304)

| Path | Response |
|---|---|
| `/` | the page **without data** (no token needed, nothing to protect): it reads `/api/progress.json` with the token; its **Refresh** button fetches it again without reloading (open tab, filters and detail window kept), optionally every 60 s; "data as of HH:MM" is `generated_ts`; a failed fetch is said and the previous data stay. The served page's policy adds `connect-src 'self'` and nothing else |
| `/api/progress.json` | this schema, with `Authorization: Bearer <token>` only (401 otherwise); `?project=P` takes a project known to the unfiltered snapshot (400 otherwise), empty = all projects |

* **Bound to `127.0.0.1` only**; no option changes the address. The `Host`
  header must be `127.0.0.1:<port>` or `localhost:<port>` (403 otherwise:
  DNS rebinding).
* **Read-only**: `GET` and `HEAD` only (405 with `Allow: GET, HEAD`
  otherwise), and every snapshot is read through a database session opened
  with `default_transaction_read_only=on`.
* **Token**: the host is shared by several agents and Unix accounts, which
  `127.0.0.1` does not keep out. Each start draws a random token, printed
  **only on the command's output**: `http://127.0.0.1:<port>/#t=<token>`.
  It sits in the URL **fragment**, which the browser never sends; the page
  moves it to `sessionStorage` (isolated per origin, so per port), removes
  it from the URL and sends it in an `Authorization: Bearer` header. **No
  cookie**: browsers share `127.0.0.1` cookies across ports, so any local
  server another account runs on another port would receive them. The
  custom header also turns any cross-origin fetch into a preflight, which
  gets 405. The token is compared in constant time; a restart draws a new
  one (old links stop working). Request lines are logged on stderr as
  method and path only, and error pages (malformed request included) never
  echo the request.
* A snapshot is kept 5 s per project: an open tab does not hammer the
  database. Responses carry `Cache-Control: no-store`.

## Conventions

* Instants: UTC epoch seconds (numbers), keys suffixed `_ts`, or `null`
  when the instant is not known. Durations in seconds (`_s`).
* Amounts: US dollars (`_usd`), estimates included (see `budget`).
* Data ameesh does not have is `null` or an empty list, never guessed;
  `missing` states plainly what is missing.
* Compatibility: a field may be **added** without changing the version;
  removing a field or changing its meaning moves to `ameesh-progress/2`.

## Root

| Key | Type | Meaning |
|---|---|---|
| `schema` | `"ameesh-progress/1"` | schema version |
| `generated_ts`, `generated_at` | number, ISO text (UTC, `Z`) | instant of the snapshot |
| `host` | text | host that produced the snapshot |
| `project` | text \| null | `--project` filter |
| `window` | `{from_ts, to_ts}` | timeline window |
| `overview` | list | (L304) one card per entry of `projects`, same order: `{project, active, lots_open, lots_merged_in_window, agents_active, agents_working, spend_24h_usd, decisions_pending, milestone, warnings}`; `milestone` = the next unreached plan milestone carrying a lot of the project (earliest dated first, undated last) `{id, title, date, progress, lots_merged, lots_total}`, or null |
| `decisions` | list | (L304) pending decision requests, oldest first, `ameesh-decision/1` items (`ameesh decisions --json`); filtered by `--project` on their `project` |
| `versions` | object | (L160f, #497) the versions of the products and their next releases, `ameesh-versions/1` schema (see below); `null` never: `--no-versions` leaves it saying why it is empty |
| `ci_suite` | object | (#1476) the duration of the full CI suite as the watcher saw it, per repository (`ameesh-ci-suite/1`, see below) |
| `roadmap` | object \| null | (L96) the roadmap, `ameesh-roadmap/1` schema ([EXPLOITATION.md](EXPLOITATION.md), "Feuille de route" (roadmap) section); `null` with `--no-gantt` |
| `projects` | list | (L62) the per-project view of `ameesh projects` (item schema: `ameesh-projects/1`, [EXPLOITATION.md](EXPLOITATION.md)); filtered by `--project` on the project name; at the top of the text and the page |
| `lots` | list | see below, oldest to newest |
| `agents` | list | see below, by name |
| `milestones` | list | project milestones: `WorkPackage` `milestone` cards in the canon (L29) |
| `epics` | list | (L29) lots grouped by plan epic, with progress |
| `stale_after_s` | integer | (L29) stagnation threshold applied, in seconds |
| `actions` | list | actions in the window |
| `budget` | object | spending and gauges |
| `truncated` | object | rendering limits reached: `{lots?, actions?}`, each `{shown, total, limit}`; `{}` if nothing is cut |
| `missing` | list of texts | data the view cannot (yet) read |

## `lots[]`

A lot is included if it is open (neither `merged` nor `promoted`) or modified in the
window. Rendering limit: 500 lots, open ones first then the most
recently modified (cut reported in `truncated.lots`); the list is
rendered in creation order. The milestones and state of a displayed lot are
read without limit (full log, all its actions).

| Key | Meaning |
|---|---|
| `id`, `title`, `type`, `project` (`app`), `workstream`, `issue_ref` | identity of the lot |
| `state` | `active` \| `review` \| `blocked` \| `approved` \| `merged` \| `closed` (L29: abandoned or superseded) |
| `work_state` | raw state from `work_items` (`intake`, `build`, `qa`, `merged`, `promoted`, `blocked`, `waiting_human`, `closed`) |
| `milestones` | `{requested, frozen, verdict, merged}`: instants, `null` if unknown |
| `last_frozen_ts` | last freeze (different from `milestones.frozen`, the first one, if the lot was refrozen) |
| `verdict` | retained verdict (the most recent): `ok` \| `blocked` \| null |
| `milestones_source` | `milestones` (lot milestones table, L10) \| `events` (deduction) |
| `blocked_verdicts` | number of blocking verdicts (integer; null if the source does not know) |
| `blocks` | number of holds (`blocked`, `waiting_human`) |
| `assignee`, `reviewer` | author; the designated reviewer (`work move … qa --reviewer`, #705) while the review is open, otherwise the author of the retained verdict (approver for a merge action); #1979: `null` when the pilot waived the re-verdict (`work reviewer … --no-verdict`) — the lot goes straight to `approved` |
| `qa_loops` | `qa → build` round trips counted by `work_items` |
| `updated_ts`, `budget_usd`, `spent_usd` | last modification, lot budget |
| `actions` | ids of the actions linked to the lot |
| `package`, `epic` | (L29) WorkPackage card of the lot and its epic, or null |
| `pr_ref` | (L29) PR whose merge closed the lot, or null |
| `closed` | (L29) `{reason: abandoned\|superseded, superseded_by, at_ts}` for a closed lot, otherwise null |
| `waiting_for` | (L29) what the lot is waiting for and from whom: `{what, who, label}` (`what`: start, build, verdict, fix, merge-approval, merge, merge-outcome, decision, unblock, packet; `who` null when ameesh does not know); null for a merged or closed lot |
| `last_activity_ts` | (L29) last transition, note, milestone or action |
| `stale` | (L29) `{since_ts, idle_s, threshold_s}` if the unclosed lot has had no activity since the threshold, otherwise null; the page no longer draws it as active |
| `delegation` | (L40, 0030) delegation with a deadline (`ameesh work delegate`): `{delegated_by, delegate, delegated_ts, due_ts, due_in_s, overdue, settled, label}` — `due_in_s` negative when overdue, `due_ts` null and `settled` true when the delegation is settled (the delegate has worked), `label`: "délégué par X, échéance dans 12 min" (delegated by X, due in 12 min) / "délégué par X, en retard de 5 min" (delegated by X, 5 min overdue); null outside delegation and for a merged or closed lot (see [EXPLOITATION.md](EXPLOITATION.md)) |
| `estimate` | (L157) estimated duration and measured actual: `{minutes, source, by, at_ts, started_ts, finished_ts, actual_minutes, elapsed_minutes, ratio, gap_minutes, overrun, label}` — start = first move to build (or qa) or first turn of the assignee, end = merge; `elapsed_minutes` while in progress, `actual_minutes` once delivered; `label`: "estimé 2 h · réel 2 h 40 (+40 min, ×1,33)" (estimated 2 h · actual 2 h 40 (+40 min, ×1.33)) (see [EXPLOITATION.md](EXPLOITATION.md), "Durée estimée et durée réelle des lots" (estimated and actual lot duration)) |

Milestones (single function `progress._jalons_de_lot`). Source: the lot
milestones table (L10, `work_item_milestones`) when the lot has a **declared** freeze or verdict
there (all instants then come from the table); otherwise
deduction from the transition log:

* `requested`: creation of the lot;
* `frozen` (freeze): first declared freeze, otherwise first entry into `qa`;
* `verdict`: retained verdict, the most recent — declared verdict, otherwise exit from
  `qa` to `merged` (`ok`) or to `build` (`blocked`); approval of the
  lot's `git-merge` action counts as an `ok` verdict;
* `merged`: merge milestone, otherwise first entry into `merged`, otherwise confirmed
  end of the `git-merge` action.

State: `merged` (`merged`/`promoted`); `blocked` (`blocked`/`waiting_human`);
in `qa`, `approved` if the retained verdict is `ok` and later than the **last**
freeze, or if the pilot waived the re-verdict (`ameesh work reviewer <id>
--no-verdict`, #1979 — a current blocking verdict keeps `review` and the fix
due), otherwise `review` (a refrozen lot goes back to review); `active` otherwise
(`intake`, `build`).

## `agents[]`

| Key | Meaning |
|---|---|
| `name`, `harness`, `model`, `host`, `project` (`team`, otherwise `chantier`) | identity |
| `effort` | `ameesh set … effort=` setting; **null** = harness default or unknown (local setting of another host) |
| `state` | `working` \| `idle` \| `paused` \| `stopped` |
| `status`, `status_text` | raw registry status and its text (reason for a pause) |
| `paused_on_budget` | (L242b, #1070) `true` when the pause is the budget/pace guard (`status` `blocked` and text `budget : …`, the rule of `handoff.paused`); another `blocked` cause (host not ready, authentication) keeps `false` |
| `since_ts` | start of the current turn (`working`), end of the last turn (`idle`), last update otherwise |
| `turn` | current turn or null: `{started_ts, duration_s, label, task}` (`task` = current prompt, truncated to 140 characters) |
| `turns`, `unread`, `pending_prompt`, `lease_live` | turns counted, unread, pending prompt, live lease |
| `builds` | lots in `build` assigned to the agent, **all projects** (the §4 WIP cap of `ameesh work wip` is global, owner 14/10/2026); the text view flags the count when it is above the cap |
| `mode`, `stop_reason` | `execute` \| `externe`; structured stop reason when the state is `stopped`, otherwise `null` (L37, decision 0030) |

State: `running` under a live lease → `working`; `running` without a live lease,
`stopped`, `dead` → `stopped`; `blocked` (budget or L13 accounting pause)
→ `paused`; `idle`, `queued` → `idle`. The turn start comes from the accounting
marker set before each turn (`spend_pending`).

## `milestones[]`

**Project** milestones declared in the canon (`WorkPackage` cards of kind
`milestone`, L29, [work plan](PLAN-DE-TRAVAIL.md)): `{id, title,
at_ts, date, status, responsible, canon_ref, epics, lots_total, lots_merged,
lots_abandoned, lots_open, lots_pending, progress}`. L96: `date` is the day
of the milestone (ISO) — set by `ameesh work plan <fiche> --livraison J`, otherwise
declared in the canon (`date` or `delivery`) — and `at_ts` its local midnight; null
without a date. Empty without a synchronized plan.

## `epics[]` (L29)

`{id, title, milestone, responsible, status, canon_ref, planned, lots,
work_items, lots_total, lots_merged, lots_abandoned, lots_open, lots_pending,
progress}`. `planned` (L96): `{start, end, delivery, source}` — effective planned
dates (set in ameesh, otherwise in the canon; `source`:
`ameesh`, `canon`, `mixte` or null). The units of an epic are its `lot` cards (`lots[]`:
`{id, title, status, work_items}`, status `pending` with no lot created, `open`,
`merged`, `abandoned`) and the lots attached directly to the epic
(`work_items`). `progress` = merged / (total − abandoned), null if
none; computed over all lots, regardless of the window.

**Activation (#5244, canon §5 and §14; added fields).** `progress` and
`lots_merged` still count the merge. Done means active: `progress_active` =
`lots_active` / `lots_activation_total`, null when the activation was not computed
(an older snapshot) or no unit counts, 0 when the evidence is unreadable (the units
are then in `lots_unmeasured`). `lots_activation_total` is the units not abandoned
at activation: unlike `lots_total − lots_abandoned`, it counts an operator gesture
(closed `done`, reserved to an external executor), which waits for its receipt. The
views use it for what is done, and fall back on `progress` on an older snapshot.
`lots_waiting_activation` counts units known not to be active yet, `lots_unmeasured`
those with no readable evidence (not done, no lateness raised). A `lots[]` entry
adds `step` (`active`, `waiting`, `unmeasured`, or as `status`). In the roadmap, a
task adds `activation` (`{step, counted, label}`, null while open), a package
`progress_active`, `units_active`, `units_waiting_activation`, `units_unmeasured`.
Steps: `merged`, `active` (commit in the staging attested on every active host),
`gesture` (closed `done`, reserved to an external executor, no receipt), `receipt`
(a gated action of the lot confirmed under a signed `ameesh-receipt/1`), `delivered`
(off target), `unmeasured` (`src/ameesh/activation.py`).

## `versions` (L160f, #497)

`ameesh-versions/1`. One **product** per version-tag stream of the
repository a project cites (`issue_ref`, else the `github.projects`
setting) — the same model for `--serve` and `--html`. Products and trains
are indexed by `(stream, version)`, never by version alone: `v1.0.0` and
`desktop-v1.0.0` are two products. GitHub is read **read-only** with the
host token (`GH_TOKEN`/`GITHUB_TOKEN`; `gh` only as a local fallback, never
on the server) and only for tags, releases, one file and one commit date:
merged lots and their PRs come from the database (`sync-github`). The
reader is cached 5 min and shared by the whole server; a failed read is
cached 60 s and the previous data, when there is some, keeps being served
marked stale; `--no-versions` reads nothing.

| Key | Meaning |
|---|---|
| `schema` | `"ameesh-versions/1"` |
| `github` | `{source, read_ts, age_s, stale}`: how GitHub was read (`api` with the token, `gh` as a local fallback), when it was last read and its age — a cache served keeps the instant it was read, never the snapshot's, so a stale cache shows (`stale`); `null` with `--no-versions` |
| `products[]` | one entry per product, see below |
| `read_ts` | the same instant as `github.read_ts`, kept at the root |
| `missing` | texts: what the view cannot read (no repository known, no GitHub reader…) |

`products[]`:

| Key | Meaning |
|---|---|
| `project`, `repo`, `product`, `label`, `prefix` | the project, its repository and the product; one stream per tag prefix; the prefix-less stream is `<project> server/prod` when the repository has several |
| `last` | last **published** version: `{version, tag, published_ts, date, url, prerelease, source, read_ts}` |
| `last_tag` | the newest tag when it is ahead of `last` (tag without release, draft release): `{version, tag, date, published_ts, draft, url, source, read_ts}`; a `missing` line says the tag has no published release |
| `next` | the next release, or `null`: `{version, tag, state, date, date_source, date_label, date_partial, lots[], release, pr, notes, notes_reason, summary, themes[], merged_lots[], merged_since, missing[]}` |
| `unassigned` | `true` on the single `<project> (product not assigned)` entry: the merged lots whose title names no product surely; never duplicated under two products |
| `missing` | texts: what this product lacks |

The next version is the smallest one above the stream's last **published**
version, announced by an open release lot (`Release x.y.z`, `REL-…`,
`QA-…`) or a draft release — a draft does not raise the base, it shows as
prepared on the next train (`next.release.draft`, state `draft` when no lot
drives it). Semver pre-releases (`0.2.11-rc.1`) are kept whole and sort
before their final version; `next.tag` is the expected tag. `next.date` is
a **firm** date only when every lot of the train has one (their latest
delivery, end or start; otherwise the p80 of the L157 reference table,
`date_source`: `plan` or `p80`); as soon as one lot lacks a date, `date` is
`null`, `date_partial` keeps the latest known date as a lower bound (shown
as `not firm, latest …`) and `next.missing` says `N of M lot(s) have no
planned date`. `next.state` is the most demanding state of the train
(`blocked`, `waiting_human`, `qa`, `build`, `intake`), otherwise `draft`
(draft release) or `pull request` (open release PR).

Content: the notes file `docs/versions/<version>.md` read from the **head
branch of the release PR** (the lot's branch, from the database) — never
from `release/vX`, the tag or the default branch; `notes.source` names that
reference. Without that file, `notes` is `null`, `notes_reason` says why
and the lots merged since the last tag are grouped by their epic
(`themes[]`), each lot attributed to a product only when its title names
one surely (a stream prefix, or `serveur`/`server`/`prod` for the
prefix-less stream); the others go to the `<project> (product not
assigned)` entry. `merged_lots[]` carries each lot with its `pr` read from
the database. Every datum read from GitHub (`last`, `last_tag`, `release`,
`notes`) carries `source` and `read_ts`; those read from the database
(`lots[]`, `merged_lots[]`, `pr`) say `ameesh work items`. The text render
adds one line: `GitHub (api): read 3 min ago`.

## `ci_suite` (#1476)

How long the full CI suite takes, as the CI watcher (`ci_runs`, L161c) saw
it: from its first sighting of a `tests.yml` run to the sighting that
concluded it. GitHub's own creation instant is not kept, and the delay is
bounded by the `ameesh notify` interval on both sides, so the median is
right to within one interval.

| Key | Meaning |
|---|---|
| `schema` | `"ameesh-ci-suite/1"` |
| `window_days`, `target_s` | reading window (7 days) and the target the lot is measured against (15 min, #1476) |
| `repos` | one entry per repository, by name |

A `repos[]` entry: `{repo, runs, median_s, p80_s, last_s, last_concluded_ts,
priority_runs, priority_median_s}`. Only **concluded** runs count (`success`
or `failure`; a cancelled or superseded run has no suite duration) in the
contexts that **guarantee** the full suite: a push or pull request whose
source or target is `release/**` or `integration/**`, or a manual run
(`workflow_dispatch`). A push to `main` is left out: an all-documentation
pushed range takes the L161a fast lane there (the parts are skipped) and
`ci_runs` keeps neither the plan mode nor the jobs, so counting it would pull
the median down. The reading is bounded to the 500 most recent concluded runs
**per repository**. `priority_runs` and `priority_median_s` restrict to the
#1468 priority pool (`integration/**`, `release/**`): a push on `main` follows
the merge of a package already validated on its integration branch and is not
the critical path. The text render shows the median and p80 against the
target; the page shows the same.

## `actions[]`

`{id, project, work_item, connector, operation, class, state, proposed_by,
attempts, created_ts, approved_ts, launched_ts, finished_ts}` — actions
modified in the window or non-terminal (`proposed`, `approved`,
`launched`, `unknown`). Rendering limit: 500, non-terminal ones first
(cut reported in `truncated.actions`). The actions of a displayed lot
are always in its `actions` field, even beyond the limit.

## `budget`

| Key | Meaning |
|---|---|
| `currency` | `"USD"` |
| `paid_harnesses` | harnesses paid per token (actual spending); the others are plans whose cost is an estimate |
| `hourly_cap_usd` | hourly cap on per-token paid usage (decision 0019) |
| `limits` | L70: caps in force and their source (`base`, `config`, `défaut`) — same shape as `limits` in `ameesh budget --json`; null without configuration |
| `spend` | `{window, 1h, 24h}`, each `{paid_usd, plan_value_usd, total_usd}`: `paid_usd` = **spent** (paid per token, estimated turn by turn); `plan_value_usd` (L95) = **value consumed on plans**, theoretical list-price equivalent that costs nothing extra per turn; `total_usd` = their sum, kept for pre-L95 readers but no longer to be displayed |
| `real` | L95: **actual** spending drawn from balance readings (`ameesh cost balance`), versus the estimate, one entry per (provider, currency, period): `{provider, currency, period (24h, or window if the window is not 24 h), real_spent, topups, estimated_usd, gap_usd, alert, start_ts, end_ts, readings, accounts}` — see below |
| `by_agent` | `{agent, harness, model, paid, usd, turns, failed_turns, input_tokens, fresh_input_tokens, cached_input_tokens, output_tokens}` over the window. L95: `turns` only counts turns that consumed something; `failed_turns` the turns with no usage at all (harness failure, interrupted turn: $0 and 0 tokens); `fresh_input_tokens` the input outside the cache (Codex counts the cache in `input_tokens`) |
| `plans` | plan gauges read on the host: `{harness, key, used, pace_cap, elapsed, resets_ts, window_s, exceeded}` (fractions 0..1) |
| `accounts` | L95: each declared account of the host (`ameesh accounts list`), with **all** its windows: `{harness, account, active, forced, forced_until, ok, reason, next, why, expires_in_s, gauges: [{key, used, last_used, pace_cap, elapsed, resets_ts, reset_passed}], losses: [{key, resets_ts, in_s, lost}]}`; `forced_until` (#3755) is the force expiry (epoch, null without a force); `losses` = capacity lost at the next reset if nothing changes, `next` = account a new session would take (L74, decision 0034); empty without declared accounts |

**Actual spending versus the estimate (L95).** The drop in balances between two
readings is the actual spending (a rise is a top-up, counted separately
in `topups`). The estimate `estimated_usd` is the sum of the ledger of the
harness of the same name over the interval **actually covered** by the readings
(`start_ts` → `end_ts`), not over the requested window. `alert` is true when
the gap `gap_usd` (estimated − actual) exceeds both 25% of the actual spending
(`AMEESH_BUDGET_GAP`, fraction) and $0.50; a currency other than USD is
not compared (`gap_usd` null). Rows set aside by `ameesh cost correct`
(migration 0043) count nowhere.

The HTML page and the text output never add up `paid_usd` and
`plan_value_usd`: two distinct totals, "dépensé (payé au token)" (spent (paid per token)) and
"valeur consommée sur les forfaits" (value consumed on plans).

The gauges and accounts are those of `ameesh cost` and `ameesh accounts
list`: read from the local harness logs **of the host producing
the snapshot**; their history has been in the database since L26
(`ameesh cost gauges --json`, see `docs/EXPLOITATION.md`).

## Example (abridged)

```json
{
  "schema": "ameesh-progress/1",
  "generated_ts": 1791200000.0,
  "generated_at": "2026-10-05T11:33:20Z",
  "host": "poste-1",
  "project": "demo",
  "window": {"from_ts": 1791113600.0, "to_ts": 1791200000.0},
  "lots": [{
    "id": 12, "title": "Export des factures", "state": "review", "work_state": "qa",
    "milestones": {"requested": 1791150000.0, "frozen": 1791180000.0,
                   "verdict": null, "merged": null},
    "verdict": null, "blocked_verdicts": 0, "blocks": 0,
    "assignee": "agent-a", "reviewer": null, "actions": []
  }],
  "agents": [{
    "name": "agent-a", "harness": "codex", "model": null, "effort": "high",
    "state": "working", "since_ts": 1791199000.0,
    "turn": {"started_ts": 1791199000.0, "duration_s": 1000,
             "label": "tour consigne (codex)", "task": "Corriger le test d'export"}
  }],
  "milestones": [],
  "epics": [],
  "budget": {"spend": {"1h": {"paid_usd": 0.1, "plan_value_usd": 0.3, "total_usd": 0.4}},
             "real": [], "plans": [], "accounts": []},
  "missing": ["…"]
}
```
