# Project readiness and dispatch plan

`ameesh work plan --project acme [--json]` reads the current project backlog.
It lists every open lot, its planned dates and each reason it is waiting.
`ameesh work plan <id> --start … --estimate …` keeps its existing meaning;
project mode is read-only and cannot be combined with those edits.

The plan and automatic dispatch use the same readiness checks: a lot must be
unassigned in intake (or returned to the queue from build), have an accepted
spec, no unfinished prerequisite, no open `conflicts_with` peer, no pending
decision, no active pilot hold, no future planned start and no reservation for
an external executor or human/production capability. A conflict decision alone
does not close the peer or remove the conflict link.

A dispatchable bug puts its project in the bug phase. Enhancements wait until
no bug is dispatchable. Bugs awaiting a human, review or deployment do not
block enhancements. During dispatch, the bug phase is evaluated for each agent
with its capabilities, review eligibility and working directory. If that agent
can take no ready bug, it may take an enhancement; the other agents retain
their own candidate lists. The assignment transaction repeats these checks,
including for a bug that appeared after selection. The read-only CLI shows
project readiness before these agent-specific checks.
Every dispatch rechecks the plan; a new bug preempts at
the next dispatch without interrupting assigned work. Priorities, planned
start/end/delivery dates, age and lot number order available lots; unfinished
dependencies precede their dependants in the waiting list. Dates are the same
stored planned dates that `work schedule` maintains. Reads recompute the plan
from current state, links, decisions, dates and acceptance evidence; no cached
plan requires invalidation.

Projects may supply a spec acceptance rule in the host configuration:

```json
{
  "project_planning": {
    "acme": {
      "spec_acceptance": {
        "mode": "marker",
        "field": "body",
        "value": "Spec: accepted"
      }
    }
  }
}
```

`marker` requires the literal value in the lot's `body` or `source`. A project
adapter can import its acceptance evidence there; dispatch never calls a
remote service or executes configuration. Invalid rules retain the lot with
an explanation. Existing projects without a rule preserve acceptance at intake;
this can also be declared explicitly as `{"mode": "intake"}`. A marker rule
applies to bugs and enhancements. Configure the same rule on each dispatcher
host of a project.
