# Configurable dsh Responses provider

The `deepseek` harness family describes the executing harness. A GPT model
executed by dsh therefore remains `deepseek` for allocation and independent
review; no provider name creates a new review family.

A host may declare `dsh_provider` in its configuration. An account may declare
the same object under `accounts.deepseek[]`; an account declaration replaces
the host object in full, so switching account cannot accidentally retain
another account's endpoint or key reference. Absent configuration preserves
the existing descriptor and official DeepSeek route.

```json
{
  "dsh_provider": {
    "provider": "chosen-responses",
    "endpoint": "https://provider.example/v1",
    "key_env": "CHOSEN_PROVIDER_KEY",
    "billing_account": "chosen-billing",
    "models": {
      "chosen-model": {
        "context_tokens": 200000,
        "max_output_tokens": 16000,
        "tariffs": {"default": [2, 0.2, 8]},
        "response_models": ["chosen-model-2026-01-01"],
        "reasoning_efforts": {"off": null, "high": "high", "max": "high"},
        "source": "dated provider model-limit and pricing reference"
      }
    }
  },
  "accounts": {
    "deepseek": [{
      "name": "chosen",
      "type": "api_key_env",
      "env": "CHOSEN_PROVIDER_KEY",
      "key_env": "OWNING_EXECUTOR_KEY_REFERENCE",
      "billing_account": "chosen-billing"
    }]
  }
}
```

This is a schema example with fictitious names and prices, not an activation
recipe. Key deposition and the first automated call require the separate owner
decision naming the authorized key holder. This change creates no account,
key, balance statement, deployment or paid request.

The selected account must be an `api_key_env` account whose destination `env`
matches the route's `key_env`, and its `billing_account` must match.
The destination must be dedicated to this route: automatic harness credential
names (including `DEEPSEEK_API_KEY`), configuration variables, runtime variables
and mesh/tool credentials are refused. The account's source `key_env` or
`key_file` remains separate from this destination; it is read only by its owner.
Existing account validation enforces the owning UID and private mode for `key_file`,
or reads the selected source variable in the executor's own environment.
No secret is read from another process or inserted into a configuration or
patch file. The ordinary DeepSeek balance probe skips configured-provider
accounts before reading their key: it cannot send an alternate provider key
to the official DeepSeek endpoint. Balance statements stay explicit. Mediated executors are refused: they do not hold this credential.

## Admission and accounting

`context_tokens` must be a documented, provider-enforced **maximum input token
count** for that exact model and endpoint, including instructions, function
schemas, history, tool results and reasoning replay. It is not an estimate from
character count or the size of the current prompt. If the provider offers no
finite enforceable input limit, it cannot be enabled through this adapter.
The source reference must establish that limit and the dated rates. Admission
reserves the whole context limit plus the enforced output cap, even for a
small prompt. This is deliberately conservative.

Tariffs are USD per million tokens: `[uncached input, cached input, output]`,
per permitted service tier. No tier is inferred from a global price table.
Reasoning tokens are already included in Responses `output_tokens` and are
never added twice. The broker accepts only inline text/history and local
function tools. Media, server tools, retained context, background jobs,
truncation, unknown request fields and unpriced tiers are refused. The
`reasoning.encrypted_content` expansion is allowed for inline reasoning replay.

Before each HTTP attempt the broker rechecks the ordinary daily limits,
atomically reserves the account's daily bound, and reserves the cycle liability
through `cycle_budget.execute`. The owning connection is fresh for each
attempt. The upstream transport makes exactly one HTTPS exchange, with neither
SDK retries nor redirects. A dsh/pi-ai retry is a new incoming HTTP request and
therefore gets a new attempt ID and both guards. Ordinary turn admission stays
in force as an additional reservation. Request admission checks the request's
own bound atomically; it does not add another `profile.reserve_usd` on top of
the existing turn reservation.

Complete Responses usage settles at the declared input/cache/output tariffs.
The real response model and tier must match declared prices. A model's optional
`response_models` lists exact returned snapshot identities covered by that
model's documented bounds and tariffs. Unlisted identities keep the full
reservation; no date suffix or alias is guessed. Missing or partial
usage, timeout, invalid response or cancellation keeps the full cycle liability;
the daily ledger records its conservative bound. A write failure closes this
turn's broker. Each response has its own `turn_costs` row (`source=relay`,
`harness=deepseek`, account and turn ID). Malformed usage still records the
conservative amount with only valid nonnegative integer counters. The usual
turn row carries zero dollars and the sum of its provider-response tokens,
avoiding a second charge at the descriptor's DeepSeek rates. Only this native
row carries the session: a fresh worker can reload session and reread totals
without counting responses as extra turns. Cached input is split out of total
input for the existing DeepSeek session convention.
Dated provider statements and operator reconciliation remain separate: a
transport observation never invents or refreshes available credit.

The upstream SSE response is buffered with a 32 MiB bound before forwarding,
so the harness receives completed events rather than incremental network
output. Requests are limited to 8 MiB. A broker is scoped to one turn, bound to
loopback, and protected by a fresh random credential. It never discovers models
or credentials over the network. It is a trusted adapter boundary, not process
isolation against an executor administrator.

## dsh routes

The explicit `llm-pi-ai` route uses `api: openai-responses`, an explicit model
catalog and credential reference. The real credential stays in the broker;
dsh and its children receive only the turn's loopback credential. Its internal
credential store is disabled for this composition. Existing native subagent
and compaction requests use the same default LLM route and broker; alternate
models must be in the declared catalog and have tariffs.

| Installed dsh route | Configured-provider behavior |
| --- | --- |
| Main model, reasoning and tool follow-ups | Guard each HTTP request |
| Native compaction and native subagent model requests | Same broker and guards |
| pi-ai transport retries and `llm-retry` requests | Each HTTP attempt guarded independently |
| `session-title-llm` | Disabled; no implicit title request |
| `llm-deepseek`, `llm-deepseek-account`, `deepseek-account` | Disabled |
| `web-search-deepseek`, `web-deepseek-api` | Disabled; no paid DeepSeek web route |
| Plain HTTP fetch and local function tools | No provider key; paid server tools refused |

The current composition contract is dsh 0.2.1-rc.4. The fake-provider integration
test boots installed dsh with a private temporary home, composes the patch,
and serves one Responses stream through the broker. No external credential
or provider connection is involved. Closing a broker refuses further
admission and dispatch before clearing its key. Independent broker tests cover the same
boundary for main/reasoning/tool/compaction/subagent/retry requests.

Protocol reference: [Responses create and usage fields](https://developers.openai.com/api/reference/resources/responses/methods/create).
