# Work plan — milestones, epics, lots (L29)

The plan is **declared in the canon** (OKF, [decision 0005](design/decisions/0005-canon-okf.md));
the **execution state** of lots stays in ameesh (`work_items`); GitHub is only
a **view** of it. A change made in GitHub is never taken back as state: at
most, it is reported as a **proposal** to be carried into the canon through
a PR.

## The model

```
jalon (milestone) ──► epic ──► lot          WorkPackage cards in the canon
                                 │
                                 └──► work_items (#12, #13…)   state, L10 milestones, actions
```

A `WorkPackage` card (frontmatter only, like every card in the profile):

```yaml
type: WorkPackage
title: "Recherche plein texte dans le catalogue"
kind: lot                    # milestone | epic | lot
parent: catalogue            # identifier of another WorkPackage card
responsible: human:alice     # REQUIRED, resolved to a Member card (as for Agent)
team: acme-web               # optional
scope: ["src/catalogue/recherche/**"]   # file globs, optional
status: draft                # declarative, optional (lifecycle of the card, not progress)
id: cat-recherche            # optional: defaults to the file name without .md
start: 2026-10-12            # L96, optional: planned start, end, delivery (YYYY-MM-DD)
end: 2026-10-16
delivery: 2026-10-19         # a milestone may write `date:` (alias of delivery)
```

Rules checked by `ameesh canon check`:

| Code | Severity | Rule |
|---|---|---|
| `package-kind-invalid` | error | `kind` ∉ {milestone, epic, lot} |
| `package-parent-unknown` | error | `parent` designates no card |
| `package-kind-incoherent` | error | a lot sits under an epic or a milestone; an epic under a milestone (or nothing); a milestone has no parent |
| `package-cycle` | error | the parents form a loop |
| `package-responsible-missing` / `-unresolved` | error | lead missing, or not resolving to a single human Member |
| `package-duplicate`, `package-id-invalid` | error | duplicate identifier or outside the grammar (`[A-Za-z0-9][A-Za-z0-9._-]*`, 64 chars) |
| `package-lot-orphan` | warning | lot without a parent |
| `package-date-invalid` | warning | (L96) `start`, `end`, `delivery` or `date` unreadable (expected `YYYY-MM-DD`): the date is ignored, the card is still read |

A plan error **blocks no agent** (it is not part of the claim
condition); it appears in the canon diagnostics.

`ameesh canon sync` copies the cards into the `work_packages` table
(migration 0026) with their `canon_ref` (`member:path@commit`). A card in
error is not written (its previous copy remains); a card removed from the
canon is marked absent, never deleted; the attached lots follow the
current parent of their card.

Full example: [`examples/canon/plan/`](../examples/canon/plan/) (milestone
`v1`, epics `catalogue` and `paiement`, three lots).

## Lots (`ameesh work`)

```
ameesh work add --title T --package <fiche>       attach at creation (lot or epic)
ameesh work link <id> <fiche> | --none             attach / detach afterwards
ameesh work list [--stale-after 6h] [--json]       EPIC column, waiting, stagnation
ameesh work show <id> [--json]                     card, epic, PR, waiting
ameesh work close <id> --abandoned                 lot that will not be done
ameesh work close <id> --superseded-by <id2>       lot replaced or absorbed by another
ameesh work close <id> --delivered --sha <head>    lot delivered outside the target (#988)
ameesh work close <id> --done                      delivered without a merge (no branch)
ameesh work packet record <carrier> [--repo R]     link its composition units (#2443)
ameesh work packet show <lot|carrier> [--json]     recorded package composition
```

`package` on `work add`/`work link` is the **plan card** (`work_packages`).
The package-composition link of #2443 is separate: `work packet record` reads
the carrier's `integration/**` composition commits (`ameesh-work:` trailers,
never the manifest), and `work show` then prints `package : #<carrier> …`.

