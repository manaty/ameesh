# Operate agents

What an orchestrator, human or agent, reads and sets to run a team of agents
with its own tools. Every `--json` output below is a **stable schema**: a field
can be added, none changes meaning or disappears without a new schema number.
Instants are UTC epoch seconds (floats, suffix `_ts`), `null` when unknown,
never 0; durations are whole seconds.

## Session policy

| Policy | Effect |
|---|---|
| `par-lot` (default) | a **fresh session when the lot of the turn differs** from the lot of the current session; rotation on size and turn duration stays as a safeguard |
| `taille` | rotation on size and turn duration only (no rotation on a change of lot): for **orchestrators** |
| `jamais` | no rotation |

```bash
ameesh set <agent> session_policy=par-lot|taille|jamais   # empty value = default
```

The runner's default is `AMEESH_SESSION_POLICY` (`par-lot`). The canon profile
has no key for it: the setting goes through `ameesh set` only.

- **Lot of the turn**: the lot (`--lot`) of the turn's messages if there is a
  single one; else the only open lot assigned to the agent; else unknown, and an
  unknown lot never triggers a rotation.
- **Rotation**: a summary is produced in the old session, the old id is kept in
  the session history, the thread keeps a trace, and the first turn of the new
  lot opens on the summary. If the summary fails, the turn runs in the old
  session (no retry at every turn; the `session_too_big` alert keeps watch).
- An **orchestrator** receives messages from every lot: set it to `taille`,
  otherwise every change of lot would open a session.

## `ameesh set`

```bash
ameesh set <agent> model=… effort=… tier=… session_policy=… mode=execute|externe
```

