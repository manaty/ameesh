# Configuration

ameesh reads its configuration from, in order of precedence:

1. environment variables `AMEESH_*` (the older `AGENT_MESH_*` names are still
   accepted as aliases);
2. the JSON file `~/.config/ameesh/config.json` (or the path in
   `AMEESH_CONFIG`), whose keys are the setting names below;
3. built-in defaults.

No secret is stored in the repository. The database password comes from
`PGPASSWORD` or `~/.pgpass` (mode `0600`), or from a full DSN.

## Database and host

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `AMEESH_DSN` | `dsn` | `postgresql://agent_mesh@127.0.0.1:55432/agent_mesh` (the bench) | Postgres DSN |
| `AMEESH_SCHEMA` | `schema` | `public` | schema used by ameesh |
| `AMEESH_DRIVER` | `driver` | `auto` | `auto`, `psycopg` or `psql` |
| `AMEESH_BACKEND` | `backend` | `auto` | `auto` (Postgres, file fallback for mail), `pg`, `file` |
| `AMEESH_HOST` | `host` | the machine's host name | the host this runner serves |
| `AMEESH_STATE` | `state_dir` | `~/.local/state/ameesh` | local state (sessions, logs, threads) |
| `AMEESH_THREADS` | `threads_dir` | `<state>/fils` | readable threads |
| `AMEESH_PROJECT` | `project` | — | default project of a thread |
| `AMEESH_HUMANS` | `humans` | — | names that are humans, not agents (a list in JSON) |
| `AMEESH_CONNECT_TIMEOUT` | `connect_timeout` | 3 s | connection timeout |
| `AMEESH_STATEMENT_TIMEOUT_MS` | `statement_timeout_ms` | 30000 | statement timeout (server and client) |
| `AGENT_MAIL_STATE` | `v0_state` | `~/.local/state/agent-mail` | v0 file mailbox (fallback, import/export) |

## Runner

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `AMEESH_LEASE_TTL` | `lease_ttl` | 300 s | lease duration |
| `AMEESH_POLL` | `poll` | 5 s | fallback poll interval |
| `AMEESH_IDLE_NUDGE` | `idle_nudge` | 1200 s | nudge after inactivity |
| `AMEESH_EVENT_COALESCE` | `event_coalesce` | 120 s | at most one wake-up per window for events |
| `AMEESH_INTERRUPT_SENDERS` | `interrupt_senders` | — | senders whose `--urgent` messages interrupt a turn |
| `AMEESH_SESSION_MAX_TOKENS` | `session_max_tokens` | 150000 | rotate the session above this size |
| `AMEESH_SESSION_MAX_TURN_SECONDS` | `session_max_turn_seconds` | 900 | rotate after a turn longer than this |
| `AMEESH_SESSION_MIN_TURNS` | `session_min_turns` | 3 | minimum turns before a rotation |
| `AMEESH_FAST_FAILURE_S` | `fast_failure_s` | 60 s | a failed turn shorter than this counts as a fast failure (1.4.1) |
| `AMEESH_FAILURE_BACKOFF_MAX` | `failure_backoff_max` | 300 s | wait between failed turns doubles from 5 s up to this |
| `AMEESH_MAX_FAST_FAILURES` | `max_fast_failures` | 5 | consecutive fast failures after which the runner stops the agent (`stop_reason` `erreur`) |
| `AMEESH_WORKTREE_ROOTS` | `worktree_roots` | `~/development` | where to look for a moved working directory |
| `AMEESH_BUDGET_USD_PER_HOUR` | `budget_usd_per_hour` | 10 | hourly cap of pay-per-token usage, summed over all pay-per-token agents; it pauses only those agents, never a subscription agent (0 disables the guard) |
| `AMEESH_BUDGET_CHECK_INTERVAL` | `budget_check_interval` | 30 s | cadence of budget status updates |
| `AMEESH_SESSION_POLICY` | `session_policy` | `par-lot` | default session policy (`par-lot`, `taille`, `jamais`) |
| `AMEESH_BALANCE_INTERVAL` | `balance_interval` | 900 s | provider balance reading by the runner (0 = never; only with a key) |
| `AMEESH_DEEPSEEK_API_BASE` | — | the provider's API | base URL of the balance reading |
| `AMEESH_RESOURCE_INTERVAL` | `resource_interval` | 60 s | host resource reading by the runner (0 = none; see `ameesh hosts`) |
| `AMEESH_RELOCATE` | `relocate` | off | move an agent of a host under pressure to another admitted host, between two turns |
| `AMEESH_SHARED_SESSIONS` | `shared_sessions` | off | session storage shared between hosts: a move keeps the native session instead of rotating with a summary |
| `AMEESH_CONTAINER_RUNTIME` | `container_runtime` | `auto` | containers attached to a turn: `auto` (docker, then podman), a runtime name, or `none` |
| `AMEESH_CONTAINER_TIMEOUT` | `container_timeout` | 5 s | timeout of the read-only container listing |
| `AMEESH_FORGE` | `forge` | — | forge used by the persona visibility check: `gh`, `git`, `auto`, or empty |
| `AMEESH_FORGE_HOSTS` | `forge_hosts` | `github.com` | forge hosts `gh api --hostname` can query (comma-separated) |
| `AMEESH_VISIBILITY_TTL`, `AMEESH_VISIBILITY_TIMEOUT` | `visibility_ttl`, `visibility_timeout` | 300 s, 10 s | cache duration and timeout of the visibility check |
| `AMEESH_ALERT_LONG_TURN`, `_IDLE_MAIL`, `_DEAD_GRACE`, `_SESSION_TOKENS`, `_STALE_LOT`, `_ORPHAN_LOT`, `_DELEGATION_GRACE`, `_INTERVAL` | — | see `ameesh alerts` | alert thresholds (also used by `ameesh notify`) |
| `AMEESH_ALERT_PLAN_TAIL`, `_PLAN_USED`, `_PLAN_PACE_GAP`, `_IDLE_CAPACITY`, `_ORCHESTRATOR_HELD`, `_ORCHESTRATORS`, `_HOST_UNDERUSED`, `_HOST_UNDERUSED_LOAD`, `_HOST_UNDERUSED_TURNS` | 86400, 50, 25, 1800, 1800, —, 3600, 0.25, 1 | see `ameesh alerts` | underuse alert thresholds (L94); 0 disables |
| `AMEESH_PRICES` | — | built-in defaults | price table (JSON) |
| `AMEESH_<HARNESS>_BIN`, `AMEESH_BIN_DIR` | — | `PATH` | harness binaries (`CLAUDE`, `CODEX`, `DSH`) |

