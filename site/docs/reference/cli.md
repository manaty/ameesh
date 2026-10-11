# CLI reference

This reference is written from the real `--help` output of ameesh v1 and its
integrated lots. The built-in help is in French; option names are identical.
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
agent-mail send <dest|all> <text…> [--from NAME] [--lot ID|REF] [--new-lot "title"]
                [--kind request|reply|notify|event] [--urgent]
                [--sign --key FILE] [--expires 24h] [--queue]
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
agent-mail status "<current work>"    # agent status (registry + terminal title)
agent-mail hook <claude|codex|deepseek>   # hook: reads JSON on stdin, delivers unread mail
agent-mail statusline                 # Claude Code status line
agent-mail migrate                    # versioned migrations
agent-mail doctor [--notify-test | --probe]
```

The session identity is `$AGENT_MAIL_NAME` (set by the runner). If
`$AMEESH_RUNNER_ID` and `$AMEESH_LEASE_EPOCH` are also set, the lease must be
alive and held by that runner. The working directory never gives an identity.
Hooks never fail the agent: any error exits 0 silently. A message from an agent
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

## Runner: `ameesh run` / `agent-runner`

```
agent-runner [--host H] [--runner-id ID] [--agents a,b] [--once [--wait S]]
             [--dry-run] [--max-turns N] [--lease-ttl S] [--poll S]
             [--idle-nudge S] [--migrate]
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
ameesh decisions [--for human:ID] [--json] # what waits for a human
ameesh progress [--json] [--html FILE] [--project P] [--since SINCE]
ameesh cost report [--json]                # agent, harness, model, spend, gauges
ameesh cost spent [agent] [seconds]        # spend (USD) over a sliding window
ameesh cost over [agent]                   # exit 0 if the plan's pace is exceeded
ameesh cost turns [--agent A] [--since SINCE] [--limit N] [--json]      # usage per turn
ameesh cost gauges [--harness H] [--since SINCE] [--json]               # history of plan gauges
ameesh cost balance [--provider deepseek] [--record] [--since SINCE] [--json]
```

`--since` takes a duration (`16h`, `2d`) or an ISO date. `cost balance
--record` reads the balance now (read-only, free).

`ameesh list` shows a **PROJECT** column (the agent's team, else its
chantier) and groups agents by project, a **LOTS** column (open lots assigned
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

## Operating agents

```
ameesh set <agent> key=value [key=value …]   # model=… effort=… tier=… session_policy=par-lot|taille|jamais
                                             # mode=execute|externe
                                             # (empty value = default; effect at the next turn)
ameesh alerts [--json] [--follow] [--interval S] [--long-turn S] [--idle-mail S]
              [--dead-grace S] [--session-tokens N] [--stale-lot S]
              [--orphan-lot S] [--delegation-grace S] [--mail-undeliverable S]
ameesh notify [--once] [--dry-run] [--json] [--interval S] [alert thresholds…]
ameesh notify --test human:ID [--json]
ameesh restart <agent> --brief FILE|- [--wait S] [--json]
ameesh adopt <agent> --session ID --harness claude|codex|deepseek [--account ACCOUNT]
             [--cwd DIR] [--brief FILE|-] [--chantier C] [--force] [--json]
