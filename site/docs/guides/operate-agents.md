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
ameesh set <agent> model=… effort=… tier=… session_policy=…
```

- `tier`: service tier passed to the harness through its descriptor, like
  `model` and `effort`. Codex: `-c service_tier="<tier>"` (for example `fast`,
  `flex`). A harness that declares none ignores it, and `set` says so.
- `effort` and `tier` are written in the host's local state **and** in the
  database, so `list --json` shows them whatever the host.
- Every setting takes effect at the **next turn**.

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
              [--stale-lot 21600]
```

One JSON object per line (`ameesh-alert/1`):

| `type` | When |
|---|---|
| `long_turn` | a running turn longer than the threshold |
| `idle_with_mail` | an idle agent with unread mail older than the threshold |
| `dead_runner` | a lease expired without renewal, beyond the grace period |
| `session_too_big` | tokens re-read at the last turn of the current session above the threshold (default 15 M) |
| `stale_lot` | an unmerged lot without activity for the threshold (default 6 h), with what it waits for: verdict, correction, merge or a human decision |

Without `--follow`: the current alerts. With `--follow`: a `raised` line when an
alert appears (an alert that lasts is not repeated) and a `resolved` line when
it disappears. Thresholds can also be set with `AMEESH_ALERT_LONG_TURN`,
`AMEESH_ALERT_IDLE_MAIL`, `AMEESH_ALERT_DEAD_GRACE`,
`AMEESH_ALERT_SESSION_TOKENS`, `AMEESH_ALERT_STALE_LOT` and
`AMEESH_ALERT_INTERVAL`.

## Fresh session on a brief: `ameesh restart`

```bash
ameesh restart <agent> --brief FILE|- [--wait S] [--json]
```

Stops the running turn cleanly, forgets the session, and makes the brief the
first message of the fresh session. Without a live lease it is applied at once;
with one, the request is written and the runner is woken: it stops the turn,
puts that turn's prompt back behind the brief, and applies the request under its
lease. `--wait` waits for the application. Refused for a stopped agent or an
empty brief.

## Direct interruption: `ameesh interrupt`

```bash
ameesh interrupt <agent> <message…> [--from NAME]
```

Drops an urgent event: the runner stops the running turn and serves the message
first. Only senders listed in `AMEESH_INTERRUPT_SENDERS` may do it (the same
rule as the runner); anyone else is refused with exit code 1 and nothing is
dropped.