- `mode` (v1.4.0): `execute` (default) for an agent run by a runner under a
  lease, or `externe` for a human's session that only uses the mailbox and is
  **never woken** by ameesh. See [Agent mode](#agent-mode-and-stop-reason).
- `tier`: service tier passed to the harness through its descriptor, like
  `model` and `effort`. Codex: `-c service_tier="<tier>"` (for example `fast`,
  `flex`). A harness that declares none ignores it, and `set` says so.
- `effort` and `tier` are written in the host's local state **and** in the
  database, so `list --json` shows them whatever the host.
- Every setting takes effect at the **next turn**.

## Agent mode and stop reason

The rule of design decision 0030 is **no work without a way to wake the
agent**: a lot, a delegation or an orchestrator role goes only to an agent
ameesh knows how to wake. Each agent therefore has an explicit mode:

| Mode | Meaning |
|---|---|
| `execute` | run by a runner under a lease; woken by its mail, events and nudges |
| `externe` | a session started by a human; mailbox only (through a [session binding](#session-bindings)), never claimed nor woken by ameesh |

- An external agent **must have a responsible human**; `set` and `show` say so
  otherwise. `ameesh list` prefixes its status with `ext/`.
- An agent created by a hook without a lease is born `externe`; a hook never
  changes the mode of an existing agent. `ameesh attach` stays allowed on an
  external agent, without changing its mode.
- When an agent stops, a **structured stop reason** is recorded: `manuel`
  (`agent-runner stop`), `bail_expire` (the turn died, the lease expired) or
  `retire_du_canon` (removed by `canon sync`); `externe` and `erreur` are
  reserved. It is cleared as soon as the agent runs again.

## `ameesh list --json`: schema `ameesh-agent/1`

Each agent row keeps the historical keys and adds:

| Key | Content |
|---|---|
| `schema` | `"ameesh-agent/1"` |
| `harness`, `model`, `effort`, `tier` | harness and settings, or `null` |
| `session_policy`, `session_policy_set` | effective policy; the agent's own setting or `null` |
| `state` | `working`, `idle`, `paused` or `stopped` (same rules as `ameesh progress`) |
| `state_since_ts`, `state_for_s` | since when |
| `lot` | `{id, title, state, source}`: the session's lot (`"session"`), else the most recent open lot assigned (`"assigned"`), or `null` |
| `title` | title of the current lot, else the status text |
| `unread`, `oldest_unread_ts` | mail not delivered yet |
| `turn_started_ts` | start of the running turn, or `null` |
| `last_turn_reread_tokens` | tokens re-read at the last turn (input + cached input) |
| `restart_pending` | an `ameesh restart` request waits to be applied |
| `mode` | `execute` or `externe` (v1.4.0) |
| `stop_reason` | `manuel`, `bail_expire`, `retire_du_canon` (`externe`, `erreur` reserved), or `null` while the agent runs (v1.4.0) |

The text view of `ameesh list` counts the lots once at the bottom and shows,
for each agent, a **LOTS** column with its open assigned lots.

## Cost per turn, plan gauges, provider balance

```bash
ameesh cost turns   [--agent A] [--since 24h] [--limit 500] [--json]   # ameesh-turns/1
ameesh cost gauges  [--harness claude|codex] [--since 7d] [--json]     # ameesh-gauges/1
ameesh cost balance [--provider deepseek] [--record] [--since 7d] [--json]   # ameesh-balance/1
```

- **turns**: the per-turn ledger as is, most recent first: agent, harness,
  turn, model, session, USD, input, cached input and output tokens.
- **gauges**: history of the subscription gauges, recorded where they are
  already read (budget guard, `cost report`, `progress`): one row when a gauge
  changes, or at most every ten minutes. A history failure never breaks the
  budget guard.
- **balance**: the balance of a pay-per-token provider (DeepSeek in this
  version), read-only and free, with the **real spend** per hour and per day
  computed from the drops between successive readings (a rise is a top-up).
  The key comes from `DEEPSEEK_API_KEY` and goes only into the request header,
  never into a log, a message, a thread or the database; without a key nothing
  is read. Requests require HTTPS and never follow a redirect. Readings are
  taken by `--record` and by the runner every `AMEESH_BALANCE_INTERVAL` seconds
  (default 900, 0 = never). Limit: spend made in the same interval as a top-up
  is not seen.

## Alerts: `ameesh alerts`

```bash
ameesh alerts [--follow] [--json] [--interval 30] [--long-turn 1800]
              [--idle-mail 300] [--dead-grace 30] [--session-tokens 15000000]
              [--stale-lot 21600] [--orphan-lot 1800] [--delegation-grace 300]
```

One JSON object per line (`ameesh-alert/1`):

| `type` | When |
|---|---|
| `long_turn` | a running turn longer than the threshold |
| `idle_with_mail` | an idle agent with unread mail older than the threshold |
| `dead_runner` | a lease expired without renewal, beyond the grace period |
| `session_too_big` | tokens re-read at the last turn of the current session above the threshold (default 15 M) |
| `stale_lot` | an unmerged lot without activity for the threshold (default 6 h), with what it waits for: verdict, correction, merge or a human decision |
| `host_pressure` | a reading of the host crosses a limit of its `policy.resources` (v1.3.0); fields `host`, `critical`, `breaches` |
| `orphan_resource` | a resource of a turn (process group, labelled container) that outlives its turn, or that a dead runner left behind (v1.3.0); it is reported, never removed |
| `stopped_with_mail` | a stopped or dead agent with unread mail older than `--idle-mail`, **except** a manual stop (v1.4.0); fields `stop_reason`, `mode`, `responsible` |
| `orphan_lot` | an open lot that nobody will move forward (v1.4.0); `reason`: `inconnu` (assignee unknown to the registry), `arrete` (assignee in `execute` mode stopped or dead), `externe_sans_responsable`, or `sans_tour` (an `intake`/`build` lot whose last activity **and** the assignee's last turn are older than `--orphan-lot`, default 30 min) |
| `delegation_expired` | a delegation past its deadline and not yet processed by a runner (`reason: en_retard`, after `--delegation-grace`), or a lot handed back to the delegator in the last hour (`reason: rendu`) (v1.4.0) |

A lot without assignee, or assigned to a human, is never orphan; an external
agent is never counted as `sans_tour` (it does not run ameesh turns). The
`responsible` field of `stopped_with_mail`, `orphan_lot` and
`delegation_expired` names the human who should act: the recipient of
[pushed alerts](#pushed-alerts-ameesh-notify).

Without `--follow`: the current alerts. With `--follow`: a `raised` line when an
alert appears (an alert that lasts is not repeated) and a `resolved` line when
it disappears. Thresholds can also be set with `AMEESH_ALERT_LONG_TURN`,
`AMEESH_ALERT_IDLE_MAIL`, `AMEESH_ALERT_DEAD_GRACE`,
`AMEESH_ALERT_SESSION_TOKENS`, `AMEESH_ALERT_STALE_LOT`,
`AMEESH_ALERT_ORPHAN_LOT`, `AMEESH_ALERT_DELEGATION_GRACE` and
`AMEESH_ALERT_INTERVAL`.

**Underuse alerts** (L94) flag waste as well as overload, and `ameesh notify`
pushes them by default:

| Type | Raised when |
|---|---|
| `plan_underused` | a subscription account will lose unused capacity at its next reset: less than `--plan-used` % (50) used with less than `--plan-tail` (24 h, at most a quarter of the window) left, or more than `--plan-pace-gap` points (25) below the allowed pace on a window of a day or more; reports the estimated loss |
| `idle_capacity` | wakeable agents idle without a lot for more than `--idle-capacity` (30 min) while lots wait unassigned, mail waits at a busy agent, or pay-per-token agents work while subscription agents sleep; suggests dispatching or waking the orchestrator |
| `orchestrator_held` | an orchestrator (`--orchestrators`, `roles: [orchestrateur]` in its canon fiche, or an agent that handed out lots) is held by `ameesh attach` for more than `--orchestrator-held` (30 min) with unread mail |
| `host_underused` | a host stays almost idle for `--host-underused` (1 h: load per CPU below 0.25, under one turn on average) while another host is under pressure or agents wait elsewhere; a suggestion only, nothing is moved |
| `balance_low` | a pay-per-token provider's runway at the real spend rate (balance readings over 6 h, top-ups ignored) falls under `--balance-hours` (48 h), or its balance under `--balance-min` USD (20); urgent under 12 h or 5 USD |

A threshold of 0 disables the matching alert.

## Pushed alerts: `ameesh notify`

`ameesh alerts` is **pulled**; `ameesh notify` (v1.4.0) follows the same alerts
(same thresholds, same options, same de-duplication as `--follow`) and
**pushes** them to the responsible human: desktop notification, ntfy, Slack.

```bash
ameesh notify [--once] [--dry-run] [--json] [--interval 30] [thresholds…]
ameesh notify --test human:<id> [--json]
```

- Types pushed by default: `stopped_with_mail`, `orphan_lot`, `dead_runner`,
  `idle_with_mail`, `delegation_expired` (setting `notify.types`).
- **Recipient**, first found: the alert's `responsible`; the agent's
  responsible human; the responsible of the lot's work package; the
  responsible of the host's `Host` card; `notify.default_human`. An alert
  without a recipient or channel is logged once and kept: if a route appears
  while it lasts, it is sent.
- **Reliability**: the delivery state is kept in `<state>/notify/state.json`
  (`0600`), so a lasting alert is not sent again after a restart and an alert
  resolved meanwhile gets its resolution. One `notify` per state directory (a
  second one exits with code 1). Beyond `rate_per_minute` per human, alerts are
  folded into a summary. A failed channel does not stop the others and is
  retried up to `max_attempts` times.
- `--json`: one object per delivery, schema `ameesh-notify/1` (`event`,
  `type`, `agent`, `lot`, `host`, `human`, `source`, `delivery`, `channels`,
  `title`, `text`, `ts`).

Configuration (the `notify` key, channels, and the rule that secrets come only
from the environment or a `0600` file): see
[Configuration](../reference/configuration.md#pushed-alerts-notify). An example
systemd user unit ships as `deploy/systemd/ameesh-notify.service` (secrets in
`~/.config/ameesh/notify.env`, `0600`). To put it in service:

```bash
ameesh notify --test human:alice          # one test message per channel
ameesh notify --once --dry-run            # what the service would send now
systemctl --user enable --now ameesh-notify
journalctl --user -u ameesh-notify -f
```

The `desktop` channel of a user service needs a graphical session (session
D-Bus).

## Guarded assignment and delegation

```bash
ameesh work add --title T --assignee AGENT [--externe] …
ameesh work assign <id> <agent> [--externe] [--actor A]
ameesh work delegate <id> <agent> --within 30m [--actor A]
ameesh work expire-delegations [--dry-run] [--json]
```

**Guarded assignment** (v1.4.0). A lot goes only to a **wakeable** agent:
known to the registry, in `execute` mode, ephemeral and not expired, admitted
on its host for its current profile, and with a responsible human when a canon
is configured. Otherwise the command refuses with the reason (exit 1),
writes nothing, and says what to do: register the agent, assign to a human
(`human:<id>`), or `--externe` for an external session with a responsible
human. Details:

- an unknown name is refused; a bare name that designates a known human
  (a `Member` of a configured canon, `AMEESH_HUMANS`) is normalised to
  `human:<id>` with a warning;
- a stopped agent is accepted with a warning (the lot waits for it, and the
  `orphan_lot` alert follows it);
- a canon that is momentarily invalid does not block an assignment (warning);
  claiming stays closed until the next valid sync;
- `work assign` reassigns an open lot, logs "assigned to X (before: Y)" in the
  lot's events, and clears any delegation in progress.

**Delegation with a deadline**. `work delegate` hands a lot to a wakeable
agent (same guard; a human is refused, use `work assign`) and drops an event
in its mailbox, which wakes it. The delegator is `--actor`, else the current
assignee; the lot comes back to them. At the deadline (`--within`: `30m`, `2h`,
`1d` or seconds), processed by the runner at **every pass**:

- if the delegate has done nothing on the lot since the delegation (no turn on
  that lot, no transition, note, milestone, action or linked message), the lot
  **goes back to the delegator**, with an event in their mailbox if it is an
  agent (a human is told by the `delegation_expired` alert);
- otherwise the delegation is **settled**: the deadline is cleared and the lot
  stays with the delegate.

A new delegation replaces the previous one. `work list`, `work show` and
`ameesh progress` say "delegated by X, due in 12 min" or "overdue by …".

## Dated commitments and the roadmap: `ameesh plan`

**Rule for orchestrators:** every dated commitment made in a conversation
("we'll do it on Monday", "delivery on Thursday") is recorded in ameesh **when
it is made**, with its source:

```bash
ameesh plan add "Separate accounts per organisation" --pour 2026-10-12 \
    --projet ameesh --lot 65 --source "conversation of 2026-10-10"
ameesh work plan 65 --debut 2026-10-10 --fin 2026-10-11 --livraison 2026-10-12
```

A date that only lives in a conversation or in the body of a task is neither
on the Gantt (`ameesh plan show`, and the roadmap section of `ameesh
progress`) nor watched by the `engagement_overdue` alert, which notify pushes
to the responsible human. `ameesh plan propose` reads the canon's decisions
and the bodies of open tasks and **proposes** commitments, decision
milestones and dependencies; nothing is created without `--record`, and a
recorded proposal waits for `ameesh plan accept`.

## Session bindings

The working directory **never** gives an identity (decision 0030). A session
started by a human (an external agent) receives its mail through the hook only
if it is explicitly **bound**:

```bash
ameesh mail bind <agent> --session <id> --harness claude|codex|deepseek --pid <harness PID>
ameesh mail bindings [--all] [--json]
ameesh mail unbind --session <id> --harness <harness>
```

The session id is the one the harness passes to its hook (Claude Code shows it
in `/status` and in its transcript name; Codex in its session file name).
`--pid` is recommended: the hook then requires that PID among its ancestors.
Without a binding the hook delivers nothing and writes nothing. An agent in
`execute` mode is refused (an external session would steal its mail) unless
`--force`; reclassify it first with `ameesh set <agent> mode=externe` if it is
in fact a human's session. Inside a bound session, `agent-mail whoami` shows the
agent, the source `session` and the binding.

## Host resources: `ameesh hosts`

Since v1.3.0, each runner publishes a reading of its host every
`AMEESH_RESOURCE_INTERVAL` seconds (default 60): available memory, swap used,
1-minute load, free disk of the working directory, turns in progress.

```bash
ameesh hosts [HOST] [--history 10] [--json]   # ameesh-host/1
```

Before each turn, the runner compares its last reading with the limits of the
host (`policy.resources` of its `Host` card, else prudent defaults: 1 GiB of
available memory, 8 GiB of swap, a load of twice the CPU count, 2 GiB of free
disk). Crossing a limit **holds new turns** (running turns finish); a critical
crossing pauses the agents of lowest `priority` that have no running turn,
never in the middle of a turn. See
[Runner and leases](../concepts/runner-and-leases.md#host-resources-and-back-pressure).

## Fresh session on a brief: `ameesh restart`

```bash
ameesh restart <agent> --brief FILE|- [--wait S] [--json]
```

Stops the running turn cleanly, forgets the session, and makes the brief the
first message of the fresh session. Without a live lease it is applied at once;
with one, the request is written and the runner is woken: it stops the turn,
puts that turn's prompt back behind the brief, and applies the request under its
lease. `--wait` waits for the application. Refused for a stopped agent (use
`ameesh resume`) or an empty brief.

## Adopt an interactive session: `ameesh adopt`

```bash
ameesh adopt <agent> --session ID --harness claude|codex|deepseek
             [--account ACCOUNT] [--cwd DIR] [--brief FILE|-] [--chantier C]
             [--force] [--json]
```

Putting an **existing interactive session** under the runner is an ameesh
operation (v1.4.0), not a prompt. Run it on the session's host:

1. The session file is looked for under the folder of each declared account of
   the harness (`--account` targets one); the account where it is found
   becomes the session's **account of origin**. Not found: refused, with the
   folders searched.
2. The session **must be closed**: no process holds its file or carries its id
   on its command line. Otherwise the command lists the PIDs; `--force`
   overrides, with a warning in stderr and in the thread.
3. Refused if the agent holds a live lease.
4. In one statement: `mode=execute`, harness, host, working directory
   (`--cwd`, else the one recorded in the session log, else the registry's),
   session and account of origin, status `queued`, and the brief (`--brief`,
   else a standard resume instruction) at the head of the pending prompt. An
   unknown agent is created (`--chantier`). An audit event goes to the
   project's thread, and the agent's session bindings are revoked.

The human can then come back with `ameesh attach <agent>`, on the same session
and under the lease. If, at the first turn, the active account is not the
session's account and the session cannot move between accounts, the runner
rotates with a summary written under the account of origin.

## Restart a stopped agent: `ameesh resume`

```bash
ameesh resume <agent> [--brief FILE|-] [--fresh] [--json]
```

Restarts a **stopped**, **dead** or **idle** agent. Refused for an unknown
agent, an external agent (use `adopt`), an agent that is not wakeable for
another reason, or a running turn.

- The recorded session is **kept** when the account the next turn would use
  can resume it (same account, a session portable between accounts, or no
  declared account). The choice is simulated: `resume` switches no account and
  records nothing.
- If the session is not portable but its old account is still usable, it is
  kept and the runner rotates at its first turn (summary under the old account,
  fresh session under the new one).
- Otherwise, or with `--fresh`, or without a session, the session is
  forgotten and the fresh one opens on a **deterministic resume brief** built
  by ameesh without calling a model: identity and role, open assigned lots with
  what they wait for, the last exchanges of the project's thread, the unread
  count, the path of the old transcript, then the instruction (`--brief`).
  The brief is capped at 16 KB.

`agent-runner register <agent> <harness>` without `--session` keeps the
recorded session; to start afresh, `ameesh resume <agent> --fresh`.

## Direct interruption: `ameesh interrupt`

```bash
ameesh interrupt <agent> <message…> [--from NAME]
```

Drops an urgent event: the runner stops the running turn and serves the message
first. Only senders listed in `AMEESH_INTERRUPT_SENDERS` may do it (the same
rule as the runner); anyone else is refused with exit code 1 and nothing is
dropped.
