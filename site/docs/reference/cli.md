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

## Mailbox: `ameesh mail` / `agent-mail`

```
agent-mail send <dest|all> <text…> [--from NAME] [--lot ID]
                [--kind request|reply|notify|event] [--urgent]
                [--sign --key FILE] [--expires 24h]
agent-mail list                       # agents, host, lease, unread
agent-mail inbox [NAME]               # unread messages of NAME, without marking them read
agent-mail whoami                     # bound identity (name + lease); --cwd = diagnostic
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
ameesh show <agent> [--json]               # one agent
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

## Operating agents

```
ameesh set <agent> key=value [key=value …]   # model=… effort=… tier=… session_policy=par-lot|taille|jamais
                                             # (empty value = default; effect at the next turn)
ameesh alerts [--json] [--follow] [--interval S] [--long-turn S] [--idle-mail S]
              [--dead-grace S] [--session-tokens N] [--stale-lot S]
ameesh restart <agent> --brief FILE|- [--wait S] [--json]
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
| `restart --wait` | wait up to S seconds for the runner to apply the request |
| `interrupt --from` | sender (else the bound identity); must be an authorised sender |

See [Operate agents](../guides/operate-agents.md) for the schemas.

## Readable threads: `ameesh fil`

```
ameesh fil list
ameesh fil show <project> [<lot>] [--last N] [--meta]
ameesh fil tail <project> [<lot>] [--last N] [--meta] [--interval S]   # default 0.5 s
```

## Work items: `ameesh work`

```
ameesh work add --title TITLE [--type bug|evolution] [--source S] [--app APP]
                [--body BODY] [--issue-ref REF] [--workstream W]
                [--assignee A] [--budget USD] [--actor ACTOR]
ameesh work list [--state S] [--assignee A] [--limit N] [--json]
ameesh work show <id> [--json]
ameesh work move <id> <intake|build|qa|merged|promoted|blocked|waiting_human> [--note N] [--actor A]
ameesh work note <id> <text> [--actor A]
ameesh work milestone <id> <frozen|verdict> [ok|blocked] [--sha SHA] [--note N] [--actor A]
```

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
| `--canon` | canon root (else `AMEESH_CANON`) |
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
                      [--expect-commit SHA] [--consume] [--by BY] [--json]
ameesh authenticator list [--approver human:ID] [--all] [--expect-commit SHA] [--json]
```

`--allow-facade` defaults to `webauthn` and `device-es256`. `--expect-commit`
requires the authenticator to come from that canon commit. `--consume` consumes
the nonce (single use). `authenticator list --all` includes revoked entries.

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