`close` moves the lot to the terminal state `closed` and records a `closed` milestone:
nothing stays open by oversight. A closed lot is **never reopened** by a
merge observed later (the observation is reported). `--done` (#1280) closes a
lot delivered without a merge — a review, an analysis — and is refused when
the lot carries a branch or a PR; on a review already closed `superseded by`
its own reviewed parent, it rectifies the reason without reopening. For a
review, the title only attaches the parent — `REVUE #N`, or the manual mission
`R1`/`R2 <harnais> #<parent>` (#1249) — and `--done` also requires the
review's `verdict` milestone on that parent (a `blocked` verdict counts: it is
a delivered review, not a product GO), so a delivered review is proven, never
deduced from its title.

`--delivered --sha <reviewed head>` closes a lot whose deliverable (report, note,
rehearsal) lives on a reviewed branch, without a merge into the target: the
`delivered` milestone carries the reviewed head and the closing actor, and it
counts as a delivery in `ameesh work estimates` (#988).

For each unmerged lot, ameesh says **what it is waiting for and from whom**
(`waiting_for`: `{what, who, label}`), derived from the state, the milestones (L10) and
the actions: start, author's work, reviewer's verdict, author's fix,
merge approval, merge, unknown merge outcome,
human decision (by the card's lead), unblocking. An action of the lot
waiting for a human (approval, reconciliation) takes precedence over everything; a
blocking verdict only counts if it follows the last freeze. When ameesh does not know
who, it says so ("relecteur non désigné" (reviewer not assigned)) instead of guessing.
**Moving a lot someone else holds** (#5035): `ameesh work move` is refused,
nothing changed, unless the actor is its holder or the agent who delegated it, a
human, a pilot, the bureau or the auditor, or a reviewer designated on a lot in
review. Entry to `validated` stays open: the review quorum guards it. A session
bound to an agent acts in its own name, whatever `--actor` says; `--correct` and
`--package-exclusion` keep their own authority.
The reviewer of a review can be **designated on entry to `qa`** (#705):
`ameesh work move <id> qa --reviewer <agent>` — a registry name, or a human
(`human:…`). A lot already in `qa` gets its reviewer with `ameesh work reviewer
<id> <agent>`, without a build/`qa` round trip; only a human, an orchestrator,
a design agent or the auditor designates (#3324, auditor card of 24/10, level
1), never the lot's assignee. The wait then names that reviewer before any
verdict, and the `stale_lot` alert reaches them; a `qa` lot
with no designated reviewer alerts the project's orchestrators, who must
designate one. A reviewer whose **declared harness** is the author's (claude,
codex and deepseek are distinct families) gets an indicative warning on
stderr (#800): the designation is kept — canon §2 asks for another model
family, and the real family is judged on the review turn (decision 18855a).
Two corrections are gestures of the same authority (#1979):
`ameesh work reviewer <id> --remove` removes all designations made by mistake
(the lot goes back to waiting for a reviewer, and the alert asks for one
again); `ameesh work reviewer <id> --no-verdict` marks "verdict not required"
when no re-verdict is due (mechanical composition resolution, canon §11
bis/#87, a verdict kept from a reviewed head) — the lot then waits only for
its merge and no reminder reaches a reviewer. Designating a reviewer lifts
the waiver; a current blocking verdict refuses it (a fix is due).

The **review class** of the lot (canon §2, #1822) routes that wait too: a light
lot's review happens after the merge, so a light lot with no current verdict
waits for the merge, not for a verdict — even when a reviewer is designated.
The class is read once per pass, and only for lots whose wait would otherwise
be a verdict; when it cannot be read (no repository, no freeze, unreadable
policy), the prudent verdict wait is kept instead of guessing a class.

Several reviewers can be designated for the same lot (#2076): repeat
`ameesh work reviewer <id> <agent>` to add a name. `work show` and the JSON
`reviewers` list retain them all. The wait and alerts name each reviewer
whose verdict on the frozen head is still due; one GO does not complete the
other reviews. Withdraw a designation explicitly with
`ameesh work reviewer <id> <agent> --remove` (the same authority is required);
a removal that removes nothing (an undesignated name, or an empty list)
changes nothing and writes no journal line. Assigning or delegating the lot
to a reviewer removes that person's designation and preserves the others;
the journal names each removed reviewer. Migration 0456 keeps the legacy `reviewer` field for older
runners during an upgrade. An older runner that writes `reviewer` as a
replacement therefore *adds* a name instead: the previous designation is
kept, and is withdrawn explicitly with `--remove` (or by upgrading the runner).

A lot neither merged nor closed with no activity (change, transition, note,
milestone, action, linked message) since the threshold (`--stale-after`,
`AMEESH_STALE_AFTER`, default 6 h) is **stale**: it is reported with the
duration, and the timeline no longer draws it as active.

These rules live in a single module, `ameesh.stagnation`, called by
`ameesh progress`, `ameesh work list/show` and the `stale_lot` alert of
`ameesh alerts` (which keeps its one-word `waiting_for` field and adds
`waiting`, the detailed form, and `project`).

## Observed merge: automatic closing

A merge is proven **by content**, never by name (a branch can be
renamed, a merge can be done by rebase, squash or cherry-pick). Five paths,
all idempotent, which set the lot to `merged` with its `merged` milestone
(merge commit and author):

1. **the gate**: when the `git-merge` action linked to the lot (`--work-item`) is
   confirmed (execution, late outcome or reconciliation);
2. **the content**, in a local clone (read-only, no `fetch`):

   ```
   ameesh work sync-merges --git-dir D [--target T] [--repo owner/repo] [--dry-run] [--json]
   ```

   Without `--target`, the target is the lot's (`branch_target`), else the
   repository's declared `git config ameesh.target`, else its default branch
   (`origin/HEAD`, then main/master); when nothing is declared and that ref does
   not carry the freeze, the integration refs (`develop`, `main`, `master`,
   `release/*`) are tried and the closing names the one that delivered. An
   explicit `--target` stays alone authoritative (a missing one is an error).

   only the lot's **last freeze** is authoritative (most recent `frozen`
   milestone, with its commit): an earlier freeze already merged proves nothing
   about the current work. That commit is an **ancestor** of the target; otherwise
   every commit of the freeze has its **`git patch-id --verbatim`** among the commits
   of the target since their common base (rebase, cherry-pick); otherwise the whole
   diff of the freeze is one commit of the target (**squash**). `--verbatim` does not
   normalize any whitespace (`--stable` would confuse `"a b"` and `"ab"`): false
   negative rather than false positive; without it (git < 2.40), only ancestry
   proves. Each of these proofs is voided by an **effective revert** (#1825): a
   later `Revert "…"` that cites the commit carrying the content, undoes exactly
   its patch, and is reapplied by no later commit — the content is not delivered
   and the lot stays open (`how: reverted`). Detection is best-effort, not a
   guarantee: it scans only the most recent commits of the target (200) and
   requires `git patch-id --verbatim` (git ≥ 2.40); a revert outside that window,
   or an ancestry proof without `--verbatim`, still closes a reverted lot
   wrongly. `sync-branches` and `sync-merges` carry the same guard. Closing is
   conditioned in the database on the examined freeze: a new
   freeze declared in the meantime prevents it (`refrozen`).

   A closure by content is **withheld** (#2744) while the lot is `blocked` or
   carries an unlifted `hold: …` note (#1664): the merge proof stays in the
   sweep report (`how`, `ref`, `target`), the journal receives once
   `contenu fusionné, fermeture retenue : <motif>`, the hold holder (or the
   agent who blocked the lot) is woken, and the lot stays open. An `unhold:`
   note or a state change lifts the hold; the next sweep closes. #3219: the
   assignee's own rule-1 holds (not a pilot's holds) do not withhold the
   closure — the proven content is the delivery, so the sweep lifts them and
   the merge note says `levée du hold de l'assigné <agent>`. The holds of a
   lot are a set: a hold of a pilot or of another actor still withholds even
   when the assignee's hold covers it and after the assignee lifts theirs
   (its author or a pilot may lift it). The check and the closure share the
   lot's row lock — hold/lift notes (`--wake-at` included), moves and state
   changes take it too — so a retention cannot slip between the journal read
   and the close. A journal read error is an explicit `error` (the lot stays
   open, never closes), not a silent hold. The same guard covers
   `sync-branches` and the units of `sync-packets`.

   A package's units are closed by its manifest, without a card or
   `package_id`: on the target, `reviews/**paquet**.md` lists the composed
   units. `ameesh work sync-packets --git-dir D [--target T]` (read-only)
   examines only those units' last freeze, the targeted sweep that
   `sync-merges` cannot be (all open lots, more than 120 s on ameesh); the
   runner does the same after each branch sweep on the fresh target of each
   `radar` project (#2426). A unit without a freeze is closed by the
   `ameesh-work: <id>` trailer of its composition commit when that commit is
   an ancestor of the target; a withdrawn line or a re-freeze declared after
   the composition closes nothing;
3. **the linked PR** (as a fallback, or alone):

   ```
   ameesh work sync-github --repo owner/repo [--limit 200] [--dry-run] [--json]
   ```

   reads merged PRs (`gh pr list`, read-only); a PR is linked to
   a card by its branch `lot/<id>` or `lot/<id>-…` (longest
   case-insensitive match) or by an `ameesh-lot: <id>` line in the body;
   an `ameesh-work: <n>` line designates one specific lot (and only that one).
   Only lots created **before** the merge are closed.

4. **the lot's branch** (L118), with or without PR or freeze:

   ```
   ameesh work sync-branches [--host H | --all-hosts] [--dry-run] [--json]
   ```

   a lot that carries its branch (`work add|assign --branch`, or an `agent/…`
   branch cited by a `mail send --lot`) is closed when the branch enters
   its target: tip brought in by a merge commit, last commit seen
   ahead found in the target (ancestor, patch-id, squash), or merge
   commit citing the branch. The runner performs this check every 5 min
   in the assignee's working directory; see EXPLOITATION.md,
   "Courrier lié aux lots et fusion des branches" (lot-linked mail and branch merges). The same check closes a
   lot, with or without a branch, that a **merge** commit of the target designates
   with an `ameesh-work: <id>` line (or with `#<id>` in its title if the
   repository enables it: `git config ameesh.lotRef hash`);
5. **the declaration** (fix of 2026-10-11), for a merge nothing
   else sees:

   ```
   ameesh work merged <id> --sha <merge commit> [--note …]
   ```

   any open state → `merged`, in one step, `merged` milestone with the commit.

A lot absorbed by another is closed with `--superseded-by`; a lot delivered
outside the target (its reviewed head stays on its branch) with
`--delivered --sha <reviewed head>` (#988).

## GitHub projection

```
ameesh work project-github --repo owner/repo [--dry-run] [--canon-url URL] [--json]
```

* one issue per `lot` card: title `[<id>] <title>`, link to the card
  (`--canon-url https://…/blob/{commit}`; otherwise the `canon_ref`), labels
  `ameesh` and `ameesh:<state>` (`intake`, `build`, `qa`, `merged`, `blocked`,
  `waiting_human`); closed (completed) when the lot is merged, closed
  (not planned) when it is abandoned;
* one issue per `epic` (label `ameesh:epic`), progress in the body, and
  its lots as **sub-issues** (GitHub sub-issues API); if the API is
  unavailable, a `- [x] #12` task list in the epic's body;
* each issue carries a hidden marker `<!-- ameesh:package=<id> d=… -->`:
  it finds the issue among **all** the repository's issues, whatever its
  labels — never a duplicate, even if the `ameesh` label was removed (idempotent
  update: nothing is written if nothing changed) and keeps the fingerprint of what ameesh wrote. A title, a body,
  an `ameesh…` label or a state changed in GitHub is reported as
  `PROPOSITION` (to be carried into the canon), then the view is rewritten. Other
  labels are never touched;
* `--dry-run` reads GitHub and shows what would be created or updated, without
  writing anything.

The **lots** themselves, attached to a card or not, each have their own issue
(L126): `ameesh work project-github --app <projet>`, repository read from the
host configuration, kept up to date automatically by `ameesh notify`; see
[EXPLOITATION.md](EXPLOITATION.md), "Issues GitHub des lots" (GitHub issues for lots).

`gh` is resolved as for the `git-merge` connector (`AMEESH_GH_BIN`,
`AMEESH_BIN_DIR/gh`, PATH); the tests use a fake `gh`
(`tests/fake_github.py`), never GitHub.

## `ameesh progress`

Fields **added** to the `ameesh-progress/1` schema (see [PROGRESS.md](PROGRESS.md)):
`epics` (lots grouped by epic, progress), `milestones` (the `milestone`
cards), `stale_after_s`, and per lot `package`, `epic`, `pr_ref`,
`closed`, `waiting_for`, `last_activity_ts`, `stale`. The text and the page
show waiting, stagnation and the EPICS section.

## Roadmap: planned dates and commitments (L96)

The plan says **what**; the roadmap says **by when**, and compares with
reality. Migration 0044.

* **Planned dates** of a task (`ameesh work plan <id> --debut J --fin J
  --livraison J --source S`) and of a card (`ameesh work plan <fiche> …`,
  which takes precedence over the canon dates above). Rescheduling is not
  an activity of the task.
* **Dated commitments** (`ameesh plan add "…" --pour J --projet P --source
  S [--lot N] [--depend-de REF]`) and **decision milestones** (`--decision`);
  `plan accept | done | cancel`. A commitment attached to a task is met
  at its delivery.
* **Proposals** (`ameesh plan propose [--from docs/design/decisions]
  [--record]`): drawn from `Decision` cards (open question → decision
  milestone; "pour le 2026-10-20" (by 2026-10-20), "on fera ça lundi" (we'll do it on
  Monday) → commitment; "lots L97–L104", "même vague que L95" (same wave as L95) →
  item and dependencies) and from the body of open tasks. To be validated: nothing is created without `--record`,
  nothing is dated without `plan accept`.
* **Gantt**: `ameesh plan show [--json]` and the `roadmap` section of `ameesh
  progress` (shown by default, `--no-gantt` to remove it) — epics and
  milestones (planned from the card, actual from their tasks), tasks (planned versus the
  generic actual milestones: requested, in progress, submitted, verdict, delivered),
  commitments, pending decisions, today line, delays, source of each
  item; plus "who is advancing on what".
* **Alert** `engagement_overdue` (pushed by `ameesh notify`): a
  commitment or a date set by a person passed without the item being
  reached. Dates computed by `work schedule` (source `reference p80…`) are
  re-dated on its next pass, and a lot no longer driven by its holder
  (blocked, under review, or held by a planned wait) raises no date alert;
  a lot with both a date and an engagement gives one alert (the engagement).

Project tracking is in the **generic core**: the roadmap code
names no business state or milestone; the mapping to the
current lot lifecycle lives in `roadmap_dev.CORRESPONDANCES`, which the future
"development" module will take over. Details, the `ameesh-roadmap/1` schema and
the orchestrators' prompt: [EXPLOITATION.md](EXPLOITATION.md), "Feuille de route" (roadmap)
section.

## Continuous improvement queue (L119, decision 0037)

```
ameesh work backlog add --title T --value "valeur attendue" --score 1-100
                        [--priority 1|2|3] [--source S] [--team E] [--requires CAP …]
                        [--app P [--new-project]] [--chantier C] [--review-of LOT]
ameesh work backlog list [--all] [--json]
```

The item inherits its creator's project, as `work add` does (L118c, #495);
`--app` must name a known project, otherwise the item is refused with the
close names; `--new-project` introduces a project nobody declares yet, and
the item's history records who did (#506, as `work add` and `--new-lot`).

A queue item is a lot of type `improvement`, in `intake`, with no
assignee; its expected value (one sentence) and its score are mandatory: no
busywork. The queue is read in pick-up order: priority, then
score, then age. A queued item is neither stale nor a "lot without
assignee" for `idle_capacity`: it is waiting for its taker.

On each pass, `ameesh notify` hands the best-ranked item that
matches the team and capabilities of a wakeable agent idle for
`--take-idle` (30 min), through guarded assignment and a lot-linked mail.
Safeguards:

* **only if no lot is waiting for it**: no open lot, no unread mail,
  no pending prompt, no session lot still open; and as long as a lot
  of the project is waiting for a taker, no improvement is taken;
* **never an irreversible or production action**: the pick-up mail
  forbids it (deployment, merge, data deletion, production
  server, committed spending); the agent proposes it to a human;
* plans first; what they leave is taken by the pay-per-token agents in the
  same pass, never during `balance_low`; nothing under a reached cap nor on a
  host under pressure, on low battery or not ready; at most
  `--take-max-per-hour` (2) pick-ups per sliding hour for the whole mesh.

When agents are idle and the queue is empty (and no lot of the
project is waiting), the `backlog_empty` alert invites the human lead to
fill it.

#### Workstream basket and lot threshold (#2584, canon §2 l.129)

Canon §2 (owner 15/10/2026, made precise on 19/10/2026): a non-blocking
defect (N, P2, P3) does not create a lot; it becomes a lot only from the
canon's value threshold, and a defect found while reviewing a review
follow-up creates nothing. The threshold is `backlog.lot_min_value` in
`processus/mecanique.yaml`, read at the canon's exact commit
(`backlog.rules`). **Without that block nothing below applies**: no threshold
is assumed (§7), every item stays a lot as before.

* **Under the threshold, a basket line, not a lot.** The item is appended to
  the open basket of its workstream for the day (canon timezone): one
  `improvement` lot per project, workstream and day, whose `source` is the key
  `backlog-basket:<project>:<workstream>:<day>`. The workstream is
  `--chantier`, else the sheet (`--package`), else the sheet or the number of
  the reviewed lot, else the project. The basket's value is its highest line,
  its priority the highest one, its required capabilities the union of the
  lines'; each line is also a journal note (`basket line …`). A basket already
  taken (assigned, or out of `intake`) is closed: the next line opens a new
  one. Two simultaneous adds never open two baskets (one writer per key).
* **At or above the threshold**, the item stays its own lot; with
  `--review-of N` its source starts with `review-of:#N`.
* **No N on a review follow-up.** The reviewed lot is `--review-of`, else the
  `#N` of a review source ("relecture #313 (qa-01, N1)", "verdict … sur
  #2577"), else the `#N` of a follow-up title ("#2464 N (dev7): …"); an
  after-merge review lot points to the lot it reviews. When that lot is a
  basket, a backlog item born of a review, or a lot marked N/P2/P3, the add is
  refused: the reviewer fixes it on the branch or leaves it.
* **Roadmap first.** `work assign` refuses to give an item under the threshold
  (a basket, an old low-value item) to an agent while a roadmap lot of its
  project can be taken (unassigned, takeable, no planned wait, neither an
  after-merge review nor a follow-up); a human may still take it. The idle
  pick-up already held the whole queue while any project lot waits.

### WIP limit: at most 2 lots in `build` per agent (§4)

Owner 14/10/2026, canon §4: an agent holds at most
`build.max_in_build` lots in `build` (2 today), **all projects
confused** (the cap is global, not per team). Since #2451 the value is
read from the compiled canon (`ameesh.work.build_limit`, global
document of `processus/mecanique.yaml`); without a readable canon the
sweep and the automatic pick-up refuse rather than assume a cap. Two
guards:

* **the automatic pick-up refuses the third lot**: the count and the
  assignment are written in one transaction under the agent's advisory
  lock (`assign_capped`), so two simultaneous pick-ups cannot cross the cap;
* **the sweep returns the surplus**: `ameesh work wip [--apply] [--limit N]
  [--json]` gives back to the team queue the lots of an agent above the
  cap, the least recently active first. Without `--apply` it only shows
  what would be returned (dry run, nothing written). Each return is
  revalidated **under the agent's lock** (`unassign_surplus`): the lot must
  still be `build`, still assigned to that agent, carry no pilot exception,
  and the surplus must still be real — two concurrent sweeps never return
  more than the surplus, and a lot moved to `qa` or pulled back meanwhile is
  left alone. A returned lot keeps its state, its branch, its branch head
  and its reviews; an `event` mail tells the agent, and a free agent of its
  team can take it as it is.

An agent can hand back an `intake` or `build` lot itself: `ameesh work
return <id> [--reason T]` (the assignee, a human, or a pilot); a lot under
review (`qa`), waiting for a human or blocked is not handed back that way.
The pilot's **explicit** exception is recorded on the lot's last assignment
by `ameesh work assign <id> <agent> --wip-exception "why"` (human or
orchestrator/design); the sweep leaves that lot alone. Every note that hands
a lot over counts as an assignment (assignment, delegation, automatic
pick-ups, budget handoff), so the next one without the marker removes the
exception; `work assign` on the same agent with a branch only sets the
branch, leaving the running delegation and the pilot exception in place.
`ameesh list` shows a per-agent `BUILD` column (flagged with `!` above the
cap), `ameesh progress` carries `builds`, and `ameesh notify` can run the
sweep (`--wip-max-returns N`, off by default; `--wip-apply` returns the
lots, without it the pass is a dry run).

### Handoff on a budget pause (L233b, L165)

On each pass, before any pick-up, `ameesh notify` handles the lots of a
**non-critical Claude agent paused on budget** (status `blocked`,
`budget : …`):

* its **non-started lots (`intake`) are returned to the team queue**:
  assignee cleared, a journal note says why; the automatic take (L165) then
  gives them to any free capable agent of the team, DeepSeek or Codex, within
  its budget and review class — no taker needs to be free at that moment;
* its **started lots (`build`) are handed** to a **free DeepSeek or Codex
  agent of the same project**, within its budget and able to do the lot
  (team, capabilities): one lot per taker, longest idle first,
  `--handoff-idle` (5 min) is the taker's minimum rest.

At most `--handoff-max-per-hour` (4) returns and handoffs per sliding hour
for the whole mesh (`0` turns it off). Critical agents are those of L242b
(declared, orchestrators, design, auditors). A returned lot gets its journal
note only (the automatic take warns the taker); a handed `build` lot also
gets an `event` mail to both agents. Never handled:

* a lot under review or frozen (`qa`, or a freeze milestone);
* a sensitive lot: reserved capability (`decision`, `production`), or a
  pushed branch touching a `sensitive` path of the canon's review policy (an
  unreadable policy counts as sensitive);
* a lot waiting for a decision (request or decision milestone);
* a lot whose last assignment came from a human (`human:…`);
* a `build` lot whose branch is not pushed, or where a worktree of the
  paused agent (its working directory, the lot's tracked worktrees) has
  uncommitted changes or unpushed commits: it is skipped with a note on the
  lot, once per pause. Those worktrees are only read on the paused agent's
  host.

When the pause ends, nothing comes back on its own: the note says so, and
the orchestrator decides. A review by another family stays required.
