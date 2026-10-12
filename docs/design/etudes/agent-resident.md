---
type: Study
title: "Study — the resident agent: the human talks to the agent, no longer takes it"
description: "Reverse the attach model: the agent stays driven by ameesh at all times and the human comes to talk to it (ameesh chat, then the Nexlink page). Real tests of the harnesses (Claude Code stream-json, Codex app-server, DeepSeek dsh over ACP), design of the resident process under a lease, inputs and authority, output broadcast, rotation, measured costs, risks; decision 0035 (accepted on 2026-10-10) and lots L75–L83."
status: draft
tags: [agent-resident, conversation, chat, attach, bail, harnais, acp, sessions, autorite]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T18:00:00+02:00" }
sources:
  - { resource: "../decisions/0014-orchestrateurs-a-tours-et-placement.md", title: "Decision 0014 (turn-based orchestrators, attach)" }
  - { resource: "../decisions/0018-vitesse-des-agents.md", title: "Decision 0018 (priority messages, preemption)" }
  - { resource: "../decisions/0024-vue-temps-reel.md", title: "Decision 0024 (real-time view)" }
  - { resource: "../decisions/0025-une-session-par-lot.md", title: "Decision 0025 (sessions, rotation with summary)" }
  - { resource: "../decisions/0030-pas-de-travail-sans-reveil-possible.md", title: "Decision 0030 (no work without a possible wake-up)" }
  - { resource: "orchestration-et-vivacite.md", title: "Study: orchestration and liveness (blockage of 2026-10-07)" }
  - { resource: "standards-des-harnais.md", title: "Study: harness standards (ACP)" }
  - { resource: "https://code.claude.com/docs/en/headless", title: "Claude Code — headless mode (-p, stream-json)" }
  - { resource: "https://docs.claude.com/en/docs/agent-sdk/streaming-vs-single-mode", title: "Claude Agent SDK — streaming input or single message" }
  - { resource: "https://github.com/openai/codex/tree/main/codex-rs/app-server", title: "Codex app-server (JSON-RPC protocol)" }
  - { resource: "https://agentclientprotocol.com/protocol/prompt-turn", title: "ACP — prompt turn, session/cancel" }
---

# Context

Owner's direction (2026-10-10): "we should favour interactive access to an
agent that is available at all times to listen and answer."

Today, to talk to an agent, the human **takes** it: `ameesh attach` sets an
exclusive lease `attach:<user>@<host>` (`runner.py`, `attach_owner`,
`run_attach`). The runner steps aside, the interactive harness runs in the
human's terminal, and the lease is returned on exit. If the human puts the
session in the background or forgets it, nobody wakes it any more: mail,
prompts and alerts pile up. This is the "human session" form of the blockage
of 2026-10-07 ([0030](../decisions/0030-pas-de-travail-sans-reveil-possible.md),
[study](orchestration-et-vivacite.md)).

The direction reverses this model: the agent stays **driven by ameesh at all
times**; the human comes to **talk** to it; opening or closing the
conversation changes nothing for the agent.

Choices already validated by the owner:

* **(a)** a human message that arrives during a turn is served **at the end of
  the turn** by default; the `!urgent` prefix interrupts **at the safe
  point**;
* **(b)** first conversation client: the terminal (`ameesh chat <agent>`),
  then the Nexlink page (real-time view,
  [0024](../decisions/0024-vue-temps-reel.md)); Slack later.

# What the harnesses allow (tests of 2026-10-10)

Real, short tests, in a toy repository in the scratchpad, with the credentials
already configured on the workstation, without touching agents, services or
the database. Versions: Claude Code 2.1.286, Codex CLI 0.159.3, DeepSeek
Harness (`dsh`) 0.2.0-rc.2. Cheap models: Haiku, `gpt-6-luna` (low effort),
`deepseek-v4-flash` (reasoning off). Durations are orders of magnitude on a
context of 6 k to 27 k tokens. They do not predict the latency of a real
orchestrator.

## Claude Code: `-p --input-format stream-json --output-format stream-json`

Checked by test:

