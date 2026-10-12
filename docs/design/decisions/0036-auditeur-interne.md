---
type: Decision
title: "An internal auditor checks ameesh's operations every hour"
description: "An auditor persona, on the most capable model the organisation assigns to it and at the highest effort level of its harness (amendment of 2026-10-11), runs every hour and on every urgent alert over the state of the mesh, and every night and after every notable incident conducts a deep analysis of time losses down to their root cause. It fixes, merges and deploys ameesh bugs on its own when CI is green, acts alone on what is reversible and proposes the rest; every accepted rule becomes a decision."
status: stable
tags: [exploitation, audit, ressources, budgets, auto-reparation]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "owner's decision in his conversation with mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-10T18:30:00+02:00" }
sources:
  - { resource: "../../AUDITEUR.md", title: "Internal auditor prompt" }
---

# Context

On 2026-10-10, the owner asked "that ameesh have an internal auditor which
regularly, for example once an hour, checks that everything is going well,
that resource usage is optimal, and adapts the rules if needed".

ameesh already raises alerts: liveness ([0030](0030-pas-de-travail-sans-reveil-possible.md)),
under-use (L94), host pressure ([0028](0028-ressources-des-hotes-et-repartition.md)),
low balance. It pushes them to the responsible human. Nobody, however, reviews
them together, makes the reversible move that is often enough (resuming an
agent, lowering a cap, stopping an idle CI machine), or turns a recurring
finding into a rule.

# Decision

1. **An `auditeur` persona**, ameesh team, responsible `human:smichea`,
   DeepSeek harness, model `deepseek-flash`, API key. Its card is in the
   canon, with the capabilities `[read, report-drift, propose]` and no other.
   *Amended on 2026-10-11, see below: the model is no longer fixed by the
   decision; it is the most capable one the organisation assigns to the
   auditor, at the highest effort level of its harness.*
2. **Rhythm**: one pass per hour, and one pass on every urgent alert.
   The pass is short: the checklist, the permitted moves, the report.
   *Amended on 2026-10-11: a deep analysis is added every night and after
   every notable incident.*
3. **Checklist**, kept in [the prompt](../../AUDITEUR.md) with the exact
   commands:
   - agents that are stuck, stopped or spinning idle; long turns; sessions
     that grow;
   - plans that are under-used or at the threshold; actual spend against the
     estimate; balance of the pay-per-token provider;
   - load of the workstation and the servers;
   - queue of the Nexlink CI runners and state of the reserve machine, read
     through the GitHub API, with no secret in the prompt;
   - lots without an agent and overdue commitments;
   - security drifts visible from ameesh.
4. **Room for action on two levels**, written in the card:
   1. *It acts alone, because it is reversible*: `ameesh resume` or
      `ameesh restart` of a stuck agent or of a session that is too big;
      nudging the orchestrator by mail; **lowering** a cap
      (`ameesh budget set`, never upwards); starting or stopping the CI
      reserve through the existing script if its access allows it, otherwise
      it reports it.
   2. *It proposes and the human decides*: any rule change (choice of
      accounts, thresholds, caps upwards), adding or removing machines, any
      new spend. It goes through a PR on the canon or an approval request.

   It never uses `approve` (R8, [0012](0012-autorite-par-ameesh-approve.md)).
   *Amended on 2026-10-11: it fixes, merges and deploys ameesh bugs on its
   own, CI green, and never suspends the work of others.*
5. **Reporting**: a short hourly report in the ameesh project thread; an
   alert pushed only when a human decision is needed; a daily summary.
   *Amended on 2026-10-11: a report at the end of each deep analysis, one lot
   per root cause, and the measurement of its usefulness.*
6. **Learning**: every rule proposed and then accepted becomes a written
   decision. This is the self-repair loop of
   [0022](0022-auto-reparation.md), applied to operations.

# Consequences

- Card `agents/auditeur.md` and placement on the workstation in the manaty
  canon; commissioning by the runner (`ameesh-runner-agent@auditeur`), after
  the card is merged.
