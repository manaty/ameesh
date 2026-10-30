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

An Agent card may instead declare `releve: election` with a list of
`candidats` (hosts its Placement already admits). The runner is installed on
every candidate; any of them may take the lease when it is free or expired,
and the winner becomes the holder. `canon sync` writes the placement verdict
of **each** candidate host, so the host policy and visibility rule are still
checked on the host that claims. Such a role is not moved by L232e: there is
no local work to publish, and the lease epoch fences the former holder. A
planned `host drain` returns the holder's lease at once (epoch incremented in
the drain transaction), so a candidate can take over without waiting for the
expiry; an unplanned stop waits for the lease to expire. The repository note
`docs/HOST-HANDOFFS.md` has the full election rules. The
best *ready* candidate wins, with a short per-rank delay before the claim
(0 s, 20 s, 40 s…); availability over seven days decides, capacity only breaks
ties, and a holder is replaced only after a challenger has stayed 5 points
ahead for an hour, at the end of the holder's turn. `ameesh role status` shows
the holder, the epoch and the ranking.

A candidate runner takes **no turn before the lease**: the worker is created
only after the claim, renews the lease before each turn, and its harness is
killed if the lease is lost. On a **cross-host** election the winner opens a
new session on a deterministic resume briefing built from the mesh database,
the canon and GitHub — no session crosses hosts. The claim persists that
briefing and resets the session in the same statement; a cross-host claim
without the briefing is refused (the line is not moved), and the briefing is
dropped with the session, so it never resurfaces later. When the first turn
opens a work item session, the work item's minimal brief completes the election briefing
instead of replacing it. A same-host re-claim (runner restart) keeps the local
session and is not an election.

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
- **Fencing the pilot's gestures.** For an elected role, a merge, a merge
  queue entry or landing, an assignment, a recorded packet composition and a
  decision request are refused unless the session holds the live lease at its
  own epoch. The check renews the lease and journals the gesture in the same
  transaction; a stale session is refused, naming the current holder. An EP5
  timer passes `AMEESH_ROLE` and its epoch: a role without an epoch is
  refused. Just before an external call (push, `gh pr merge`) the lease is
  renewed with a margin at least the call's worst case; the residual race is
  documented in `docs/HOST-HANDOFFS.md`. Humans, host-pinned agents and
  runner-internal sweeps without `AMEESH_ROLE` are unchanged
  (`pilot_gesture.py`).
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

1. `policy.workspaces.dirs[<agent>]` (v1.3.1);
2. `policy.workspaces.roots[<team>]`;
3. `policy.workspaces.root/<team>`.

All three accept the `{agent}` template (v1.3.1), for one worktree per agent:

```yaml
policy:
  workspaces:
    roots: {acme-web: "~/src/acme-web-{agent}"}   # one worktree per agent
    dirs: {docs-writer: ~/src/acme-docs}          # a named exception
```

The legacy keys `work_dirs`, `work_roots` and `work_root` are still read, with
a `host-policy-legacy-key` warning and no removal date; the same key in both
forms is a `host-policy-invalid` error.

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

