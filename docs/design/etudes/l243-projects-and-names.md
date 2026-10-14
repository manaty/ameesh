---
type: Study
title: "Multi-project agents and the default agent naming"
description: "L243 (lot #419): a `projects:` list on Agent cards, consumers that stop assuming one project per agent, and the default name <org>-<repo>-<role>-<seq>; four implementation lots."
status: draft
tags: [canon, agents, projects, naming]
generated: { by: "mesh-design", at: "2026-10-12" }
---

# Design for lot #419

Owner validation, 2026-10-12 15:05 (relayed by bureau, #10433):
- (1) an agent can work on several projects;
- (2) new agents get the name `<org>-<repo>-<role>-<seq>`;
- (3) existing agents are renamed, keeping their old names as aliases. This
  is lot #423 (L243b) and is out of scope here.

## Where we start

An Agent card has a single `team:` (`canon.py`, `Agent.team`). It is synced
to `agent_registry.team` (migration 0008). About twenty consumers read it, and
every one of them expects a single string. Most fall back to `chantier`.

- Pick-up: `backlog.lot_fits` and `backlog.fits` compare the lot's team
  (`coalesce(w.team, package.team, parent.team, w.app)`) with `agent.team`.
- Orchestrators: `backlog._team_orchestrators`, `alert_briefs._of_teams` and
  `sous_utilisation` keep the orchestrators whose team is the agent's team.
- Display: `projects.agent_project` and `fil.agent_project` (plus the
  `mailbox.PROJECT_SQL` copy of the same rule) give one project per agent.
  `ameesh projects`, `mesh list` and the threads use it.
- Work directory: `policy.work_dir(agent.team, agent.title)`.

## (1) `projects:` on Agent cards

```yaml
title: mty-amh-impl-01
projects: [ameesh, nexlink]   # first = primary project
team: ameesh                  # still read when projects: is absent
```

- **Effective projects.** If `projects:` is present, it is used. Otherwise
  `[team]`, then `[chantier]`, then `[]`. When both `projects:` and `team:` are
  set and `team` is not `projects[0]`, the canon check reports
  `agent-team-mismatch` as an error. A card never carries two truths.
- **Storage.** A new column `agent_registry.projects text[]` is added, and
  `projects` joins the `DECLARATIVE` set of `canon_sync`. `team` keeps being
  written as `projects[0]`, so consumers that need only one value keep reading
  the primary project unchanged. Nothing reads `chantier` for canon-governed
  agents any more.
- **Validation.** An unknown project gives an `agent-project-unknown` warning.
  A project is known when it appears in the federation's `projects`, in
  `github.projects` or in a lot's `app`. Duplicates in the list are an error.
- **Consumers.** Each of these changes from "equal to the team" to "member of
  `projects`":
  - pick-up (`lot_fits`, `fits`);
  - orchestrator selection (`_team_orchestrators`, `alert_briefs`,
    `sous_utilisation`);
  - `roadmap --project` for agents;
  - `progress` (`r.team = %s` becomes `%s = any(r.projects)`).

  In `ameesh projects`, an agent appears in the group of every project it
  belongs to. Only its primary group carries its cost, so totals are not
  counted twice; the other groups mark it `(+)`. The `mesh list` project
  column shows `ameesh+1`.
- **Threads and mail.** These do not change. A message about a lot goes to the
  lot's project (`app`/`workstream` first, as today). A message without a lot
  goes to the sender's primary project.
- **Pick-up order.** Each turn, a multi-project agent looks at the unassigned
  lots of all its projects with the existing ordering: priority, then age. The
  canon can make one project win ties with `project_weight: {nexlink: 2}`.
  This is deferred until someone needs it; there is no field for it today.
- **Work directories.** The agent's `cwd` stays the work directory of its
  primary project. A lot from another project gets its worktree under that
  project's work root, `policy.work_dir(lot_project, agent)`. Its session is
  opened there through the per-lot sessions of #408 (L165b, merged:
  `agent_lot_sessions`, migration 0384), which store `cwd` per (agent, lot).
  Until #811 is merged, a multi-project agent only picks up lots from its
  primary project automatically. Lots from other projects must then be
  assigned to it explicitly.

## (2) Default name `<org>-<repo>-<role>-<seq>`

- **Segments.** They are declared in the canon's federation file, so each
  organisation chooses its own:

  ```yaml
  extensions:
    ameesh:
      naming:
        org: mty
        projects: {ameesh: amh, nexlink: nxl}
        roles: {implementer: impl, designer: conc, qa: qa, reviewer: rev,
                orchestrator: orch, operator: ops}
  ```

  The segments are lower-case `[a-z0-9]{2,6}`. When a project has no short
  name, its name is used truncated to 6 characters. Role keys are the roles of
  #414 (L242c, home#25). A role with no short name is refused, so a name is
  never guessed.
- **Form.** A single-project agent is named `mty-amh-impl-01`. A
  multi-project agent drops the repo segment: `mty-impl-01`. The sequence has
  two digits (three after 99). It counts per prefix, and a number is never
  reused: retired names stay reserved, because mail, threads and lots keep
  referring to them. The harness is never part of the name. `NAME_RE` already
  accepts these names.
- **Generator.** `ameesh canon agent name --role implementer
  [--project ameesh …]` prints the next free name. The candidates are checked
  against every card, every registry row (including stopped agents) and every
  alias (#423). `canon agent spawn` uses the same generator when no name is
  given.
- **Check.** A new card whose title does not follow the convention gets an
  `agent-name-convention` warning, not an error. Existing names stay valid
  until #423, and an organisation without a `naming:` block gets no warning at
  all.

## Lots

Implementation goes to DeepSeek agents, at the owner's request of 12/10
14:46. Every PR is reviewed by Codex. Durations are design estimates.

| Lot | Content | Depends on | Estimate | State (13/10) |
|---|---|---|---|---|
| #435 | Card field, validation (`agent-team-mismatch`, `agent-project-unknown`), migration `projects text[]` (0388), sync with `team = projects[0]`, both drivers and grants | — | 3 h | merged |
| #810 | Consumers: pick-up, orchestrators, alert briefs, idle-capacity briefs, `roadmap --project`, `progress`, `ameesh projects` and `mesh list` display | #435 | 3 h | intake |
| #811 | Work directory per lot project, session `cwd` taken from the lot (`agent_lot_sessions`) | #810 | 2 h | intake |
| #468 | `naming:` block, generator `canon agent name`, `spawn` without a name, `agent-name-convention` warning, reserved retired names | #414 | 3 h | merged |

#423 (renaming with aliases) uses the names of #468 and adds the alias layer.

## Out of scope

- Renaming the existing agents: #423.
- Per-project budgets for one multi-project agent. Today the cost is counted
  in the primary project.
- Weighting pick-up between projects (`project_weight`). Deferred until there
  is a need.