- **Rhythm in v1**: the runner nudges an idle agent only once
  (`--idle-nudge`), then waits for mail. The hourly pass therefore comes
  from a systemd timer that writes to it, and the urgent-alert pass from a
  second timer that reads `ameesh alerts`. Both are described in the
  prompt. A lot to be opened will make alerts routable to an agent in
  `ameesh notify`, which will replace the second timer.
- The auditor touches neither the code, nor the canon `main`, nor secrets; its
  level-1 moves can all be undone by an inverse command.
  *Amended on 2026-10-11: it touches ameesh code, through PR and CI, for bug
  fixes only.*

# Amendment of 2026-10-11: deep analysis at maximum effort, and bug fixing

Owner's request on 2026-10-11; prompt [AUDITEUR.md](../../AUDITEUR.md)
updated.

## Finding

The auditor has never run: its canon card has not been merged.
Meanwhile, during the night of 2026-10-10, an analysis conducted without it
showed what the owner expects. For each time loss of a project served by
the mesh, it followed the chain of causes down to its root in ameesh, then
it launched the fixes. Four angles, conducted in parallel by read-only
sub-agents: the timeline of a critical path and its waits; the cost of
coordination; the causes in ameesh code; the execution incidents. It found
about ten root causes; four fixes went out as PRs and lots were opened.

On 2026-10-11, the owner specified what he expects: "an agent that
regularly analysed ameesh's activity and fixed the problems, exactly like
what we are doing right now; it is important that this agent be at max
effort". The same day he ruled that **bug fixes are merged and deployed
without his validation**. New features, migrations that change behaviour,
production moves on projects, spending and secrets remain his decisions.

The hourly pass of §2 is not enough for this: with about ten commands, it
sees the symptoms (the alerts), not the chain that leads to them.

## Model and effort (§1)

**The model is no longer fixed by the decision.** The auditor runs on the
most capable model the organisation assigns to it, at the highest effort
level of its harness. The concrete choice (harness, provider, model, effort,
authentication by subscription or by key) belongs to its canon card; the
account is chosen at each session by ameesh (amendment of
[0034](0034-consommer-d-abord-ce-qui-expire.md)). The capabilities remain
`[read, report-drift, propose]`: merging a bug fix is not `approve` (R8), it
is the owner's rule.

Effort is set per agent, `ameesh set auditeur effort=<niveau>`. ameesh
passes the value as is to the harness, through its descriptor (command-line
option or configuration file), without validating it: the level chosen is the
highest the harness accepts, as listed by its command-line help. ameesh does
not yet read an effort from the canon (only `model` is synchronised):
commissioning applies the setting, and a lot will make effort declarative in
the canon.

Cost follows [0019](0019-budgets-et-routage.md): plan rhythm, spending caps
and pausing when all accounts are at the threshold apply as they are. A deep
analysis at maximum effort, with its sub-agents, costs far more than a short
pass; prepaid capacity that would be lost at reset (`plan_underused`) is its
best use.

## Two kinds of passes (§2)

1. **Short pass**, every hour and on every urgent alert: the checklist of
   §3, kept, the permitted moves, the report. A notable incident is not dug
   into there: it opens a deep analysis.
2. **Deep analysis**, every night, after every notable incident and at the
   owner's request. It reuses the method of the night of 2026-10-10:
   four angles conducted in parallel, each by a read-only sub-agent:
   1. *timeline*: the critical path of each active project and its waits
      (who was waiting for what, from whom, for how long);
   2. *cost of coordination*: messages, turns, copies, acknowledgements,
      broadcasts, relays, rotations;
   3. *causes in ameesh code*: for each loss, the file and the line, the
      mechanism, the fix, the test that reproduces it;
   4. *execution incidents*: runner logs, host pressure, killed turns,
      cleanup, CI.

   The auditor does the synthesis itself. It links each time loss to its
   root cause, with evidence at each link, ranks the causes by time lost
   and sets aside those already covered by a lot or a PR.

A notable incident is: an agent stopped after failures, a turn killed or
closed by a cap, critical pressure on a host, an orphaned resource, a red CI
on the main branch, a deployment rollback, a wait of more than one hour on a
critical path, an urgent alert that a short pass did not resolve.

