# Deferral observation, version 1 (`ameesh-deferral/1`)

Contract element of the [external module public contract v1](module-contract-v1.md),
tranche **E3b** of the [IMA mapping](../design/ima-public-contract-mapping.md)
([#7675](https://github.com/manaty/ameesh/issues/5868), under
[#5827](https://github.com/manaty/ameesh/issues/5827)). It answers one question:
*why does this agent not run a turn right now?* — with a bounded, closed and
stable block. It is an observation, never a permission: `null` means "no known
deferral", not "available", and no value promises that a turn will run.

## Shape

```json
{"schema": "ameesh-deferral/1", "state": "deferred", "reason": "pressure",
 "since_ts": 1794904037.7, "age_s": 42,
 "queue": {"state": "queued", "reason": "pressure", "host": "h1",
           "scope": "own_cgroup", "critical": false, "detail": "…",
           "queued_s": 42, "retry_s": 60, "capacity": null}}
```

| Field | Meaning |
| --- | --- |
| `schema` | `ameesh-deferral/1`; required. |
| `state` | Closed enum; at `/1` only `deferred`. An unknown state is read as "not proven available". |
| `reason` | Closed enum below; the first matching fact wins. |
| `since_ts` | Epoch seconds when the current hold started, or `null` when no fact carries it. Never guessed from a timestamp of another event. |
| `age_s` | Whole seconds since `since_ts`, or `null`; never negative. |
| `queue` | Optional. Present only for `pressure`: the **same** `queued` object already published by `ameesh list --json` (`scope`, `critical`, `detail`, `queued_s`, `retry_s`, `capacity`), not a new free-text error field. |

The block is absent (or `null`) when no hold is known: a running, idle or
paused-for-later agent and an operator pause that has not taken effect yet
(`after_turn` on a running agent) carry no block. An absent block is not a
successful turn.

## Closed reasons

| `reason` | Fact that produces it |
| --- | --- |
| `operator` | The operator paused the agent and the pause has taken effect (not a running agent). `since_ts` is the pause time. |
| `pressure` | The persisted status is the native pressure `queued` object (`policy.resources`, `pressure_scope`, capacity). |
| `authentication` | Harness/provider authentication refusal (`auth_failure.STATUS_PREFIX`). |
| `provider_credits` | Provider credit exhaustion (`provider_credits.STATUS_PREFIX`). |
| `budget` | Account or turn budget pause (`budget.PAUSE_PREFIX`). |
| `account` | No usable account (`accounts.CONFIG_BLOCK_PREFIX`). |
| `account_window` | Provider short-window cooldown (`account_window.is_short_window`). |
| `session` | Session lock held by another process (`AgentWorker.DEFERRED_STATUS`). |
| `database` | Database unreachable (`AgentWorker.DB_STATUS`). |
| `host` | Host not ready (`AgentWorker.HOST_STATUS`). |
| `workdir` | Work directory missing (`AgentWorker.WORKDIR_STATUS`). |
| `shutdown` | Queued because the runner is shutting down (`runner shutting down:`). |
| `restart` | Queued because the runner is outdated (`outdated runner:`). |
| `other` | A `blocked` status with no recognised text: a refusal to guess. |

`other` is not a category to rely on for control flow; a consumer that needs
to distinguish a new cause waits for a contract revision rather than parsing
`status_text`. The runner constants are duplicated in
`ameesh.deferral_observation` so the module stays importable without the
executor; `tests/test_7675_deferral.py` asserts they equal the runner
constants, so a rename cannot silently change the mapping.

## Bounded and private

* One block for the current hold: no history, no counter, no queue position
  beyond the published `queue` object.
* No free text is copied: the operator/runner text stays in `status_text`,
  and no prompt, mail body, payload or credential enters the block.
* The accounting state is **not** restated. `accounting.state` (see
  [`ameesh-turn-observation/1`](../EXPLOITATION.md#observation-dun-tour--ameesh-turn-observe-tpk-2167-lot-2894-b))
  remains authoritative, including when the ledger evidence is ambiguous: the
  native reservation is retained and the block never claims the work failed or
  was settled. A deferred block and `accounting.state == "unknown"` coexist.

## Surfaces

The same block is embedded, under the `deferral` key, in:

| Surface | Since |
| --- | --- |
| `ameesh list --json` (`ameesh-agents/1`) | #7675 |
| `ameesh show --json` and `GET /api/v1/agents/{agent}` | #7675 |
| `GET /api/v1/agents` | #7675 |
| `ameesh turn observe --json`, `GET /api/v1/turns/{turn}`, `GET /api/v1/requests/{request}` (`ameesh-turn-observation/1`) | #7675 |

The HTTP schemas are the ones in [OpenAPI v1](openapi-v1.json); the JSON
Schemas constrain `reason` and `state` to the module's closed lists, so a new
value cannot be documented without the builder and its tests seeing it.

## Versioning

In `/1`, fields are added and enum values may be added; an existing required
field, its type or its meaning does not change. A consumer ignores unknown
fields, requires `schema`, and reads an unknown `reason` or `state` as
`unknown`, never as success or availability. Removing a reason, or narrowing
the block, is a breaking change under the public contract's compatibility
rule. The proposed IMA adoption and the removal of its private pressure /
scheduling overrides remain tracked in
[IMA#1803](https://github.com/thepackengers/issue-manager-agent/issues/1803);
this document alone does not close that request.
