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
  The waiting lot must itself be **frozen with an acquired `ok` verdict on its
  last freeze**, and must still be **in review (`qa`)** (refused otherwise): a
  lot still in build, or still waiting for its review, cannot be parked. If it
  later leaves `qa` (rework, blocked, waiting for a human), the declared
  landing stops deferring the alert — it only replaces the merge wait. The
  grouped read re-checks this admission on every pass, with the same truth as
  `progress`: a new freeze of the waiting lot without an acquired verdict, or
  a lost freeze/verdict, gives the signal back even if the lot stays `qa`. The
  deadline is a short, finite day: `--until`
  must be today or later and at most `AMEESH_LANDING_MAX_DAYS` ahead
  (an operating bound, 90 days by default, like `AMEESH_STALE_AFTER`),
  refused otherwise.
  While the vehicle is open, not `blocked`, its last freeze is exactly that
  pinned head **and** the day is not past (its day counts in full, like a
  hold), the lot is not stale. A new freeze, a vehicle left without a freeze,
  a vehicle moved to `blocked`, `ameesh work landing <id> --clear`, a closed
  or merged vehicle, or a past deadline all give the signal back: the pilot
  renews the declaration explicitly on the new head. Since #3641, a re-freeze
  never carries the declaration automatically: the declarant is warned
  (vehicle journal and mail) and must renew it, because ancestry and revert
  detection do not prove the unit's content is still on the new head. The
  warning is revalidated under the landing lock and the vehicle row lock — the
  new head must still be the vehicle's current freeze and the declaration must
  still carry its old pin — so a delayed older freeze neither overwrites nor
  warns about a declaration renewed since. A
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

