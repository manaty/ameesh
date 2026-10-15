# Planned waits and stale lots (L368)

A lot is stale when it is open and inactive beyond the configured threshold
(six hours by default). A declared wait can defer this alert:

- Its `planned_start`, set by `ameesh work plan <id> --start <day>`, is after
  the current instant. Dates begin at local midnight on the observing host,
  consistent with the plan view. At the planned start, the ordinary inactivity
  rule applies again; scheduling does not reset activity.
  If the lot has no start date, the nearest dated WorkPackage/ancestor supplies
  it (the package's ameesh date takes precedence over its canon date). An
  explicit lot date overrides that inherited date.
- An **open** commitment associated with the lot has a `depends_on` entry
  naming another open execution lot. Accepted numeric forms are `#123`,
  `123`, `lot 123`, `lot #123`, `task 123` and `task #123`.
- An open commitment associated with a WorkPackage applies to that package
  and its descendants. An exact WorkPackage ID in `depends_on` resolves to
  its open execution lots (including descendant packages); `lot <id>`,
  `package <id>` and `WorkPackage <id>` also identify a plan package. These are the
  existing plan dependency sources.
- A direct prerequisite declared with `ameesh work dependencies <id> --add <blocker>`
  names another open execution lot ([L385](WORK-DEPENDENCIES.md)).
- An explicit **hold**: a journal note whose text begins with `hold:` (for
  example `ameesh work note <id> "hold: operator window"`), written by a
  **pilot** — a human actor (`human:<id>`) or an orchestrator/design agent
  (`AMEESH_ALERT_ORCHESTRATORS`, or the `orchestrateur`/`conception` role on its
  Agent card in the canon), the same authority as a state correction. It
  carries the gate no open lot represents — an operator window, a human
  decision with no open request. The hold is attributed and dated (`by`,
  `since`) and only an explicit lift (`unhold:`, `hold lifted:`,
  `hold released:`, `hold cleared:`) or a state change ends it: an ordinary
  note (triage, status report) does not, and a later `hold:` note re-arms it.
  The write path authenticates the claim: `ameesh work note` and
  `ameesh work move --note` refuse a `hold:`/lift note that claims a pilot
  identity the writing session cannot prove (same guard as a state
  correction, `history=False`). The runner wake keeps its own, older
  convention (`LotSessions.HOLD_PREFIX`, last non-empty note decides): any
  assignee may write a plain `hold:` to stop its own wake, but such a hold is
  not a pilot's and does not defer this alert; the two conventions are
  deliberately separate.

Grouped views (`work list`, `progress --json`, the stale alert, the dependency
labels) read the hold from a targeted storage stream
(`Progress.lot_hold_events`: state transitions plus the `hold:`/lift notes,
merged by event id), never from the full journal: ordinary note text is not
read or transferred, the event order is preserved, and a state change still
lifts the hold. A hold written on a transition
(`ameesh work move <id> <state> --note "hold: …"`) is read with that
transition. The storage pre-selects on the same motifs as this module
(`HOLD_NOTE_PATTERNS`, applied to the note stripped of its edge whitespace),
so no ordinary note is fetched, and a transition note is stored stripped —
what was authenticated is what is stored and read.
- An **open** commitment associated with the lot names an external gate in
  `depends_on`: `gate:<name>` (or `external:<name>`). The gate is not an
  ameesh lot, so nothing else can release the hold; the commitment's due day
  is what ends it (the day counts in full, as for `engagement_overdue`, and
  the ordinary inactivity rule applies again the next day). Closing,
  cancelling or completing the commitment releases the lot immediately.

  ```sh
  ameesh plan add "hold until the liquichain window" --lot 768 \
      --due 2026-10-15 --depends-on gate:liquichain-window
  ```

  The hold stays visible in the plan view. `ameesh plan cancel <id>` releases
  the lot at once; renewing it means cancelling and adding a new dated
  commitment (a later due day pushes the hold). A hold whose date cannot be
  read does not suppress the alert: a hold always ends.

`merged`, `promoted` and `closed` blockers no longer defer the alert. Missing
references, self references, proposed/done/cancelled commitments and free
text such as “after #123 merges” do not suppress it. An unresolved package
without an open execution lot does not hide stagnation. Parent traversal is
cycle-safe. Closing a blocker does not manufacture activity on the waiting
lot: the existing threshold determines whether it is immediately stale.

All three consumers use the same resolver: `ameesh alerts`, the lot
annotations in `work list/show`, and `progress --json`. The latter two keep
their existing `waiting_for` information and add `planned_wait`:

```json
{"what": "scheduled", "start": "2026-10-13"}
```

or:

```json
{"what": "dependency", "lots": [123, 124]}
```

or, for a pilot hold:

```json
{"what": "hold", "by": "mesh-design", "since": 1760544000.0}
```

or, for a dated external hold:

```json
{"what": "hold", "gate": "liquichain-window", "until": "2026-10-15", "commitment": 42}
```

`planned_wait` is null when no explicit wait applies. While it applies,
`stale` is null and no `stale_lot` is raised, so the notifier also stops its
six-hour reminder for that episode. The output limit is applied **after**
filtering; scheduled lots cannot fill the candidate window and hide an
actionable lot. Dependency truth is read without a rendering bound, including
blockers outside the current project or progress window.

Only structured fields influence this decision. A wait mentioned in mail,
a note or a commit requirement remains actionable until it is represented
by a planned date, commitment/plan dependency, direct prerequisite link, an
explicit `hold:` note or a dated external hold. This preserves alerts for
unexplained waits. The two hold forms are complementary: each releases only
its own hold, so lifting a pilot hold leaves a still-valid gate in force, and
a gate that expires or is cancelled does not lift an active pilot hold.
