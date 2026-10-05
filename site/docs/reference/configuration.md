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
| `AMEESH_WORKTREE_ROOTS` | `worktree_roots` | `~/development` | where to look for a moved working directory |
| `AMEESH_BUDGET_USD_PER_HOUR` | `budget_usd_per_hour` | 10 | hourly cap of pay-per-token usage (0 disables the guard) |
| `AMEESH_BUDGET_CHECK_INTERVAL` | `budget_check_interval` | 30 s | cadence of budget status updates |
| `AMEESH_SESSION_POLICY` | `session_policy` | `par-lot` | default session policy (`par-lot`, `taille`, `jamais`) |
| `AMEESH_BALANCE_INTERVAL` | `balance_interval` | 900 s | provider balance reading by the runner (0 = never; only with a key) |
| `AMEESH_DEEPSEEK_API_BASE` | — | the provider's API | base URL of the balance reading |
| `AMEESH_ALERT_LONG_TURN`, `_IDLE_MAIL`, `_DEAD_GRACE`, `_SESSION_TOKENS`, `_STALE_LOT`, `_INTERVAL` | — | see `ameesh alerts` | alert thresholds |
| `AMEESH_PRICES` | — | built-in defaults | price table (JSON) |
| `AMEESH_<HARNESS>_BIN`, `AMEESH_BIN_DIR` | — | `PATH` | harness binaries (`CLAUDE`, `CODEX`, `DSH`) |

## Canon

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `AMEESH_CANON` | `canon` | — | root of the OKF bundle |
| `AMEESH_CANON_REF` | `canon_ref` | manifest `ref`, else `origin/main` | canonical revision; also the trusted branch of the passkey registry |
| `AMEESH_CANON_UNTRUSTED` | `canon_untrusted` | off | allow a non-git canon (tests, prototypes only) |
| `AMEESH_REQUIRE_RESPONSIBLE` | `require_responsible` | on when a canon is configured | an agent without a resolved human responsible is not claimable |
| `AMEESH_CANON_SYNC_INTERVAL` | `canon_sync_interval` | 300 s | periodic `canon sync` by the runner (0 = at start-up only) |

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
