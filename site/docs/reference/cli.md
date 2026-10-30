# CLI reference

This reference is written from the real `--help` output of ameesh v1 and its
integrated lots. The built-in help follows the English drive (L148, L156); a
few recently added subcommands still carry French labels, and the French
spellings of the renamed commands and options remain hidden aliases
([Switch from a v0 setup](../guides/switch-from-v0.md)).
Run `ameesh <command> --help` for the authoritative text.

Three entry points are installed by the package:

| Command | What it is |
|---|---|
| `ameesh` | the mesh command and its subcommands |
| `agent-mail` | alias of `ameesh mail` (the harness hooks call it) |
| `agent-runner` | alias of `ameesh run` |
| `ameesh-approve` | the human approval service, run under its own Unix user |

From a clone, `bin/ameesh`, `bin/agent-mail`, `bin/agent-runner` and
`bin/ameesh-approve` do the same with `PYTHONPATH=src`, or use
`PYTHONPATH=src python3 -m ameesh …`.

**Closed output.** When the reader closes `ameesh`'s standard output (for
example `ameesh alerts --json | head`), the command stops quietly, without a
traceback, with exit code **141** (128 + `SIGPIPE`, as the shell reports it).
A broken pipe anywhere else (a harness, a socket) is still an error.

**Version.** `ameesh --version` (or `ameesh version`) prints the installed
package version, read with `importlib.metadata` (from a clone without
installation: the `pyproject.toml` next to `src/`). `ameesh doctor` prints it
on its first line.

## Mailbox: `ameesh mail` / `agent-mail`

```
agent-mail send <dest|all> <text…|-> [--from NAME] [--lot ID|REF]
                [--new-lot "title" [--estimate 2h] [--app PROJECT]]
                [--kind request|reply|notify|event] [--urgent] [--ack] [--cc NAME[,NAME…]]
                [--sign --key FILE] [--expires 24h] [--queue]
                # a body of `-` is read on standard input: send <dest> - <<'EOF' … EOF
                # (quoted delimiter: nothing is expanded, no shell command runs)
agent-mail forward <OLD> <NEW> [--dry-run] [--json]   # redeliver a dead mailbox
agent-mail list                       # agents, host, lease, unread
agent-mail inbox [NAME]               # unread messages of NAME, without marking them read
agent-mail whoami [--session ID --harness H]   # bound identity and its source (runner, explicit,
                                               # session); --cwd = diagnostic
agent-mail bind <NAME> --session ID --harness claude|codex|deepseek [--pid N] [--force]
agent-mail bind --import FILE         # import bindings from a local bridge file
agent-mail unbind --session ID --harness H
agent-mail bindings [--all] [--json]  # active session bindings (--all: revoked ones too)
agent-mail alias <NAME> <DIR> [PROJECT]
agent-mail status "<current work>"    # status text (registry + terminal title); the status stays the runner's
agent-mail hook <claude|codex|deepseek>   # hook: reads JSON on stdin, delivers unread mail
agent-mail statusline                 # Claude Code status line
agent-mail migrate                    # versioned migrations
agent-mail doctor [--notify-test | --probe]
```

