---
type: Study
title: "L460 — launch-time cgroup isolation for tool freezing"
description: "Feasibility by harness, recovery requirements, and a recommendation to drop arbitrary tool freezing."
status: draft
tags: [pression, harnais, cgroup, reprise]
generated: { by: "ameesh-cx1", at: "2026-10-12" }
---

# Recommendation

**Drop automatic freezing of arbitrary tools.** Launch-time cgroup isolation
is technically plausible for selected shell commands, particularly through
DeepSeek's replaceable executor, but it does not make suspension safe for
commands holding database transactions or shared locks. Keep pressure
admission control and the duration caps proposed in [PR #423](https://github.com/manaty/ameesh/pull/423).
A future experiment should isolate explicitly eligible jobs and prove
recovery before enabling any freeze.

This study replaces the implementation deliverable of lot #460, following
mesh-design's agent-mail of 2026-10-12 at 19:29. The `revue-460-design`
verdict blocked the pidfd guardian proposal: loss of both handle holders,
ACK/STOP ordering, shared-resource deadlocks, and classification changes
after inspection were unresolved. Lot #460 remains blocked; this document
does not authorize host changes.

# Baseline and evidence

Inspected ameesh `origin/main` at `4a056a9`. Normal turns use
`AgentWorker.run_turn` with `Popen(start_new_session=True)`; background work
is tracked by process group. Only pressure-exempt audit turns use the
special systemd scope. The user, server and guest runner unit templates
lack `Delegate=yes`; the guest also sets `ProtectControlGroups=yes`.
There is no existing normal-turn or tools cgroup to freeze.

Read-only local checks found Claude Code **2.1.286**, Codex CLI **0.159.3**
and DSH **0.1.5-rc.2**. DSH's shim had no selected Node version in this
session; invoking its installed `lib/bin.js` with its installed Node
reported the version without changing the environment. Upstream documents
were checked on 2026-10-12. No provider call, live tool interception,
cgroup creation, freeze or unit change was performed. The conclusions
below distinguish a documented integration point from a tested guarantee.

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
boundaries; creating sibling scopes must not bypass those limits.

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
fencing; and DB/lock safety. On current evidence, **drop the freeze and
retain admission control and bounded termination**.