* **One process, several messages.** Each `{"type":"user",…}` line written to
  standard input opens a turn. The process stays alive between messages (5 s
  of inactivity, then 3 minutes) and exits cleanly (code 0) when standard input
  closes. The session (`session_id`) is the same from start to end.
* **One message per turn, usage per turn.** Each turn ends with a `result`
  event that carries `usage` (input tokens, cache tokens read and written,
  output tokens) **for that turn only**, `modelUsage`, `num_turns`,
  `queued_turn_count` and `total_cost_usd`. The `total_cost_usd` is
  **cumulative over the session**, and stays so after a `--resume` in another
  process: a turn resumed separately showed $0.0434 after $0.0402. ameesh
  already tells the two apart (`cost.py`).
* **Message written during a turn: injected at the next safe point, not
  queued.** A message sent 2.5 s after the start of a turn that was running
  `sleep 6` was read **as soon as the tool ended**, in the same turn. The two
  prompts got a single answer ("THREE / FOUR") and a single `result`.
  Consequence for choice (a): **ameesh** must hold the message until the
  `result`. Writing it at once to standard input gives exactly the `!urgent`
  behaviour (safe point = end of the tool call in progress), without
  interruption.
* **Interruption without killing the process.** `{"type":"control_request",
  "request_id":…, "request":{"subtype":"interrupt"}}` was acknowledged
  (`control_response` `success`). The turn ended with `error_during_execution`,
  and the next message was served normally by the same process.
* **Native compaction.** `/compact` sent as a message produced a
  `compact_boundary` (`pre_tokens` 25,389 → `post_tokens` 4,779, 13.6 s), and
  the session continued.
* **Hooks.** The `SessionStart` hook appears in the stream (`hook_started`,
  `hook_response`), once per process. The `UserPromptSubmit` and `Stop` hooks
  are not reported in the stream: that they fire on every message is
  **assumed** (docs), **not verified**.
* **Startup.** About 7 s up to `init`, of which 3 s of `SessionStart` hook on
  this workstation.

Not verified:

* the `control_request` protocol is documented only through the Agent SDK; it
  is an internal surface that may change;
* the renewal of OAuth credentials in a process that lives several days;
* the behaviour of an idle process beyond the prompt cache duration. The cache
  lives on the server side: a late message rewrites the cache, whether the
  process is resident or not.

## Codex: `exec resume` per turn or `codex app-server`

* **`codex proto` and `codex mcp-server` no longer exist** in 0.159.3: the
  help does not list them, and they fall back to the interactive CLI.
  `codex mcp` only manages external MCP servers. The only resident path is
  **`codex app-server`** (marked *experimental*). It is a JSON-RPC server over
  stdio, or over `unix://` or `ws://` with `--listen`, whose schema is
  produced by `codex app-server generate-json-schema`.
* Checked by test, in a single process: `initialize` and `initialized`,
  `thread/start` (folder, model, `approvalPolicy`, `sandbox`, `ephemeral`),
  then **several `turn/start`** on the same thread:
  * the second complete turn took 4.4 s;
  * **`turn/steer`** during a turn (`expectedTurnId`): the message entered the
    turn after the command in progress, and the answer took both prompts into
    account;
  * **`turn/interrupt`**: the turn was closed immediately and the thread
    stayed usable;
  * usage arrives per model call in `thread/tokenUsage/updated` (`last` and
    `total`, `modelContextWindow`);
  * about 3 s separate the last message from `turn/completed`: this is
    probably the workstation's `Stop` hook, **not verified**.
* The protocol also exposes `thread/resume`, `thread/compact/start`,
  `thread/fork`, `thread/inject_items`, `thread/queue/changed` and
  `hook/started`/`hook/completed`.
* Per turn, `codex exec --json … resume <id>` cost **≈ 10 s** per message on
  the same model (11.4 s for the first turn, then 10.4 s and 9.9 s). The
  `turn.completed` of `exec` carries the **cumulative** usage of the thread
  (14,286, then 28,590, then 42,913 input tokens), which `cost.py` already
  handles.
* Leads not verified:
  * `codex queue --thread` ("queues a message for an existing session");
  * `codex --remote unix://…` (a native interface plugged into a running
    app-server). It could allow an `attach` **without stopping** the resident.

