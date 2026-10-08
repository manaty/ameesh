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

An agent is **pinned to a host** (in v1, harness sessions are local files),
unless moving between admitted hosts is enabled (see
[below](#host-resources-and-back-pressure)). The **lease is the only
permission to run**.

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

A session started by a human (an agent in `externe` mode) has an identity only
through an explicit **session binding** (`ameesh mail bind`, v1.4.0), found by
the session id the harness passes to the hook and, optionally, checked against
an ancestor PID. Without one, the hook delivers nothing. A hook without a lease
never overwrites the recorded session of an agent run by a runner. See
[Operate agents](../guides/operate-agents.md#session-bindings).

## Only agents ameesh can wake get work

Since v1.4.0 (design decision 0030), each agent has a **mode**: `execute` (run
by a runner under a lease) or `externe` (a human's session, mailbox only). The
runner **never claims** an external agent, even with a session and pending
mail. Lots and delegations go only to agents that are *wakeable*: known, in
`execute` mode, admitted on their host and with a responsible human. When an
agent stops, a structured reason is recorded, and the alerts
`stopped_with_mail`, `orphan_lot` and `delegation_expired` (which
`ameesh notify` can push to the responsible human) report work nobody will
move forward. See [Operate agents](../guides/operate-agents.md#agent-mode-and-stop-reason).

## Working directory

The directory where the runner starts an agent's harness is a setting of the
**host**, not of the placement (since v1.3.0). `canon sync` copies it into the
registry's `cwd` column. The first path found wins:

1. `policy.work_dirs[<agent>]` (v1.3.1);
2. `policy.work_roots[<team>]`;
3. `policy.work_root/<team>`.

All three accept the `{agent}` template (v1.3.1), for one worktree per agent:

```yaml
policy:
  work_roots: {acme-web: "~/src/acme-web-{agent}"}   # one worktree per agent
  work_dirs: {docs-writer: ~/src/acme-docs}          # a named exception
```

- **Transitional fallback.** If the policy gives nothing for an agent, the
  `cwd` of its old `Placement` card is used; `canon check` warns
  `admission-cwd-inherited` and `canon sync` notes it on the agent's row.
  This fallback is removed in **v1.5.0** at the latest: migrate as soon as you
  see it.
- **No directory at all**: `canon check` warns `host-work-dir-missing` and the
  row keeps an empty `cwd`.
- **Missing directory at run time.** The runner blocks the agent (`blocked`,
  "directory missing") with a **single** log line; its prompt and mail wait.
  It re-reads the registry row at every poll: as soon as a `canon sync` fixes
  the `cwd`, or the directory reappears, it logs "working directory found",
  lifts the status and resumes, without a restart. These status changes are
  fenced by the lease: they never override a stop or another block (budget,
  host pressure). The search for a moved worktree is spaced out (5 s, 10 s,
  20 s… up to 5 min per agent).

## Host resources and back-pressure

Since v1.3.0, each runner publishes a reading of its host every
`AMEESH_RESOURCE_INTERVAL` seconds: available memory, swap used, 1-minute load,
free disk of the working directory, turns in progress (`ameesh hosts`). The
limits are declarative, in `policy.resources` of the `Host` card
(`min_mem_available`, `max_swap_used`, `max_load`, `min_disk_free`; sizes in
bytes or with a unit such as `1GiB`), else prudent defaults.

- Before each turn, a crossed limit **holds new turns**; running turns finish.
- A **critical** crossing pauses the agents of lowest `priority` (an `Agent`
  card key, 0 by default) that have no running turn, never in the middle of a
  turn.
- A resource of a turn that survives it (process group, container labelled
  `ameesh.turn`/`ameesh.agent`) is reported (`orphan_resource`), never
  removed.
- **Moving between admitted hosts** is off by default (`AMEESH_RELOCATE=1`):
  when the host is under pressure and another host admitted for the agent is
  available, the agent moves there between two turns. The native session is
  kept only with shared session storage (`AMEESH_SHARED_SESSIONS=1`); otherwise
  the agent rotates with a resume summary.
- With several canons on a host (v1.4.0), the physical limits applied are the
  **strictest** declared by the host's `Host` cards across the loaded canons,
  and the smallest `max_agents` caps how many agents the runner runs at once.

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
ameesh list [--json]          # every agent: harness, host, status, lease, unread, lots, budget
                              # (--json: schema ameesh-agent/1, with state, current lot, settings, mode)
ameesh alerts [--follow]      # long turn, idle with mail, dead runner, session too big, stale lot,
                              # host pressure, stopped with mail, orphan lot, expired delegation
ameesh show <agent> [--json]  # one agent in detail
ameesh hosts [--json]         # host resources and limits
```
