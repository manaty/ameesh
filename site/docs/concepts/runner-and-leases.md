# Runner and leases

## One runner per host

`ameesh run` (alias `agent-runner`) is the host's runner. It claims the agents
placed on its host, renews their leases, and starts a **turn** in the agent's
harness when there is something to do. It wakes on Postgres `LISTEN/NOTIFY`,
with a fallback poll (`--poll`, default 5 s).

A turn is triggered, in this order, by: a pending prompt, unread messages,
events, or an idle nudge after `--idle-nudge` seconds of inactivity
(default 1200).

Three harness adapters ship with v1:

| Harness | Command line (simplified) | Resume |
|---|---|---|
| Claude Code | `claude -p --output-format stream-json …` | `--resume <session>` |
| Codex | `codex exec --json …` | `codex exec … resume <thread>` |
| DeepSeek Harness | `dsh --profile agent --json …` | `--session-id <session>` |

The runner reads each harness's JSONL stream, records the session id in the
database **and** on disk, and logs the raw stream under the agent's state
directory. The harness binary is found in `AMEESH_<HARNESS>_BIN`, then
`AMEESH_BIN_DIR/<name>`, then the `PATH`, which is how the bench uses fake
harnesses.

## Leases

An agent is **pinned to a host** (in v1, harness sessions are local files). The
**lease is the only permission to run**.

- **Exclusive claim.** A claim is one conditional, atomic `UPDATE`: two runners
  cannot win the same agent. This is tested with 2 and 4 concurrent
  connections, and with two runner processes competing for the same lease.
- **Same conditions as eligibility.** The claim re-checks, in the same
  statement, everything that makes an agent claimable: an ephemeral agent not
  expired, a resolved responsible human when required, the canon state and the
  placement verdict.
- **Fencing.** Renewing, starting a turn and taking a prompt all require a live
  lease. An expired lease cannot be extended; the runner has to claim again,
  with a new epoch.
- **Lease loss stops the harness.** The lease is renewed by a heartbeat during
  turns. If it is lost, the harness's whole process group receives `SIGKILL`
  and the turn is not counted.
- **Runner death.** If a runner dies (`SIGKILL`, power cut), the lease expires
  (`--lease-ttl`, default 300 s) and another runner of the same host takes the
  agent over. On `SIGTERM`/`SIGINT`, leases are released immediately.

!!! note "The lease invariant, honestly"
    The target is that no process of an agent's group is alive after the known
    deadline of its lease, unless renewed, **within a scheduling margin**
    (the delay between the deadline and the effect of `SIGKILL`, measured by
    the tests). An absolute guarantee is not promised: the operating system
    can suspend a process arbitrarily.

## Identity is bound to the lease

A harness started by the runner receives `AGENT_MAIL_NAME`, `AMEESH_RUNNER_ID`
and `AMEESH_LEASE_EPOCH`. The mailbox hook delivers mail only if that lease is
alive and held by that runner. The working directory **never** gives an
identity: reading in another agent's worktree does not consume its mail.

## Interactive takeover: `ameesh attach`

```bash
ameesh attach <agent> [--wait] [--ttl S]
```

`attach` takes the lease for an interactive runner
(`attach:<user>@<host>`), suspends automatic claiming of that agent, starts the
harness interactively on the **same session**, renews the lease while the
session lives, and gives the lease back on exit. It is refused while a turn is
running, unless `--wait`. This is how orchestrators become turn-based agents
that a human can still talk to directly.

## Events and priority interruption

- **Events** (`ameesh mail send <agent> "…" --kind event`) wake an agent like a
  message, but a burst of events wakes it at most once every
  `AMEESH_EVENT_COALESCE` seconds (default 120). `--urgent` pierces the
  coalescing.
- **Interruption.** An `--urgent` message from an authorised sender
  (`AMEESH_INTERRUPT_SENDERS`) **stops the current turn**: the harness is
  stopped, the turn's prompt is put back in the queue and the urgent message is
  served first, on the same session. An urgent message from a sender who is not
  authorised is delivered as a normal one. `ameesh interrupt <agent> <message>`
  does the same directly, and refuses a sender who is not authorised.

## Registering agents by hand

On the bench, or without a canon:

```bash
ameesh run register <name> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXT] \
    [--session ID] [--chantier C] [--model M] [--budget USD]
ameesh run stop <name>        # no new lease; the current turn finishes
```

`canon sync` never lifts a stop set by `ameesh run stop`.

## Seeing it

```bash
ameesh list [--json]          # every agent: harness, host, status, lease, unread, budget
                              # (--json: schema ameesh-agent/1, with state, current lot, settings)
ameesh alerts [--follow]      # long turn, idle with mail, dead runner, session too big, stale lot
ameesh show <agent> [--json]  # one agent in detail
```
