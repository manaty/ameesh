---
type: Study
title: "Study — a stopped orchestrator blocks a project: liveness, resume, alerts, delegation, isolation"
description: "Analysis, in the ameesh code, of a project blocked by its stopped external orchestrator (2026-10-07); proposed decision \"no work without a possible wake-up\" and lot plan L36–L41."
status: stable
tags: [orchestrateur, bail, sessions, alertes, delegation, cloisonnement, comptes]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T12:00:00+02:00" }
sources:
  - { resource: "decisions/0010-responsabilite-et-orchestrateurs.md", title: "Decision 0010 (responsible human)" }
  - { resource: "decisions/0014-orchestrateurs-a-tours-et-placement.md", title: "Decision 0014 (turn-based orchestrators, attach)" }
  - { resource: "decisions/0025-une-session-par-lot.md", title: "Decision 0025 (sessions, resume summary)" }
  - { resource: "decisions/0027-bascule-automatique-entre-comptes.md", title: "Decision 0027 (account switch)" }
  - { resource: "../BASCULE.md", title: "Switchover v0 → v1 (coexistence, step 8)" }
---

# Context

Report from the owner (2026-10-07): a project of some thirty lots
has been blocked since the morning. Its orchestrator is an interactive Codex
session, without a lease. Its last turn ended normally, then it
waited for a human prompt that never came. Its relays are also
external, stopped or idle, and the session's Codex account is at its threshold.
The owner's proposals P1 to P6: adoption, resume, pushed alerts,
delegation with a deadline, isolation, `ameesh list` counter.

The state observed the same day (`ameesh list`, `ameesh show`, `ameesh work
list`, `ameesh alerts`) confirms the finding and makes it worse. In what follows,
"the orchestrator" refers to this session.

# What the code actually does

## 1. "External agent" does not exist in the model

* The orchestrator's status (« … mailbox identity only, no runner
  lease ») is **free text** set by
  `agent-mail status`. No column distinguishes an agent driven by
  the runner from a human session: for the registry, it is a
  `stopped` agent like any other.
* A hook that sees an identity without a lease **registers** it on first contact
  (`cli.py` `cmd_hook` → `registry.upsert`, `status` defaulting to `idle`,
  `harness` `other`). The "explicit" identity (`identity.py`, only
  `AGENT_MAIL_NAME`) is accepted without any verification.
* The orchestrator has **no responsible human** (`ameesh show`:
  « responsable : aucun », "responsible: none"), which contradicts 0010.

## 2. A lot can be assigned to any name

* `work_items.assignee` is free text, set at creation
  (`work add --assignee`); there is **no reassignment** and **no
  verification** that the assignee exists, has a lease or a runner.
* On the blocked project, seven lots are assigned to names **absent from the
  registry** (relays launched outside ameesh, for a bounded turn) and about
  ten others to stopped agents.
* The label « attend : démarrage par X » ("waiting: start by X") / « travail de X » ("work of X")
  (`stagnation.py`, `_LABELS`) never says that X is unknown, stopped or
  external.
* No notion of delegation or deadline on a lot.

## 3. Alerts do not see a stopped agent and are never pushed

* `idle_with_mail` only targets the derived state `idle` (`exploitation.py:177`);
  `stopped` and `dead` are merged into `stopped` (`progress.py:332-341`) and
  do not alert. At the time of the analysis, several stopped agents each had
  5 to 17 unread messages, without any alert.
* A manual stop is distinguished from an expired lease only by `status_text`
  (« arrêté à la main », "stopped by hand", `runner.py:2490`, versus « bail expiré », "lease expired",
  `registry.py:433`): there is no structured stop reason.
* `stale_lot` only fires after 6 h and does not check whether the assignee can
  run.
