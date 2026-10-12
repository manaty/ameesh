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

`planned_wait` is null when no explicit wait applies. While it applies,
`stale` is null and no `stale_lot` is raised, so the notifier also stops its
six-hour reminder for that episode. The output limit is applied **after**
filtering; scheduled lots cannot fill the candidate window and hide an
actionable lot. Dependency truth is read without a rendering bound, including
blockers outside the current project or progress window.

Only structured fields influence this decision. A wait mentioned in mail,
a note or a commit requirement remains actionable until it is represented
by a planned date, commitment/plan dependency or direct prerequisite link. This preserves
alerts for unexplained waits.