## DeepSeek: `dsh` headless and the `acp` profile

* The `agent` profile used by ameesh is the **headless** mode: one task, then
  exit (`--session-id` to resume). By test, **≈ 13.2 s per message**, even
  with reasoning off: profile startup and session restore.
* The shipped profile **`dsh --profile acp`** (package
  `@deepseek-ai/dsh-acp`, "ACP server for automation") serves ACP v1 over
  stdio. It does not need a new profile under `~/.dsh`. Checked by test:
  * `initialize` announces `session/list`, `session/resume` and
    `session/close`;
  * `session/new`: 2.6 s from launch;
  * `session/set_config_option` sets the model and the effort;
  * **several `session/prompt`** on the same session: about 1 s per short
    message;
  * `session/cancel`: `stopReason: cancelled` in 40 ms, and the session stays
    usable;
  * `session/update` carries `usage_update` (context `used`/`size`), but
    **not** billed tokens;
  * **a second `session/prompt` during a turn is refused** (`-32602 "a
    prompt is already in flight"`): the queue is therefore the client's job,
    and ACP has no equivalent of *steer*.
* The `dsh-sdk-jsonrpc-server` server (`session/prompt` queued,
  `session.event`) also exists. It is not mounted in a shipped profile:
  **not tried**.

## ACP harnesses in general

The ameesh bridge (`acp.py`) today does **one turn per process**:
`initialize`, `session/new` or `session/resume`, one `session/prompt`, then
exit. The protocol allows keeping the connection and chaining
`session/prompt` calls (ACP documentation, and the dsh test above). The "one
prompt at a time" rule applies to the session, and `session/cancel` is the
only interruption. The bridge can therefore become resident without changing
protocol.

## Summary

| Harness | Long session | Message during a turn | Interruption without killing | Usage per message | Compaction | Startup → next turn (test) |
|---|---|---|---|---|---|---|
| Claude Code | yes (stream-json input) | injected at the safe point (end of tool) | `control_request interrupt` | `result.usage` per turn; cumulative cost | `/compact` | ≈ 7 s resident; ≈ 11.3 s with `--resume` per turn |
| Codex | yes (`app-server`, experimental) | explicit `turn/steer` | `turn/interrupt` | `thread/tokenUsage/updated` | `thread/compact/start` | ≈ 4.4 s resident; ≈ 10 s with `exec resume` |
| dsh (ACP) | yes (`--profile acp`) | refused: client-side queue | `session/cancel` | context only | not exposed in ACP (internal compaction) | ≈ 1 s resident; ≈ 13.2 s headless |
| other ACP | yes (protocol) | refused or agent-specific | `session/cancel` | depends on the agent | depends on the agent | not measured |
| harness without a resident protocol | no | — | process stop (0018) | turn stream | rotation L11 | fallback "one turn per event" |

# Design: the resident agent

## Principle

A **resident** agent is an `exécuté` agent (driven by a runner, 0030) whose runner keeps
the harness process **open under a lease**. It passes the inputs to it one by
one and broadcasts its outputs. Availability is guaranteed by **ameesh**, not
by the process. A resident can be **dormant** (process stopped, lease and
session kept): the first message restarts it within a few seconds, by
resuming the session. A human no longer takes an agent to talk to it.

```
 humans (ameesh chat, Nexlink page) ───┐
 mail, events, alerts ─────────────────┼─► agent input queue ─► runner (lease) ─► resident harness process
 prompts (restart, adopt, resume) ─────┘     (priorities, authority)                       │
                                                                                          ▼
                subscribers ◄── conversation log + NOTIFY ◄── harness stream (text, tools, usage, end of turn)
                                         └─► readable thread (R12), ledger (cost per message)
```

## 1. The resident process under a lease

* **Capability declared by the descriptor**, in a new `ameesh.resident`
  block:
  * `{"protocol": "claude-stream-json"}`: same options as `command`, plus
    `--input-format stream-json`;
  * `{"protocol": "codex-app-server"}`;
  * `{"protocol": "acp", "launch": ["--profile", "acp"]}` for dsh, and the
    generic ACP bridge for the others.

  Without this block, or with `resident: false` on the agent's card, the agent
  stays at **one turn per event**.
