<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Site assistant — "Ask about ameesh"

A small chat bubble on the public site (landing page and documentation) that
answers questions about ameesh, in the visitor's language, from the
documentation only. Its backend is one serverless function (Scaleway Functions,
Python runtime) in this folder.

```
chatbot/
  handler.py            entry point: handler.handle(event, context)
  ameesh_chat/
    app.py              HTTP: CORS, limits, spend cap, retrieval, model call
    search.py           lexical search (BM25, accents folded, small glossary, CJK bigrams)
    indexer.py          documentation → passages (title, section, public link)
    prompt.py           system rules and message assembly (passages = data)
    guard.py            pricing, spend cap (month + day), sliding-window rate limits
    store.py            spend counter: one JSON object in Object Storage (S3 SigV4)
    transport.py        HTTPS only, no redirect followed, same origin
  build_index.py        builds index.json from site/docs/ and docs/design/
  package.sh            index + code → dist/chatbot.zip
  requirements.txt      empty: standard library only
site/landing/chat.js    the widget (no library, no CDN); chat.css; chat-config.js
```

Tests (no network, fake model): `tests/test_l33_chatbot.py`, part of
`scripts/test.sh`.

## How it works

1. The browser sends `{"question", "history"}` (history: the last three
   exchanges, kept in the page only) to the function.
2. The function checks the origin, the size of the question, the global and
   per-address rate limits.
3. It searches the passage index (built at deploy time from `site/docs/` and
   `docs/design/`) and keeps the best passages within a bounded context budget
   (7,000 estimated tokens by default, at most 8 passages).
4. It computes an **upper bound** of the call's cost (see below) and
   **reserves** it in the durable counter — written, then read back — against
   the monthly and daily caps. No durable reservation, no model call.
5. It calls the model (OpenAI-compatible chat completions; DeepSeek by default)
   with strict system rules: answer only about ameesh, only from the excerpts,
   say so when the answer is not there, decline anything else and any attempt
   to change the rules, reply in the question's language, cite the pages used.
   The excerpts are sent as delimited data, never as instructions.
6. The real cost (from the `usage` returned by the API and the configured
   prices) replaces the reservation in the counter. A missing or malformed
   `usage` keeps the full reservation.

Nothing of a conversation is stored or logged. The function writes one JSON
log line per request with fixed fields: `code`, `status`, `ms`, `in_tok`,
`out_tok`, `usd`, `passages`. The spend counter object holds only totals and
open reservations (amount, time, period, random instance id).

Answers are produced by a third-party model provider: the widget says so in a
short notice above the input.

## Configuration

Environment variables of the function. **Secret** ones must be set as secret
environment variables, never in the repository or the page.

