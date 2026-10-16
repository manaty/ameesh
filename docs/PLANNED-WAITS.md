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
- A **declared landing**, `ameesh work landing <id> --vehicle <lot> --until <day>`
  (#1945), parks a lot whose
  work is done on a package vehicle: an open lot that carries the composition.
  The declaration is an admission **pinned to the vehicle's freeze**: the
  vehicle must carry a `frozen` head (refused otherwise, and refused while it
  is `blocked`), and the pin is read in the same transaction as the write.
  While the vehicle is open, not `blocked`, its last freeze is exactly that
  pinned head **and** the day is not past (its day counts in full, like a
  hold), the lot is not stale. A new freeze, a vehicle left without a freeze,
  a vehicle moved to `blocked`, `ameesh work landing <id> --clear`, a closed
  or merged vehicle, or a past deadline all give the signal back: the pilot
  renews the declaration explicitly on the new head. A
  self, unknown or finished vehicle does not suppress the alert; nor does
  prose, a bare admission to a packet or an old transport chain.

  ```sh
  ameesh work landing 1427 --vehicle 1675 --until 2026-10-18   # pin the current freeze
  ameesh work landing 1427 --clear
  ```

  The step is read at each pass from `work_items.state`, but the displayed
  head is the pinned one (the admission), never a later freeze. The gesture is
  journalled (`Landing declared: vehicle #1675 until … (frozen …)`) and counts
  as activity, like a prerequisite edit. A planned start, a declared
  prerequisite or a gate stays ahead in the displayed wait: the more explicit
  wait wins. When a landing is active on a frozen lot whose verdict is
  acquired, the registry label names the vehicle, its step, its pinned head
  and the deadline instead of `merge (executor not designated)`.

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

or, for a dated external hold:

```json
{"what": "hold", "gate": "liquichain-window", "until": "2026-10-15", "commitment": 42}
```

or, for a declared landing on a vehicle:

```json
{"what": "landing", "vehicle": 1675, "step": "qa", "head": "04965e6ca1b2…", "until": "2026-10-18"}
```

`planned_wait` is null when no explicit wait applies. While it applies,
`stale` is null and no `stale_lot` is raised, so the notifier also stops its
six-hour reminder for that episode. The output limit is applied **after**
filtering; scheduled lots cannot fill the candidate window and hide an
actionable lot. Dependency truth is read without a rendering bound, including
blockers outside the current project or progress window.

Only structured fields influence this decision. A wait mentioned in mail,
a note or a commit requirement remains actionable until it is represented
by a planned date, commitment/plan dependency, direct prerequisite link,
dated external hold or declared landing. This preserves alerts for
unexplained waits.
