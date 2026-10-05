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
- the **pace guard** of subscriptions: a plan is going too fast when its usage
  reaches `min(90 %, elapsed share of the window + 10 points)`. The 10-point
  margin covers orchestrators and humans, who share the same plan.

The decision is fresh at every turn. A paused agent shows the reason in
`ameesh list`, and the thread keeps a trace.

## Commands

```bash
ameesh cost report [--json]             # agent, harness, model, spend 1 h / 24 h, gauges
ameesh cost spent [agent|all] [seconds] # spend (USD) over a sliding window (default 3600 s)
ameesh cost over [agent]                # exit 0 if the plan's pace is exceeded
ameesh cost turns [--agent A] [--since 24h] [--json]     # the per-turn ledger
ameesh cost gauges [--harness H] [--since 7d] [--json]   # history of plan gauges
ameesh cost balance [--provider deepseek] [--record] [--json]   # provider balance, real spend per hour and day
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