* A **`ResidentWorker`** (a variant of `AgentWorker`) starts the harness when
  it claims the lease, opens or resumes the session (`--resume`,
  `thread/resume`, `session/resume`), then loops: it waits for an input,
  serves it, reads the stream until the end of the turn (`result`,
  `turn/completed`, answer to `session/prompt`), writes the end of turn, and
  starts again.
* **Lease renewed continuously**, whether or not there is a turn: this is the
  current turn heartbeat (`_heartbeat`), extended to the whole life of the
  process. The invariant does not change: **lease not renewed before expiry →
  harness group killed** (SIGKILL), even when idle. The handling of database
  failures from L72 (close retries up to a third of the lease) applies as is.
* **Automatic restart**: process dead, stream unreadable or protocol in error
  → restart, resuming the same session. The waits double after each failure;
  the series of quick failures of L48 stops the agent with a structured reason
  (0030 §4), and the alert goes to the manager.
* **Sleep**: process stopped cleanly, lease **kept**, status `dormant` visible
  in `ameesh list`. It is triggered:
  * after a configurable inactivity (30 min by default);
  * under host back-pressure (L31, L31b), which puts idle residents to sleep
    first;
  * before an account change (0027, L30);
  * when the harness binary has changed on disk.

  The wake-up happens at the next input.
* **No zombies.** The harness group is recorded as a resource
  (`_open_turn_resource`, extended to the life of the process); the runner
  cleans up at startup the groups of a dead runner; the systemd unit keeps
  `KillMode=control-group`.
* **Liveness probe.** For Codex, a light `thread/read`; in ACP, a
  `session/list`; for Claude, nothing, and we just use `poll()`. On top of
  this comes the inactivity watchdog during a turn, already present in
  `acp.py`.

## 2. Inputs: one queue, priorities, an envelope

All the inputs of an agent go through a **single queue**, read by the
`ResidentWorker` between two turns:

| Rank | Input | During a turn | Outside a turn |
|---|---|---|---|
| 1 | `!urgent` human message from an authorised human | **served at the safe point**: written to standard input (Claude); `turn/steer` (Codex); ACP: `session/cancel`, then a new prompt | immediate turn |
| 2 | human message | held until the end of the turn (choice a) | immediate turn |
| 3 | urgent mail from an authorised sender (0018) | interruption **without killing the process**, replacing the group kill | immediate turn |
| 4 | pending prompt (`restart`, `adopt`, `resume`, delegation) | end of turn | turn |
| 5 | mail, grouped events, alerts | end of turn | turn, grouped (C9) |
| 6 | inactivity nudge | — | turn if nothing else |

* Several human messages that arrive during a turn are **grouped** in the next
  turn, each with its author. Mail is never mixed with a human message in the
  same input: it follows in the turn after. A human who is waiting for an
  answer thus never sees it delayed by a batch of mail.
* **Input envelope.** ameesh writes the header of each input, never the
  sender:
  `[ameesh <token> — human smichea (verified: local identity) — manager]`
  or `[ameesh <token> — agent nexlink-orch — mail, without owner authority]`.
  The `<token>` is a random value drawn when the session opens, given to the
  agent in its startup prompt. The other agents do not know it. ameesh
  **neutralises**, in the body of agent messages and tool outputs, any line
  that imitates the header.
* **At-least-once delivery.** A human message is marked served only at the end
  of the turn that took it into account. If the lease is lost or the process
  dies, it is served again, with its identifier in the envelope so that the
  agent can recognise a duplicate.

## 3. Authority of the human message

* **An agent message never has the owner's authority.** Neither the rule, nor
  the persona prompt, nor the envelope lets an agent relay "the owner said".
  A quotation of a human by an agent remains an agent message.
* **Authority does not rest on the model.** The envelope informs the agent; it
  decides nothing. A committing action (merge, deployment, spending,
  irreversible action) always goes through **ameesh-approve** (0012) or
  through the capabilities of the card. A chat message, even verified, does
  not replace a signed approval.