## Canon

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `AMEESH_CANON` | `canon` | — | root of the OKF bundle (a list of one canon) |
| `AMEESH_CANONS` | `canons` | — | several canons (v1.4.0): paths separated by `:` in the environment; in the file, a list of paths or of `{"path", "ref", "untrusted"}` objects. The **first** is the default canon |
| `AMEESH_CANON_REF` | `canon_ref` | manifest `ref`, else `origin/main` | canonical revision of the default canon; also its trusted branch for the passkey registry |
| `AMEESH_CANON_UNTRUSTED` | `canon_untrusted` | off | allow a non-git canon (tests, prototypes only) |
| `AMEESH_REQUIRE_RESPONSIBLE` | `require_responsible` | on when a canon is configured | an agent without a resolved human responsible is not claimable |
| `AMEESH_CANON_SYNC_INTERVAL` | `canon_sync_interval` | 300 s | periodic `canon sync` by the runner (0 = at start-up only) |

### Several canons

```json
{
  "canons": [
    {"path": "~/canon", "ref": "origin/main"},
    {"path": "~/development/acme/home"}
  ]
}
```

- `canon` / `canon_ref` (or `AMEESH_CANON`) are still accepted: a list of one
  element, and nothing changes for a host with a single canon. `canon` and
  `canons` together in the file are an error.