* Nothing is sent to a human: no Slack, no webhook, no desktop notification.
  The only send is the Postgres NOTIFY to the runners. Alerts are
  read by pulling `ameesh alerts`. This contradicts 0014 ("alerts to the relevant
  responsible human").

## 4. Resume is not an ameesh operation

* There is neither `adopt` nor `resume`. There is `agent-runner register <nom>
  <harnais> --session S`, which writes the identifier of a session that
  the runner will resume, and `ameesh attach`, which takes the lease and opens the
  interactive harness on the same session: this is the model of 0014, already coded
  but not enforced. `register` without `--session` keeps the recorded session
  (`coalesce`), and `ameesh restart`, which forgets it, is refused for a
  stopped agent: there is therefore no clean way to start again from a fresh session
  for a stopped agent.
* A session's account is read from the marker in `events.jsonl`
  (`account_turn._session_account`); a session that the runner did not
  launch has none. Codex: a session is **never** portable to another
  `CODEX_HOME` (`accounts.session_portable`); the switch then does
  a rotation **with a summary produced under the old account**, which fails
  if the old account is unusable.
* The Codex descriptor declares `interactive: []` (attach allowed) and
  `session.flag: ["resume"]`: the « interactif : - » seen by the owner is
  a display matter, not a limit.

## 5. Root cause of the isolation failure and of the "deafness" of external sessions

* The hooks on this workstation (`~/.claude/settings.json`, `~/.codex/hooks.json`,
  `~/.codex-secondary/hooks.json`) call `~/.local/bin/agent-mail`, which
  is **still v0** (coexistence, [BASCULE.md](../../BASCULE.md), before
  step 8). v0 **infers the identity from the folder**: `AGENT_MAIL_NAME`, otherwise
  an alias from `~/.config/agent-mail/aliases.tsv` by path prefix,
  otherwise the folder name. The alias `orchestrateur → /home/smichea/Work`
  (nexlink project) captures everything that lives under `~/Work`, and a folder
  named like a Nexlink agent takes its identity. This is the most
  likely explanation for another team's mail being injected into a session of the
  blocked project, though the exact folder was not verified.
* v0 reads the **file** mailbox. v1 mail (Postgres, `ameesh mail send`)
  is only delivered by the runner, in a turn's prompt, hence **to an
  agent that has a lease**. An external session is therefore deaf to its v1 mailbox
  by construction: even if woken up, the orchestrator would not have seen its
  messages without running `ameesh mail inbox` by hand.
* Nothing in the ameesh code ties an identity to the session identifier
  or to process ancestry; only `find_tty` walks up the processes, for
  the terminal title. **Outside the repository**, a local bridge written on
  2026-10-07 (`~/.local/bin/ameesh-session-mail-hook`, table
  `~/.config/ameesh/external-session-bindings.json`) does it: it ties
  the harness's session identifier and an ancestor PID to a name, sets
  `AGENT_MAIL_NAME`, then calls the v0 `agent-mail`. It is only wired
  into the secondary and tertiary Claude configurations, not into Codex:
  the orchestrator did not benefit from it. And even when bound, it only delivers the v0 file
  mailbox, never v1 mail. L41 brings this idea into ameesh. On the v1 side: `send all` has **no team or
  project filter** (`backend.py:230`), and a hook's `bk.register(..., session_id)`
  can **overwrite** the `session_id` of a runner agent, which the runner will
  resume on the next turn.

## 6. `ameesh list`

`mesh_cli.py:155-170` is inside the agents loop: the global lot total
(`work.delays`, unfiltered) is printed, and recomputed, on every row.

# Proposed decision (adopted on 2026-10-07: [0030](../decisions/0030-pas-de-travail-sans-reveil-possible.md))

**"No work without a possible wake-up."** It extends 0014: what 0014
announced for orchestrators becomes a rule checked by ameesh for
every work holder.

1. **Explicit agent mode.** Each agent is `exécuté` (driven by a
   runner, under lease) or `externe` (human session, mailbox only). The
   mode is a column, not free text. An external agent **must**
   have a responsible human, and `ameesh list` displays it
   as such.
2. **Guarded assignment.** A lot, a delegation or an orchestrator role can
   only be entrusted to a **wakeable** agent: known, in `exécuté` mode,
   admitted on a host whose runner is alive. Other cases are **refused**
   with the reason. Only the responsible human can force an assignment to an
   external agent (`--externe`). The lot then displays « attend : <humain> (session
   externe) » ("waiting: <human> (external session)"), and it is that human who receives the alerts. P1 ("no
   external orchestrator without a lease") is the general case of it.
3. **Adoption and resume are ameesh operations, never prompts.**
   * `ameesh adopt <agent> --session <id> --account <compte> [--cwd D]` turns
     an existing interactive session into an `exécuté` agent. It
     records the session **and its original account**, requires that the interactive
     session be closed (no harness process on that session
     file), then wakes the agent. The human comes back to it via `ameesh attach`.
   * `ameesh resume <agent>` restarts a stopped agent on its recorded
     session and its account, and ties the identity to the new execution
     (identity and lease variables set by the runner). If the original
     account is at its threshold, the rule of 0027 applies: same session if
     it is portable, otherwise rotation. The summary is made under the old account
     if it remains usable. Otherwise, a **deterministic resume brief** built
     by ameesh: the project thread, lots, latest messages, and path of the
     original transcript for reading. No more digging through `~/.codex*/sessions`
     by hand.
4. **Liveness alerts, pushed to the responsible human.**
   * Structured stop reason: `manuel`, `bail_expiré`, `retiré_du_canon`,
     `externe`, `erreur`.
   * `stopped_with_mail`: stopped or dead agent with unread mail older
     than the threshold, except for a `manuel` stop.
   * `orphan_lot`: open lot whose assignee is unknown, stopped, external without
     a reachable human, or with no turn on that lot for N min (default 30). The
     threshold is much shorter than the 6 h of `stale_lot`.
   * `delegation_expired` (see 5).
   * **Sending** by an `ameesh notify` service (systemd unit, like
     the runner), which follows the alerts and routes them to the responsible
     human of the agent or the lot. Destinations: desktop notification, ntfy,
     Slack via incoming webhook (0004). It avoids duplicates, sends the
     resolution and rate-limits. The secrets remain those of the host.
5. **Delegation with a deadline.** `ameesh work delegate <lot> <agent> --within
   30m` records the delegator, the delegate and the deadline. At the deadline, with no
   turn by the delegate on that lot (`session_work_item`), the lot **returns to the
   delegator**. An event goes to their mailbox, which wakes an executed
   agent, and the alert goes to the responsible human. `work assign` (reassignment)
   joins the CLI, guarded by rule 2.
6. **Isolation: identity never comes from the folder.**
   * The hooks move to v1 (step 8 of the switchover, brought forward for the
     hooks), since v1 already infers no identity from the folder.
   * External session: explicit binding `ameesh mail bind <agent>`, which ties
     the harness's session identifier (received by the hook as JSON) to the name.
     The hook only delivers if this binding exists; without a bound identity, it delivers
     **nothing**.
   * A hook no longer writes the `session_id` of an `exécuté` agent.
   * `send all` is limited to the sender's team or project.
7. **`ameesh list`**: one global lots line, and the number of open lots
   per agent on its row.

# Proposed lot plan

Migrations 0030 and 0031 are reserved. Each lot: branch
`lot/L3x-…`, tests, independent review, then merge **with the
owner's agreement**.

| Lot | Content | Depends on | Size |
|---|---|---|---|
| **L36** — immediate fixes | P6 (`list`: total once, count per agent); "attend" labels that say *unknown* / *stopped* / *external*; warning on `work add` for an unknown assignee; the hook no longer overwrites the `session_id` of a runner agent; `send all` limited to the project | — | S |
| **L37** — mode, stop reason, liveness alerts | migration 0030: `agent_registry.mode`, `stop_reason`, responsible human required for `externe`; `stopped_with_mail`, `orphan_lot`; `work assign`; rule 2 (refusal, `--externe` by the responsible human) | L36 | M |
| **L38** — pushed alerts | `ameesh notify` (systemd): desktop, ntfy, Slack webhook; routing to the responsible human; duplicates, resolution, rate; extended `doctor --notify-test` | L37 | M |
| **L39** — adoption and resume | `ameesh adopt`, `ameesh resume`; session's original account in the registry; closed-session check; deterministic resume brief when the old account is unusable | L37 | M–L |
| **L40** — delegation with a deadline | migration 0031: `delegated_by`, `due_at` on `work_items`; `work delegate`; deadline handled by the runner (return to the delegator, event, `delegation_expired`) | L37 | M |
| **L41** — hook isolation | `ameesh mail bind` (binding by the harness's session identifier); the v1 hook delivers nothing without a binding; procedure for switching the hooks to v1 (`BASCULE.md`, step 8 brought forward, with rollback) | L36 | M |

Recommended order: L36, then L37, then L38 / L39 / L41 in parallel, then L40.
L36 and L38 are enough for such a blockage to no longer go
unnoticed; L39 and L41 remove the cause.