* **Identity of whoever types in `ameesh chat`**:
  * **v1, local identity**: the Unix user who runs `ameesh chat` on a mesh
    host, linked to a `human:<id>` by the member's card in the canon (new key,
    for example `local_accounts: ["smichea@pc-smichea"]`). Otherwise the human
    is "unverified": their message is delivered, but without authority,
    without `!urgent` and without `!stop`. The right to write to the database
    remains the real barrier: whoever has the DSN can write. This is accepted
    as long as the database is private, and it is the limit of v1.
  * **With a server (L55) and OIDC (0033, lot D1)**: `ameesh chat` and the
    Nexlink page present an OIDC token; the server verifies the identity and
    the link to the canon, and **it is the server that writes** the message.
    Nobody inserts directly into the table any more.
  * **Who can talk to an agent**: its manager, its deputies and its superiors
    (0033 §7 and §9), with the manager's authority. The other members of the
    team: to be decided (question 2).

## 4. Outputs: conversation log and subscribers

* **Conversation log**: a table, with a migration to reserve after 0041.
  * Columns: agent, session, sequence number, type, author, bounded text, turn
    identifier, timestamp.
  * Types: `message_humain`, `entrée` (mail, event), `texte`, `outil` (name
    and summary, never the full output), `fin_de_tour` (cost, status), `état`
    (sleeps, wakes, restarts, rotation).
  * The agent's text is grouped in slices of about 500 ms: we do not write one
    delta per token.
* **NOTIFY** `ameesh_chat` with `agent` and `seq` (well under the 8,000-byte
  limit); clients re-read from their last `seq`. With the `psql` driver, which
  has no LISTEN, the client polls.
* **Readable thread (R12)**: each human message and each final answer of the
  agent are also written to the thread (`fil.py`). This is the auditable
  trace; the conversation log is its detail. Default retention is that of 0033
  §9: 90 days for the detail, one year for the thread.
* **Real-time view (0024)**: an agent's card shows its state (working, idle,
  dormant), its current conversation and an input field. It is the same source
  as `ameesh progress --json`.

## 5. Several humans at the same time

* **One conversation per agent, shared**: all participants see the whole
  thread, with the author of each message. This is the direct consequence of
  R12 ("no hidden channel") and of the fact that an agent has only one session
  at a time in v1. `ameesh chat` shows on entry who else is present and
  reminds that the conversation is shared.
* Concurrent messages are served in order of arrival and grouped if they fall
  during the same turn.