Only one analysis at a time, and at most two post-incident analyses per day on
top of the nightly one: beyond that, the incident waits for the night. Under
host pressure, the angles run one after the other; under critical pressure,
the analysis waits. The sub-agents run in its harness, under its account:
the analysis incurs no spend.

## What it produces (§5)

- **A short report to the owner**, in the ameesh thread: at each short
  pass, as before, and at the end of each deep analysis (time lost found,
  root causes, what is fixed, what is proposed).
- **One lot per root cause**, with its chain of causes, its evidence and the
  measurement of time lost: a `bug` lot that it fixes itself, an item in the
  improvement backlog ([0037](0037-jamais-a-l-arret.md)), or a rule
  proposal. A cause outside ameesh (a project's rule, a host's configuration,
  a provider's limit) goes to whoever is in charge of it.
- **Fixes as PRs for ameesh bugs.** It merges and deploys them when CI is
  green; it proposes everything else.

## Room for action (§4)

The two levels remain, and a third one is inserted between them.

1. *It acts alone, because it is reversible*: unchanged, except what would
   suspend someone else's work. `ameesh restart` only targets an agent
   outside a turn; lowering a cap must not put anyone on pause at that
   moment, otherwise it alerts. Starting the CI reserve remains permitted:
   it is a spend that this decision already allowed, bounded by stopping the
   reserve after 30 minutes of inactivity.
2. *It fixes ameesh bugs, merges and deploys them.* A bug is a behaviour
   contrary to a decision, to the specification, to the documentation or to
   the manifest intent of the code; its fix restores it without adding a
   command, an option, a configuration key, an alert or a rule. When in
   doubt, it is an evolution, and it proposes it. The circuit:
   - a `bug` lot and a branch in a fresh worktree;
   - a test that fails before the fix and passes after (except for a
     documentation fix), then the smallest fix;
   - a PR that states the observed loss, the chain of causes and its
     evidence, the fix and its inverse;
   - CI green on an up-to-date base;
   - before the merge, the verdict of a reviewer other than itself: a fresh
     read-only sub-agent that knows only the lot, the evidence and the
     diff; for the sensitive class of [0018](0018-vitesse-des-agents.md) (SQL
     and migrations, security, authorisation, runner, gate, approval,
     canon reading, deployment), another agent of the mesh, from another
     provider when there is one;
   - the merge, then the main-branch CI green on the merged commit;
   - deployment through `deploy/mise-a-jour`: check, back up, install,
     restart each runner outside a turn, verify; `retour` (rollback) at the
     first failed check, then a PR that reverts the fix. It launches the
     restart outside its own turn, and its runner restarts like the others,
     between two turns. A fix that carries a migration is deployed by it
     only if the migration is additive and changes no behaviour, so that
     `retour` remains possible without touching the database.
     Deploying a commit deploys everything that separates it from the
     installed commit: if that gap carries anything other than bug fixes (an
     evolution merged but not yet deployed, a migration that is not
     additive), it does not deploy, and its fix waits for the owner's
     decision. If it has no access to one of the mesh hosts, it does not
     deploy halfway: the fix stays merged and it reports it.
3. *It proposes, the human decides*: unchanged. Added to this are evolutions
   (new features), migrations that change behaviour, publishing a release,
   and any fix it cannot carry through to the end of the circuit above.

## Safeguards

- Never a production move on a project served by the mesh, never a new
  spend, never a secret read, copied or written.
- It never modifies its prompt, this decision, its canon card, its
  capabilities or any permission (database roles, repository rights,
  branch protection, host access). If it thinks it should, it says so in
  its report.
- Its changes to ameesh always go through a PR and CI, never through a
  direct write to the main branch. It does not make CI green by changing
  it: a PR that touches CI or the test scripts, or that removes, skips or
  weakens a test, is never merged by it.
- Every deployment follows `deploy/mise-a-jour` (check, back up, rollback
  possible) and restarts the runners outside a turn.
- It never suspends the work of others on its own initiative: no stop, no
  pause, no interruption, no mode change, no restart of an agent in a
  turn, no process or container killed, even orphaned. When it would be
  needed, it alerts the responsible person.
- Its sub-agents only read: no mail, no ameesh move, no git or GitHub
  write; the database is read under a read-only role.
- Never `approve` (R8).

## Measuring its usefulness

Every week, in the ameesh thread:

- **time loss avoided**: each cause lot carries the measured loss and the
  query that measures it. Seven days after the fix is deployed, the auditor
  redoes the same measurement over a period of the same length; the
  difference is the time avoided. A loss that does not go down reopens the
  cause;
- **causes closed**: fix deployed or rule accepted; recurrences are counted
  separately;
- **false positives**: finding disproved (lot abandoned with the note "faux
  positif" (false positive)), PR closed for lack of a real defect, fix
  reverted; reported against the number of findings;
- **its cost**: its turns, sub-agents included, as a share of plan or as
  spend.

These figures are the owner's criterion for keeping, reducing or extending
the auditor ([0023](0023-critere-d-adoption-des-reglages.md)).

## Unchanged

The checklist (§3); learning (§6): an accepted rule becomes a decision; the
alert pushed only when a human decision is needed; the daily summary; the
capabilities in the canon; never `approve`.

## Consequences of the amendment

- The canon card, whose PR is not merged, takes the harness, the provider,
  the model and the authentication mode chosen by the organisation, and
  declares the effort. Commissioning applies
  `ameesh set auditeur effort=<niveau>`.
- The owner creates a read-only login role on the database, member of the
  roles of the supervision contract
  (`deploy/sql/role-superviseur.sql` and `role-superviseur-contenus.sql`):
  this is a permission. Without this role, the analysis makes do with the
  `ameesh … --json` commands.
- A third timer sends the "Analyse profonde" (deep analysis) mail every
  night.
- **Bug to fix before the first deep analysis.** Claude Code also fires
  hooks for the tools of its sub-agents, and the hook input then carries
  `agent_id`; `agent-mail hook` does not look at it. A message delivered at
  `PostToolUse` while a sub-agent is working therefore goes into the context
  of that sub-agent and is marked delivered: the main thread does not see it.
  The defect affects every agent that launches sub-agents. The lot will
  verify it with a test before fixing; in the meantime, the prompt asks
  sub-agents to copy back any message received, and the auditor to re-read
  the mail delivered during the analysis.
- Lots to open: declarative effort in the canon; generic
  `deploy/mise-a-jour` scripts (they still carry the names and checks of the
  L60–L74 update); routing urgent alerts to an agent in `ameesh notify`,
  already planned.
- The loop of [0022](0022-auto-reparation.md) applies to ameesh itself:
  the auditor carries the signals, triage, repair, verification, delivery
  and post-delivery monitoring. For ameesh bug fixes only, in its own mesh,
  delivery goes all the way to deployment without a human; the production of
  projects remains human.

# Amendement du 2026-10-12 : conformité aux procédures (L234)

Lot #260, affecté par mesh-design à ameesh-cx2 (courrier #8168 du
2026-10-12 à 06:07), après la question du propriétaire relayée par bureau
et le constat de l'auditeur du même jour à 05:50. L'évolution est soumise
au circuit de relecture et de décision du projet.

La consigne ajoute le point 12 : contrôler la conformité des agents à leur
procédure, y compris les faits sans alerte — attente humaine sans demande
`ameesh decide`, décision attendue par courrier libre, intervention manuelle
d'un humain ou de bureau, annonce qui ne correspond pas au registre et aux
journaux. Chaque écart confirmé porte la règle, les preuves, la cause, le lot
et le correctif ; le passage suivant vérifie ce qui a été corrigé. Les
interventions de bureau sont consignées dans le fil avec leur lot pour
permettre cette remontée vers la cause racine.

Le passage court est exempt des pauses et attentes de charge de l'hôte,
y compris sous pression critique et batterie basse. Le harnais tourne à
faible priorité CPU et I/O quand les outils du poste le permettent ; les
gardes de budget et de sécurité restent inchangées. L'analyse profonde
attend toujours sous pression critique.

La prise automatique ne confie à l'auditeur que des bugs dans son périmètre,
jamais des évolutions ou améliorations, ni un lot qui mentionne ses
instructions protégées (consigne, décision 0036, fiche, capacités,
permissions). Une attribution explicite ne l'autorise pas davantage à les
modifier : le lot est rendu à l'orchestrateur pour un autre agent.
