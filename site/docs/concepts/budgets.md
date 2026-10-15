# Budgets and cost

Agents spend money in two ways: **pay-per-token** usage billed by API key, and
**subscriptions** (plans) with usage windows. ameesh accounts for both, per
turn, and guards each turn before it starts.

## Per-turn accounting

Each turn writes one row in the cost ledger (`turn_costs`), which carries both
the cost of the turn and the raw cumulative value the next turn will diff
against:

| Harness | How the cost of a turn is computed |
|---|---|
| Claude Code | difference of the session's cumulative `total_cost_usd` |
| Codex | difference of the cumulative usage reported by `turn.completed` |
| DeepSeek Harness | usage per step × price table |

The price table is a JSON file (USD per million tokens: input, cached input,
output), read from `AMEESH_PRICES` or the default path of the host; models it
does not list fall back to built-in defaults. If the billed model is unknown,
the most expensive known price of the family is used, never a cheaper default.

The accounting work of a turn is marked in the database **before** the turn
starts. A marker that is still present, unreadable or inconsistent at restart
puts the agent in an **accounting pause** (fail closed); if the marker cannot
be written, the turn does not start.

## Subscription gauges, read at the source

ameesh only **reads** the harnesses' own logs, never writes to them:

- Claude Code: `rate_limit_event` in the agent's stream-json log (five-hour and
  seven-day windows);
- Codex: `rate_limits` primary and secondary in the local session logs.

## Provider balance

