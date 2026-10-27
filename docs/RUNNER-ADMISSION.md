# Dynamic admission of a runner (`--agents-file`, #3925)

Requested in manaty/ameesh#2975: a runner that serves many short-lived agents
(one per document, a pool of roles) must admit a new agent without a restart,
because a restart interrupts every agent it hosts.

## What is dynamic, and what was not

- **Canon admission was already dynamic.** Without a filter, every sweep asks
  `registry.claimable` for the agents of this host. An Agent card plus a
  Placement written by `canon sync` after the runner started is claimed on
  the next pass.
- **`--agents a,b` is read once at startup.** That list is the only static
  part of admission.

## `agent-runner --agents-file FILE`

The file is the admission source on the host, and the host operator owns it.
The runner reads it again whenever its stamp changes (mtime, size, inode),
on every pass of the main loop, right after the configuration reload.

```
# IMA document agents
ima-doc-1042
ima-doc-1043, ima-role-triage
```

The format:

- One agent name per line, or several separated by commas.
- `#` starts a comment.
- Duplicates are ignored.

The filter is the `--agents` list joined with the names in the file. The
file only narrows admission. These checks still apply at claim time:

- the registry, and the governed canon: `canon_claim_predicate_sql`, with the
  Placement admitted and the host canon `ok`;
- the host's `max_agents`. A claim beyond it is refused with the note
  `lease_not_taken: host capacity: max_agents=N`;
- the host gate and its slots;
- account and credential checks when the turn starts.

**Refusals never stop the runner.**

- An invalid entry is skipped and logged with its line and the reason. That
  covers a name that is not an agent name, and an alias that cannot be
  resolved.
- If the file disappears or cannot be read, the last list read stays in
  force.
- A transient failure is not an invalid entry. That covers a read refused
  (permissions) and a name the runner cannot resolve because the database is
  unreachable. The whole last list stays in force, and the next pass tries
  again without the file being edited. Each cause is logged once.
- If the file is missing at startup, the runner admits nobody.

**An empty list admits nobody.** This holds for claims, role standings,
branch sweep, measures and the device control channel. It does not mean
"all agents".

## Retirement

An agent stops being claimable as soon as it is withdrawn, in either of these
ways:

- it is taken out of the admission file;
- `canon sync` removes its Agent card or Placement, which sets
  `placement_ok = false`.

A worker that already holds the agent's lease checks admission between two
turns (`AgentWorker.admission_withdrawn`). If the agent is no longer admitted,
the worker releases the lease. The current turn always finishes first. A
worker idle when the file changes is woken so that it leaves at once.

Some cases are not retirement:

- A host canon that is temporarily invalid. Only the Placement counts, as for
  `registry.wakeable`.
- An elected role (`releve: election`), which keeps its own handover (EP2).
- A mediated runner, where the server judges admission at every lease.

## Out of scope

- IMA's per-member provisioning.
- A governed non-Git registry for per-member agents (item 4 of the ticket).

Both are left for a decision on the ticket.
