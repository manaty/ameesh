# Project readiness and dispatch plan

`ameesh work plan --project acme [--json]` reads the current project backlog.
It lists every open bug and evolution, its planned dates and each reason it is
waiting. Improvement-backlog items are not part of this plan: they stay in
their own queue, taken by priority, score and age (L119).
`ameesh work plan <id> --start … --estimate …` keeps its existing meaning;
project mode is read-only and cannot be combined with those edits.

The plan and automatic dispatch use the same readiness checks: a lot must be
unassigned in intake (or returned to the queue from build), have an accepted
spec, no unfinished prerequisite, no open `conflicts_with` peer, no pending
decision, no active pilot hold, no future planned start and no reservation for
an external executor or human/production capability. Intake lots whose footprint
overlaps an occupying lot also wait (#4173). A conflict decision alone
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

## Validation deadlines and authority (#4256)

Every new `ameesh decide ask` request has a deadline: one business day at the
same local time in the recipient's timezone, skipping Saturday and Sunday,
unless `--by` supplies an explicit instant. Its notification includes the
local deadline, timezone, lot's issue and every repeated `--issue URL`.
`--domain ready`, `--domain priority`, or an adapter-defined domain selects
an ordered authority route from the project's configuration, for example:

```json
{"project_planning":{"acme":{"validations":{
  "owner":"human:owner", "owner_timezone":"Europe/Paris",
  "domains":{"ready":[
    {"human":"human:validator","timezone":"Asia/Manila"},
    {"human":"human:product","timezone":"Europe/Paris"}
  ]}
}}}}
```

The adapter supplies these identities from its authority directory; ameesh
contains no customer-specific role or person. The first entry is solicited.
A domain without an explicit route keeps the ordinary decision recipient
resolved from the requester/lot; configuring the escalation owner does not
redirect that request. Its timezone defaults to `validations.timezone` (UTC
when omitted); a recipient matching `validations.owner` uses
`owner_timezone`. Explicit domain entries may provide their own timezone.
At expiry, the runner escalates once to the next entry, or to the configured
owner to name a substitute if no next entry exists. It never skips an entry.
A response before expiry cancels escalation. The initial validator can still
answer after escalation; the first authorized response remains the winner.
A contradictory late response opens a new decision linked as `contradicts`,
without replacing the recorded winner.

The factual record includes the request time, deadline, default application,
escalation, responder and late responses. An escalation also records a request
under the substitute's identity, with the solicitation time and deadline,
so the owner can query that person's history. It is kept outside public work
notes and threads. Public decision views expose only `validation.status =
escalated`, never the private audit or authority snapshot. History by person
requires a non-agent human session and possession of a registered, live owner
key:

```sh
ameesh decide validation-history --person human:validator --as owner --key /path/to/owner.key
```

The private history operations are absent from the mediated executor API.
Existing requests are unchanged; new requests require migration 0515 before
executors are restarted. Bare times supplied to `--by` retain the CLI's local
host-time interpretation; the resulting instant is announced in the
recipient's configured timezone. A route without a timezone uses UTC.