**Body from standard input (#3504).** `send <dest> -` reads the message body
on standard input; use it with a quoted here-document whenever the body cites
a command, so the sender's shell runs nothing:

```sh
agent-mail send <dest> - --lot <id> <<'EOF'
The command `ameesh work close 12 --abandoned` stays text here.
EOF
```

The body arrives verbatim (surrounding whitespace trimmed, like an argument
body); an empty or non-UTF-8 input is refused before any delivery. The `-`
alone after the recipient means stdin; a `-` mixed with argument text is
refused. An argument body containing the confirmation of an ameesh command
(`delivered to …`, `milestone … added to lot #…`, `decision #…`,
`work item #… closed:`, `lot #… → merged (commit …)`) gets a warning on
standard error: it probably came from command substitution.

The session identity is `$AGENT_MAIL_NAME` (set by the runner). If
`$AMEESH_RUNNER_ID` and `$AMEESH_LEASE_EPOCH` are also set, the lease must be
alive and held by that runner. The working directory never gives an identity.
Hooks never fail the agent: any error exits 0 silently. A hook fired inside a
subagent (its input carries `agent_id`: a Claude Code subagent or in-process
teammate, a Codex subagent) delivers nothing, marks nothing and writes nothing:
the agent's mail waits for the main session's next hook. A message from an agent
never carries the owner's authority; the sender is shown.

**Session bindings** (v1.4.0). A session started by a human (an *external*
session) has an identity only if it is **bound**: `bind` links the harness's
own session id to an agent on this host, and the hook finds it by the session
id the harness passes. With `--pid`, the bound PID must be an ancestor of the
hook (recommended: without it, anyone who knows the session id on this host
receives the agent's mail). Without `AGENT_MAIL_NAME` and without a binding,
the hook **delivers nothing and writes nothing**. An agent in `execute` mode
(run by a runner) is refused unless `--force`, since an external session would
steal its mail; an agent holding a live lease never binds. `unbind` revokes the
binding of one session.

`send all` reaches only the sender's **team** (or project); a sender with
neither keeps the global broadcast, and stopped agents are left out (`--queue`
includes them).

**Undeliverable mail** (2026-10-11). With the database, `send` deposits
nothing (exit code 2) when the recipient is not in the registry — no agent is
created, and close names are suggested (edit distance, prefix, and for a role
name such as `orchestrator` the orchestrators of the sender's team) —, when the
recipient is **stopped** (unless `--queue`; the message says since when, why,
who is responsible and which agent took over its work, when known), or when
the sender identity is a stopped agent (the session's bound identity,
`whoami`, is shown). A send never writes the recipient into the registry.
Mail left more than 15 minutes in a stopped or unknown mailbox raises the
`mail_undeliverable` alert. `forward` redelivers the pending mail of a stopped
(or unknown) agent to a live one, keeping the original sender and date; the
original is no longer pending.

`--kind event` wakes the agent like a message, coalesced (see
`AMEESH_EVENT_COALESCE`); `--urgent` pierces the coalescing and, from an
authorised sender, interrupts the current turn.

**Batched mail** (L125). An idle agent is woken only when the batching window
(`AMEESH_MAIL_BATCH`, default 90 s, per agent `ameesh set <agent>
mail_batch=…`) has elapsed since its oldest waiting message; that one turn
carries everything that arrived meanwhile. A message sent with `--urgent`, or
by a human, wakes it at once. Passive mail never opens a turn by itself and is
read on the next turn: an acknowledgement (`--ack`, or a very short message
that is only a thank-you or a receipt), a copy (`--cc NAME`: the person in copy
gets an `event`, without being woken) and a `send all` announcement without
`--urgent`. `send B,C` is refused: use `--cc C`.

## Runner: `ameesh run` / `agent-runner`

```
agent-runner [--host H] [--runner-id ID] [--agents a,b] [--once [--wait S]]
             [--dry-run] [--max-turns N] [--lease-ttl S] [--poll S]
             [--idle-nudge S] [--migrate]
             [--take-idle S] [--take-max-per-hour N]
             [--take-lot-idle S] [--take-lot-max-per-hour N] [--take-interval S]
             [--balance-hours H] [--balance-min USD] [--balance-window S]
agent-runner register <name> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXT]
             [--session ID] [--chantier C] [--model M] [--budget USD]
agent-runner stop <name>
```

| Option | Meaning |
|---|---|
| `--host` | host whose agents this runner claims |
| `--runner-id` | lease identifier (default `host:pid`) |
| `--agents` | comma-separated list of agents |
| `--once` | a single pass (exit 0 if a turn happened, 3 otherwise) |
| `--wait` | with `--once`: wait for work up to N seconds |
| `--dry-run` | print the commands without running them |
| `--max-turns` | stop after N turns |
| `--lease-ttl` | lease duration in seconds (default 300) |
| `--poll` | fallback poll interval in seconds (default 5) |
| `--idle-nudge` | nudge after N seconds of inactivity (default 1200) |
| `--migrate` | apply migrations before starting |
| `--take-idle` | take a backlog item for an idle agent after S seconds (default 1800; 0 turns it off) |
| `--take-max-per-hour` | backlog takes per rolling hour, for the whole mesh (default 2) |
| `--take-lot-idle` | take the best unassigned lot of an idle agent's team after S seconds (default 900; 0 turns it off) |
| `--take-lot-max-per-hour` | team lots taken per rolling hour, for the whole mesh (default 6) |
| `--take-interval` | seconds between two takes by this runner (default 60; 0 turns the runner's take off) |
| `--balance-hours`, `--balance-min`, `--balance-window` | thresholds of the `balance_low` alert (runway in hours, USD floor, averaging window), the same as `ameesh alerts`; a pay-per-token agent never takes under that alert |

Since #804 the runner triggers the take itself, for the agents it serves, on
the host where they run: the same `backlog.auto_take` and the same safeguards
as `ameesh notify` (idle, budget, hosts on hold, `balance_low`, hourly caps
counted in the database), with the same `AMEESH_TAKE_*` settings. The remaining
place is re-read before each take, so simultaneous passes no longer consume
the same place. Distribution no longer depends on a central notifier;
`ameesh notify` keeps its own take where it runs.

`register` registers an agent in the registry; `stop` marks it stopped (no new
lease; the current turn finishes).

### `ameesh attach`

```
ameesh attach <agent> [--wait] [--ttl S]
```

Interactive session on the agent's lease, same session. `--wait` waits for the
end of a running turn; `--ttl` sets the lease duration.

## Observability

```
ameesh list [--json]                       # every agent (--json: schema ameesh-agent/1)
ameesh projects [--project P] [--all] [--json]  # projects in progress: who works on what (ameesh-projects/1)
ameesh show <agent> [--json]               # one agent
ameesh hosts [HOST] [--history N] [--json] # host resources: last reading, limits, short history
ameesh debit [--json] [--check] [--hours H] [--threshold DURÉE] [--ci-hours H] [--ci-slow DURÉE]
                                           # measured mesh debt (schema ameesh-debit/1)
ameesh events [--after ID] [--limit N] [--json] | prune [--days N] [--dry-run]
                                           # append-only event log (L85)
ameesh decisions [--all] [--for human:ID] [--json]  # what waits for a human
ameesh progress [--json] [--html FILE | --serve [--port 8765]] [--project P] [--since SINCE]
                [--no-gantt] [--no-versions] [--stale-after S]
ameesh plan add|list|accept|done|cancel|propose|show   # roadmap and dated commitments (L96)
ameesh budget [show|set|unset] [--json]    # budget caps stored in the database (L70)
ameesh accounts list|use|auto|healthy|check  # provider accounts and failover (L30, L226)
ameesh heavy [--nice N] [--max-duration 90m] -- <command> | --status [--json]
ameesh harness list|show|check|versions    # harness descriptors and versions per host (L120)
ameesh serve [--port 8780] [--token-file F] [--human human:ID] | --openapi  # read API (L84)
ameesh module <name> …                     # installed project module commands
ameesh cost report [--json]                # agent, harness, model, spend, gauges
ameesh cost spent [agent] [seconds]        # spend (USD) over a sliding window
ameesh cost over [agent]                   # exit 0 if the plan's pace is exceeded
ameesh cost turns [--agent A] [--since SINCE] [--limit N] [--json]      # usage per turn
ameesh cost gauges [--harness H] [--since SINCE] [--json]               # history of plan gauges
ameesh cost balance [--provider deepseek] [--record] [--since SINCE] [--json]
```

`ameesh progress --serve` (L304) serves the same page and its JSON at
`/api/progress.json` locally, read-only, with a Refresh button; `--html FILE`
writes a standalone static page instead. `--no-gantt` drops the roadmap
section, `--no-versions` skips the GitHub reads of the **versions** tab
(L160f): per project and product, the last published version and the next
release (its open lots, planned date, state and content — the release notes
of the release branch, otherwise the lots merged since the last tag), every
datum carrying its source and read time.

`ameesh budget show` lists the caps in force; `set --per-hour X [--per-day Y]
[--agent A]` writes one in the database (reloaded live, no restart), and
`unset` removes a database cap (a config-file cap stays). `ameesh accounts`
shows the declared accounts, the active one and the switches; `use` forces an
account, `auto` hands back to the automatic choice, `healthy` lifts an
`auth_blocked`, and `check` probes every credential of the host (L226a).
`ameesh heavy --max-duration 90m` caps the command's duration (default
`AMEESH_HEAVY_MAX_S`, then `heavy_max_seconds`, then 60 minutes; `0` = none):
a message then SIGTERM/SIGKILL and exit 124. A test suite a turn launches
outside `ameesh heavy` gets the same cap, recognised on its argv (#453).

`--since` takes a duration (`16h`, `2d`) or an ISO date. `cost balance
--record` reads the balance now (read-only, free).

`ameesh list` shows a **PROJECT** column (the agent's team, else its
chantier, the `--chantier` of `register` and `adopt`) and groups agents by project, a **LOTS** column (open lots assigned
to each agent) and prefixes the status of an external agent with `ext/`.
`ameesh projects` shows, per project, each agent's state (working, paused,
idle, stopped, with the reason), its current lot (the last open lot the agent
quoted in its own mail, else its session's lot, else its most recent open
assigned lot; never a merged or closed lot), unread mail, 24 h spend and
whether it runs on a plan or pays per token, plus the open lots nobody can
move forward; it flags projects with open work and no active agent. The same
view heads `ameesh progress` (text, HTML page, `projects` key in JSON). `ameesh hosts` shows,
per host, the last resource reading published by its runner (available memory,
swap used, 1-minute load, free disk of the working directory, turns in
progress), the effective limits and where each comes from, and the short
history (`--history`, default 10 readings); `--json` gives one object per host
(schema `ameesh-host/1`).

`ameesh debit` measures the debt the milestones hide: lots in `qa` or
`blocked` by age, reviews still owed, blocking verdicts untreated, holds and
armed wakes, idle agents with work due, turns and merges per hour, CI job
durations (canon of 24/10/2026). `--json` prints the full
`ameesh-debit/1` report; `--check` exits 2 when an agent wait exceeds
`--threshold` (default 2 h; human and external waits are excluded).

`ameesh events` reads the append-only event log, written by a trigger in the
same transaction as each change: lot transitions, turns, costs, thread
entries, decisions, receipts, alerts and pilot gestures. `--after ID` resumes
after a position (default: the last 20 events), `--limit` bounds the page (max
1000), `--json` prints an `ameesh-events/1` object; `prune --days N
[--dry-run]` applies the retention by hand, one year by default.

## GitHub: `ameesh github`

```
ameesh github read <path> [--ttl S]        # one REST GET through the host's shared cache (L848, L860)
ameesh github gestes [--since 24h] [--agent A] [--match S] [--json]  # gh commands journaled per turn
ameesh github identity <agent> [--json]    # the GitHub identity an agent resolves to
```

`github read` is the watch scripts' entry point: it serves a GitHub REST path
(`/repos/owner/repo/…`) through the same disk cache as `ameesh notify`, with
`If-None-Match` revalidation (a 304 costs no quota) and a stop when the
resource's quota is exhausted; the path is normalised without its leading
slash. It prints the JSON body on one line. Exit codes: 0 a served read
(network, revalidation or cache), 1 a GitHub or transport error, 2 a usage
error, 3 quota exhausted with nothing cached for that path — wait for the reset
instead of looping. Reads only: writes (a merge, a run restart) still go
through `gh`, and the token is neither written nor journaled. `github gestes`
lists the `gh` commands run by agents per turn (who cancelled, merged or
labelled), and `github identity` shows the GitHub identity an agent resolves
to (#1645). See `docs/EXPLOITATION.md` and the GitHub-watch process
(`processus/demandes-github.md` in the canon).

## Operating agents

```
ameesh set <agent> key=value [key=value …]   # model=… effort=… tier=… session_policy=par-lot|taille|jamais
                                             # lot_wake=on|off context_max_tokens=15M|0
                                             # turn_max_seconds=30m|2h|0 turn_mail_max=20|0
                                             # turn_grace_seconds=20m|0 mail_batch=90s|2m|0
                                             # mode=execute|externe
                                             # (empty value = default; effect at the next turn)
ameesh alerts [--json] [--follow] [--interval S] [--long-turn S] [--idle-mail S]
              [--dead-grace S] [--session-tokens N] [--stale-lot S]
              [--orphan-lot S] [--delegation-grace S] [--mail-undeliverable S]
              [--turn-churn N] [--short-turn S]
ameesh notify [--once] [--dry-run] [--json] [--interval S] [alert thresholds…]
ameesh notify --test human:ID [--json]
ameesh watch enable --agent A [--every 30m] | disable --agent A | show [--json]
ameesh restart <agent> --brief FILE|- [--wait S] [--json]
ameesh adopt <agent> --session ID --harness claude|codex|deepseek [--account ACCOUNT]
             [--cwd DIR] [--brief FILE|-] [--chantier C] [--force] [--json]
ameesh resume <agent> [--brief FILE|-] [--fresh] [--json]
ameesh interrupt <agent> <message…> [--from SENDER]
ameesh host drain HOST [--for 8h] | undrain HOST | status [HOST]   # durable host withdrawal (L232e)
ameesh handoff plan AGENT --repository R [--dry-run] | show OPERATION  # reserved host move (L232a)
ameesh role status [ROLE] [--json] | role canary [--json]  # elected roles, leases and epochs (L165)
ameesh role run <agent> [--min-left S] [--dry-run] [--json] -- <command…>
                                           # run a mechanical pass on the live lease holder only
ameesh role timers [--role R] [--install|--check|--remove] [--no-apply] [--no-sync] [--json]
ameesh service install|uninstall|status [--agent A]… [--no-runner] [--no-notify] [--dry-run] [--json]
```

| Option | Meaning |
|---|---|
| `alerts --json` | one JSON object per line (schema `ameesh-alert/1`) |
| `alerts --follow` | stream: new alerts and resolutions |
| `--long-turn` | threshold of a long turn, seconds (default 1800) |
| `--idle-mail` | unread mail of an idle agent for S seconds (default 300) |
| `--dead-grace` | lease expired for S seconds (default 30) |
| `--session-tokens` | tokens re-read per turn above which the session is too big (default 15 M) |
| `--stale-lot` | lot without activity for S seconds (default 21600 = 6 h) |
| `--orphan-lot` | `intake`/`build` lot without activity nor a turn of its assignee for S seconds (default 1800) |
| `--delegation-grace` | grace period after a delegation's deadline before `delegation_expired` is raised (default 300) |
| `--mail-undeliverable` | mail pending for S seconds in the mailbox of a stopped agent or of a name absent from the registry (default 900; 0 disables `mail_undeliverable`) |
| `--turn-churn`, `--short-turn` | `turn_churn`: at least N short turns in the last hour (default 12), a turn being short under S seconds (default 120; L125) |
| `set … lot_wake=` | wake a runner when an open lot of the agent becomes actionable (default on; per agent, local state of its host) |
| `set … context_max_tokens=`, `turn_max_seconds=`, `turn_mail_max=`, `turn_grace_seconds=`, `mail_batch=` | per-agent ceilings and batching window (see [Configuration](configuration.md#runner)) |
| `set … mode=` | `execute` (default: run by a runner under a lease) or `externe` (a human's session, mailbox only, never woken by ameesh) |
| `notify --once` | a single pass, then exit |
| `notify --dry-run` | print what would be sent; send nothing, write no state |
| `notify --take-idle` | auto-take from the improvement backlog: wakeable agent idle without a lot for S seconds (default 1800; 0 disables; L119) |
| `notify --take-max-per-hour` | auto-takes per sliding hour, whole mesh (default 2) |
| `notify --test` | a test message on each channel of that human (exit 0 if all pass, 1 otherwise, 2 without a channel) |
| `restart --wait` | wait up to S seconds for the runner to apply the request |
| `adopt --account` | look for the session file under this declared account only |
| `adopt --force` | adopt even if a process still holds the session (warning in stderr and in the thread) |
| `resume --fresh` | forget the recorded session and open a fresh one on a deterministic resume brief |
| `interrupt --from` | sender (else the bound identity); must be an authorised sender |

`ameesh notify` takes the same threshold options as `ameesh alerts`. See
[Operate agents](../guides/operate-agents.md) for the schemas, the alert types,
`adopt`, `resume` and the `notify` configuration.

The **watch** (L121) is a timer on the host that sends the agent the versioned
instruction `consignes/veille.md` every `--every` (default 30 m); `show` lists
the watches of the mesh, `disable` removes the timer and the watch. A **host
drain** (L232e) durably withdraws a host: claims, turns and prompt consumption
close while the current turn finishes, critical agents first; `undrain` returns
it, `status` reads the lifecycle state, and an unplanned loss is recovered only
after a proof scoped to the source, agent and lease epoch. `ameesh handoff
plan` reserves a host move at a lot boundary with a durable journal (L232a).

**Elected roles** (L165). `role status` prints, per role, the holder and its
lease epoch, the ranked candidates and whether each can push an alert;
`canary` is a no-op gesture fenced by the environment's lease epoch (0
accepted, 1 refused, 2 no lease to guard), used to prove the fencing before a
real gesture. `role run` runs a mechanical command only on the host that holds
the role's live lease — the epoch is passed to the command — so a timer cannot
run the same pass twice after a failover; a delegated pass (`--pass alerts`)
runs on the holder when it can push, else on the first candidate that can.
`role timers` installs, verifies or removes the matching systemd timers on the
candidates (`--install`, `--check`, `--remove`), and its diagnostics name the
missing file or state, never a secret.

**User services** (macOS). `service install` writes and loads the launchd
agents: the host runner and `ameesh notify`, or one runner per `--agent` (the
flag repeats, or takes a comma-separated list); `--no-runner` and `--no-notify`
leave one side alone, and `--dry-run` prints the plists without writing.
`uninstall` unloads and removes them; `status` reads the installed agents and
their launchd state (`--json` for machine reading).

## Readable threads: `ameesh fil`

```
ameesh fil list
ameesh fil show <project> [<lot>] [--last N] [--meta]
ameesh fil tail <project> [<lot>] [--last N] [--meta] [--interval S]   # default 0.5 s
ameesh fil expire [--days N] [--dry-run]                              # retention (L361)
```

**Retention (L361, decision 0033 §9).** Entries older than one year
(`thread_retention_days`, `AMEESH_THREAD_RETENTION_DAYS`; `0` = never) are
removed by `ameesh fil expire`, also run daily by the runner's housekeeping.
Only the **oldest run** of complete ameesh entries goes: undated text stops
the expiry and stays. The file is rewritten whole under the thread's exclusive
lock and renamed over the old one, so a concurrent reader or writer reopens
the named file and no append is lost. Entry ids stay valid (logical offsets);
the header keeps the shift, the cut-off date and the number of entries removed
so far, and an expired id reads as "from the oldest kept entry".

**Mail linked to lots (L118).** When the sender is an orchestrator (declared
in `AMEESH_ALERT_ORCHESTRATORS`, `roles: [orchestrateur]` on its canon Agent
record, or an agent that assigned lots in the last 30 days):

* a message to an agent with **no open lot** prints a warning (stderr and the
  thread metadata); the message is still delivered;
* `--lot ID|REF` resolves an open lot (number, `#12`, or a unique reference:
  `issue_ref`, plan record, branch, first word of the title) and assigns it to
  the recipient if it has no assignee, belongs to the sender or to a human,
  through the guarded assignment (a refusal delivers nothing, exit 2). A lot
  owned by **another agent** is never taken over: warning with the
  `ameesh work assign` command. An unknown reference stays a free thread label;
* `--new-lot "title"` (any sender) creates the lot, assigned to the recipient,
  and attaches the message to it; `--estimate 2h` gives its estimated duration,
  **mandatory when the sender is an agent** (refused otherwise, nothing is
  delivered, exit 2; a warning for a human, L157); `--app PROJECT` files it in
  that project, which must be known — the refusal names the close projects —
  unless `--new-project` accepts an unknown one on purpose (#495);
* a single `agent/…` branch quoted in the message is set on the attached lot
  when it has none.

A message sent with `--lot` is filed in the **lot's project thread**
(`work_items.app`), not the sender's or the recipient's (L118c); the fallbacks
are unchanged ([Readable threads](../concepts/threads.md)).

From any other sender, `--lot` assigns nothing; when it designates an open
lot (number, or a reference matching a single open lot) the message is tied
to the lot's number, otherwise it stays a plain thread label. See
`docs/ORCHESTRATEUR.md`.

## Work items: `ameesh work`

```
ameesh work add --title TITLE [--type bug|evolution] [--source S] [--app APP]
                [--new-project] [--body BODY] [--issue-ref REF] [--workstream W]
                [--assignee A] [--budget USD] [--actor ACTOR] [--externe]
                [--branch agent/…] [--target BRANCH] [--priority 1|2|3]
                [--estimate 90m|2h|1h30|1,5h|2d|MINUTES] [--estimate-source S]
ameesh work plan <id|record> [--start DAY] [--end DAY] [--delivery DAY] [--source S]
                [--estimate DURATION] [--estimate-source S]
ameesh work estimates [--app PROJECT] [--json]   # actual/estimated ratios (ameesh-estimates/1)
ameesh work list [--state S] [--assignee A] [--limit N] [--stale-after S] [--json]
ameesh work show <id> [--json]
ameesh work move <id> <intake|build|qa|merged|promoted|blocked|waiting_human> [--note N] [--actor A]
ameesh work move <id> merged --correct REASON [--actor A]
ameesh work promote <id> [--note N] [--actor A]
ameesh work merged <id> (--sha SHA | --pr OWNER/REPO#N) [--migration-landed N]
                     [--note N] [--actor A]
ameesh work assign <id> <agent> [--externe] [--actor A] [--claim] [--branch agent/…] [--target BRANCH]
ameesh work dependencies <id> [--add ID|--remove ID] [--actor A]   # blocking prerequisites (L385)
ameesh work migration <id> [--number N] [--release] [--repo R]   # reserve a migration number (L122)
ameesh work set-app <id> <project> [--actor A] | backfill-app [--dry-run]
ameesh work link <id> [CARD] [--none] [--actor A] | close <id> (--abandoned | --superseded-by ID | --delivered --sha SHA | --done)
ameesh work packet record <carrier> [--repo R] [--base SHA] [--head REF] [--dry-run] [--json]
ameesh work packet show <lot|carrier> [--json]   # recorded package composition (#2443)
ameesh work sync-merges [--target T] [--dry-run] [--json]   # close frozen lots whose content is merged (L29), unless blocked or held (#2744)
ameesh work sync-packets [--target T] [--dry-run] [--json] # close the units of merged packages (manifest, #2426)
ameesh work sync-github [--dry-run] [--json]          # close lots whose PR is merged (read-only)
ameesh work schedule [--apply] [--json]               # planned dates at the p80 (L157c)
ameesh work measure [<id>…] [--no-ci] [--json]        # actual time per phase, merged diff size (L157c)
ameesh work project-github --app PROJECT [--repo R] [--dry-run]   # one issue per lot (L126), a view
ameesh work resolve <lot> --with <other> [--json]     # prepare a conflict resolution (L160d)
ameesh work queue add <id> | remove <id> | show [--target BRANCH] [--json]   # merge queue (L160e)
ameesh work land <id> [--project P] [--json]          # land the queue head (L160e2)
ameesh release open <id> --base SHA                   # freeze the release perimeter (L750)
ameesh release check <id> [--repo R] [--json]         # moved base, full suite on the head (L756)
ameesh release reason <id> --text "…"                 # record why the release base moved after the freeze
ameesh work sync-branches [--host H | --all-hosts] [--dry-run] [--json]
ameesh work delegate <id> <agent> --within 30m|2h|1d|SECONDS [--actor A]
ameesh work expire-delegations [--dry-run] [--json]
ameesh work note <id> <text> [--actor A]
ameesh work milestone <id> <frozen|verdict> [ok|blocked] [--sha SHA] [--note N] [--actor A]
                       [--repo R] [--no-static-checks REASON]
        # frozen: the project's static checks (scripts/freeze-checks.py of SHA,
        # run on SHA's tree) pass first; red refuses the freeze (#2542)
        # a diff that touches the merge gate (paths of the canon rule naming
        # src/ameesh/fire.py) also runs the gate's database-free contract
        # classes; unknown gate paths run them too (#3687)
        # frozen SHA: full 40-hex commit, or a 12+ hex abbreviation resolved in
        # the author's repository and written full; HEAD/a branch is refused,
        # and a legacy non-full freeze is a doubt, never a merge (#2786)
        # the target is read published and refreshed (bounded fetch of origin);
        # a stale clone neither accepts an already-delivered freeze nor writes
        # the absence mark that would make it look delivered (#2786, cx1)
ameesh work backlog add --title T --value "expected value" --score 1-100
                        [--priority 1|2|3] [--source S] [--team T] [--requires CAP …]
                        [--body B] [--package CARD] [--json]
ameesh work backlog list [--all] [--limit N] [--json]
ameesh work unrecorded [--since 24h|2h|7d|ISO-DATE] [--limit N] [--json]
```

The development stages `merged` and `promoted` both persist as the generic core
`delivered`. `workflow_stage` and `legacy_state` retain the stage; the historical
command names remain accepted.

**Plumbing around the lot.** `work dependencies` records explicit blocking
prerequisites between lots (L385). `work migration` reserves the next free
migration number for the lot, per repository, **before** the file is written
(L122); `--number` reserves an exact one, `--release` gives it back. It is
idempotent and visible in `work show`. `work merged --migration-landed N`
records the number that actually landed when the landing renumbered the
reservation (a packet takes the target's max+1, #1567); the option also
corrects an already merged lot, so `work show` names the landed number (its
file, and the reservation when it differs) instead of showing only the
reservation. `work set-app` corrects a
lot's project and `work backfill-app` fills the uniquely inferred ones, both
with a journal entry (#333). `work link` attaches a lot to a WorkPackage card
(`--none` detaches it),
`work close` closes an abandoned, superseded or delivered-outside-target lot
(`--delivered --sha <reviewed head>`: the `delivered` milestone carries the
head and counts as a delivery in `estimates`, #988), and `work sync-merges`
closes the frozen lots whose content is already merged (L29), including a
unit linked to a merged package by `work packet record` (#2443). `work packet
record` reads the package's composition commits (`ameesh-work:` trailers) and
writes the link — never the manifest; `work packet show` prints it. A lot that
is `blocked` or carries an unlifted `hold:` note is left open, with a journal
line `contenu fusionné, fermeture retenue : <motif>` and a wake-up to the
holder (#2744). `work schedule`
proposes the planned dates of open assigned lots at the p80 of the L157
reference table (`--apply` writes), and `work measure` records the actual time
per phase and the merged diff size. `work project-github` projects the lots (or
the plan) onto GitHub issues — a view, never a source (L126). `work resolve`
prepares the resolution of a conflict between two lots ahead of the merge
(L160d); `work queue add` enters a ready lot into its target's merge queue
(`remove` and `show` consult it) and `work land` merges the queue head by hand
(L160e, L160e2). `work list --stale-after`
changes the staleness window (default 6 h).

**Every human request becomes a lot** (L130). Orchestrators, design agents and
the owner's chat record every request from a human as a lot right away, with
the human's exact words as `--source`, a `--priority` (1 high, 2 normal,
3 low) and an `--estimate` (L157), and quote the lot number in their answer.
`work unrecorded` (read-only) lists the human messages of the period (mail
from a human, answers to decision requests, requests relayed by the chat) that
are attached to no lot, cite no lot (`L142`, `lot #142`, `ameesh-work: 142`)
and whose words no recent lot quotes in its source or body; acknowledgements
and `--cc` copies are skipped, a broadcast counts once. JSON schema
`ameesh-unrecorded/1`. See `docs/ORCHESTRATEUR.md` and `docs/EXPLOITATION.md`.

**Estimated and actual duration** (L157, migration 0052). Every lot carries an
estimated duration from its creation: minutes with its source and author
(`--estimate`, `--estimate-source`; `work plan <id> --estimate` sets or
revises it, keeping the history in `work_item_estimates`). It is **mandatory
when an agent of the registry creates the lot** (`work add`, `mail send
--new-lot`): refused otherwise, nothing is created; a human gets a warning.
ameesh measures the actual duration itself: start = first move to `build`
(or `qa`), or the assignee's first turn on the lot (noted by the runner);
end = the `merged` milestone. `work show` (line `estimate`), `ameesh projects`
(short **DURATION** column: `1h10/2h` elapsed/estimated, `2h` not started,
`2h40/2h!` overrun), `ameesh progress`, the roadmap and the header of the
lots' GitHub issues (L126) show the estimate, the actual duration and the
gap. `work estimates` gives, over delivered lots, the actual/estimated ratio
(median, p80) per lot type and per estimate author, using the estimate in
force when work started: the internal auditor audits it and adjusts the
estimation process (`docs/AUDITEUR.md`).

**Continuous-improvement backlog** (L119, decision 0037). `work backlog add`
queues a lot of type `improvement` (in `intake`, unassigned); the expected
value (one sentence) and its score are mandatory, refused otherwise by the CLI
and by the database. `work backlog list` shows the queue in take order:
priority, then score, then age (`--all` also lists items already taken and not
finished). At every pass, `ameesh notify` hands the best-ranked matching item
(team, required capabilities) to a wakeable agent idle for `--take-idle`
(30 min), through guarded assignment and a mail tied to the lot — **only if no
work awaits it** (no open lot, unread mail, pending prompt or open session lot)
**and no project lot waits for a taker**. The take mail forbids any
irreversible or production step (deploy, merge, data deletion, production
server, committed spend): the agent proposes it to a human. Subscription agents
first; what they leave is taken by pay-per-token agents in the same pass, never
during `balance_low`; nothing while a budget cap is reached or the agent's host
is under pressure, on low battery or not ready; at most `--take-max-per-hour`
(2) takes per sliding hour for the whole mesh.

Since v1.4.0, `work add --assignee` and `work assign` give a lot only to an
agent ameesh can **wake** (known, in `execute` mode, admitted on its host, with
a responsible human when a canon is configured); otherwise they refuse with
the reason (exit 1) and write nothing. A human (`human:<id>`) is always
accepted. `--externe` forces the assignment to an external agent, only if it
has a responsible human. `work delegate` hands a lot to a wakeable agent with a
deadline: if the delegate has not worked on it by then, the lot goes back to
the delegator (`--actor`, else the current assignee). The runner processes
deadlines at every pass; `expire-delegations` does it by hand. See
[Operate agents](../guides/operate-agents.md#guarded-assignment-and-delegation).

`--claim` assigns only a lot that has **no assignee**: a lot already taken is
refused (exit 1) naming its holder, and the write is conditional on the
assignee still being empty, so two concurrent claims produce a single
assignment and the other is refused without changing hands (#1026). The
`stale_lot` (unassigned lot) and `idle_capacity` (lots without an agent)
briefs carry that command; a deliberate reassignment of a lot already held
stays `work assign` without `--claim`.

**Branch of a lot (L118).** `--branch` records the lot's branch, `--target`
its target (default: `git config ameesh.target` in the repository, else
`origin/HEAD`, else `main`/`master`). The runner checks, every
`AMEESH_BRANCH_SWEEP_INTERVAL` (300 s), the open lots with a branch whose
assignee is on its host, in the assignee's working directory (read only, no
`fetch`), and closes as merged the lot whose branch entered its target, with
or without a PR: tip merged by a merge commit, last commit seen ahead found in
the target (ancestor, patch-id, squash — also after the branch was deleted),
or a merge commit of the target quoting the full branch name. A freshly
created branch, already an ancestor of its target, is not a merge.
`work sync-branches` runs the same check by hand (`--dry-run`: close nothing).
A configured `ameesh.target` that does not resolve is reported as an error,
never silently replaced by `main`.

**Stacked PRs.** Before merging a PR whose base is not the repository's
default branch, the fire (direct `gh pr merge`), `work merge-pr` and the merge
queue verify that this base is not already merged into the default branch:
merging there would land the content on a dead branch and lose it
(home#112/#113 after `canon/2452` was merged). The refusal names the fix
(`gh pr edit --base <default> N`, then freeze and re-review). A `gh pr merge`
run by hand outside ameesh — office — keeps that check manual:

```
git -C <clone> merge-base --is-ancestor origin/<base> origin/<default> \
  && gh pr edit <N> --repo <owner/repo> --base <default>
```

**Merges without a PR or a branch.** The same check also covers open lots
without a branch whose assignee is on the host: a merge commit of the target,
made after the lot was created, designates the lot with an `ameesh-work: <id>`
line, or with `#<id>` in its subject when the repository opts in
(`git config ameesh.lotRef hash`; elsewhere `(#45)` is a GitHub PR number).
The closed lot records the merge commit and a note saying how the merge was
found. By hand, `work merged <id> --sha SHA` moves a lot from any open state
(`intake`, `build`, `qa`, `blocked`, `waiting_human`) to `merged` at once,
with its `merged` milestone; a closed lot is never reopened. When the lot's
PR lives in another repository than the lot's (the canon `manaty/home`, for
example), `work merged <id> --pr OWNER/REPO#N` reads it with `gh`: the PR must
be `MERGED`, its head must be the lot's freeze (else its recorded branch),
and GitHub must report its merge commit; that commit must be in the
repository's default branch (`gh repo view` then `gh api compare`: `behind`
or `identical`, any doubt refuses — a merged stacked base that never entered
the default does not deliver). `--sha` is only an equality check; without a
reported merge commit, the declaration is refused.
A lot set to `promoted` by mistake goes back with `work move <id> merged
--correct "reason"`, logged, for humans and orchestrator or design agents
only.

**Actor.** Without `--actor`, `work` commands record the session's bound
identity (`AGENT_MAIL_NAME`, or a session binding), else `inconnu` with a
warning — never an empty actor. `work delegate` keeps its meaning (`--actor`
is the delegator).

### CI of a lot: `ameesh ci`

```
ameesh ci status <lot> [--limit N] [--json]        # latest runs and failing tests (ameesh-ci-status/1)
ameesh ci run <lot> [--sha S] [--avis 0|1|2] [--full] [--exception incident|unblock]
                   [--reason R] [--force] [--dry-run] [--json]
ameesh ci cancel <lot> [--all] [--repo R] [--dry-run] [--json]
```

On a lot branch a push starts nothing: review comes before CI (canon §3). Once
the required verdicts are on the frozen head, `ci run` requests the lot CI
**once**, on that head, through a `ci-lot-<sha>` label (impacted + contract).
It refuses without a `frozen` milestone `S`, a branch head and open PR head
still at `S`, `--avis N` `ok` verdicts on that exact commit by distinct actors
other than the assignee (0 light, 1 sensible, 2 SQL/security/prod), a workflow
at `S` that carries the trigger, and no identical request already green or in
flight. `--full --reason …` requests the whole suite; `--exception
incident|unblock --reason …` runs CI beside the review and writes the
exception on the lot (canon §11 bis); `--force` replaces a red or cancelled
request of the same composition. `ci status` shows the latest runs and their
failing tests, and `ci cancel` cancels the open runs of the lot that do not
carry its frozen commit (`--all` also cancels the frozen-head run).

## Decisions: `ameesh decide`

```
ameesh decide ask (--lot <id|ref> | --new-lot "title") --question "…"
                  --option a="…" [--option b="…" …] [--recommend a [--why "…"]]
                  [--urgent] [--by 2h|18:00|"2026-10-12 09:00"] [--needs-signature]
                  [--from-mail <id>] [--json]
ameesh decide <id> <option | "free text"> [--key FILE [--as NAME] | --signed] [--json]
ameesh decide withdraw <id> [--why "…"] [--json]
ameesh decide show <id> [--json]
ameesh decisions [--all] [--for human:ID] [--json]
```

An agent files a request with `decide ask`: it is attached to the project and
the work item, which moves to `waiting_human` (it returns to its previous state
on the answer), the target owner is notified, and the answer comes back by mail
(it wakes the requester). Only the **owner** answers, by `ameesh decide`, from
the CLI, the chat or a signed approval; the answer records the identity, the
channel and the exact text (L124, L233). `--from-mail <id>` converts a decision
written in prose in mail (the lot of the mail by default). `ameesh decisions`
lists the queue across projects, alongside gated actions and the prose decisions
waiting with no question (`--all` includes the answered ones).

## Personas and their sessions

```
ameesh activity <persona> [--since 24h] [--limit N] [--json]   # bounded activity log (L59)
ameesh sessions <persona> [--limit N] [--json]                 # current and past sessions (L52)
ameesh session list <agent> [--host H]                         # off-device session backups (L53)
ameesh session backup <agent> [--force]                        # back up the current session now
ameesh session restore <agent> [--session S] [--key K] [--home DIR] [--overwrite]
                        [--allow-other-agent]                  # restore the latest backup (or K)
ameesh memory status <persona>        # repository, working copy and unpushed changes
ameesh memory consolidate <persona>   # ask its session to consolidate journals into memories
ameesh memory close <persona>         # push remaining work, then remove the local copy
ameesh chat [--harness claude|codex] [--instruction] [--dry-run]  # the owner's conversation agent (L123)
```

A **persona** is a durable identity whose sessions, activity and memory are kept
across harnesses (L51, L52, L54, L59). Session backups are off-device, with an
explicit S3 endpoint (`AMEESH_BACKUP_S3_*`); `memory consolidate` asks the
persona's session to fold its journals into memories, `close` pushes the
remaining work then removes the local copy. `ameesh chat` is the owner's
conversation agent: it gathers decisions of all projects, forwards requests to
orchestrators and never takes another agent's session (L123).

## Canon and placement

```
ameesh canon check [--canon DIR] [--ref REV] [--fetch] [--json] [--host H]
ameesh canon show  [--canon DIR] [--ref REV] [--fetch] [--json]
ameesh canon sync  [--canon DIR] [--ref REV] [--fetch] [--json] [--host H] [--bootstrap-ref BRANCH]
ameesh placement check [--canon DIR] [--ref REV] [--fetch] [--json] [--agent A]
ameesh agent name  --role R [--project P …] [--canon DIR] [--ref REV] [--fetch] [--json]
ameesh agent spawn [name] --by CREATOR --ttl TTL [--role R] [--project P …] [--cwd DIR] [--prompt TEXT] [--json]
ameesh review-class [FILE …] [--diff REF] [--canon DIR] [--ref REV] [--fetch] [--json]
ameesh process check FILE [--ref REF] [--version N] [--json]
ameesh identity verify [FILE|-] [--issuer URL --client-id ID] [--domain D …] [--binding subject|email]
```

| Option | Meaning |
|---|---|
| `--canon` | id of a configured canon (its federation id): only that canon; without it, `check`, `show` and `sync` process every configured canon, one block each (`{"canons": [...]}` in JSON). Also accepts a canon root (else `AMEESH_CANON`) |
| `--ref` | canonical revision (else `AMEESH_CANON_REF`, the manifest's `ref`, `origin/main`) |
| `--fetch` | `git fetch` before reading |
| `--host` | host whose state is diagnosed or synced (else `AMEESH_HOST` / this machine) |
| `--bootstrap-ref` | first bootstrap of the passkey registry only: trusted canonical branch (`origin/<BRANCH>`), logged, ignored afterwards |
| `--ttl` (spawn) | expiry: `30m`, `2h`, `1d` (max `7d`) |
| `--role`, `--project` (name, spawn) | role and projects of a generated name; `--project` repeats, several projects drop the repo segment (`<org>-<role>-<seq>`). Segments come from `extensions.ameesh.naming`; a role without a short name is refused, and a name already used by a card or any registry row (stopped agents included) is never reused. Two repos never share a segment and one repo keeps the same segment in every canon (a divergent mesh is refused); a configured canon that cannot be read also refuses generation, naming the canon and its ref |
| `--diff` (review-class) | files changed since REF (`REF..HEAD` + working tree) |

`ameesh process check` compiles the `workflow:` block of a Process card
(`processus/*.md`, see [Write a Process card](../guides/write-a-process.md));
it reads no database and writes nothing. `--ref` and `--version` label the
checked revision (default `file:<path>` and `0`), `--json` prints the encoded
native document and labels. Exit 0 when the block compiles (or the card has no
workflow), 1 on a named refusal, 2 when the file cannot be read.

`ameesh identity verify` checks an OIDC identity token (read from the FILE
given, or from standard input when it is omitted or `-`) and binds its `{issuer,
subject}` — or its `email` on an explicit `--binding email` — to a member of
the canon (L86). It prints the bound `human` as JSON; exit 3 when no member
matches, 1 on a named refusal (bad token, unknown issuer, unreadable
configuration). It reads the canon, not the registry.

## Hosts, capacity and enrolment

```
ameesh hosts [HOST] [--history N] [--json]     # host resources (L31)
ameesh host drain HOST [--for 8h] | undrain HOST | status [HOST]   # lifecycle (L232e)
ameesh handoff plan AGENT --repository R [--dry-run] | show OPERATION
ameesh guard check|run|drain                   # capacity guard of a lending VM (L159e)
ameesh offload run [--host H] [--profile P] [--repo NAME] [--timeout S] [--env K=V]…
                [--fallback local|none] -- <command…>
        | status [<job>] | cancel [<job>]      # heavy suites on a lending VM (L159b)
ameesh host enroll|revoke|list|show            # enrol lending devices (L110; human only)
ameesh device enroll|challenge|show            # device-side enrolment (L110)
```

`guard check` prints one measure, the decision and the state; `guard run` is the
guard loop (offer, levels 0–4, enforcement, drain and stop); `guard drain` asks
the running guard for a drain (L159e). A host fiche read by the canon's offer
reader knows the guard's three reclaim keys (`io_pressure_max`, `disk_free_min`,
`disk_paths`; L159a/#479). `offload run` snapshots the working tree through a
temporary index (no history travels), sends a thin pack to the VM and runs the
command in a throwaway checkout: output and exit code come back unchanged, and
`--fallback local` (the default) runs it here when the VM is unreachable before
the job starts (L159b). `host enroll` emits an enrolment code, `host revoke`
revokes an executor, `host list|show` reads them; `device enroll|challenge|show`
is the device side (L110; authorised human only).

## Housekeeping, shared dependencies, conflicts

```
ameesh housekeeping [--dry-run | --apply] [--by BY] [--json]   # cleanup (L73; ex menage)
ameesh deps link|local|unlink|status|entries                   # shared node_modules (L231)
ameesh conflicts [--project P] [--all] [--json]                # conflict radar (L160b)
ameesh harness list|show|check|versions                        # descriptors, versions per host (L120)
ameesh module <name> …                                         # installed project module commands
```

`housekeeping` is the L148 name of the old `menage` (the French spelling stays a
hidden alias): `--apply` is for a human or the runner only, never an agent
session; it frees the build output of idle worktrees and removes merged kept
worktrees after 24 h (L230), and it never promises to delete a `/tmp` entry that
appeared during another agent's turn (L222). `deps` shares one `node_modules`
per lockfile and per host, sealed read-only; `local` (alias `unlink`) restores a
local install before packaging (L231). `conflicts` is the passive conflict radar
between lots (L160b). `harness versions` tracks the harness versions per host and
their differences (L120).

## Read API and the UI

```
ameesh serve [--port 8780] [--token-file FILE] [--human human:ID] [--organisation NAME]
ameesh serve --openapi        # print the OpenAPI 3.1 document (docs/api/openapi-v1.json)
```

A read-only local HTTP server on 127.0.0.1 (L84): GET and HEAD only, Bearer
token in a `0600` file, each response the same serialisation as the matching
`--json` output (one source of truth). `GET /ui/` serves the standalone page
built from `ui/` (L88); `GET /.well-known/ameesh` and `/api/v1/openapi.json`
need no token. The contract is `docs/api/openapi-v1.json`.

## Mediated executor (path B)

```
ameesh serve --exec-only --auth-file FILE [--listen HOST:PORT] …
ameesh host enroll|revoke|list|show
ameesh device enroll|challenge|show
```

The mediated executor lets a device run agents through the mesh server without
database, forge or provider credentials (L107–L114): contract 1.1, mediated
storage and runner, single-use enrolment and revocation, host gate, model relay,
server-side work repositories, executor image entry point and signing helper.
Operator procedure: `docs/EXECUTEUR-MEDIEE.md`; review notes:
`docs/PATH-B-REVIEW.md`.

## Actions: `ameesh action`

```
ameesh action propose --connector shell-noop|git-merge --operation OP --project P --target T
                      [--args JSON | --args-file F] [--class read|reversible|irreversible|costly]
                      [--amount MINOR_UNITS] [--currency C] [--work-item ID] [--approver human:ID]
                      [--policy-version V] [--receipt-class CLASS] [--by BY] [--json]
                      [--noop-dir DIR] [--timeout S]
ameesh action show <id> [--json]
ameesh action list [--state STATE] [--project P] [--limit N] [--json]
ameesh action request <id> --approver human:ID [--assume-duplicate] [--requested-by WHO]
                      [--local] [--ttl S] [--json]
ameesh action fetch-receipt <id> [--request-id req_…] [--wait S] [--out FILE] [--by BY] [--json] [verify options]
ameesh action approve <id> [--receipt FILE] [--standing] [--by BY] [--json] [verify options]
ameesh action execute <id> [--by BY] [--json] [verify options] [--noop-dir DIR] [--timeout S]
ameesh action retry <id> [--receipt FILE] [--standing] [--by BY] [--json] [verify options]
ameesh action reconcile <id> [--force] [--by BY] [--json] [--noop-dir DIR] [--timeout S]
ameesh action replace <id> --receipt FILE [--by BY] [--json] [verify options]
ameesh action cancel <id> [--note N] [--by BY]
ameesh action recover [--grace S] [--by BY]
```

*verify options*: `--rp-id RP_ID`, `--origin ORIGIN` (repeatable), `--allow-facade
webauthn|ed25519|device-es256`, `--level standard|eleve`.

| Option | Meaning |
|---|---|
| `--class` | can raise the connector's class, never lower it |
| `--amount` | in minor units |
| `--receipt-class` | classes that require a receipt (default `irreversible`, `costly`) |
| `--assume-duplicate` | a decision that assumes the duplicate of an action with unknown outcome |
| `--requested-by` | `agent:<id>` or `human:<id>` (default: the session, else the proposer) |
| `--local` | without ameesh-approve: the raw request to sign (tests, tools) |
| `--wait` (fetch-receipt) | wait for the signature up to S seconds |
| `--out` | also save the receipt (mode 0600) |
| `--standing` | coverage by a bounded standing approval |
| `--force` (reconcile) | interrupted launch: do not wait for the deadline |
| `replace --receipt` | receipt that assumes the duplicate (`request --assume-duplicate`) |

## Receipts and authenticators

```
ameesh receipt verify <file> [--rp-id RP_ID] [--origin ORIGIN]… [--allow-facade F]…
                      [--level standard|eleve] [--digest sha256:…] [--action-id ID]
                      [--kind action|standing|any] [--decision approve|deny|any]
                      [--expect-commit SHA] [--consume] [--by BY] [--canon ID] [--json]
ameesh authenticator list [--approver human:ID] [--all] [--expect-commit SHA] [--canon ID] [--json]
```

`--allow-facade` defaults to `webauthn` and `device-es256`. `--expect-commit`
requires the authenticator to come from that canon commit. `--consume` consumes
the nonce (single use). `authenticator list --all` includes revoked entries.
With several canons, authenticators are kept per canon: `--canon ID` verifies a
receipt against, or lists, the authenticators of that canon.

## Consistency with ameesh-approve: `ameesh approve-check`

```
ameesh approve-check [--url URL] [--json]
```

Compares the policy published by ameesh-approve (`GET /health`, no token, no
secret) at `approve_url` (or `--url`, default `AMEESH_APPROVE_URL`) with this
verifier's policy (`AMEESH_APPROVE_RP_ID`, `AMEESH_APPROVE_ORIGINS`). Exit
codes: 0 consistent; 1 differences, listed; 2 configuration missing or service
unreachable. Nothing is written.

## Approval bridge: `ameesh process-approvals`

```
ameesh process-approvals refresh [--config F] [--dry-run] [--json]  # read the IMA issue, write the local projection
ameesh process-approvals check [--config F] [--json]                # freshness, counts, digests
ameesh process-approvals show --repo R --revision SHA --path P [--source github] [--config F] [--json]
```

The IMA/GitHub approval bridge (#4006) reads the IMA approval issue (#704)
with the bureau's GitHub token and writes a read-only local projection that
the Process workflow gate consumes. A projection that is missing, stale,
unreadable, writable by the group or others, or of an unknown schema is
refused (fail-closed): the `approvals` source disappears from the snapshot and
every guard that requires it refuses `source-unavailable`. `source=api` is
never an authority; only the `github` read counts, and `--source` accepts
`github` alone. `refresh` runs on `bureau-goal` (it is the only host that talks
to GitHub) and `--dry-run` reads and normalises without writing; `check` and
`show` make no network call, so an agent host can use them. The source block
of the configuration (repository, issue, approvers) anchors the projection's
identity: a copy from another repository, issue or approver is refused.
Operator procedure and the configuration key (`approvals`) are in
`docs/EXPLOITATION.md`; the contract is in
`docs/design/process-workflow-registry.md`.

## Observations journal: `ameesh process-observations`

```
ameesh process-observations refresh [--config F] [--dry-run] [--json]  # collect and write the local projection
ameesh process-observations check [--config F] [--json]                # freshness, counts, digests
ameesh process-observations show [--config F] [--id ID] [--lot N] [--app A] \
    [--kind deployed|pass|fail] [--environment staging|preprod|acceptance] \
    [--artifact SHA] [--all] [--json]
ameesh process-observations revoke --id ID --reason R [--actor A] [--config F] [--json]
```

The IMA observations journal (#4425) is the native path for deployments, QA
results and human acceptances. The bureau (`bureau-goal`) is the only
collector: it appends to a JSONL journal (0600, directory 0700), whose entries
are chained by JCS+SHA-256 digests, and derives the read-only
`ameesh-observations/1` projection. Hosts only read that projection and never
contact a source. A projection that is missing, stale, unreadable, writable by
the group or others, of another identity or of an unknown schema is refused
(fail-closed): the `observations` source disappears from the snapshot and
every guard that requires it refuses `source-unavailable`. A revoked,
superseded, expired or future record is never collected.

`refresh` collects the native staging attestations
(`ameesh-staging-actif/1`, release cut-off reports) and the human acceptances
of the #4006 approval projection; the QA envelope waits for the external IMA
#796 contract and a configured `qa` key is refused, no envelope being
invented. `deployed` never expires by age: the next deployment of the same
environment supersedes it; QA `pass`/`fail` expire after 7 days; acceptances
follow #4006's own expiry and revocation. `--dry-run` reads and derives
without writing either file. `revoke` appends a terminal revocation and
republishes the projection. `check`/`show` make no network call, so an agent
host can use them. The configuration key (`observations`) and the operator
procedure are in `docs/EXPLOITATION.md`; the contract (journal, projection,
identity, refusal codes) is in `docs/design/process-workflow-registry.md`.

## Model catalogue: `ameesh models`

```
ameesh models list [--harness H] [--json]
ameesh models show <model> [--json]
ameesh models discover [--source anthropic|deepseek|harness|openai|prices]
                       [--dry-run] [--notify AGENT] [--sender NAME]
```

## Version watch: `ameesh veille`

```
ameesh veille scan [--dry-run] [--json] [--no-probe] [--harness ID]…
```

`#3264 A` / `#4291`: once a day (`ameesh-veille.timer`), `scan` reads the
harness versions installed on this host (as `ameesh harness versions` does),
the versions published upstream (npm `dist-tags` and `time`, or the
repository's releases), the models served by the provider and the identifier
actually billed in `turn_costs.model`, then keeps **one** open evaluation lot
per harness. A newer version replaces that lot's object instead of opening a
second one; an object already evaluated (merged or closed lot) is not
reopened. Only stable and `rc` versions count — alpha, beta and equivalent
pre-releases are ignored — and only a version newer than the newest installed
version on any host is considered; a version gap between hosts is written in
the lot. A provider recommendation ("version X or later") makes the lot P1,
otherwise P2. Nothing is hard-coded per client: harnesses, npm package,
repository, changelog and provider come from `veille.sources` in the mesh's
configuration (see `docs/design/3264-veille-versions.md`); `veille.host`
designates the one host that reconciles lots, the other hosts only recording
their installed versions.

`--dry-run` reads and decides without writing a version reading or a lot;
`--no-probe` skips re-reading the installed versions of this host; `--harness`
(repeatable) limits the scan to the declared harnesses. `--json` prints an
`ameesh-veille/1` report (`enabled`, `reconciled`, `sources`, `actions`,
`errors`). With no `veille.sources`, it prints `veille: off` and exits 0; a
host that is not `veille.host` prints `veille: probe only` after recording its
versions. Exit 0 otherwise, 1 when the report carries an error (an unreadable
source stays visible in the timer's journal and is retried on the next pass),
2 on a usage error.

## Comparative replay: `ameesh replay`

```
ameesh replay bench --project P [--repo R] [--json]
ameesh replay run --project P [--harness H] [--evaluate harness|model] [options]
ameesh replay report FILE [--json] [--note]
```

`#3264 B` / L15: the comparative benchmark behind the version watch. `bench`
selects, read-only, the last three delivered lots of the project — one per
class (light, normal, sensitive), the next delivered lot filling a missing
class — with their frozen base and tests; a lot without a provable frozen
base, of class release, or with a doubtful diff is rejected with its reason on
stderr, never guessed.

`run` replays each lot twice, in an isolated tree (`git archive` of the frozen
base, no remote, no history or branches, no DSN, state or mailbox; the dsh
home is a copy without hooks), varying only the **evaluated parameter**:
`--evaluate harness` uses two binaries at the same model, effort and turn cap;
`--evaluate model` uses one binary with two model ids at the same effort and
turn cap. It measures cost (input, cache and output tokens priced by
`cost.py`), duration and turn count, runs the real lot's tests (the files its
diff added or modified, plus the neighbouring modules of the impact map when a
safe plan exists), and has a reviewer of another family grade the two
anonymous submissions from 0 to 10 and list the defects — the reviewer sees
neither version, cost, duration nor folder label. `--reviewer` names that
harness; the plan is set by `--old-binary`/`--new-binary`,
`--old-model`/`--new-model`, `--model`, `--effort`, `--turn-cap`,
`--test-cap`, `--review-cap`, `--test-command`, `--map`, `--lots`,
`--work-root`, `--report`, `--dsh-home`, `--env-file`, `--max-runs`,
`--max-reviews` and `--budget-usd`. `--resume` continues an existing report
only when the full plan identity matches (binaries, models, effort, bench,
reviewer, caps) and refuses to relabel measurements produced under other
conditions; `--keep` keeps the replay trees. The conclusion is **better** (no
measure worse, at least one better), **equivalent** (cost and duration within
±10 %, grade within ±1 point) or **regression**; a replay that fails the real
lot's tests is never an adoption. The bench writes no note and no milestone:
the evaluation agent cites the report in its lot's note.

`report` reads an `ameesh-replay-report/1` report and prints its table and
conclusion; `--note` prints the replay note (report path and SHA-256), `--json`
the full report. `run` exits 3 when the conclusion is `incomplete` (replays or
reviews still missing), 1 on a named refusal or an unreachable database, 2 on
a usage error (`--resume` without `--report`, no subcommand); `bench` and
`report` exit 0, 1 on a named refusal or an unreachable database.

## Schema, diagnosis, v0 bridge

```
ameesh migrate
ameesh doctor [--notify-test] [--probe]       # --probe: database, schema, migrations; nothing else
ameesh import-v0 [--state DIR] [--agents a,b] [--dry-run]
ameesh export-v0 [--state DIR] [--agents a,b] [--keep]   # --keep: copy instead of move
```

## Provenance: `ameesh provenance`

```
ameesh provenance record --commit SHA [--lot ID] [--quiet] [--json]
ameesh provenance install [--repo R] [--json]
ameesh provenance intent [--json]
ameesh provenance created [--json]
ameesh provenance show [--commit SHA | --milestone ID] [--json]
```

`#1465`: the exact origin of a commit and of a milestone — actor, session,
turn and harness. `install` writes the git hooks of a repository: `intent`
attests the commit that is about to be made (pre-commit), `created` records
the object once it is born (post-commit); `record` does the same for an
existing commit (`--lot` attaches it to a work item), and `show` reads the
receipt, or nothing when it is unknown. A provenance receipt proves where a
change came from; it is never human authority (see the bench authority model
below).

## Bench authority model (Ed25519)

These commands are the bench's original authority model. In v1, a human's
authority is a receipt from ameesh-approve; an Ed25519 key kept on an agents'
host never counts as human authority, and only proves an agent's provenance.

```
ameesh key generate --out DIR [--name NAME] [--i-am-the-owner]
ameesh key register <agent> --public-key FILE [--role owner|agent] [--note N]
ameesh key show <agent> [--json] | key list [--json] | key revoke <agent>
ameesh approve --key KEY --action A --hash H [--kind K] [--decision approved|rejected]
               [--expires 24h] [--as AGENT] [--meta JSON]
ameesh approvals [--action A] [--hash H] [--limit N] [--json]
ameesh verify --action A --hash H [--consume] [--by BY] [--json]
```

## `ameesh-approve`

```
ameesh-approve serve [--config F] [--rp-id R] [--origin O]… [--public-url U]
                     [--bind 127.0.0.1] [--port 8765] [--token-file F]
                     [--state-dir D] [--proposals-dir D] [--level standard|eleve]
                     [--profile strict|compatible] [--tls-cert F --tls-key F]
ameesh-approve enroll-link --approver human:ID [--ttl S] [config options]
ameesh-approve gen-token [--token-file F]
```

- `serve` runs the service on the loopback interface: HTTP behind an HTTPS
  reverse proxy, or local TLS (`--tls-cert`, `--tls-key`: PEM files, private
  `0600` files of the service's user) behind a TLS-passthrough gateway; the
  certificate is reloaded when it changes and on `SIGHUP`. `--profile`:
  `strict` (default: RP ID = host of the single `https` origin) or
  `compatible` (local trials, explicit). `--origin` is an exact allowed origin,
  for example `https://approve.example.org` (repeatable).
- `enroll-link` creates a single-use enrolment link, to open **on the phone**;
  the passkey created becomes a **proposal** for the canon, active after a
  reviewed pull request.
- `gen-token` creates the service token (256 bits, file `0600`, never
  overwritten); default `AMEESH_APPROVE_TOKEN_FILE` or
  `~/.config/ameesh-approve/service-token`.

Run it under the service's Unix user, never the agents'. It reads the database
(trust registry, actions) with ameesh's configuration (`AMEESH_DSN`,
`AMEESH_SCHEMA`…).
