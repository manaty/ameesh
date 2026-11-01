# Configuration

ameesh reads its configuration from, in order of precedence:

1. environment variables `AMEESH_*` (the older `AGENT_MESH_*` names are still
   accepted as aliases);
2. the JSON file `~/.config/ameesh/config.json` (or the path in
   `AMEESH_CONFIG`), whose keys are the setting names below;
3. built-in defaults.

A path named by `AMEESH_CONFIG` (or the alias `AGENT_MESH_CONFIG`) must exist:
an explicitly named missing file stops ameesh with `config file not found`
before any connection, instead of silently reading another configuration
(#1127). Without either variable, `~/.config/ameesh/config.json` is read, and
the historical `~/.config/agent-mesh/config.json` is the fallback.

No secret is stored in the repository. The database password comes from
`PGPASSWORD` or `~/.pgpass` (mode `0600`), or from a full DSN.

## Modules and workspace policy in the canon

Module activation belongs in the trusted canon's `federation.yaml`, rather
than the host JSON configuration:

```yaml
extensions:
  ameesh:
    modules: [it]
    teams:
      ops:
        modules: {it: {require_qa: [promoted]}}
    projects:
      service:
        modules: {it: {require_qa: [merged, promoted]}}
      support:
        modules: [support-work-delivery]
```

Lists select default options; mappings supply per-module options. For a
module selected at several scopes, project options override team options,
which override federation options. The Pipeline name in this example requires
a valid matching card in `pipelines/`. Apply declarations with `ameesh canon
check` and the [trusted canon sync procedure](../concepts/canon.md#only-the-approved-source).
`ameesh modules --active --json` inspects the applied policies; `doctor`
compares them with the canon and installed descriptors.

Python extensions register the `ameesh.modules` package entry-point group.
The canon selects a registered name and its options; it supplies no Python
import path. Installation alone does not activate a project policy. See
[Write a module](../guides/write-a-module.md) for the package contract.
`dev` is always active in a standard installation and cannot be selected or
disabled per project. A project without selected modules is distinct from a
bare registry with no built-ins or entry points: without `dev`, workspace
housekeeping and worktree operations are no-ops.

Host working directories and housekeeping belong to `policy.workspaces` on
the Host card. For example:

```yaml
policy:
  workspaces:
    root: /srv/work
    roots: {ops: /srv/ops}
    dirs: {worker1: /srv/ops/worker1}
```

The first match wins: `workspaces.dirs[<agent>]`, then
`workspaces.roots[<team>]`, then `workspaces.root/<team>`. The older `work_dirs`, `work_roots` and `work_root` keys remain
accepted aliases. The `dev` module supplies the `workspaces` capability and
its host audit; see [Write a canon](../guides/write-a-canon.md#3-hosts-and-their-policies).

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
| `AMEESH_MAIL_BATCH` | `mail_batch` | 90 s | batching window: an idle agent is woken this long after its oldest waiting message, in one turn for all of them; `--urgent` and human mail wake at once; per agent (local state of its host): `ameesh set <agent> mail_batch=…`; 0 = wake at once |
| `AMEESH_LOT_WAKE`, `_MARGIN`, `_MAX` | `lot_wake`, `lot_wake_margin`, `lot_wake_max` | on, 60 s, 3 | the runner wakes an idle agent that holds an actionable lot (`intake`/`build`) after `mail_batch` + margin, with the instruction to resume it; at most N fruitless wake-ups per lot (doubling back-off in the database), then the auditor takes over (L165b); per agent `ameesh set <agent> lot_wake=off`, per host `AMEESH_LOT_WAKE=0` |
| `AMEESH_INTERRUPT_SENDERS` | `interrupt_senders` | — | senders whose `--urgent` messages interrupt a turn |
| `AMEESH_SESSION_MAX_TOKENS` | `session_max_tokens` | 150000 | rotate the session when its cumulative tokens reach this: `S_hist` = `input_tokens + output_tokens` of its turns (not the turn's re-reads); the cache convention is per harness (#4448), so the same count does not mean the same thing across harnesses; the applied canon's `session.max_tokens` / `session.max_tokens_by_harness` threshold takes precedence, this common value is the fallback |
| `AMEESH_SESSION_MAX_TURN_SECONDS` | `session_max_turn_seconds` | 3600 | rotate after a turn longer than this; the threshold is floored at twice the effective turn cap (`turn_max_seconds`, the agent's own setting included) — a turn allowed to last 1,800 s does not, by itself, make the session be forgotten; without a turn cap, the configured threshold applies alone |
| `AMEESH_CONTEXT_MAX_TOKENS` | `context_max_tokens` | 15000000 | close the turn at the next safe point and rotate the session when the context of the last main-thread model call (fresh input + cache read + cache written; Codex's input already includes its cache) reaches this; the turn's total re-reads are not the measure (#2336), but without a per-call reading (turn closed before its result, restarted worker) a non-Codex session falls back to the last recorded turn's re-read (input + cache read), and Codex does not (#2932); per agent: `ameesh set <agent> context_max_tokens=…`; 0 = off |
| `AMEESH_TURN_REREAD_BUDGET`, `AMEESH_SESSION_REREAD_BUDGET` | `turn_reread_budget`, `session_reread_budget` | 0 (off) | opt-in re-read budgets on `R_turn` and `R_session`: close the turn at the next safe point and continue in the same session, with no rotation or compaction; the session budget acts at each slice crossed; no measure, no decision; not a window size (#4970) |
| `AMEESH_WINDOW_MAINTENANCE_SHARE`, `AMEESH_WINDOW_INPUT_RESERVE`, `AMEESH_WINDOW_OUTPUT_RESERVE` | `window_maintenance_share`, `window_input_reserve`, `window_output_reserve` | 0 (off), undeclared, undeclared | opt-in window maintenance of a resident Claude session: when the projected next call nears this share of the effective model's window, ask a native compaction between two turns, then renew the session if the projection stays near; replaces the legacy 50 % compaction when on; an unknown window, measure or undeclared reserve means no decision, named in `turn.context`; the context cap still comes first (#4971) |
| `AMEESH_TURN_MAX_SECONDS` | `turn_max_seconds` | 1800 | close a turn that runs longer at the next safe point (end of the running tool call); work resumes next turn, same session; per agent: `ameesh set <agent> turn_max_seconds=…`; 0 = no limit |
| `AMEESH_TURN_MAIL_MAX` | `turn_mail_max` | 5 | messages the mail hook delivers during one runner-led turn; the rest wait for the next turn and the agent is asked to wrap up; per agent: `ameesh set <agent> turn_mail_max=…`; 0 = no bound |
| `AMEESH_TURN_GRACE_SECONDS` | `turn_grace_seconds` | 1200 s | grace after a turn's end before a background resource held only by it is closed (`0` = immediate cleanup) |
| `AMEESH_DRAIN_SECONDS` | `drain_seconds` | 1800 s | drainage left to the running turn and its background work when the **runner itself** stops (systemd `SIGTERM`, `SIGINT`): no new turn, then stop; `0` = immediate stop (the running turn is stopped and its instruction put back); a second signal stops at once, and the systemd unit keeps `TimeoutStopSec` above. A host withdrawal is separate: the `host_gate`/`host_gate_drain` settings and `ameesh host drain --for` |
| `AMEESH_HOUSEKEEPING_INTERVAL` | `housekeeping_interval` | 600 s | cadence of the runner's housekeeping pass (L73) |
| `AMEESH_DB_ADMISSION_PRIORITY` | — | 1 | priority admission on the shared psycopg connection (#4602): a worker woken by mail and the `agent_mail` listener go before idle polling passes; `0` restores the plain `RLock` |
| `AMEESH_DB_AGING_S` | — | 2 s | a NORMAL admission waiting this long goes before newer CONTROL ones (starvation guard) |
| `AMEESH_DB_WAIT_WARN_S` | — | 1 s | a shared-connection admission wait above this is logged once per agent per 30 s (`deferred: shared database admission`) |
| `AMEESH_THREAD_RETENTION_DAYS` | `thread_retention_days` | 365 | readable-thread retention: entries older than this are removed by `ameesh fil expire` and the runner's housekeeping (L361, decision 0033 §9); 0 = never |
| `AMEESH_SANDBOX_TOKEN_RETENTION_DAYS` | `sandbox_token_retention_days` | 30 | retention of a closed sandbox turn's relay-token digest: the runner's daily housekeeping deletes it past this many days (the turn, its result and its costs stay; #5217); 0 = never |
| `AMEESH_HEAVY_MAX_S` | `heavy_max_seconds` | 3600 s | duration cap of a heavy command (`ameesh heavy`, or a suite a turn launches), message then SIGTERM/SIGKILL and exit 124 (#453); 0 = none |
| `AMEESH_SESSION_MIN_TURNS` | `session_min_turns` | 3 | minimum turns before a rotation |
| `AMEESH_FAST_FAILURE_S` | `fast_failure_s` | 60 s | a failed turn shorter than this counts as a fast failure (1.4.1) |
| `AMEESH_FAILURE_BACKOFF_MAX` | `failure_backoff_max` | 300 s | wait between failed turns doubles from 5 s up to this |
| `AMEESH_MAX_FAST_FAILURES` | `max_fast_failures` | 5 | consecutive fast failures after which the runner stops the agent (`stop_reason` `erreur`); host errors (harness or interpreter not found, database unreachable, missing working directory) never count |
| `AMEESH_<HARNESS>_BIN` | `harness_bins` | — | harness binary (`{"claude": "/path"}` in the file); then `AMEESH_BIN_DIR`, `PATH`, the systemd user manager `PATH`, known locations (mise, `~/.local/bin`, npx cache) |
| `AMEESH_HARNESS_SEARCH` | — | known locations | directories searched after `PATH` (`:`-separated, globs allowed); empty disables every fallback |
| `AMEESH_HOST_RETRY_MAX` | `host_retry_max` | 300 s | "host not ready": the wait between two attempts doubles from 15 s up to this; the agent resumes by itself |
| `AMEESH_POWER_STOP_GRACE` | `power_stop_grace` | 120 s | on critical battery, time left to running turns before a clean stop |
| `AMEESH_WORKTREE_ROOTS` | `worktree_roots` | `~/development` | where to look for a moved working directory |
| `AMEESH_BUDGET_USD_PER_HOUR` | `budget_usd_per_hour` | 10 | hourly cap of pay-per-token usage, summed over all pay-per-token agents; it pauses only those agents, never a subscription agent; `0` disables the guard **and with it the daily ceiling below** (the daily cap is only checked while the hourly cap is > 0) |
| `AMEESH_BUDGET_USD_PER_DAY` | `budget_usd_per_day` | 0 | sliding 24 h cap of pay-per-token usage; a cap set in the database (`ameesh budget set --per-day`) wins over it (L70); 0 = none; only enforced while the effective hourly cap is > 0 |
| `AMEESH_CRITICAL_AGENTS`, `AMEESH_CRITICAL_FALLBACK_HARNESSES`, `AMEESH_NONCRITICAL_HOURLY_SHARE` | `critical_agents`, `critical_fallback_harnesses`, `noncritical_hourly_share` | —, —, 0.75 | named critical agents (in addition to the canon's orchestrator, design and auditor roles) are never paused on budget while an account has margin; a critical agent at threshold falls back to these harnesses (Codex, then DeepSeek) and comes back; a non-critical agent stops above this share of the hourly cap (L242b) |
| `AMEESH_BUDGET_CHECK_INTERVAL` | `budget_check_interval` | 30 s | cadence of budget status updates |
| `AMEESH_SESSION_POLICY` | `session_policy` | `par-lot` | default session policy (`par-lot`, `taille`, `jamais`) |
| `AMEESH_BALANCE_INTERVAL` | `balance_interval` | 900 s | provider balance reading by the runner (0 = never; only with a key) |
| `AMEESH_DEEPSEEK_API_BASE` | — | the provider's API | base URL of the balance reading |
| `AMEESH_IDLE_AFTER_S`, `AMEESH_BASELINE_HOSTS` | `idle_after_s`, `baseline_hosts` | 1800 s, 2 | read-only host inactivity window and baseline size; fresh uninterrupted samples and no agent work are required; critical elected leaders and reserves remain baseline when cards do not declare enough baseline hosts |
| `AMEESH_LIFECYCLE_HOOK`, `AMEESH_LIFECYCLE_HOOK_TIMEOUT` | `lifecycle_hook`, `lifecycle_hook_timeout` | —, 300 s | external host lifecycle adapter: ameesh runs `<command> busy\|draining\|idle-drained <host>` and owns no cluster or cloud credential; `lifecycle_hooks` maps one command per host and wins over the Host card's `policy.lifecycle_hook`, which wins over the global key. A failed or timed-out hook keeps the previous state and the claims closed |
| `AMEESH_HOST_RELEASE_INTERVAL` | `host_release_interval` | 300 s | spacing of the idle projection that may drain a burst host; transitions and the automatic return are checked every pass |
| `AMEESH_HOST_RELEASE_DRAIN_BOUND` | `host_release_drain_bound` | 2100 s | a withdrawal that has not reached `idle-drained` after this delay raises `host_release_overdue`; it is never forced |
| `AMEESH_HOST_RELEASE_RETRY` | `host_release_retry` | 300 s | retry delay after a failed adapter transition |
| `AMEESH_HOST_RELEASE_SETTLE` | `host_release_settle` | 60 s | quiet time after the last turn before `idle-drained` can be proven |
| `AMEESH_HOST_RELEASE_BOOT_GRACE` | `host_release_boot_grace` | 900 s | a released host whose `busy` hook answered is booting with stale readings on purpose; loss detection leaves it alone this long (or until the first post-ack reading reopens claims), then gives up |
| `AMEESH_RESOURCE_INTERVAL` | `resource_interval` | 60 s | host resource reading by the runner (0 = none; see `ameesh hosts`) |
| `AMEESH_BRANCH_SWEEP_INTERVAL` | `branch_sweep_interval` | 300 s | runner check of lot branches: a lot whose branch is merged into its target is closed (L118; 0 = none) |
| `AMEESH_RELOCATE` | `relocate` | off | move an agent of a host under pressure to another admitted host, between two turns |
| `AMEESH_SHARED_SESSIONS` | `shared_sessions` | off | session storage shared between hosts: a move keeps the native session instead of rotating with a summary |
| `AMEESH_CONTAINER_RUNTIME` | `container_runtime` | `auto` | containers attached to a turn: `auto` (docker, then podman), a runtime name, or `none` |
| `AMEESH_CONTAINER_TIMEOUT` | `container_timeout` | 5 s | timeout of the read-only container listing |
| `AMEESH_FORGE` | `forge` | — | forge used by the persona visibility check: `gh`, `git`, `auto`, or empty |
| `AMEESH_FORGE_HOSTS` | `forge_hosts` | `github.com` | forge hosts `gh api --hostname` can query (comma-separated) |
| `AMEESH_VISIBILITY_TTL`, `AMEESH_VISIBILITY_TIMEOUT` | `visibility_ttl`, `visibility_timeout` | 300 s, 10 s | cache duration and timeout of the visibility check |
| `AMEESH_ALERT_LONG_TURN`, `_IDLE_MAIL`, `_DEAD_GRACE`, `_SESSION_TOKENS`, `_STALE_LOT`, `_ORPHAN_LOT`, `_DELEGATION_GRACE`, `_MAIL_UNDELIVERABLE`, `_TURN_CHURN`, `_SHORT_TURN`, `_INTERVAL` | — | see `ameesh alerts` | alert thresholds (also used by `ameesh notify`) |
| `AMEESH_ALERT_PLAN_TAIL`, `_PLAN_USED`, `_PLAN_PACE_GAP`, `_IDLE_CAPACITY`, `_ORCHESTRATOR_HELD`, `_ORCHESTRATORS`, `_HOST_UNDERUSED`, `_HOST_UNDERUSED_LOAD`, `_HOST_UNDERUSED_TURNS` | 86400, 50, 25, 1800, 1800, —, 3600, 0.25, 1 | see `ameesh alerts` | underuse alert thresholds (L94); 0 disables |
| `AMEESH_ALERT_BACKLOG_EMPTY` | 1800 | see `ameesh alerts` | `backlog_empty`: idle agents and an empty improvement backlog (L119); 0 disables |
| `AMEESH_TAKE_IDLE`, `AMEESH_TAKE_MAX_PER_HOUR` | 1800, 2 | see `ameesh notify`, `ameesh run` | auto-take from the improvement backlog by idle agents (L119, decision 0037); `AMEESH_TAKE_IDLE=0` disables; subscription agents first, pay-per-token agents take what they leave (never during `balance_low`) |
| `AMEESH_TAKE_LOT_IDLE`, `AMEESH_TAKE_LOT_MAX_PER_HOUR`, `AMEESH_TAKE_INTERVAL` | 900 s, 6, 60 | see `ameesh notify`, `ameesh run` | an idle wakeable agent with no lot takes the highest-priority unassigned lot of its team after S seconds, at most N per rolling hour for the whole mesh (L165); the runner triggers the take every `AMEESH_TAKE_INTERVAL` seconds (`0` turns the runner's take off) and `ameesh notify` keeps its own where it runs (#804); `0` turns a take off |
| `AMEESH_ALERT_BALANCE_HOURS`, `_BALANCE_MIN`, `_BALANCE_WINDOW` | 48, 20, 21600 | see `ameesh alerts`, `ameesh run` | `balance_low`: runway at the real spend rate (from balance readings), USD floor, averaging window (L94); the runner holding the take reads it with the same thresholds (#804) |
| `AMEESH_MESH_BACKUP_WINDOW`, `AMEESH_MESH_BACKUP_MIN_SHARE`, `AMEESH_MESH_BACKUP_MIN_GROUP_SHARE` | `mesh_backup_window`, `mesh_backup_min_share`, `mesh_backup_min_group_share` | `03:30-04:30`, 0.9, 0.05 | #4609: daily UTC window of the mesh database backup (`ameesh-sauvegarde.timer` in `deploy/serveur`, 03:30 plus up to 15 min of jitter), written `HH:MM-HH:MM`; the minimum share of all tables (never-scanned tables count as not read) whose `last_seq_scan` must be at or after the transaction instant; and the minimum share of tables that must share that instant (a small ordinary transaction is not a dump). `mesh_backup_unseen` requires PostgreSQL 16 or newer (`pg_stat_user_tables.last_seq_scan` exists since PostgreSQL 16) and proves the read only, not the dump file nor its S3 copy; a zero share disables the alert |
| `AMEESH_PRICES` | — | built-in defaults | price table (JSON) |
| `AMEESH_<HARNESS>_BIN`, `AMEESH_BIN_DIR` | — | `PATH` | harness binaries (`CLAUDE`, `CODEX`, `DSH`) |
| `AMEESH_MAX_HEAVY_SUITES` | `max_heavy_suites` | 0 (auto) | heavy suites allowed at once on this host (L131); 0 = derived from the host's capacity |
| `AMEESH_OFFLOAD_HOST` | — | — | SSH destination of the lending VM for `ameesh offload` (L159b); without it, nothing is offloaded |
| `AMEESH_OFFLOAD_SSH` | — | `ssh` | ssh command used by `ameesh offload` (tests, extra options) |

## Host lifecycle and handoff

| Variable | Config key | Default | Meaning |
|---|---|---|---|
| `AMEESH_HOST_GATE`, `AMEESH_HOST_GATE_PATH` | `host_gate`, `host_gate_path` | —, — | host gate (`file` or `none`; L159f): the runner reads the host's gate state from this file |
| `AMEESH_HOST_GATE_ACK`, `AMEESH_HOST_GATE_DRAIN` | `host_gate_ack`, `host_gate_drain` | —, 0 | acknowledgement file written by the runner, and drain duration applied when the gate says `draining` |
| `AMEESH_HOST_GATE_FALLBACK` | `host_gate_fallback` | — | state assumed when the gate is unreadable: `available`, `draining` or `stopped` |
| `AMEESH_HOST_MEDIATED` | `host_mediated` | off | declares this host a mediated host (L107–L114): provider and database credentials stay on the mesh server. It does **not** select the transport — `backend = "mediated"` (`AMEESH_BACKEND`) does; the flag also allows the `socket` host gate |
| `AMEESH_HANDOFF_MAX_PER_HOUR`, `AMEESH_HANDOFF_IDLE`, `AMEESH_HANDOFF_ENABLE_WORKERS` | — | 4, 300 s, 4 | budget handoff (L233b, L165): at most N lots per rolling hour leave a non-critical Claude agent paused on budget — its non-started lots returned to the team queue (automatic take), its started lots handed to a free DeepSeek or Codex agent of the same project, which takes one after S seconds idle; the workers bound concurrent held host handoffs (L232a) |
| `AMEESH_GUARD_CONFIG` | — | `/etc/ameesh/guard.json` | capacity-guard configuration (`ameesh guard`, L159e) |
| `AMEESH_GUEST_CGROUP` | — | detected | guest cgroups counted in the lendable capacity of the host measure (L159d) |

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
| `channels` | channel settings; a channel can have any name if it carries its `type` (`"ntfy-bruno": {"type": "ntfy", …}`); `desktop` and `slack` need no declaration; `nexlink` always does |
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
| `nexlink` | signed `POST <url>/mesh/alerts`, end-to-end encrypted for the owner's Nexlink app (#3536) | `url` (HTTPS, or HTTP on loopback), `token_env` or `token_file` (**required**: named HMAC secret), `fallback` (declared `ntfy`/`slack` channel) |

**`nexlink` channel (#3536, owner decision #138440).** The alert is encrypted
to the account's X25519 key-wrap key (`GET /mesh/alerts/keys`, generation 1:
AES-256-GCM under a random CEK, the CEK wrapped per active key with Nexlink's
`wrapToX25519` format, subject `meshAlert = 5`), so the hub only stores
`{v, alert_id, severity, occurred_at, ciphertext, nonce, wraps}`. `alert_id` is
a deterministic UUIDv5 of the alert identity (the `since` of the episode, or
the raised instant when it has none so two successive `account_overage` stay
distinct; a rate summary carries the identity of its state bucket and its
members are frozen at the first send, later ones forming the next episode).
Nexlink stores it as a `uuid` and refuses unknown envelope fields;
`X-Mesh-Idempotency-Key` is that `alert_id`. A resolution is not pushed to the hub in v1 (its schema has no
state; the app row stays raised), so `ameesh notify` skips it for this channel.
With no active key the alert is sent **content-free** (`content_free: true`)
and the app opens the console. Requests are signed with
`X-Mesh-Key-Id` (`AMEESH_NEXLINK_KEY_ID`, default `ameesh-v1`),
`X-Mesh-Timestamp` and `X-Mesh-Signature: v1=HMAC-SHA-256(...)` over
`key_id "\n" timestamp "\n" body`. 401/400/404/422 are
permanent, 429/5xx are retried, 409 `key_unknown` refreshes the keys once.
On the second consecutive failure (or at once on a permanent error) the text
— including a rate summary — is sent once per `alert_id` to the `fallback`
channel, prefixed `repli nexlink —`; the primary keeps being retried and a
fallback success is not a Nexlink delivery. `ameesh notify --test` prints the read receipt
(`GET /mesh/alerts/{id}`). The `cryptography` extra is required
(`pip install 'ameesh[crypto]'`; the host installers pose
`ameesh[psycopg,crypto]` and `ameesh doctor` refuses a routed `nexlink`
channel without it, naming it before the first send). ntfy stays the transition channel and is
removed only after 7 green days in production **and** a real urgent alert read
with the app closed.

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

## Read API, mediated executor and relay

| Variable | Config key | Meaning |
|---|---|---|
| `AMEESH_SERVE_TOKEN_FILE` | — | token file of the read API (`ameesh serve`, L84); default `~/.config/ameesh/serve-token` |
| `AMEESH_EXEC_URL`, `AMEESH_EXEC_TOKEN_FILE` | `exec_url`, `exec_token_file` | mesh server URL and 0600 file holding the **executor's access token** of a mediated host (`backend = "mediated"`, L107–L114); the token itself comes from the executor's identity, not from a one-shot enrolment (L110) |
| `AMEESH_EXEC_HOME` | — | executor home holding its key and stable per-agent work path (default `/var/lib/ameesh-exec`) |
| `AMEESH_EXEC_SIGNER` | — | signing helper of the executor image |
| `AMEESH_RELAY_URL`, `AMEESH_RELAY_BIND` | — | model relay of the mediated executor: upstream and bind address |
| `AMEESH_RELAY_CONCURRENCY`, `AMEESH_RELAY_RATE`, `AMEESH_RELAY_MAX_BODY`, `AMEESH_RELAY_MAX_STREAM`, `AMEESH_RELAY_MAX_TOKENS`, `AMEESH_RELAY_UPSTREAM_TIMEOUT` | — | relay bounds: concurrency, rate, body size, stream size, tokens, upstream timeout |
| `AMEESH_RELAY_VERIFIER` | — | relay **lease-token** verifier, `module:object`: the object (or a no-argument factory) must expose `verify_token(token) -> Identity`; the relay composes the sandbox-turn fencing itself (`sandbox_relay.compose`, #5140/#5217), so a sandbox bearer (`amsb1.`) is verified there and a lease bearer of a sandbox agent is refused; without it the relay refuses to start |
| `AMEESH_RELAY_DEEPSEEK_KEY_FILE`, `AMEESH_RELAY_DEEPSEEK_UPSTREAM` | — | DeepSeek key file and upstream URL of the relayed provider (`DEEPSEEK_API_KEY` is the in-process alternative) |

Operator procedure and exact relay values: `docs/EXECUTEUR-MEDIEE.md`.

## Personas and session backups

| Variable | Meaning |
|---|---|
| `AMEESH_BACKUP_S3_ENDPOINT`, `AMEESH_BACKUP_S3_REGION`, `AMEESH_BACKUP_S3_ACCESS_KEY`, `AMEESH_BACKUP_S3_SECRET_KEY` | S3 storage of session backups (L53): required when the host's `session_backup.target` is `s3://bucket/prefix`. A local backup needs an **absolute** path in `session_backup.target`; with no target there is no backup at all |

## Shared dependencies

| Variable | Config key | Meaning |
|---|---|---|
| `AMEESH_DEPS` | — | `ameesh deps` behaviour (L231): default shares one `node_modules` per lockfile; `allow-review` allows installs in a review worktree; `off` passes npm through |
| `AMEESH_FILL_FROM` | — | package set a cache fill starts from |

Host resource views include a `footprint` projection (also in the JSON `measure`).
Host cards may declare `capacity: baseline` or `capacity: burst`,
`tarif_eur_h`, and `policy.lifecycle_hook` (an external adapter command).
Missing prices and reservations remain unknown. The projection requires fresh,
uninterrupted samples with readable cgroup limits; older publishers without
the readability marker cannot prove inactivity. Declared runs are zero with
source “aucune interface de déclaration” until #4147 publishes a real run
source; the automatic release treats that as unknown and refuses to drain
(`host_release_no_runs`). With a hook, a burst host proven idle is drained
(L232e, claims closed), then `idle-drained` is emitted only after no turn and
no lease remain and a fresh post-withdrawal reading exists; a return emits
`busy` before any claim reopens. See [EXPLOITATION.md](https://github.com/manaty/ameesh/blob/main/docs/EXPLOITATION.md)
(“Automatic release of a burst host”).
