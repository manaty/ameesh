# agent-mesh v1 — Postgres mailbox and per-machine runner

This document covers items 1 and 2 of the spec (§5): what was built,
the decisions made, and what is deliberately left out. It is written to
be reviewed by another agent (cross-review) and to serve as the basis for
porting to an existing ticket orchestrator (M1: standalone
`agent-mesh` package).

---

## 1. Language: Python 3 stdlib + `psycopg` if present, otherwise `psql`

**Decision: Python 3**, with a `db.py` layer that talks to Postgres through
`psycopg` (v3) when it can be imported, and through the `psql` binary otherwise.

Why Python rather than Node:

1. **v0 is in Python.** `agent-mail.v0.py` defines identity (aliases,
   `AGENT_MAIL_NAME`, directory sanitization), hook rendering and the
   file mailbox format. Reimplementing these rules in a second language
   would have created two sources of truth for compatibility; in Python, the
   logic is carried over identically and tested against the v0 script itself
   (same JSON, byte for byte).
2. **Zero dependencies possible.** The stdlib covers JSON, subprocesses,
   sockets, threads, `pty`, `argparse`, `unittest`. With the `psql` driver, the
   package runs with **no** Python dependency at all — which matters on an agent
   PC where you don't install a venv lightly. Node has no Postgres client
   in its stdlib: it would have needed `pg`, hence a `node_modules` to
   maintain.
3. **A single runtime for the three harnesses.** The runner is a local service:
   Python is already present wherever `agent-mail` v0 runs (it is the same
   interpreter), and `psql` is present wherever Postgres is used.
4. **`psycopg` remains the nominal path** when it is there (installed by
   the orchestrator or by a venv): direct connections, native LISTEN/NOTIFY,
   no process per query. It is an accelerator, not a prerequisite.

The Node alternative had a real advantage (the Codex harness and `jq` are already
in the npm ecosystem), but it loses on v0 compatibility and on
the lack of a stdlib Postgres client.

---

## 2. Data model (`0001_init.sql`, `0002_overview.sql`)

Versioned migrations `NNNN_nom.sql`, applied once, SHA-256 fingerprint
recorded in `schema_migrations`, advisory lock
(`pg_advisory_xact_lock`) so that two runners starting together don't
step on each other, and **immutability**: modifying an already-applied migration
is an error, not a silent update (you must add a new one).

### `agent_registry` — presence and lease