ameesh resume <agent> [--brief FILE|-] [--fresh] [--json]
ameesh interrupt <agent> <message…> [--from SENDER]
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
| `set … mode=` | `execute` (default: run by a runner under a lease) or `externe` (a human's session, mailbox only, never woken by ameesh) |
| `notify --once` | a single pass, then exit |
| `notify --dry-run` | print what would be sent; send nothing, write no state |
| `notify --take-idle` | auto-take from the improvement backlog: wakeable agent idle without a lot for S seconds (default 1800; 0 disables; L119) |
| `notify --take-max-per-hour` | auto-takes per sliding hour, whole mesh (default 2) |
| `notify --take-paid` | let pay-per-token agents take too (after subscription agents; never during `balance_low`) |
| `notify --test` | a test message on each channel of that human (exit 0 if all pass, 1 otherwise, 2 without a channel) |
| `restart --wait` | wait up to S seconds for the runner to apply the request |
| `adopt --account` | look for the session file under this declared account only |
| `adopt --force` | adopt even if a process still holds the session (warning in stderr and in the thread) |
| `resume --fresh` | forget the recorded session and open a fresh one on a deterministic resume brief |
| `interrupt --from` | sender (else the bound identity); must be an authorised sender |

`ameesh notify` takes the same threshold options as `ameesh alerts`. See
[Operate agents](../guides/operate-agents.md) for the schemas, the alert types,
`adopt`, `resume` and the `notify` configuration.

## Readable threads: `ameesh fil`

```
ameesh fil list
ameesh fil show <project> [<lot>] [--last N] [--meta]
ameesh fil tail <project> [<lot>] [--last N] [--meta] [--interval S]   # default 0.5 s
```

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
  and attaches the message to it;
* a single `agent/…` branch quoted in the message is set on the attached lot
  when it has none.

From any other sender, `--lot` assigns nothing; when it designates an open
lot (number, or a reference matching a single open lot) the message is tied
to the lot's number, otherwise it stays a plain thread label. See
`docs/ORCHESTRATEUR.md`.

## Work items: `ameesh work`

```
ameesh work add --title TITLE [--type bug|evolution] [--source S] [--app APP]
                [--body BODY] [--issue-ref REF] [--workstream W]
                [--assignee A] [--budget USD] [--actor ACTOR] [--externe]
                [--branch agent/…] [--target BRANCH]
ameesh work list [--state S] [--assignee A] [--limit N] [--json]
ameesh work show <id> [--json]
ameesh work move <id> <intake|build|qa|merged|promoted|blocked|waiting_human> [--note N] [--actor A]
ameesh work move <id> merged --correct REASON [--actor A]
ameesh work merged <id> --sha SHA [--note N] [--actor A]
ameesh work assign <id> <agent> [--externe] [--actor A] [--branch agent/…] [--target BRANCH]
ameesh work sync-branches [--host H | --all-hosts] [--dry-run] [--json]
ameesh work delegate <id> <agent> --within 30m|2h|1d|SECONDS [--actor A]
ameesh work expire-delegations [--dry-run] [--json]
ameesh work note <id> <text> [--actor A]
ameesh work milestone <id> <frozen|verdict> [ok|blocked] [--sha SHA] [--note N] [--actor A]
ameesh work backlog add --title T --value "expected value" --score 1-100
                        [--priority 1|2|3] [--source S] [--team T] [--requires CAP …]
                        [--body B] [--package FICHE] [--json]
ameesh work backlog list [--all] [--limit N] [--json]
```

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
first; pay-per-token agents only with `--take-paid`, never during
`balance_low`; nothing while a budget cap is reached or the agent's host is
under pressure, on low battery or not ready; at most `--take-max-per-hour` (2)
takes per sliding hour for the whole mesh.

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

**Merges without a PR or a branch.** The same check also covers open lots
without a branch whose assignee is on the host: a merge commit of the target,
made after the lot was created, designates the lot with an `ameesh-work: <id>`
line, or with `#<id>` in its subject when the repository opts in
(`git config ameesh.lotRef hash`; elsewhere `(#45)` is a GitHub PR number).
The closed lot records the merge commit and a note saying how the merge was
found. By hand, `work merged <id> --sha SHA` moves a lot from any open state
(`intake`, `build`, `qa`, `blocked`, `waiting_human`) to `merged` at once,
with its `merged` milestone; a closed lot is never reopened. A lot set to
`promoted` by mistake goes back with `work move <id> merged --correct
"reason"`, logged, for humans and orchestrator or design agents only.

**Actor.** Without `--actor`, `work` commands record the session's bound
identity (`AGENT_MAIL_NAME`, or a session binding), else `inconnu` with a
warning — never an empty actor. `work delegate` keeps its meaning (`--actor`
is the delegator).

## Canon and placement

```
ameesh canon check [--canon DIR] [--ref REV] [--fetch] [--json] [--host H]
ameesh canon show  [--canon DIR] [--ref REV] [--fetch] [--json]
ameesh canon sync  [--canon DIR] [--ref REV] [--fetch] [--json] [--host H] [--bootstrap-ref BRANCH]
ameesh placement check [--canon DIR] [--ref REV] [--fetch] [--json] [--agent A]
ameesh agent spawn <name> --by CREATOR --ttl TTL [--cwd DIR] [--prompt TEXT] [--json]
ameesh review-class [FILE …] [--diff REF] [--canon DIR] [--ref REV] [--fetch] [--json]
```

| Option | Meaning |
|---|---|
| `--canon` | id of a configured canon (its federation id): only that canon; without it, `check`, `show` and `sync` process every configured canon, one block each (`{"canons": [...]}` in JSON). Also accepts a canon root (else `AMEESH_CANON`) |
| `--ref` | canonical revision (else `AMEESH_CANON_REF`, the manifest's `ref`, `origin/main`) |
| `--fetch` | `git fetch` before reading |
| `--host` | host whose state is diagnosed or synced (else `AMEESH_HOST` / this machine) |
| `--bootstrap-ref` | first bootstrap of the passkey registry only: trusted canonical branch (`origin/<BRANCH>`), logged, ignored afterwards |
| `--ttl` (spawn) | expiry: `30m`, `2h`, `1d` (max `7d`) |
| `--diff` (review-class) | files changed since REF (`REF..HEAD` + working tree) |

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

## Model catalogue: `ameesh models`

```
ameesh models list [--harness H] [--json]
ameesh models show <model> [--json]
ameesh models discover [--source anthropic|deepseek|harness|openai|prices]
                       [--dry-run] [--notify AGENT] [--sender NAME]
```

## Schema, diagnosis, v0 bridge

```
ameesh migrate
ameesh doctor [--notify-test] [--probe]       # --probe: database, schema, migrations; nothing else
ameesh import-v0 [--state DIR] [--agents a,b] [--dry-run]
ameesh export-v0 [--state DIR] [--agents a,b] [--keep]   # --keep: copy instead of move
```

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
