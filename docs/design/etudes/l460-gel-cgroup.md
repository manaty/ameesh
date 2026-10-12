---
type: Study
title: "L460 — launch-time cgroup isolation for tool freezing"
description: "Prefer admission control and existing cgroup weights over arbitrary tool freezing; assess harness isolation and recovery limits."
status: draft
tags: [pression, harnais, cgroup, reprise]
generated: { by: "ameesh-cx1", at: "2026-10-12" }
---

# Recommendation

**Drop automatic freezing of arbitrary tools; prefer admission control plus
low CPU/I/O weights on existing runner/guest cgroups.** The guest already
has this cheaper mechanism. It keeps commands schedulable without tool
hooks or a thaw guardian. Keep the duration caps proposed in
[PR #423](https://github.com/manaty/ameesh/pull/423). Verify effective
hierarchy and controller support before extending weights to other runner
units. Launch-time tool isolation remains plausible, particularly through
DeepSeek's executor, but does not make transaction/lock suspension safe.

This study replaces the implementation deliverable of lot #460, following
mesh-design's agent-mail of 2026-10-12 at 19:29. The `revue-460-design`
verdict blocked the pidfd guardian proposal: loss of both handle holders,
ACK/STOP ordering, shared-resource deadlocks, and classification changes
after inspection were unresolved. Lot #460 remains blocked; this document
does not authorize host changes.

# Baseline and evidence

Inspected ameesh `origin/main` at `4a056a9`; these file/line links pin that
baseline:

- Normal launch uses `Popen(start_new_session=True)`
  ([src/ameesh/runner.py:3419](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/runner.py#L3419));
  background survivors are selected by process group
  ([src/ameesh/background.py:238](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/background.py#L238)).
- The audit-only scope route
  ([src/ameesh/runner.py:5352](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/runner.py#L5352))
  sets `KillMode=control-group`
  ([src/ameesh/platform/audit_guard.py:28](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/platform/audit_guard.py#L28)).
- Runner templates use `KillMode=mixed` and contain no delegation setting:
  [deploy/systemd/ameesh-runner-agent@.service:55](https://github.com/manaty/ameesh/blob/4a056a9/deploy/systemd/ameesh-runner-agent%40.service#L55),
  [deploy/serveur/ameesh-runner@.service:30](https://github.com/manaty/ameesh/blob/4a056a9/deploy/serveur/ameesh-runner%40.service#L30),
  [deploy/guest/ameesh-guest-runner@.service:55](https://github.com/manaty/ameesh/blob/4a056a9/deploy/guest/ameesh-guest-runner%40.service#L55).
  The guest additionally sets `ProtectControlGroups=yes`
  ([deploy/guest/ameesh-guest-runner@.service:69](https://github.com/manaty/ameesh/blob/4a056a9/deploy/guest/ameesh-guest-runner%40.service#L69)).

There is no existing normal-turn or tools cgroup to freeze. New sibling
scopes would have their own lifecycle: runner `KillMode=mixed` cleanup and
process-group tracking cannot automatically manage them.

Read-only local checks found Claude Code **2.1.286**, Codex CLI **0.159.3**
and DSH **0.1.5-rc.2**. DSH's shim had no selected Node version in this
session; invoking its installed `lib/bin.js` with its installed Node
reported the version without changing the environment. Upstream documents
were checked on 2026-10-12. No provider call, live tool interception,
cgroup creation, freeze or unit change was performed. The conclusions
below distinguish a documented integration point from a tested guarantee.
Claude/Codex hook coverage was not exercised on the installed versions;
the r1 reviewer could not independently verify upstream contracts under
its no-network constraint. Their routes remain documentation-based
feasibility claims, not installed-version coverage results.

# Cheaper alternative: admission control and existing cgroup weights

The guest slice already sets `CPUWeight=10` and `IOWeight=10`
([deploy/guest/ameesh-guest.slice:24](https://github.com/manaty/ameesh/blob/4a056a9/deploy/guest/ameesh-guest.slice#L24)).
Its parent drop-in sets both as well, so the hierarchy competes at the
application's level, rather than merely between guest children
([deploy/guest/ameesh.slice.d/guest-weight.conf:3](https://github.com/manaty/ameesh/blob/4a056a9/deploy/guest/ameesh.slice.d/guest-weight.conf#L3)).
The offload installer also sets them on the dedicated user's slice
([deploy/guest/install.sh:326](https://github.com/manaty/ameesh/blob/4a056a9/deploy/guest/install.sh#L326)).
`guard.py` translates both weights to systemd properties and applies them
through `set-property --runtime`
([src/ameesh/guard.py:771](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/guard.py#L771),
[src/ameesh/guard.py:949](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/guard.py#L949),
[src/ameesh/guard.py:1203](https://github.com/manaty/ameesh/blob/4a056a9/src/ameesh/guard.py#L1203)).
Reuse that weight-application mechanism; the guard's separate guest-freeze
policy is not the proposed alternative.

Combine pressure admission control with these existing low shares. For
other runner cgroups, an authorized manager can apply suitable weights
without delegation into tool subgroups. Forked descendants remaining in
that hierarchy share the policy; separate audit scopes and containers
need their placement checked. Lowering an entire runner also affects its
heartbeat, mail and psql children, so validate lease latency. Do not lower
the database service's weight along with agent work.

Limits matter:

- Weights distribute **relative contention shares**, not a CPU quota or
  guaranteed I/O rate. Idle resources remain usable; parent/sibling placement
  determines which workloads compete. See the
  [kernel weight model](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#weights).
- CPU/I/O controllers must be enabled in the relevant hierarchy. I/O
  weighting additionally requires a supported device/backend, such as
  enabled iocost or BFQ's cgroup support; a successful property write alone
  does not demonstrate effective prioritization. Verify kernel interfaces
  and behavior on the actual storage path. See the
  [kernel I/O controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#io)
  and [BFQ documentation](https://docs.kernel.org/block/bfq-iosched.html).
- CPU/I/O weights do not reclaim memory. Keep existing memory protection,
  admission control and bounded termination for memory pressure.
- Commands continue running and can release locks, avoiding a deadlock
  caused by suspension. Slower progress can still lengthen transaction or
  lock holding; weights neither eliminate application deadlocks nor bound
  completion time.

This is the preferred incremental alternative where support is verified.
No host effectiveness benchmark or weight change was performed in this
study; unsupported hosts retain admission control and duration caps.

# Harness launch points

| Harness | Launch-time route | Coverage and feasibility |
|---|---|---|
| Claude Code | A synchronous `PreToolUse` hook matching `Bash` can replace `tool_input.command` with a trusted launcher through `updatedInput`. | Plausible for Bash invocations. MCP operations, built-in tools and other execution paths are separate. Background calls, subagent hook inheritance and sandbox/escalation paths need coverage tests. |
| Codex CLI | `PreToolUse` can rewrite supported shell calls; current documentation includes `exec_command` under the `Bash` matcher. | Plausible for a pinned shell path, but documentation explicitly allows specialized paths to bypass hooks. A later `write_stdin` uses the original session, so placement must happen at its first launch. Not a complete enforcement boundary. |
| DeepSeek Harness | Replace the `ctx.shell` provider; `LocalBashExecutor.runArgv` and `startArgv` both pass argv to `ctx.subprocess.spawn`. | The clearest direct execution seam for foreground and background Bash. A custom provider can launch a trampoline before command execution. MCP and other subprocess users need separate treatment; changing the general subprocess provider would also catch infrastructure. |

Claude's [hook reference](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)
documents command-input replacement. The hook must wrap the original
command as data, preserve its quoting, cwd, environment, streams and exit
status, and avoid recursively wrapping its own launcher. A hook's process
is distinct from the eventual tool process: moving the hook into a cgroup
does not place the tool there.

The [official OpenAI hook documentation](https://developers.openai.com/codex/hooks#tool-coverage)
establishes current shell/unified-exec coverage and its limits; its
[input-rewrite contract](https://developers.openai.com/codex/hooks#pretooluse)
provides the candidate wrapper route. This does not prove equivalent
coverage on every installed CLI, resume path or sandbox mode. Environment
injection from the [configuration reference](https://developers.openai.com/codex/config-reference)
alone does not establish execution inside a designated cgroup.

DSH's installed executor source was inspected at version `0.1.5-rc.2`:
`run`/`start` construct `bash -c` argv and route through
`runArgv`/`startArgv`. The upstream
[shell subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/docs/subsystems/shell.md)
and [executor source](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/shell/bash-local/src/index.ts)
are pinned to the inspected upstream HEAD. Its executor has already changed
to a shared `execute` path, still calling `ctx.subprocess.spawn`; this
confirms a seam but also version drift. That commit is not the asserted
provenance of the installed npm build. A version-pinned provider replacement
is a candidate, not a verified change to ameesh's model-only `--patch`.

# Scope and recovery design required before a prototype

A future per-turn scope could use `Delegate=yes`, with separate
`harness/` and `tools/<invocation-generation>/` leaves. Keep the harness,
MCP servers and mail infrastructure in `harness/`; freeze only an eligible
invocation leaf. A launch trampoline must enter that leaf **before**
executing any user command, with a readiness barrier. Membership then
follows fork/exec descendants, including new process sessions. Wrapping
the whole turn or migrating discovered PIDs afterwards does not provide
that distinction. This layout is a proposal; no deployed delegation or
scope hierarchy was verified. [systemd delegation documentation](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml)
defines the ownership needed to manage child cgroups.

A separately supervised recovery service outside the turn subtree and
its OOM domain must own serialized freeze/thaw operations, generations and
deadlines. Persist the scope identity and deadline before accepting a
freeze. Startup, runner ExecStopPost, recovery-service restart and an
independent deadline trigger must all thaw the same generation. An
in-process timer or a timestamp read only by the next runner tick cannot
bound a freeze while that runner is dead. This service must operate
without SQL, tool-held locks or synchronous trace writes; absent recovery
or delegation, refuse freezing and report pressure only.

The [kernel freezer interface](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#core-interface-files)
retains state independently of runner pidfds: write `0` to thaw, and
observe `cgroup.events` to confirm a freeze. An ancestor's freeze still
applies; `SIGCONT` does not thaw a cgroup. Thaw before graceful TERM, then
use the isolated cgroup for bounded descendant cleanup. Reconcile stale
generations and competing runners without reusing scope names. Retain
existing agent/guest memory limits and background grace across new scope
boundaries; creating sibling scopes must not bypass those limits. Define
scope-specific stop ownership and background expiry explicitly: the
existing runner's mixed kill mode does not propagate to sibling scopes,
and the audit scope's control-group kill mode is a different contract.

User-manager delegation needs validation on desktop hosts. System-service
creation needs the host's authorized broker or service manager; the guest's
read-only control-group policy currently prevents this layout. macOS,
Windows and hosts without writable cgroup v2 remain admission-only.
No unit hardening should be relaxed silently.

# Why isolation is insufficient

An isolated command can open a database socket or acquire a shared lock
at any time. Suspending its complete descendant cgroup retains those
resources. Neither a launch wrapper nor a prior TCP/Unix socket scan
proves that the command is safe to suspend. Recovery outside the subtree
prevents recovery deadlock, but peers can still remain blocked throughout
the freeze. A safe eligibility contract needs enforceable resource
isolation or cooperative safe points, not an executable-name heuristic.

Reconsider only after a separate prototype proves launch placement for
every enabled shell path, foreground/background and subagents; preservation
of sandboxing and resource limits; runner/recovery-service SIGKILL and OOM
recovery at every freeze boundary; deadline racing late requests; two-runner
fencing; and DB/lock safety. On current evidence, **drop the freeze; prefer
admission control plus verified low CPU/I/O weights on existing cgroups,
with memory protection and bounded termination retained**.
