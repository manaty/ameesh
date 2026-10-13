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
```

`close` moves the lot to the terminal state `closed` and records a `closed` milestone:
nothing stays open by oversight. A closed lot is **never reopened** by a
merge observed later (the observation is reported).

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

A lot neither merged nor closed with no activity (change, transition, note,
milestone, action, linked message) since the threshold (`--stale-after`,
`AMEESH_STALE_AFTER`, default 6 h) is **stale**: it is reported with the
duration, and the timeline no longer draws it as active.

These rules live in a single module, `ameesh.stagnation`, called by
`ameesh progress`, `ameesh work list/show` and the `stale_lot` alert of
`ameesh alerts` (which keeps its one-word `waiting_for` field and adds
`waiting`, the detailed form).

## Observed merge: automatic closing

A merge is proven **by content**, never by name (a branch can be
renamed, a merge can be done by rebase, squash or cherry-pick). Five paths,
all idempotent, which set the lot to `merged` with its `merged` milestone
(merge commit and author):

1. **the gate**: when the `git-merge` action linked to the lot (`--work-item`) is
   confirmed (execution, late outcome or reconciliation);
2. **the content**, in a local clone (read-only, no `fetch`):

   ```
   ameesh work sync-merges --git-dir D [--target origin/main] [--repo owner/repo] [--dry-run] [--json]
   ```

   only the lot's **last freeze** is authoritative (most recent `frozen`
   milestone, with its commit): an earlier freeze already merged proves nothing
   about the current work. That commit is an **ancestor** of the target; otherwise
   every commit of the freeze has its **`git patch-id --verbatim`** among the commits
   of the target since their common base (rebase, cherry-pick); otherwise the whole
   diff of the freeze is one commit of the target (**squash**). `--verbatim` does not
   normalize any whitespace (`--stable` would confuse `"a b"` and `"ab"`): false
   negative rather than false positive; without it (git < 2.40), only ancestry
   proves. Closing is conditioned in the database on the examined freeze: a new
   freeze declared in the meantime prevents it (`refrozen`);
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
  commitment or planned date passed without the item being reached.

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
                        [--app P [--new-project]]
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

### Handoff on a budget pause (L233b)

On each pass, before any pick-up, `ameesh notify` hands the `intake` and
`build` lots of a **non-critical Claude agent paused on budget** (status
`blocked`, `budget : …`) to a **free DeepSeek agent of the same project**,
within its budget and able to do the lot (team, capabilities): one lot per
taker, longest idle first, at most `--handoff-max-per-hour` (4) per sliding
hour for the whole mesh (`0` turns it off; `--handoff-idle`, 5 min, is the
taker's minimum rest). Critical agents are those of L242b (declared,
orchestrators, design, auditors). The assignment stays guarded and
conditional on the assignee and the state; a note goes on the lot and an
`event` mail to both agents. Never handed:

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
the orchestrator decides. The codex review stays required.