- A list from the environment replaces the one from the file.
  `AMEESH_CANON_REF` applies to the default canon only; `AMEESH_CANONS` gives
  no `ref` to the other canons (default: the manifest's, else `origin/main`).
  A canon's `ref` is also its trusted branch for passkeys: without one, bootstrap
  it once with `ameesh canon sync --canon <id> --bootstrap-ref main`.
- Every host that shares a database must have the **same default canon**.
  Changing the order (another default canon) is handled at the next pass,
  but do not reorder and upgrade in the same step: sync once first.
- Removing a canon from the list leaves its rows untouched: its agents stop
  being claimable but are not stopped.

See [The canon](../concepts/canon.md#several-canons-on-one-host).

## Pushed alerts: `notify`

`ameesh notify` (v1.4.0) reads the `notify` key of the **host's** configuration
file, never the canon:

```json
{
  "notify": {
    "default_human": "human:alice",
    "routes": {"human:alice": ["desktop", "ntfy", "slack"]},
    "default": ["desktop"],
    "channels": {
      "ntfy":  {"url": "https://ntfy.sh", "topic": "ameesh-alice-7f3a",
                "token_env": "AMEESH_NTFY_TOKEN"},
      "slack": {"webhook_env": "AMEESH_SLACK_WEBHOOK"}
    },
    "types": ["stopped_with_mail", "orphan_lot", "dead_runner",
              "idle_with_mail", "delegation_expired"],
    "rate_per_minute": 10,
    "max_attempts": 5,
    "timeout": 10
  }
}
```

| Key | Meaning |
|---|---|
| `default_human` | recipient of an alert when no responsible human is found |
| `routes` | `{"human:<id>": [channels]}` |
| `default` | channels of a human without a route |
| `channels` | channel settings; a channel can have any name if it carries its `type` (`"ntfy-bruno": {"type": "ntfy", …}`); `desktop` and `slack` need no declaration |
| `types` | alert types pushed (default: the five above) |
| `interval` | seconds between two passes (else `AMEESH_NOTIFY_INTERVAL`, else 30) |
| `rate_per_minute` | messages per human over 60 s (0 = no limit); beyond, a summary |
| `max_attempts` | retries of a failed channel before giving up (default 5) |
| `timeout` | timeout of one delivery, seconds |

| Channel type | Delivery | Options |
|---|---|---|
| `desktop` | `notify-send` (`AMEESH_NOTIFY_SEND_BIN`, else `AMEESH_BIN_DIR/notify-send`, else the `PATH`) | `timeout` |
| `ntfy` | `POST <url>/<topic>` | `url` (HTTPS, or HTTP on loopback), `topic`, `token_env` or `token_file` (optional) |
| `slack` | incoming webhook | `webhook_env` (default `AMEESH_SLACK_WEBHOOK`) or `webhook_file` |

**Secrets are never in the configuration.** A `token`, `webhook`, `password`,
`secret` key, or the `url` of a Slack channel, is refused (exit 2). The
configuration **names** the environment variable (for example in a systemd
`EnvironmentFile`) or the file (`token_file`, `webhook_file`, mode `0600`,
owned by the user) that holds the secret. A webhook URL or token never appears
in a log, the JSON output or the state file. See
[Operate agents](../guides/operate-agents.md#pushed-alerts-ameesh-notify).

## Approvals (ameesh side)

| Variable | Config key | Meaning |
|---|---|---|
| `AMEESH_APPROVE_URL` | `approve_url` | URL of the ameesh-approve API (HTTPS, or HTTP on loopback only) |
| `AMEESH_APPROVE_TOKEN_FILE` | `approve_token_file` | copy of the service token, mode `0600`, owned by the agents' user |
| `AMEESH_APPROVE_RP_ID` | — | WebAuthn RP ID that receipts must carry |
| `AMEESH_APPROVE_ORIGINS` | — | allowed origins (`https://<host>`) |
| `AMEESH_APPROVE_TLS_NAME` | `approve_tls_name` | with `AMEESH_APPROVE_URL=https://127.0.0.1:PORT` and local TLS: the host name the certificate is verified for |

The RP ID and origins must be **identical** on the service and on every
verifier.

## ameesh-approve (service side)

`ameesh-approve` takes a JSON file (`--config`, or `AMEESH_APPROVE_CONFIG`) and
the variables `AMEESH_APPROVE_RP_ID`, `AMEESH_APPROVE_ORIGINS`,
`AMEESH_APPROVE_PUBLIC_URL`, `AMEESH_APPROVE_BIND`, `AMEESH_APPROVE_PORT`,
`AMEESH_APPROVE_STATE`, `AMEESH_APPROVE_PROPOSALS`, `AMEESH_APPROVE_LEVEL`,
`AMEESH_APPROVE_PROFILE`, `AMEESH_APPROVE_RESERVED_ZONES`,
`AMEESH_APPROVE_RESERVED_HOSTS`, `AMEESH_APPROVE_TLS_CERT`,
`AMEESH_APPROVE_TLS_KEY` and `AMEESH_APPROVE_TOKEN_FILE`; the main ones can be
overridden by `serve` options. Opening the API under the public host
(`"api_via_public": true`) exists only in the JSON file. See
[Host ameesh-approve for a team](../guides/host-ameesh-approve.md).
It reads the database with `AMEESH_DSN` and `AMEESH_SCHEMA`.