For a pay-per-token provider (DeepSeek in this version), `ameesh cost balance`
reads the account balance (read-only, free) and derives the **real spend** per
hour and per day from the drops between readings. The runner takes a reading
every `AMEESH_BALANCE_INTERVAL` seconds (default 900) when a key is present.
The key goes only into the request header. See
[Operate agents](../guides/operate-agents.md#cost-per-turn-plan-gauges-provider-balance).

## The budget guard

Before each turn, the runner checks:

- the **hourly cap** of pay-per-token usage, summed over all pay-per-token
  agents for the last sixty minutes, `AMEESH_BUDGET_USD_PER_HOUR` (default
  **10 USD/h**; `0` disables the guard). Subscription usage does not count in
  this sum;
- the **daily cap** (L70), a sliding 24 h window: `AMEESH_BUDGET_USD_PER_DAY`
  (default `0`, off) or a cap set in the database with `ameesh budget set
  --per-day`, which wins over the configuration;
- the **pace guard** of subscriptions: a plan is going too fast when its usage
  reaches `min(90 %, elapsed share of the window + 10 points)`. The 10-point
  margin covers orchestrators and humans, who share the same plan.

A cap set in the database with `ameesh budget set --per-hour/--per-day` is
reloaded live; `--agent` scopes it to one agent. Critical agents — the canon's
orchestrator, design and auditor roles, plus `critical_agents` — are never
paused while an account has margin; above `noncritical_hourly_share` of the
cap, a non-critical agent stops. A critical agent at threshold switches
account on its own, then falls back to Codex, then DeepSeek, and comes back
(L242b). When a credential is exhausted or revoked, the sentinel declares
`auth_blocked` and the runner fails over to a healthy account — primaries
last and capped, never overage (L226b).

The decision is fresh at every turn. A paused agent shows the reason in
`ameesh list`, and the thread keeps a trace.

## Daily envelope per billing account

A provider **billing account** (`openai-manaty`, `deepseek-manaty`) can carry
one USD envelope per **calendar day**, shared by every agent, key and host
that spends from it. Each host profile names the account it bills:

```json
"accounts": {"codex": [{"name": "openai-api", "type": "api_key_env",
                        "key_env": "OPENAI_KEY", "billing_account": "openai-manaty",
                        "reserve_usd": 2}],
             "deepseek": [{"name": "ds", "type": "api_key_env", "key_env": "DS_KEY",
                           "billing_account": "deepseek-manaty"}]}
```

Several keys and profile names can name the same account. Rotating a key,
recreating an agent or moving a host keeps the account's history and
envelope. A human sets the envelope once for the whole mesh:

```
ameesh budget set --account openai-manaty --per-day 100      # Europe/Paris by default
ameesh budget set --account deepseek-manaty --per-day 100 --tz Europe/Paris
```

- **Period**: the day runs from 00:00 to 24:00 in the account's zone. A DST
  day lasts 23 h or 25 h with the same cap. `ameesh budget` shows the next
  reset. This is distinct from the sliding 24 h cap of the mesh.
- **Admission**: before each turn, the runner reserves the profile's
  `reserve_usd` (2 $ by default, the bound of one turn). The reservation is
  taken in one database transaction under a lock on the account: the day's
  settled spend, plus the open reservations, plus the reservations kept for
  the day, plus the new one, must fit the cap, whatever the number of runners
  and hosts. The account decides whether a turn is paid, not the harness: an
  API-key Codex profile with a billing account is reserved and counted.
- **Settlement**: when the turn ends, its ledger row is tagged with the
  account and the reservation is closed, in one transaction. An uncertain
  outcome keeps the reservation counted in full for its day (`kept`), in
  addition to the measured cost. Uncertain outcomes are an error, a lost
  lease, preemption, a turn closed by a cap, or a crash followed by repair. A
  human checks the turn, then releases it with `ameesh budget release <id>`.
  A turn can cost more than its reservation; the excess is counted in full
  and blocks the next turns. The cap can therefore be exceeded by at most the
  excess of the turns running at that moment.
- **Exhaustion**: the agent is paused with
  `budget : account openai-manaty 99.00/100.00 $ (calendar day Europe/Paris) until 00:00`.
  No critical-agent exception applies. The critical fallback only takes a
  profile whose own account still has room. Other accounts keep their own
  capacity. Conversations and documents are untouched.
- **Audit**: setting an envelope, removing it and releasing a reservation are
  for authorized humans only. Each change is journaled in `budget_events`
  (`scope = account:<id>`) and in the thread.

!!! warning "Coverage boundary"
    The envelope covers the turns of runners connected to the mesh database.
    It does **not** cover:

    - mediated executors, which go through the model relay, still under the
      mesh caps only;
    - a profile without `billing_account`;
    - any use of the same keys outside ameesh, such as an application calling
      the provider directly or a developer's shell.

    For DeepSeek, the balance poll shows the real drop (`ameesh cost balance`).
    For OpenAI, there is no reading yet: outside spend is not seen. An
    application that wants its calls counted must go through ameesh.

## Commands

```bash
ameesh cost report [--json]             # agent, harness, model, spend 1 h / 24 h, gauges
ameesh cost spent [agent|all] [seconds] # spend (USD) over a sliding window (default 3600 s)
ameesh cost over [agent]                # exit 0 if the plan's pace is exceeded
ameesh cost turns [--agent A] [--since 24h] [--json]     # the per-turn ledger
ameesh cost gauges [--harness H] [--since 7d] [--json]   # history of plan gauges
ameesh cost balance [--provider deepseek] [--record] [--json]   # provider balance, real spend per hour and day
ameesh budget show | set --per-hour X [--per-day Y] [--agent A] | unset …   # caps in the database
ameesh budget set --account ID --per-day X [--tz Z] | unset --account ID | release N   # account envelopes
ameesh accounts list | use ACCOUNT | auto | healthy | check # accounts and failover
ameesh set <agent> model=… effort=… tier=…   # routing: applied at the next turn
ameesh progress                         # includes spend and gauges
```

The per-agent budget declared in the canon (`budget_usd_per_day`) is
synchronised into the registry and shown next to the agent's spend in
`ameesh list`; the guard itself enforces the hourly cap and the pace guard
above.

!!! note "Not yet"
    Budgets per harness and per project in the canon, and routing rules by
    task class, are part of the planned work.