* A **private** conversation would need a separate session, hence a session
  fork, which belongs to the parallel sessions of v2 (0032,
  [study](https://github.com/manaty/ameesh/blob/conception/v2-persona-roles/docs/design/etudes/sessions-paralleles.md)).
  Out of scope here: question 3.

## 6. Rotation of a growing session

A resident session grows **faster** than a lot session: each chat exchange
adds to it. Giant sessions have already been seen, with 89 M to 408 M tokens
re-read. The rule of 0025 §3 (orchestrators: long session, rotated with a
summary) applies, with three additions:

1. **Continuous measurement**: current context per message, read from the
   stream (Claude `usage`; Codex `tokenUsage.last` and `modelContextWindow`;
   ACP `usage_update.used/size`), plus the **cumulative re-read** of the
   session.
2. **Two thresholds**:
   * **native compaction** (`/compact`, `thread/compact/start`) when the
     context exceeds about half of the window;
   * **rotation** with a resume summary (mechanism L11 and context ceiling
     L60), when the per-agent ceiling is reached or when the cumulative
     re-read exceeds an alert threshold (for example 50 M).
3. **When quiet**: never during a turn, and not within 2 minutes after a human
   message, except for a hard overrun. The rotation is announced in the
   conversation ("session rotated, summary") and traced in `persona_sessions`
   (L52). For Codex and ACP, the new session opens in the same process
   (`thread/start`, `session/new`); for Claude, the process is restarted on
   the new session.

## 7. Per-message accounting

* Each end of turn writes a ledger line, as today, with in addition the list
  of the **inputs served** (identifiers of human messages and mail).
  `ameesh cost report` can then say "what this question cost me".
* **Claude**: `result.usage` per turn, and the difference of the cumulative
  `total_cost_usd` (already done). **Codex app-server**: sum of the
  `thread/tokenUsage/updated.last` of the turn, or difference of `total`.
  **dsh over ACP**: no tokens in the stream, so a balance gauge (`balance`)
  and the `dsh` session log (L60), to the nearest turn.
* The overlap with **L71** (ledger accuracy) is strong: we must start from its
  state.

## 8. Fallback: one turn per event

For a harness without a resident protocol (descriptor without `resident`), or
if the resident fails several times in a row, the agent keeps the current
model: one process per turn, with `--resume`. `ameesh chat` works the same:
same queue, same conversation log (fed by the turn's stream adapter), same
envelope. Only the latency changes: 10 to 13 s more per message according to
the tests. `!urgent` keeps the preemption of 0018 there, which stops the
process and serves the message again at the head of the queue.

# `attach`, `adopt`, `resume` and database lot no. 12

* **`attach` becomes the exception**: the native interface for what the chat
  does not do (`/login`, harness settings, debugging, plugins). Its operation
  changes:
  1. the runner finishes the current turn, then **puts the resident to
     sleep**;
  2. the lease goes to `attach:<user>@<host>` as today;
  3. on exit, the lease returns to the runner, which **restarts the
     resident** on the same session.

  New safeguards against a repeat of 2026-10-07:
  * `attach` has a **maximum duration** (`--max`, 2 h by default, extendable);
  * it **alerts** the human who holds it when inputs have been waiting for
    more than 10 min;
  * at expiry, after notice, the interactive harness is stopped and the
    resident resumes.

  The chat stays open during an `attach`: messages accumulate and are served
  on resumption. For Codex, `codex --remote` towards the resident app-server
  could offer the native interface **without** stopping the resident (not
  verified).
* **`adopt`** (**closed** external session → runner) stays. The adopted agent
  becomes resident if its harness allows it. **`resume`** stays: it restarts a
  stopped agent and wakes a dormant resident. Neither changes meaning.
* **Database lot no. 12** ("adopt a live external session without doubling
  it"): **largely useless**. There is no longer a reason to make an agent live
  in a human session: you talk to it through `ameesh chat`. A narrow need
  remains: **refuse the duplicate**. If `adopt` or `resume` target a session
  still open in a live process (session file held or modified in the last 60
  seconds, harness process found), ameesh refuses and says to close the
  session first. Proposal: requalify no. 12 as this refusal alone, small in
  size, and abandon hot adoption.

# Cost and latency

| | One turn per event | Resident session |
|---|---|---|
| Startup per message (gap between the two modes) | about 4 s (Claude; `init` at 7 s, of which 3 s of hook), about 5.5 s (Codex), about 12 s (dsh headless) | none, except on waking from sleep |
| Measured latency of a short message | Claude ≈ 11.3 s; Codex ≈ 10 s; dsh ≈ 13.2 s | Claude ≈ 7 s; Codex ≈ 4.4 s; dsh ≈ 1 s |
| Tokens re-read per message | the whole context (server cache) | **identical**: the whole context (server cache) |
| Cache write | if the cache has expired (inactivity) | same: the cache lives at the provider, not in the process |
| Startup hooks, MCP, plugins | at every message | once per process |
| Host memory | nothing between two turns | one process per awake resident (hence the sleep) |

What the tests show:

* On Claude, the resident message re-read 26,145 cache tokens, and the message
  resumed by `--resume` in a new process 26,849. The **token cost does not
  depend on the mode**: it depends on the **size of the session**. The gain of
  the resident mode is latency, between 4 and 12 s per message, and the saving
  of startups (hooks, MCP servers). It is not a token gain.
* The cost risk is therefore **the growth of the session**, which the chat
  speeds up. Rotation (§ 6) and the context ceiling of L60 are conditions of
  adoption, not options.
* On DeepSeek, the resident mode changes the use: about 1 s per answer against
  13 s, which makes a dsh orchestrator truly conversational.

Not measured:

* the real cost of a resident orchestrator over a day;
* the effect of Claude's one-hour cache (a provider setting) on spaced-out
  conversations;
* the memory of an idle resident process.

The pilot test of lot L76 must record these three points.

# Risks and safeguards

| Risk | Safeguard |
|---|---|
| **Context leak between conversations**: what one human says, the agent can repeat to another | one conversation per agent, shared and announced as such (§ 5); no private conversation in v1; never an agent of another organisation (0033 §1); the rotation summary is visible in the conversation |
| **Injection by message**: agent mail or tool output that passes itself off as the owner | envelope written by ameesh with a session token; imitation of the header neutralised; authority never drawn from the text; committing actions via ameesh-approve; `!urgent` and `!stop` reserved to verified, authorised humans |
| **Growing sessions** | per-message measurement, native compaction, rotation with summary, L60 ceiling, cumulative re-read alert (0025 §4) |
| **Zombie** or orphan process | recorded process group, cleanup at runner startup, `KillMode=control-group`, liveness probe |
| **Lease loss** | same invariant as today (group killed); unacknowledged human message, hence served again; takeover by another runner on the session |
| **Stuck resident** (endless turn, silent protocol) | inactivity watchdog; native interruption, then stop; L48 failure series; pushed alert |
| **Host memory** | sleep after inactivity; residents count in back-pressure (L31b) |
| **Unstable protocols** (experimental `app-server`, internal `control_request`) | versions pinned in the descriptors; contract tests per harness; automatic fallback to one turn per event |
| **Forgotten `attach`** | maximum duration, alert, automatic resumption |
| **Unverified human** | delivered without authority; `!urgent` refused; server and OIDC in v2 |

# Proposed decision

Text ready to adopt (file `decisions/0035-agent-resident.md`):

```markdown
---
type: Decision
title: "The resident agent: you talk to an agent, you do not take it"
description: "An executed agent keeps, when its harness allows it, a harness process open under a lease; humans talk to it through ameesh chat then the Nexlink page; a human message is served at the end of the turn, !urgent at the safe point; attach becomes the exception; fallback to one turn per event."
status: proposed
tags: [agent-resident, conversation, chat, attach, bail, autorite]
decided_by: human:smichea
decision_date:
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T18:00:00+02:00" }
---

# Decision

1. **The agent stays driven by ameesh.** An `exécuté` agent (driven by a runner, 0030)
   whose harness declares a resident protocol (Claude Code stream-json, Codex
   app-server, ACP) keeps a harness process open under a lease, renewed
   continuously. Otherwise, it stays at one turn per event. Opening or closing
   a conversation changes nothing for the agent.
2. **You talk to an agent.** `ameesh chat <agent>` (terminal) first, then the
   Nexlink page of the real-time view (0024); Slack later. An agent's
   conversation is single and shared by its participants, and its exchanges
   go to the thread (R12).
3. **Order of service.** A human message received during a turn is served at
   the end of the turn. `!urgent`, from an authorised human, is served at the
   safe point (end of the tool call in progress, or native interruption).
   Mail, events and alerts come after human messages.
4. **Authority.** ameesh writes the header of each input. An agent message
   never has the owner's authority. A verified human message (local identity
   linked to the canon, then OIDC, 0033) informs the agent but does not
   replace an approval (0012).
5. **Availability.** A resident can sleep (inactivity, host pressure, account
   change): lease kept, wake-up at the first input. It is restarted
   automatically on its session; after repeated failures, it is stopped with
   an alert to the manager (0030).
6. **Sessions.** Per-message measurement, native compaction, rotation with
   summary when quiet (0025, L60). Cost is counted per message served.
7. **`attach` becomes the exception** (native interface: login, settings,
   debugging). It puts the resident to sleep, has a maximum duration, alerts
   when inputs are waiting, and hands control back to the resident. `adopt`
   and `resume` stay. The adoption of a live external session is reduced to
   refusing the duplicate.

# Consequences

- Lots L75 to L83 (resident-agent study); a migration to reserve after 0041.
- Overlaps: L72 (lease during a database failure), L61 (fast CLI, LISTEN),
  L60 (context ceiling), L71 (ledger), L31b (pressure), 0024 (real-time view),
  D1 and L55 (OIDC, server).
```

# Lot plan

Sizes: P (small, ≤ 1 agent-day), M (medium), G (large).

| Lot | Content | Depends on | Size |
|---|---|---|---|
| **L75** — resident protocols | `ameesh.resident` descriptor block; three protocol drivers (Claude stream-json on input and output with `control_request`; Codex app-server: `thread/*`, `turn/start`, `turn/steer`, `turn/interrupt`; multi-prompt ACP, including dsh `--profile acp`); contract tests with simulated harnesses; pinned minimum versions | — | M |
| **L76** — resident runner | `ResidentWorker`: startup, continuous heartbeat, end of turn, restart with resume, sleep and wake-up, `dormant` state, group cleanup; pilot test on one agent (memory, cost of a day) | L75, **L72 merged** | G |
| **L77** — input queue and authority | single queue and priorities; table of human messages (migration); token envelope and neutralisation; service at end of turn; `!urgent` at the safe point; 0018 preemption without killing the process; at-least-once delivery; local identity (`local_accounts`) | L76 | M |
| **L78** — conversation log | table and `ameesh_chat` NOTIFY; text grouping; write to the thread (R12); retention; export in `progress --json` (0024) | L75 | M |
| **L79** — `ameesh chat <agent>` | terminal client: history, real time (LISTEN, otherwise polling), input, `!urgent`, `!stop`, participants present, agent state; also works at one turn per event | L77, L78, **L61** | M |
| **L80** — cost per message | inputs served on the ledger line; app-server and ACP usage; `cost report` per message | L77, **L71** | P |
| **L81** — resident rotation | continuous measurement, native compaction, rotation when quiet inside the process, announcement in the conversation, `persona_sessions` | L76, **L60** | M |
| **L82** — `attach` redefined | put to sleep then hand back to the resident, `--max`, alert for waiting inputs; `adopt` and `resume` wake a resident; duplicate refusal (lot no. 12 requalified) | L76 | P |
| **L83** — conversation in the Nexlink page | agent card with conversation and input in the 0024 view; OIDC identity; write by the server | L78, L79, D1 (0033), L55 | M (plus a Nexlink ticket) |

Recommended order:

1. L75 and L78 in parallel;
2. L76, once L72 is merged;
3. L77;
4. L79, L80, L81 and L82 in parallel;
5. L83 with the v2 server.

The first resident agent is the orchestrator, the one we talk to most.

**Overlaps to flag:**

* **L72 (database failure, in progress)**: a resident lives under a lease
  **even when idle**. A failure longer than the lease margin therefore kills a
  process that was doing nothing. L76 must take over the heartbeat of L72
  without duplicating it and start only after its merge. To discuss: a longer
  TTL for dormant residents, whose process has nothing to protect.
* **L61 (fast CLI)**: `ameesh chat` needs a kept connection and LISTEN, hence
  psycopg, which L61 makes mandatory. With the `psql` fallback, each request
  costs about 1.5 s towards a remote database: the chat should refuse this
  mode or switch to polling.
* **0024 (real-time view)**: the conversation log is a new event source for
  the view. The `dormant` state and the conversation appear on the agent's
  card. No change to the decision.
* Others: **L60** (context ceiling: the resident's rotation relies on it);
  **L71** (ledger); **L31b** (host pressure: residents count in it); **0032**
  and parallel sessions (private conversation = fork, later).

# Questions for the owner

1. **Which agents become resident by default?** Proposal: orchestrators and
   permanent-role agents. Build and review agents stay at one turn per event:
   they mostly serve mail, and a session per lot (0025) fits poorly with a
   long conversation.
2. **Who can talk to an agent?** Manager, deputies and superiors only, or
   also any member of its team, without authority?
3. **Shared conversation only**, or should private conversations (session
   fork, v2) be planned for now?
4. **Inactivity duration before sleep** (30 min proposed) and **maximum
   duration of `attach`** (2 h proposed)?
5. **Codex app-server is marked experimental.** Adopt it right away for Codex
   agents, or wait and keep `exec resume` (≈ 10 s per message)?
6. **Database lot no. 12**: requalify it as "duplicate refusal" (L82) and
   abandon hot adoption?
7. **Local identity in v1**: accept that a Unix user linked to the canon
   counts as a verified human as long as the database is private, or reserve
   authority to OIDC (v2)?
8. **`!stop`** (interrupt without a new message): add it at the same
   authorisation level as `!urgent`?