`name` (key, same grammar as `agent-mail` v0: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`),
`chantier`, `harness`, `host`, `cwd`, `session_id`, `status`
(`idle|queued|running|blocked|stopped|dead`), `status_text`, `model`,
`budget_usd`, `spent_usd`, `turns`, `pending_prompt`, `last_error`,
`lease_owner`, `lease_expires_at`, `lease_epoch`, `last_turn_at`, `last_seen`.

Two design points:

* **`pending_prompt`** — the v1 equivalent of `nexlink-agent start <nom> … "consigne"`:
  the prompt waits in the registry, and the runner consumes it atomically.
  The `RETURNING` of an `UPDATE` returns the *new* value (hence `NULL`): the
  consumption goes through a `taken … FOR UPDATE` CTE that captures the old
  value before clearing it. (Bug caught by the tests.)
* **Partial `upsert`** — a hook or a `status` must not
  overwrite what they don't know. The `chantier`, `harness`,
  `host` columns are therefore updated with `coalesce(nullif(%s,''), colonne)`, and not
  with the default value of the proposed row. (Second bug caught by the
  tests: a `send` reset `harness` to `'other'`.) Since 2026-10-11, a
  `send` no longer touches the registry at all: it used to register the recipient,
  which created a phantom agent for a mistyped name and overwrote the host
  of an existing agent with the sender's (see §6, "Mail to an
  unknown or stopped agent").

### `agent_mailbox` — durable messages

`sender`, `recipient`, `body`, `kind` (`request|reply|notify`), `payload jsonb`,
`work_item_id`, `status` (`pending|delivered|answered`), `created_at`,
`delivered_at`, `signature`, `signature_key`, `slack_ts`, `host`, `meta`.

This is the list from spec §5.1 (`from, to, body, created, delivered,
signature`) enriched with the columns of an existing ticket orchestrator
(`kind`, `payload`, `work_item_id`, `slack_ts`) so that the same table serves the
following transports (R6: Nexlink, Slack). `sender`/`recipient` carry
`from`/`to`, which are SQL reserved words.

An **undelivered** message is `delivered_at IS NULL`; the partial index
`agent_mailbox_unread_idx (recipient, id) WHERE delivered_at IS NULL` serves the
hot query. Delivery is done by the harness hook (as in v0) or by
the runner at the end of a successful message turn: a message is never
lost if the turn fails.

### `agent_mesh_overview` view

One row per agent with lease, unread count and budget: this is the basis of
`agent-mail list` today and of `mesh list` (item 5) tomorrow.

---

## 3. Agent leases (R3, R5)

An agent is **pinned to a host** (v1: harness sessions are
local files). The lease is the only authorization to run.

* **Claim** — `UPDATE agent_registry SET lease_owner=%s, lease_expires_at=now()+ttl,
  lease_epoch=lease_epoch+1 WHERE name=%s AND (lease_owner IS NULL OR
  lease_owner=%s OR lease_expires_at < now()) RETURNING …`. Under READ COMMITTED,
  an `UPDATE … WHERE` re-evaluates the condition after the row lock: two
  concurrent runners cannot win the same agent. Tested with 2 and 4
  simultaneous connections, then with **two `agent-runner` processes**.
* **Time fence** — `renew`, `begin_turn` and `take_pending_prompt` all
  require `lease_expires_at > now()`: an expired lease is not extended, does not
  start a turn and does not consume a prompt. The runner must
  re-`claim` (new epoch) to continue; `end_turn` remains fenced by
  `lease_owner` + `lease_epoch`, so that a turn started under a valid lease
  can be recorded.
* **Lock first, recheck after** — lease operations (`claim`,
  `attach_claim`, `renew`, `take_pending_prompt`, `begin_turn`) take the
  row lock (`SELECT … FOR UPDATE`) then recheck the deadline with
  `clock_timestamp()` **in the statement that writes**: a lock waited for
  past expiry refuses the operation, even if the holder only did
  a `SELECT … FOR UPDATE` without modifying the row (EvalPlanQual does not re-evaluate
  a `WHERE` without a new row version). `claim` applies the same rules as
  `claimable` — expired ephemeral, lead resolved when required, canon
  predicate — in the same statement, which closes the `claimable` →
  `claim` race. The marker of a live turn is `status = 'running'` with a live
  lease, not `current_prompt` (which a mail or event turn does not
  fill): `ameesh attach` never takes over a running turn, nor an
  already open attach session, and its own **deadline watchdog** kills the
  interactive harness if renewal fails (real SQL error) and the
  lease expires.
* **Renewal bounded by the deadline, not by a fixed delay** — if
  the deadline has already been reached on wake-up, the harness is stopped **without even
  calling the database**; otherwise the `renew` call runs in a thread and is abandoned
  at the latest at `lease_deadline` (budget = exact remaining time, **no
  floor**: entered 20 ms before the deadline, the call only has 20 ms).
  The heartbeat wait between two renewals is bounded the same way
  (`wait_timeout` = exact remaining time, **no positive floor** either):
  a lease at +20 ms wakes the heartbeat at the deadline, which then cuts the
  harness, instead of sleeping 50 ms more while a replacement claims
  the agent.
  On lease loss, the group receives SIGKILL **directly** (no prior
  SIGTERM) and **before any logging**: a print+flush on this critical path
  let, under load, a replacement claim while the old harness was
  still alive (codex3 probe b731f4f). The direct child is then reaped:
  a zombie still counts as alive for `killpg(pid, 0)`, which made the
  wait last 2 s and logged a false "groupe toujours vivant" (group still alive).
  Even if the database no longer responds at all (response held by a proxy, for
  example), the heartbeat concludes the lease is lost and stops the harness at
  the time a replacement can claim the agent. `statement_timeout` remains
  the server bound, the `psql` client delay the subprocess bound.
* **Independent deadline watchdog, non-blocking logging** — a dedicated thread
  (`_deadline_watchdog`) only reads `lease_deadline` and `time.time`, then
  sets `lease_lost` and sends the SIGKILL as soon as the known deadline is
  reached. It writes nothing, calls neither the database nor a file and waits for
  no other thread: even if the heartbeat is blocked in a full log, a
  query or a write, the stop fires at the deadline (codex3 probe B5a-N,
  class of blocking I/O on this path). Logs on the lease-loss path
  (heartbeat, `terminate`) go through a bounded queue written by a dedicated
  thread: if the sink saturates, the message is dropped and the loss counted and
  reported, never waited for.
  **Inventory of the remaining I/O on the lease path, with their bound:**
  the `renew` call runs in a separate thread, joined at the latest at the remaining
  lease time (`call_budget`, floor 0), with server `statement_timeout`
  (30 s by default) and `psql` client delay (+5 s); heartbeat diagnostics
  go through the bounded queue (no waiting); on lease loss, `terminate`
  sends the SIGKILL before any logging, reaps the direct child and bounds
  the group wait to 2 s; the watchdog itself has no I/O. The only thread
  that can remain blocked is the log writer, which then only loses
  messages.
  **Inventory of the I/O on the `attach` path (same rules, codex3 probe of
  2026-10-04-0442):** the lease claim (`attach_claim`) and the
  renewal (`renew`) are database calls bounded by `statement_timeout`
  and by the client budget; renewal runs in the heartbeat thread,
  never in the stop decision; the main thread kills the group
  (`os.killpg`) **before** any logging; the deadline watchdog only reads
  `etat["deadline"]` and `time.time`, sets `perdu` (the stop signal) then
  logs; all attach logs — lease taken, interactive session,
  renewal failure, lease lost, deadline reached, harness cut, lease
  returned — go through the bounded `log_async` queue (no waiting); the
  publication of the `Popen` is rechecked afterwards (the watchdog may have concluded
  in the meantime); `release` only happens after the cut. The only synchronous
  outputs of `attach` are the CLI messages (`print` on stderr) emitted before
  any lease is taken (unknown agent, turn in progress, not claimable) or by the
  `--wait` loop without a held lease.
  The watchdog **remains active during a graceful stop** (`stopping`, 3 s
  SIGTERM grace): it only stops when the lease is released (`release_lease`)
  or once the loss has been consumed, otherwise a harness that ignores SIGTERM
  would survive past the deadline during the grace period (codex3 probe B5a-S). End of turn
  goes through the escalated stop (SIGTERM then SIGKILL), no longer through a bare
  `Popen.terminate()`; and a lease lost while reading is a direct
  SIGKILL. Finally, **the publication of the `Popen` is rechecked**: the watchdog
  may conclude between process creation and the assignment of `self.proc`
  (it then sees `proc=None`); without this recheck, a harness with no stdout
  would survive the lease until the end of the turn (codex3 probe B5a-P). The recheck
  also covers worker shutdown (`stopping`) and lease release
  (`watchdog_stop`): a late publication after a `shutdown()` that has returned
  the lease is killed, even if the old deadline has not been reached (codex3
  probe B5a-Q).
* **Bounded queries** — each connection sets a `statement_timeout`
  (`AMEESH_STATEMENT_TIMEOUT_MS`, default 30 s): a lease renewal
  that hangs (lock, slow query) is cancelled by the server; the `psql` driver
  adds a client delay (the same + 5 s) that abandons the subprocess if the
  server no longer responds at all. Migrations, for their part, lift the timeout for
  the duration of their transaction (`SET LOCAL statement_timeout = 0`) and bound their
  wait for a lock — table or row (`SET LOCAL lock_timeout = 5 s`; a migration that must
  wait longer sets its own, which wins): a migration queued behind an `ACCESS EXCLUSIVE`
  lock fails in seconds and stays pending, instead of holding every reader and writer
  of the table behind it. The same bound applies to a row lock (`select … for update`):
  a data migration that must wait for another transaction's row sets its own timeout.
* **Robust heartbeat** — a database outage during a turn does not leave the
  harness running without a lease: the heartbeat counts failures
  (`max(3, ttl/3)` second interval), and stops the process as soon as the
  database stays unreachable or the known lease deadline is reached. It
  **never sleeps past the deadline** (wake-up is bounded by
  `lease_deadline`), even if the nominal interval is longer. It covers the
  whole turn, from preparation (directory, harness, canon, accounting) to the
  harness's exit, not only the published process: a short lease is renewed
  during preparation instead of expiring before the `Popen` (#2544).
* **Harness stop** — `terminate` signals the **process group** (the
  harness is launched in its own session, the pgid is recorded at launch):
  its descendants do not survive. SIGTERM, 3 seconds of grace for a clean
  stop, then SIGKILL — except on lease loss, where the grace is zero: the
  replacement may claim the agent immediately, the old child must no longer
  run. Escalation does not depend on the **parent** surviving: if the parent
  dies on SIGTERM leaving a descendant that ignores it, the remaining group
  is killed anyway. `terminate` waits until the kernel has taken the SIGKILL,
  so that "stopped" means stopped.
* **Durable prompt (R5)** — `pick` no longer empties `pending_prompt`: the
  prompt is *moved* into `current_prompt` when the turn starts, cleared
  by `end_turn` **only if the turn succeeded**, and restored as pending
  in all other cases: failed turn (including killed harness), missing
  harness, vanished directory, change of hands — including when the same
  `--runner-id` takes back an expired lease — and **lease lost mid-turn**
  (closing no longer goes through `end_turn`, which would have cleared the prompt; and
  it does not touch the status of an agent a replacement may have taken over). If
  a prompt was already pending during an interrupted turn, **both
  are kept** (the old one then the new one, separated by a blank line).
  At worst a turn is replayed, never a prompt lost.
* **Expiry** — if the runner dies (SIGKILL, power cut), the lease expires;
  any runner on the same host takes it back (`lease_expires_at < now()`).
  `reap()` marks `dead` the agents whose lease expired mid-turn.
* **Loss mid-turn** — the heartbeat detects the loss, terminates the
  harness process, and the turn is not counted. Tested: lease stolen
  during a fake 12 s turn, interruption in under 7 s.

---

## 4. LISTEN/NOTIFY: what `psql` imposes

The `agent_mailbox_notify` trigger emits
`pg_notify('agent_mail', {"to": …, "id": …})` on every insert;
`agent_registry_notify` does the same on lease/status changes
(`agent_lease`). The runner listens on both channels and wakes the relevant
worker; polling (`--poll`, default 5 s) remains a safety net.

With `psycopg`, everything is direct. With `psql`, three psql behaviors were
discovered **in testing** and dictate the implementation:

1. `psql -c …` **never reads stdin** — a listener launched with `-c "LISTEN …"`
   then `-c "SELECT pg_sleep(…)` cannot stay interactive; it needs an
   invocation without `-c`, with the `SET search_path` and the `LISTEN`s sent on
   stdin.
2. blocked in a query (`pg_sleep`), psql **does not flush** notifications:
   they only come out at the end of the query — a one-hour `pg_sleep` therefore
   wakes nothing.
3. on a pipe, psql does not process notifications at all; it needs a
   **pty**, and it only prints notifications on the next loop iteration.

The `psql` listener is therefore: an interactive `psql` on a pty, a
`SELECT 1` heartbeat twice per second, and a filter on the line
`Asynchronous notification "canal" with payload "…"`. Wake-up latency
≈ 0.5 s. The details are commented in `db._PsqlListener`, and a
`doctor --notify-test` test goes through the full loop.

With `psycopg`, `Connection.notifies(stop_after=1)` **discards** unconsumed
notifications when the generator is closed: the listener drains the connection
into a buffer (`notifies(timeout=0)`) and returns them one by one. Here again, a
test (three messages → three notifications) caught the bug.

---

## 5. Runner (`agent-runner`)

* **One per machine**, it claims the agents with `host = <hôte>`, renews their
  leases and launches the turns. Service mode, or `--once` for the test bench.
* **Adapters**: command lines taken from `nexlink-agent.v0.sh`
  (`claude -p --output-format stream-json … --resume`, `codex exec --json
  resume`, `dsh --profile agent --json --session-id`), JSONL reading,
  capture of the session and the cost when the harness provides it
  (`total_cost_usd` for Claude, `cost_usd` for DeepSeek).
* **Turns**: pending prompt → unread messages → events → idle
  nudge (same texts as v0). The raw stream is logged to
  `~/.local/state/agent-mesh/<nom>/events.jsonl`, the session in the database **and** on
  disk (recovery after crash), stderr in `stderr.log`.
* **Events (C9, `0012_events.sql`)**: a `kind = 'event'` message wakes
  the agent like a message; a batch of events only wakes it once per
  `AMEESH_EVENT_COALESCE` seconds (default 120), except `payload.urgent`. The time
  of the last wake-up lives in the database (`agent_registry.last_event_at`): a
  restart does not reset the window.
* **Batched mail (L125)**: ordinary mail only wakes an idle agent
  at the end of a window (`AMEESH_MAIL_BATCH`, default 90 s, adjustable
  per agent), counted from the arrival of the oldest message; the turn takes
  everything that arrived in the meantime. An urgent message or one from a human wakes
  immediately; an acknowledgement, a copy (`--cc`) or a broadcast to
  "all" never wakes on its own: it is read on the next turn. See
  [EXPLOITATION.md](EXPLOITATION.md#courrier-regroupé-avant-le-réveil-l125).
* **`ameesh attach <agent>` (C9)**: takes the lease for an interactive session
  (`attach:<utilisateur>@<hôte>`), even on a live lease **with no turn in
  progress** — the old runner is fenced by the epoch at its next
  renewal, which suspends automatic claiming. A turn in progress
  is never interrupted (`--wait` to wait). The harness is launched
  interactively on the **same session** (without `-p`/`exec`/`--json`), the lease is
  renewed as long as the session lives, the harness is killed if the lease is lost, and
  the lease is returned on exit.
* **Priority interrupt (0018, R19)**: an undelivered message marked
  `payload.urgent` from an **authorized sender** stops the
  running turn (brief SIGTERM then SIGKILL), puts the turn's prompt back as
  pending, and `pick()` serves the priority message **first** on the next turn,
  on the same session. Authorized senders (#795): the owner (`human:*`) always;
  `ameesh` only for an event **verified in the database** (the end of CI of one
  of the agent's work items — the run is recorded, the lot is assigned to the
  agent, and the mail is the one CI posted for that run), so an imitated
  `--from ameesh` does not interrupt; agents declared in
  `AMEESH_INTERRUPT_SENDERS` (the "interrupt" capability of a canon card will be
  added). An urgent message from an unauthorized sender interrupts nothing
  (this is logged): it is delivered like a message — since L125, without
  waiting for the batching window; the thread keeps a record of
  the interruption and its reason.
* **Session rotation (0018)**: when the session exceeds the rotation threshold
  of its harness or a turn lasted more than `AMEESH_SESSION_MAX_TURN_SECONDS`
  (default 3,600 s), after at least `AMEESH_SESSION_MIN_TURNS` turns (default
  3) and **never during a turn**, a summary turn is played in the session, the
  summary is written to the thread (R12), the old session id is kept in
  `<état>/<agent>/session-history.jsonl`, then the session is forgotten: the
  next turn opens a fresh session prefixed with the summary. The threshold is
  compiled from the canon (#4448): `session.max_tokens` (common) and
  `session.max_tokens_by_harness` (`claude`, `codex`, `deepseek`) at the
  applied commit; without that block — or with an unreadable canon — the
  executor's common `AMEESH_SESSION_MAX_TOKENS` (default 150,000) applies, and
  no harness-specific value is ever hard-coded. Codex publishes a **cumulative**
  usage per turn: its session counter is the last reading of the thread, not
  the sum of the readings. The summary is the
  **entire** final answer of the summary turn (L218), never the end of its
  display; beyond `adapters.SUMMARY_MAX_CHARS` (16,000 characters), its
  head is kept, cut at a paragraph end, and a note, included
  within the bound, states the cut. An empty final answer (for Codex: the
  agent's last message empty) is not a summary: the rotation is
  cancelled, and the account cutover uses the deterministic brief (L39).
  The latency threshold has a floor of **twice the effective turn cap**
  (`turn_max_seconds`, the agent's own setting included): a turn allowed to
  last 1,800 s does not, by itself, make the session be forgotten; a higher
  explicitly configured threshold is kept, and with no turn cap
  (`turn_max_seconds=0`) the configured threshold applies alone
  ([EXPLOITATION.md](EXPLOITATION.md#plafonds-pendant-le-tour-l105)).
  The size is the session's cumulative `input_tokens + output_tokens` over
  its turns, read from the usage in each harness's stream (keys normalized by
  `adapters.usage_tokens`; DeepSeek publishes usage per step). The cache
  convention is **not uniform** (#4448): Codex's input already includes
  cached tokens, while Claude and DeepSeek report cached input separately and
  written cache is excluded, so the same token count does not mean the same
  thing across harnesses — hence the per-harness thresholds compiled from the
  canon. **Context
  ceiling (L60)**: if the context of the last model call (the session size:
  input + cache read + cache written) exceeds `AMEESH_CONTEXT_MAX_TOKENS`
  tokens (default 15 M, adjustable per agent with `ameesh set <agent>
  context_max_tokens=…`), the same rotation takes place before the next turn,
  with no minimum number of turns. The sum of a turn's re-reads is not the
  measure (#2336): it grows with the number of steps of the turn, not with the
  session (see
  [EXPLOITATION.md](EXPLOITATION.md#plafond-de-contexte-l60)).
* **Moved working directory (0018)**: at registration, the git identity of the
  directory (common repository + branch) is written to
  `<état>/<agent>/worktree.json`. If the cwd has disappeared, a **unique** candidate
  is searched for under `AMEESH_WORKTREE_ROOTS` (default `~/development`), at bounded
  depth — nested worktrees (`.claude/worktrees/<nom>`) are
  traversed, dependencies and `.git` are not —; it is adopted (registry
  updated, thread notified) if it is not already the cwd of another agent.
* **Single stop chokepoint** — every harness stop (lease
  deadline, lease lost, runner stop, priority preemption, and tomorrow
  the L13 budget guard) goes through `stop_group_now(raison)`: the signal fires
  **before** any logging, logging goes through the bounded queue, and a
  late `Popen` publication is caught by a single recheck
  (`kill_if_stop_requested`). A new stop path therefore inherits the same
  probes; the **parameterized** test suite (`StopTriggersTest`) replays for
  each trigger the three scenarios — published harness, saturated log,
  late-published `Popen`.
* **Priority preemption latch (#2729)** — a preemption request is consumed at
  a turn's start only when it is **older than that turn's selection** (the
  generation `pick()` reads under the request lock before selecting; an urgent
  turn reads it again after, since it serves that request). A request received
  after selection, including during the possibly slow preparation, still stops
  the selected turn. Left un-consumed, the latch used to kill each following
  turn within a second (185 turns on 19/10). A preempted turn writes its series
  as a suffix of `status_text`
  (`turn interrupted: priority message [preemptions x5 since …]`); five
  consecutive preemptions without a finished turn raise the
  `preemption_streak` alert (read in `ameesh alerts`, not pushed by default).
* **`canon sync` by the runner (spec §4.4)**: when a canon is configured,
  the runner launches `canon sync` for its host **at startup**, then
  periodically (`AMEESH_CANON_SYNC_INTERVAL`, default 300 s; 0 = only at
  startup). The sync runs in its own thread with its own connection; a
  failure (unreadable canon, git, database) is logged through the bounded queue and
  **never stops the runner or a turn**: an unreadable canon closes
  claiming via `canon_state`, which L2 already does. With no canon configured,
  nothing is launched.
* **Resume path**: the session is re-read from the registry, otherwise from
  the `session` file.
* **Harness binaries**: `AMEESH_<HARNAIS>_BIN`, then
  `AMEESH_BIN_DIR/<nom>`, then the PATH. This is what lets the test bench
  use fake harnesses without ever calling the real ones.
* **Stop**: SIGTERM/SIGINT → the leases are returned (the agent restarts right away
  elsewhere); SIGKILL → natural expiry.

---

## 6. v0-compatible `agent-mail` CLI and fallback

Same commands, same texts: `send`, `inbox`, `list`, `status`, `alias`,
`whoami`, `statusline`, `hook`, plus `migrate` and `doctor`. Aliases and
identity are **shared** with v0 (`~/.config/agent-mail/aliases.tsv`,
atomic write via `os.replace`).

Hooks produce the **same JSON** as v0 (including the `Stop` decision
and the cap of 3 nudges, counted locally in
`~/.local/state/agent-mesh/hooks/<nom>.stops`): a test compares the output of the
v0 script and that of v1 for the four events.

**Fast path of the hooks and the status line (#1604).** The `agent-mail`
console script goes through `ameesh.entry`, which serves `statusline` and
`hook <harness>` without importing `ameesh.cli` (database, psycopg). A local
mailbox marker (`<state_dir>/mail/<agent>.json`) records the outcome of the
last COMPLETE read: a `PostToolUse` hook skips the database only when the
marker is fresh (`AMEESH_HOOK_FAST_TTL`, 15 s by default) and says there is
nothing to deliver. `Stop`, `SessionStart` and `UserPromptSubmit` always go
through the database; a local send (`send`, `forward`, `sweep`) invalidates the
recipient's marker; the status line is cached for a few seconds
(`AMEESH_STATUSLINE_TTL`, 5 s). The fast path delivers no mail and writes no
registry: it cannot confuse an identity or a session, at worst it delays a
delivery, which the next complete pass (at the latest the `Stop` hook) catches
up.

**Identity tied to the lease, never to the directory (mesh-design incident of 2026-10-09).**
A session only has an identity if `AGENT_MAIL_NAME` is set; if
`AMEESH_RUNNER_ID` and `AMEESH_LEASE_EPOCH` are set too (it is the
runner that sets them for the harness it launches), the lease must be **live and
held by this runner at this epoch** — otherwise the hook consumes nothing and nothing
is delivered. Without these variables, a session reading in another
agent's worktree no longer takes its mail: the directory gives no identity,
only a display name for the status bar and `whoami --cwd`
(explicitly marked "diagnostic, non autoritaire" (diagnostic, not authoritative)). `send` then requires
`--from`, `status` and `whoami` refuse, and `inbox <nom>` remains a read
without consumption.

**Git identity of a session (#834).** The runner sets the four variables
`GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and
`GIT_COMMITTER_EMAIL` for every turn, and for an `attach` session, from the agent
name of the lease (`<name>@ameesh.invalid`, the address the mediated device
already uses). Git environment variables override configuration, so a repository
whose `.git/config` is shared by all its worktrees can no longer attribute one
agent's commit to another (incident of 2026-10-13, where a resumed session
without these variables committed under the name of the agent that had written
`user.name` first). No git configuration is read or written for this.

**External sessions: explicit binding (L41, decision 0030).** An interactive
session launched by a human has neither a lease nor `AGENT_MAIL_NAME`. It only has
an identity if it is **bound**: `ameesh mail bind <agent> --session
<id> --harness claude|codex|deepseek [--pid N]` (`session_bindings` table,
migration 0034; at most one active binding per host, harness and session).
The hook finds it through the session identifier the harness passes it in
JSON; if the binding carries a PID, it must be an ancestor of the hook
process — and, L46 (migration 0036, `pid_start`), the SAME process: its start
time, recorded at binding, is rechecked; a recycled PID
yields nothing (re-bind with `--pid`). L63 (migration 0048, `pid_started_at`):
this time is in epoch seconds, read by the platform layer
(`ameesh.platform`) on all OSes; an old `pid_start` (Linux clock
ticks) is still read by conversion, on Linux only. If the OS
provides neither ancestry nor start time, `bind --pid` is refused and the
hook delivers nothing (never a silent check). Source `session`: never tied to a lease, hence subject to rule L36
(it does not replace an agent's recorded session), and with no effect as soon
as the agent holds a live lease (it is then driven by the runner; `bind`
refuses it outright). `whoami`, `send` and `inbox`, run within the session,
find it through their ancestry; `whoami` shows the source and the binding.
Without a binding, the hook delivers nothing and writes nothing. `bind --import` takes over
the local bridge file (`external-session-bindings.json`), `unbind` revokes,
`bindings` lists; every binding and revocation is written to the thread.

**Without a lease, never on the row of an `execute` agent (L46).** A hook without
a lease (source `explicit` or `session`) only advances `last_seen` on the
row of an `execute` agent: no host, harness, session, directory or
chantier (a single statement, `registry.upsert_unleased`). An unknown agent
is born `externe`; the row of an `externe` agent follows its session (rule L36).
Source `explicit` (`AGENT_MAIL_NAME` without `AMEESH_RUNNER_ID`) receives
nothing while the agent holds a live lease — refusal stated by the hook, and
enforced under lock by the reservation itself for any identity without a
lease. `ameesh mail bind` refuses an `execute` agent unless `--force` ("agent
mené par l'exécuteur ; une session externe lui volerait son courrier" (agent driven by the runner; an external session would steal its mail)) and
registers an unknown agent as `externe`.

**Mail to an unknown or stopped agent (2026-10-11).** With the database,
`send` refuses (code 2, nothing is deposited) a recipient absent from the registry
— without creating an agent, listing close names —, a stopped recipient
(`stopped`) unless `--queue`, and a stopped sender identity (with
the identity bound to the session, `whoami`); "all" skips stopped agents.
Undeliverable mail raises the `mail_undeliverable` alert, and `ameesh
mail forward <ancien> <nouveau>` re-delivers a dead mailbox to a live agent,
keeping the original sender and date. The executor's cleanup also sweeps dead
mailboxes (`ameesh mail sweep`, L976): a stopped agent's mail goes to its bound
successor, and mail nobody can read expires after `mail_dead_letter_days`
(7 by default), with a thread trace — the alert clears without a human.
Declared human mailboxes are excluded from dead-mail alerts and expiry (#4519);
unknown `human:*` recipients remain visible as configuration errors and their
ordinary mail can expire. All `human:*` mailboxes are excluded from forwarding.
Structured decision requests never expire or get forwarded, even if addressed
to an agent. Read their queue with `ameesh decisions`.

To repair a human decision accidentally forwarded by an older version, a bound
agent runs `ameesh decide repair-forward <original-id>`. The command atomically
restores the original to pending, retaining its question, owner and signature
requirements, and withdraws its copy. A copy the requester already withdrew is
accepted and left untouched, so its withdrawal trace is kept (#4576). It refuses
a request answered or withdrawn at its origin, an answered copy, changed copies
and forwarding chains; it never creates an answer or approval. The audit remains
in the original message metadata.

Details:
[EXPLOITATION.md](EXPLOITATION.md), "Décisions du propriétaire" and "Courrier en
souffrance" (undeliverable mail).

**Hooks: write first, mark after.** The JSON is written to stdout and
flushed (`flush`) *before* marking the messages delivered; if the harness has closed its
input (BrokenPipe), delivery does not happen and the mail remains for the next
turn.

**Hooks: never in a subagent (L133, 2026-10-11).** Claude Code
also fires ordinary hooks (`PostToolUse`…) in a session's subagents
(Agent/Task tool, in-process teammate), with the
`session_id` of the main session and the agent's environment; Codex
does the same for its subagents (`spawn_agent`). The hook then delivered the
agent's mail to the subagent, which ignored it, and marked it
delivered: the main session never received it (at least five messages
lost on 10/10 and 11/10). These two harnesses add `agent_id` to the input of a
hook fired in a subagent, and only there: with `agent_id` present and
non-empty, the hook exits immediately, without reading, delivering, marking or writing anything
(registration, title, counters), and without displaying anything (a "courrier
en attente" (mail waiting) line would repeat on every subagent tool call and would cost a
database round trip). The mail waits for the next hook of the main
session. `agent_type` alone does not count: Claude Code also sets it on
a main session launched with `--agent`. Known limitation: the DeepSeek harness's (`dsh`)
Claude Code hook bridge only passes `agent_id` to
`SubagentStart` and `SubagentStop`; according to its code, a `dsh` subagent
fires ordinary hooks under its own `session_id`. A session bound
with `bind` therefore delivers nothing to it, but an agent driven by the runner
(identity through the environment) remains exposed: follow-up to handle separately.

**Fallback**: if Postgres is unreachable and `AMEESH_BACKEND=auto`
(default), the CLI writes to the v0 file mailbox
(`~/.local/state/agent-mail`, same file names, same JSON) and prints a
readable warning on stderr. In `pg` mode, it fails outright. A
schema that is present but not migrated is **not** a silent fallback: the message
says to run `agent-mesh migrate`. Hooks, for their part, never make the
agent fail: any error → exit 0 with nothing.

---

## 7. Test bench

`scripts/pg-up.sh` starts `agent-mesh-pg` (`postgres:17`, port published
only on `127.0.0.1:55432`, named volume). `scripts/test.sh` runs the
suite twice: `psql` driver, then `psycopg` driver in a local venv if
installation is possible. Each run takes a throwaway `agent_mesh_test_*`
database (#1992); when the connection profile only covers `agent_mesh`, the
script derives a private `PGPASSFILE` for the bench's databases from the
existing entry and removes it on exit (#2170). During the two runs that private
profile is the only one libpq reads: the user's other profile entries are
masked (wanted — the bench's databases share the entry's host, port and role).

63 `unittest` tests (stdlib), a throwaway Postgres schema and a throwaway disk
state per class: migrations, mailbox, notifications, leases (races with 2 and
4 connections, expiry, fencing), **two concurrent runners competing
for a lease** (real processes, a single turn, codes 0 and 3), wake-up on
NOTIFY in service mode with 60 s polling (so NOTIFY is mandatory), session
resume for the three harnesses, equality of the hook JSON with v0, and file
fallback.

No test launches a real harness: the binaries in `tests/fakebin/`
emit the same JSONL streams and log their argv. No test touches
`~/.local/bin`, `~/.local/state/agent-mail` (outside temporary directories) or
real sessions.

---

## 8. Accepted limitations and follow-ups

* **No hookup to the real agents** tonight: this is a test bench. Going
  to production on this PC will be done by installing the package in a venv
  (or by launching `bin/` wrappers), migrating the `public` schema, then
  replacing `nexlink-agent` with `agent-runner` — tool by tool.
* **An agent stays pinned to its host** (v1): moving a session is a
  separate workstream (R1/R5).
* **Owner authority (R4)** and **`mesh list`** are covered in
  [`V1-AUTORITE-MESH.md`](V1-AUTORITE-MESH.md) (items 3 to 5), with
  `signing.py`, `authority.py` and `mesh_cli.py`.
* **Budget (L13, 0019)**: each turn writes a `turn_costs` row
  (per-turn accounting, L12). A turn's accounting work is marked
  **in the database** (`spend_pending`, migration 0025) before launch: a row
  that is present, unreadable or inconsistent at restart = **accounting pause**
  (fail-closed), the row only being cleared after a successful write; if the
  marker cannot be set, the turn does not start. The billed model is
  the **turn's source**: the model announced by the harness stream, otherwise the one
  frozen at launch in the marker; if it is unknown, the most expensive known rate
  of the family is billed (`deepseek-pro` for DeepSeek), never the default.
  DeepSeek does not announce its model in the stream: without a setting, the runner
  passes it the descriptor's default model (L60), which becomes the turn's
  frozen model.
  Before each turn, the guard queries `CostBook.over`: sliding hourly cap
  on **pay-per-token** usage (`AMEESH_BUDGET_USD_PER_HOUR`, default
  $10/h; the Claude/Codex plan does not count toward this sum) and the plans'
  pacing guard (`min(90 %, part écoulée + 10 points)`, gauges read at the
  source). The decision is fresh on every turn; only the status and the trace
  are rate-limited (`AMEESH_BUDGET_CHECK_INTERVAL`, default 30 s). A paused agent
  is `blocked` with the reason in `status_text` (visible in `ameesh list`)
  and the thread keeps a record of it. `budget_usd` / `spent_usd` remain the historical
  per-agent counters; `ameesh set <agent> model=… effort=…` sets the model
  and effort, applied on the next turn (options declared per harness, YAML
  patch for DeepSeek).
* **Porting (M1)**: the package is standalone (`pyproject.toml`, no import
  from an existing orchestrator). The tables and the
  `kind`/`payload`/`work_item_id` columns are ready for a takeover on the
  orchestrator side; it remains to decide who owns `work_items`.
