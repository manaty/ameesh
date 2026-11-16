# Expiring credit and per-request admission

The cycle gate is independent of a provider, harness, key and host. All users
of one billing account share a PostgreSQL ledger. It is additional to the
runner's existing daily admission, which is unchanged.

## Balance projection

`ameesh budget cycle attest` accepts a **net remaining** USD balance, a cap no
higher than that balance, the observation timestamp, credit expiry, an explicit
freshness deadline no later than expiry, and a nonsecret source reference.
It uses the same authenticated human authority as other budget settings. The
source can refer to a bureau/owner attestation; runners need no billing/admin
key. `cycle show --account ID` exposes the latest projection and counted debt.
No balance or key is configured automatically.

The cycle and request tables remain closed to the supervisor SQL role; reads
use the budget API. Their actor/agent spellings are historical evidence kept
on agent rename. Admission and settlement address accounts and attempt ids,
so those spellings confer no authority and do not select pending liabilities.

The clock is the database clock, read after the account lock. Admission refuses
an absent, stale, expired, future-dated or unreadable projection. A day boundary
has no effect. A new projection requires a strictly newer dated observation;
replaying the old observation is refused. The new balance must be genuinely net
of previously billed spend. Open/kept attempts from all previous projections
remain counted, as do results settled after the new observation. Consequently,
a late result cannot silently disappear on renewal. Reservations have no
expiry-based release. Using these credentials outside this gate requires a new
balance statement before further guarded use; such spend is not observable here.

Amounts are integer micro-USD: available funds round down and liabilities round
up. Each reservation and settlement holds the same schema/account advisory lock
in a short transaction. The transport runs after commit, outside that lock.
An attempt id admits a send exactly once; a retry needs a new id and reservation.
A successful settlement replaces the bound with total actual cost. Missing
usage, partial streams, errors, cancellation and crashes keep the full bound.
A cost above its bound preserves the larger debt and blocks the account, even
across later statements, until operator reconciliation of the adapter defect.
There is deliberately no automatic forgiveness or timeout release.

## Request contract and interception decision

The generic `cycle_budget.execute` boundary encloses **one complete transport
attempt**, with an enforced output cap and no hidden SDK retries. Its inputs are
an upper bound of all input tokens (history, instructions, tools, results and
attachments), max output including reasoning, uncached tariffs for every
permitted service tier, and a bound for other billable items. Admission uses
the highest input and output rates across tiers. Unbounded charges or missing
prices must be rejected by the transport adapter before calling the gate.
`actual_cost` must validate complete usage covering every billable category;
unknown usage retains the reservation. A required daily-admission callback
preserves the second guard; it must be wired to actual runner admission.

A generic token count is not derived from character count here. The next
provider adapter must prove its tokenizer/context upper bound, enforce output
and service-tier restrictions on the wire, refuse unsupported paid tools and
media unless separately bounded, and disable retries inside each guarded send.
This is a trusted adapter contract, not an isolation boundary against arbitrary
network calls by processes holding the provider key.

Evidence from the installed dsh packages on 2026-11-16:

- `@deepseek-ai/dsh-llm@0.2.1-rc.4`, `lib/index.js`,
  `LlmRuntime.streamWithRegistration`: `llm/stream` wraps `adapterStream`.
- `@deepseek-ai/dsh-llm-pi-ai@0.2.1-rc.4`, `lib/index.js`,
  `streamWithSnapshot`: resolves credentials, converts the complete context,
  then delegates to `snapshot.models.streamSimple`. The stream middleware is
  above this conversion and the provider transport.
- Its installed `@earendil-works/pi-ai@1.4.0`,
  `dist/api/openai-responses.js`: wraps `client.responses.create` in
  `retryProviderRequest` even though each SDK request sets `maxRetries: 0`.
  An outer reservation would therefore cover several potentially paid attempts.
- `@deepseek-ai/dsh-llm-retry@0.2.1-rc.4`, `lib/index.js`, `recover`:
  `agent/request-error` can schedule another request. Agent loops and title
  generation also initiate their own streams.

Chosen integration point: a single-attempt transport in the provider adapter,
or an equivalent local transport broker that admits **each** outgoing attempt.
An outer `llm/stream` hook alone is insufficient. This prerequisite supplies the
ledger, quote calculation and guarded execution boundary; it does not install
or activate a provider adapter, and existing dsh turns are not covered by it.
The provider follow-up must demonstrate main turns, reasoning, tool follow-ups,
compaction/title generation, subagents and retries through the same gate with a
fake transport before any activation. The key-holder decision remains separate.

## Operator reconciliation and delayed billing

Cycle mutations accept an authorized human, or a runner/session-bound agent
whose owning, fully readable canon declares the `bureau` role. An inherited
agent name, a role from another canon, an orchestrator role and a historical
assignment do not confer this authority. Other budget settings retain their
human-only authorization. The Python/storage API is trusted internal code;
operator authentication happens at the CLI boundary.

Attestation requires `--billing-lag-seconds N`: the maximum delay between
settlement and its appearance in the reported net balance. Zero asserts that
billing is immediate. Settlements since `observed_at - N seconds` remain
liabilities, including before the observation itself; older settled costs can
be absorbed by the net statement. Open/kept requests remain counted without
timeout. The API's default zero preserves the previous immediate-net contract;
callers with delayed statements must supply their proven lag. If no finite lag
can be established, do not attest that balance as usable credit. This margin is
conservative and can temporarily count a cost twice; it never releases an
uncertain reservation.

For a completed or definitively unbilled request, the operator records the
proven total cost, the nonsecret evidence reference and the reason:

```
ameesh budget cycle reconcile --account ACCOUNT --attempt-id ATTEMPT \
  --actual-usd COST --source INVOICE_OR_USAGE_REF --reason FINAL_COST_CONFIRMED
```

Zero is allowed only with evidence that the attempt was not billed. The
operator must first establish that an `open` request is no longer in flight.
The journal stores the actor, source, reason and previous liability atomically
with settlement. Known actual cost cannot be reduced, duplicate reconciliation
is refused, and an attempt identifier never admits a second dispatch. A larger
late result is retained and blocks admission, even after a prior reconciliation.

A bound breach is a separate gate: recording its actual cost does not by itself
assert that the adapter has been repaired. After all disputed known costs have
been reconciled, the operator can acknowledge the reviewed fix:

```
ameesh budget cycle clear-breach --account ACCOUNT --source INCIDENT_REF \
  --reason ADAPTER_FIXED --adapter-evidence REVIEWED_FIX_REF
```

This records the affected cycle IDs and fix reference before clearing the gate,
under the same account lock as admission and settlement. It changes neither
credit, expiry nor outstanding debt. A stale or exhausted envelope still
refuses admission. No timeout, automatic renewal or reconciliation grants
additional funds, configures a real account or sends a paid request.