`max_swap_used` is a **stock**, not a pressure: pages stay in swap until they
are touched, so the ceiling only counts with a corroborating memory pressure
(PSI memory above half its own ceiling). A host that cannot measure PSI keeps
the old meaning: with no declared ceiling, the stock is compared to the 0028
prudent 8 GiB fallback (#2354). `max_mem_pressure` (PSI memory `some`, 60 s,
10 % by default) is the admission signal that follows the real pressure; an
absent or unreadable threshold takes the default, it never removes the guard
(#2296).

- Before each turn, a crossed limit **holds new turns**; running turns finish.
- A **critical** crossing pauses the agents of lowest `priority` (an `Agent`
  card key, 0 by default) that have no running turn, never in the middle of a
  turn.
- A resource of a turn that survives it (process group, container labelled
  `ameesh.turn`/`ameesh.agent`) is reported (`orphan_resource`), never
  removed. Resources opened before the host's last boot are reported as soon
  as the runner starts.
- **Laptop power** (L106): the reading includes mains and battery
  (`/sys/class/power_supply` on Linux, "unknown" elsewhere). On battery, below
  `min_battery_percent` (25 by default) no new turn starts (`host_power_low`
  alert); below `stop_battery_percent` (10) the runner stops cleanly — running
  turns get `AMEESH_POWER_STOP_GRACE` (120 s) to finish, then are stopped at a
  safe point, prompts go back to the queue, leases are released (urgent
  alert). When mains power returns, the runner claims its agents again by
  itself. Agents meant to be always available belong on a server.

- **CPU pressure and lendable capacity** (L159d): the reading includes the
  host's pressure stall information (PSI, 60 s averages), the CPU and memory
  of the guest (the agents' slice) and of the application (everything else),
  and the capacity the host could lend, computed on the application's
  consumption over 7 days. `max_cpu_pressure` (no default) holds new turns
  above that CPU PSI; `max_mem_pressure` (10 % by default) does the same for
  memory PSI. `ameesh hosts` shows them; relocation ranks a lending host by its
  lendable memory.

## Host gate and durable withdrawal

A **host gate** (L159f) is the declarative stop on a host: the runner reads an
external state (`AMEESH_HOST_GATE=file|none`, `AMEESH_HOST_GATE_PATH`) and holds
new turns while the gate says `draining` or `stopped`; `AMEESH_HOST_GATE_FALLBACK`
decides what an unreadable gate means (`available`, `draining` or `stopped` —
never a silent green), and the runner writes its acknowledgement to
`AMEESH_HOST_GATE_ACK`. `--once` never says the host free while its lease is
still held (#379).

`ameesh host drain|undrain|status` (L232e) withdraws and returns a host durably:
claims, turns and prompt consumption close while the current turn finishes,
**critical agents first**. The lifecycle state survives a restart (migration
0380), and an unplanned loss is recovered only after a proof scoped to the
source, agent and lease epoch. A **handoff** (L232a) reserves the move at a work item
boundary, with a durable journal: `ameesh handoff plan AGENT --repository R`
then `ameesh handoff show OPERATION` (see
[Operate agents](../guides/operate-agents.md#host-lifecycle-drain-return-and-handoff)).

## Host not ready

A harness binary or interpreter that cannot be found (a user unit started
before the session imported its `PATH`, `node` missing for `dsh`), a harness
that could not execute (exit 126/127 without output), an unreachable database
(also at startup) or a missing working directory are **host** errors, not
agent failures: the agent shows `blocked` "hôte non prêt" (host not ready), the prompt and mail
wait, the fast-failure series does not move, the agent is never stopped. The
runner retries (15 s, doubling up to `AMEESH_HOST_RETRY_MAX`) and resumes by
itself; `host_not_ready` alerts the agent's responsible. Binaries are resolved
from `AMEESH_<HARNESS>_BIN`, `harness_bins`, `AMEESH_BIN_DIR`, `PATH`, the
systemd user manager's `PATH`, then known locations (mise installs and shims,
`~/.local/bin`, the npx cache); the absolute path is logged at startup.
`ameesh doctor --harness` checks the same, and lists agents run without an
enabled runner unit.

An agent held in `ameesh attach` should also have an enabled runner
(`systemctl --user enable ameesh-runner-agent@<agent>`): when the interactive
session dies without releasing its lease, that runner resumes the same session
once.
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

- **Batched mail** (L125). An idle agent is not woken by each message: the
  runner waits for a batching window (`AMEESH_MAIL_BATCH`, default 90 s,
  counted from the oldest waiting message), then opens **one** turn with
  everything that arrived. An `--urgent` message, or a message from a human,
  wakes the agent at once. Acknowledgements (`--ack`), copies (`--cc`) and
  `send all` announcements are passive: they never open a turn by themselves,
  and are read on the next one.
- **Events** (`ameesh mail send <agent> "…" --kind event`) wake an agent like a
  message, but a burst of events wakes it at most once every
  `AMEESH_EVENT_COALESCE` seconds (default 120). `--urgent` pierces the
  coalescing.
- **Interruption.** An `--urgent` message from an authorised sender
  (`AMEESH_INTERRUPT_SENDERS`) **stops the current turn**: the harness is
  stopped, the turn's prompt is put back in the queue and the urgent message is
  served first, on the same session. An urgent message from a sender who is not
  authorised does not interrupt anything; it is delivered like a message,
  without waiting for the batching window. `ameesh interrupt <agent> <message>`
  does the same directly, and refuses a sender who is not authorised.

## Registering agents by hand

On the bench, or without a canon:

```bash
ameesh run register <name> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXT] \
    [--session ID] [--chantier C] [--model M] [--budget USD]
ameesh run stop <name>        # no new lease; the current turn finishes
```

`canon sync` never lifts a stop set by `ameesh run stop`.

## Working on several projects

An agent with several declared `projects` keeps its registry `cwd` for the
primary project. A work item of another project uses that project's Host
`workspaces.roots` or `workspaces.root`, expanding `{agent}`; the agent's
`workspaces.dirs` override applies to the primary project. Prepare that directory before
automatic pick-up: the runner does not clone a repository or create an empty
directory. A missing or ambiguous secondary directory leaves the work item
unassigned; an explicitly assigned work item waits until its directory is ready —
the agent is not blocked: its primary turns and mail keep flowing, and only
that work item is retried later. A work item whose project the agent has **not** declared
(or an unknown work item) keeps the primary `cwd`, as before per-project
directories. Each work item's parked session keeps its opening directory, so
returning to it resumes there. Changing the directory starts a new session.
Multi-project agents keep separate work item sessions even with
`session_policy=jamais`. The mediated executor's single agent depot cannot
run a secondary project.

## Seeing it

```bash
ameesh list [--json]          # every agent: harness, host, status, lease, unread, lots, budget
                              # (--json: schema ameesh-agent/1, with state, current lot, settings, mode)
ameesh alerts [--follow]      # long turn, idle with mail, dead runner, session too big, stale lot,
                              # host pressure, stopped with mail, orphan lot, expired delegation
ameesh show <agent> [--json]  # one agent in detail
ameesh hosts [--json]         # host resources and limits
```