- An explicit **hold**: a journal note whose text begins with `hold:` (for
  example `ameesh work note <id> "hold: operator window"`), written by a
  **pilot** — a human actor (`human:<id>`) or an orchestrator/design agent
  (`AMEESH_ALERT_ORCHESTRATORS`, or the `orchestrateur`/`conception` role on its
  Agent card in the canon), the same authority as a state correction. It
  carries the gate no open lot represents — an operator window, a human
  decision with no open request. The holds of a lot are a **set**, not a stack:
  each active hold is attributed and dated (`by`, `since`) and retains by
  itself. An explicit lift (`unhold:`, `hold lifted:`, `hold released:`,
  `hold cleared:`) removes only its author's hold; a pilot's lift removes them
  all, and a state change ends every hold. An ordinary note (triage, status
  report) ends nothing, and a later `hold:` note adds a hold (or re-arms its
  author's).
  The write path authenticates the claim: `ameesh work note` and
  `ameesh work move --note` refuse a `hold:`/lift note that claims a pilot
  identity the writing session cannot prove (same guard as a state
  correction, `history=False`). A lift is not reserved to a pilot: the author
  of an active hold may lift their own, if the session proves that identity
  (`--actor` spoofed is refused); the **other** active holds remain, so a
  pilot's, human's or third party's hold stays reserved to a pilot (#3219).
  The reader and the write are one step under the lot's row lock — `work note`
  with or without `--wake-at`, and `work move --note` — so a hold interleaved
  between the check and the write cannot be erased. The runner wake keeps its
  own, older convention (`LotSessions.HOLD_PREFIX`, last non-empty note
  decides): any assignee may write a plain `hold:` to stop its own wake, but
  such a hold is not a pilot's and does not defer this alert; the two
  conventions are deliberately separate. A closure by content does not wait
  for the rule-1 holds of the assignee when no other hold is active (see
  [the work plan](PLAN-DE-TRAVAIL.md)): the proven content lifts them and the
  merge note records the lift.

  #3498 : a hold written by a pilot may name its **external executor** and
  the gesture expected — `hold: attente bureau : <geste>` or `hold: attente
  owner : <geste>` (`attente externe (bureau)`, `attente du bureau` are read
  too). An optional deadline `(due 2026-10-25 12:00 UTC)` or `(échéance …)`,
  before or after the gesture, bounds the reminder; without one, `ameesh
  debit` reminds after its threshold. The debit puts such a hold in
  `external_waits` with its executor (bureau or owner), not in `slow_waits`,
  and `--check` does not fire on it, even when the reminder is due: the
  report repeats the reminder to the bureau/owner (§10). A non-pilot hold
  that carries the declaration is not external — it stays a plain agent wait
  and the debit says so in `unreadable`.

  #3773 : the same hold may declare an **external third party** without
  naming the bureau or the owner — `hold: attente externe : revue Apple`,
  `hold: attente externe : examen du store`. The third party is in the
  gesture and the debit reports the executor as `externe`; the hold enters
  `external_waits` like a named executor's, outside `slow_waits`, with the
  same reminder at the declared deadline or after the threshold, carried to
  the bureau (§10). Prose without the colon separator (`attente externe de
  l'APK`) declares nothing.

  #4235 : a pilot hold may declare an **observation window** — a wanted,
  dated wait — as `hold: fenêtre : <motif>` (or `hold: fenêtre d'observation
  : …`, `hold: observation : …`, and the journal preamble
  `hold: (pilote …) Fenêtre d'observation voulue : …`), with its deadline
  armed by `--wake-at` (`[réveil programmé: …]`) or written `(due …)`. Until
  the deadline, `ameesh debit` lists it in `window_waits`, outside
  `slow_waits`, and `--check` does not fire: nothing is actionable before the
  deadline. At the deadline the hold becomes an ordinary agent wait again
  (and the overdue wake keeps its gesture). As with an external declaration,
  the window covers the lot's other holds, a non-pilot hold does not subtract
  it (the debit says so in `unreadable`), and an unreadable deadline guesses
  nothing: the hold stays an agent wait.

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

The packet candidates (`packet_candidates.inventory`, `scripts/paquet-candidats.py`)
read these active **pilot** holds with the same authority: a held lot is not
ready for the hourly package, and the candidate list gives `pilot-hold` as its
motif (canon §11 bis). The composer can overtake it only with an explicit
`--hold-override <lot> --hold-reason "…"`, and that gesture is journaled on the
lot (`hold-override: …`); the pilot's hold itself is unchanged, so the next
calculation excludes the lot again until an `unhold:` or a state change lifts
it (#3375).

That path reads the authority **strictly** (`is_orchestrator(..., strict=True)`,
`active_holds(..., strict=True)`): an unreadable registry, canon or alias raises
instead of being classified "not a pilot", and the candidate list is declared
incomplete — no composition on it. The tolerant default is unchanged for the
alerts and the stagnation, where a registry outage must not silence everything.
A readable "not a pilot" answer still makes an ordinary hold, which does not
reserve the package.

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

or, for an explicit pilot hold:

```json
{"what": "hold", "by": "mesh-design", "since": 1760544000.0}
```

or, for a lot carried by an open integration packet (#2335):

```json
{"what": "packet", "vehicle": 2305,
 "branch": "integration/paquet-h-20261017-2300", "head": "c186713d…"}
```

A packet is a structured carrier, not prose: an open work item whose branch
starts with `integration/`, whose body/manifest names each carried lot with
its frozen head (`#<lot> @ <sha>` / `#<lot> <sha>`, at least 7 hex digits).
Since #2599 the carrier's body may omit that list: the `ameesh-work: <lot>`
trailers of the composition commits on its branch, above the target, are also
read (the repo comes from `radar.projects.<app>.repo`, or from a registry
clone that resolves the carrier's head when the reading host has no such
setting, #2837; an unreadable branch concludes nothing and the alert stays
due). A bare `#<lot>` in running text
is **not** an inclusion — an excluded lot (« #2203 exclu », « garder #1024 »)
must keep its `stale` and `slo_breach` alerts. The units declared with their
head wait for the packet merge, not for a lot executor, so `stale` and
`slo_breach` are silenced for them. The `verdict_merge` clock of a merge-ready
lot starts at the first packet slot after its GO (`mechanics.next_packet` as
soon as the canon declares a cadence, even between two packets; otherwise the
observed hourly UTC cut-off): the time before that slot is not a delay under
canon §11 bis. On the normal path — any lot with a current GO and no declared
exception — the target is the real p80 of the packet CI (`progress.ci_suite`,
7-day window, #2919), with the table's 30-minute target as the fallback when
no suite is measured — that packet row has no P1 column, so a P1 keeps 30
min, not 15 —; the ×2/×3 thresholds apply to it. A unit declared by an
open packet has no escalation target of its own — the packet composer or the
pilot carries the merge (canon §9, owner decision #103509 option a, home#147).
A `packet-exception incident|unblock sha=… reason=…` declared for the current
head normally takes the lot out of the packet: the table target (30 min; 15 min
in P1) runs from `max(GO, CI green on that exact head)` (#3341) and the
escalation applies. The green is read from the tracked run (`ci_runs`, the
notifier's reading) on the frozen head — never assumed; a head with no green yet
(absent, still running, cancelled) or with a red run starts no `verdict_merge`
clock at all, so no alert asks for a merge the CI has not cleared.

When the lot is nevertheless carried by an open packet — its manifest or its
composition trailers name it with its head — the packet wins (#3487): the
`verdict_merge` clock starts at the packet slot (#2335) and uses the packet CI's
p80 (30-minute fallback), and an escalation names the packet and the state of
its full CI instead of prescribing a reassignment of the holder. The lot merges
with the packet; reassigning it cannot advance that merge.

`planned_wait` is null when no explicit wait applies. While it applies,
`stale` is null and no `stale_lot` is raised, so the notifier also stops its
six-hour reminder for that episode. The output limit is applied **after**
filtering; scheduled lots cannot fill the candidate window and hide an
actionable lot. Dependency truth is read without a rendering bound, including
blockers outside the current project or progress window.

Only structured fields influence this decision. A wait mentioned in mail,
a note or a commit requirement remains actionable until it is represented
by a planned date, commitment/plan dependency, direct prerequisite link,
dated external hold, a declared landing, an explicit `hold:` note or an
open packet carrier naming the lot with its frozen head. This preserves
alerts for unexplained waits and for lots a packet only mentions.