| Variable | Default | |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | **secret**, required: a dedicated key, with a spending limit on the provider side too |
| `CHAT_API_BASE` | `https://api.deepseek.com` | OpenAI-compatible base URL (HTTPS) |
| `CHAT_MODEL` | `deepseek-flash` | model name as the API expects it (check the provider's model list) |
| `ALLOWED_ORIGINS` | `https://ameesh.manaty.net` | comma-separated; CORS and POST are refused for other origins |
| `MONTHLY_CAP_USD` | `20` | spend cap per calendar month (UTC) |
| `DAILY_CAP_USD` | `MONTHLY_CAP_USD × 2 / 30` | spend cap per day (UTC) |
| `MAX_REQUESTS_PER_DAY` | `2000` | model calls per day, all visitors |
| `PRICE_INPUT_MISS_PER_M` / `PRICE_INPUT_HIT_PER_M` / `PRICE_OUTPUT_PER_M` | `0.27` / `0.07` / `1.10` | USD per million tokens; set them to the provider's current prices |
| `MAX_QUESTION_CHARS` | `500` | |
| `CONTEXT_BUDGET_TOKENS` | `7000` | passages sent to the model |
| `MAX_OUTPUT_TOKENS` | `700` | |
| `RATE_PER_IP` | `5/60,30/3600,80/86400` | `count/seconds` sliding windows per client address |
| `RATE_GLOBAL` | `30/60,600/3600` | same, all visitors |
| `CLIENT_IP_HOPS` | `1` | which `X-Forwarded-For` entry, from the right, is the client |
| `CHAT_TIMEOUT_S` | `25` | model call timeout |
| `STATE_BACKEND` | `s3` | `memory` only for local trials (the cap is then lost on restart) |
| `STATE_BUCKET` | — | private bucket holding the counter |
| `STATE_KEY` | `chatbot/spend.json` | object key |
| `STATE_S3_REGION` | `fr-par` | |
| `STATE_S3_ENDPOINT` | `https://s3.<region>.scw.cloud` | |
| `STATE_ACCESS_KEY` / `STATE_SECRET_KEY` | — | **secret**: API key of an IAM application limited to Object Storage in this project |

Site side: the repository variable `AMEESH_CHAT_ENDPOINT` (the function's HTTPS
URL, public by nature) is read by `scripts/build-site.sh` and written into
`chat-config.js`. Empty or unset: the bubble is hidden.

### Cost bound

The reservation must be a bound, not an estimate. The number of input tokens is
bounded by the number of **UTF-8 bytes** of everything sent (rules, passages,
history, question) plus a fixed margin per message for the chat template
(16 tokens) and per request (64): the tokenizer of the targeted models is a
byte-level BPE, where every token covers at least one byte, so no text — CJK,
emoji, code — can produce more tokens than bytes. The output is bounded by
`max_tokens` (`MAX_OUTPUT_TOKENS`), always sent to the API. The bound is priced
at the higher input price and the output price. Prices, caps and the stored
state must be finite non-negative numbers; anything else closes the admission.
If you switch to a provider whose tokenizer is not byte-level, revisit this
bound.

### Durable spend counter

Every change to the counter is a transaction: read the object and its ETag,
apply the change, write **conditionally** (`If-Match: <etag>`, or
`If-None-Match: *` when creating), then read back to confirm. A conflict
(412/409) means another writer went first: read again and replay. The
instance's lock is held for the whole transaction, so the durable totals never
go backwards.

* a reservation is durable (written and read back) **before** the model call;
* if the process stops between the call and the settlement, the reservation
  stays in the object; a new instance counts every reservation it did not make
  as **fully spent** on its first transaction, and any reservation older than
  two minutes is counted the same way — a spend is never forgotten;
* if a write fails, no new call is admitted (the next reservation cannot be
  written); a settlement that could not be written is retried on the next
  transaction, and until then the full reservation stays counted;
* one period per attempt: the month and day are read once, under the lock, for
  each attempt (including replays); a call that crosses midnight or the end of
  the month is attributed to the period of its reservation — the totals of the
  period just closed are kept in `prev_day` / `prev_month` for that purpose;
* a returned `usage` above the bounds that were sent (input byte bound,
  `max_tokens`) means the provider's contract or the price list is violated:
  at least the known cost is counted (never reduced to the reservation), the
  object gets `"closed": "usage_over_bound"`, and every instance refuses new
  questions until the operator has looked and removed that field; a missing or
  malformed `usage` keeps the full reservation;
* until the closing is written, the instance keeps it locally and refuses
  every new question (each refused attempt also tries to publish it);
* the object keeps, for two days, the amount already counted per settled
  reservation (`settled`): a late settlement, after another instance has
  counted the reservation in full, adds only the positive difference with the
  real cost, and replaying it adds nothing; with no trace left, it is counted
  in full;
* a cap set to `0` closes admission (it is not replaced by the default).

Atomicity across instances relies on the conditional writes. Scaleway Object
Storage documents them
(<https://www.scaleway.com/en/docs/object-storage/api-cli/using-conditional-writes/>);
the operator checks them once at the first deploy (see below). The instance
lock alone does not protect against overlapping instances.

### Why a JSON object in Object Storage for the counter

The counter must survive cold starts and redeploys, cost next to nothing and
add no dependency. One small private object in a bucket of the same project
does that: a few kilobytes stored and a few thousand requests a month, no
always-on service (a managed Redis or database would bill by the hour), the
standard S3 API (signed here with the standard library), and a narrowly scoped
IAM key; conditional writes with ETags give the atomicity the counter needs.
If the counter cannot be read or written, the function answers "unavailable"
rather than spend blind.

The per-address and global rate limits live in memory (they reset on a cold
start, which is acceptable for a rate limit); the address itself is never kept,
only an HMAC of it with a per-instance random salt.

## Deploy (Scaleway, generic)

Placeholders in `<angle brackets>`; nothing here is specific to one account.

```bash
# 0. a dedicated DeepSeek key, with its own spending limit at the provider

# 1. a private bucket for the counter
scw object bucket create name=<bucket> region=fr-par

# 2. an IAM application limited to Object Storage in this project, and its key
scw iam application create name=<app-name>
scw iam policy create name=<policy-name> application-id=<app-id> \
  rules.0.project-ids.0=<project-id> rules.0.permission-set-names.0=ObjectStorageFullAccess
scw iam api-key create application-id=<app-id> default-project-id=<project-id>

# 3. the archive (fresh index + code, no dependency)
chatbot/package.sh

# 4. namespace and function (public HTTPS endpoint, one instance at most)
scw function namespace create name=<namespace> region=fr-par
scw function function create namespace-id=<namespace-id> name=<function> \
  runtime=python313 handler=handler.handle privacy=public http-option=redirected \
  min-scale=0 max-scale=1 memory-limit=256 timeout=30s \
  environment-variables.ALLOWED_ORIGINS=https://ameesh.manaty.net \
  environment-variables.STATE_BUCKET=<bucket> \
  environment-variables.STATE_S3_REGION=fr-par \
  secret-environment-variables.0.key=DEEPSEEK_API_KEY \
  secret-environment-variables.0.value=<deepseek-key> \
  secret-environment-variables.1.key=STATE_ACCESS_KEY \
  secret-environment-variables.1.value=<access-key> \
  secret-environment-variables.2.key=STATE_SECRET_KEY \
  secret-environment-variables.2.value=<secret-key>
scw function deploy namespace-id=<namespace-id> name=<function> runtime=python313 \
  zip-file=chatbot/dist/chatbot.zip

# 5. the endpoint, for the site
scw function function get <function-id>      # domain_name → https://<domain_name>
# repository variable AMEESH_CHAT_ENDPOINT=https://<domain_name>, then rerun the pages workflow
```

Redeploy after a documentation change: `chatbot/package.sh` then step 4's
`scw function deploy` (the index is rebuilt each time; it is not versioned).

After the first deploy, check:

* `OPTIONS` from the site origin returns 204 with `Access-Control-Allow-Origin`;
  from another origin, 403;
* the function logs show only the fixed fields above;
* `CLIENT_IP_HOPS`: the right-most `X-Forwarded-For` entry is the visitor
  (adjust if the platform adds another hop), otherwise the per-address limit
  acts as a global one;
* the counter object appears in the bucket after the first question;
* conditional writes are enforced on the bucket: a `PUT` of the counter object
  with `If-Match: "wrong"` must be refused with 412, and a `PUT` with
  `If-None-Match: *` on the existing object as well. If not, do not open the
  site assistant.

## Cost

With the default prices (USD per million tokens: 0.27 input, 0.07 cached
input, 1.10 output), a typical question sends about 3,000 input tokens (rules
≈ 500, eight passages of the documentation, short history) and gets about 350
output tokens: about **0.0012 USD** per question.

The reservation is larger, because it is a bound: about 9,000–10,500 UTF-8
bytes for a typical English or French question with its passages, so about
**0.0035 USD** reserved (byte bound × 0.27 + 700 × 1.10, per million); the worst
case allowed by the limits (500-character question, full history, full context
budget, four-byte characters in the question and history) stays under 0.02 USD
with the current documentation. The default
20 USD/month cap therefore allows well over 10,000 questions a month, and the
derived daily cap (1.33 USD) about 1,000 a day; reservations only limit how
many calls can be in flight at once. Function and storage costs are negligible
at that volume (a question makes about four small reads and two small writes
of the counter object). Check the prices against the provider's current price
list and set the `PRICE_*` variables accordingly.
