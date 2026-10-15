# Agent identity aliases (L243b, #423, step 1)

An Agent or Persona card can declare alternative spellings of its identity:

```yaml
title: worker
aliases: [old-worker, another-name]
```

`canon sync` copies `aliases` to the agent's existing registry row. Aliases do
not create agents, leases, sessions, worktrees or keys. Removing the field
removes the declared aliases at the next successful synchronization. An invalid
alias declaration leaves the previous alias set and closes admission for that
agent through its unresolved responsible, as other invalid agent declarations
do. A cross-registry collision is reported before writes; SQL checks it again
under the same per-name locks as the human-key namespace (#330).

`ameesh.agent_names.resolve` is the common entry point. It returns the primary
name; an unknown spelling stays unchanged for the caller's usual refusal.
`human:` references and `all` are not agent aliases. Aliases must be distinct,
valid agent names, and cannot reuse another agent's primary or alias (including
stopped agents), a human key holder's name (including revoked keys), or `all`.
Canon validation also reserves Member names and rejects ambiguous declarations.
Creating a human key holder under an existing agent alias is refused too. An
alias never changes the key's role or proves owner authority.

The accepted spelling is normalized at these boundaries:

* mail sender, recipient, copies, inbox and reservation/delivery;
* registry registration, leases, fencing, status and session updates;
* external session bindings and current/history session reads;
* lot assignees, delegations and assignee filters;
* thread sender and recipients;
* canon agent/admission lookup and host `work_dirs` entries;
* runner `--agents` and interactive attach;
* settings, including the local `permission_mode` file, and key operations.

An instance `ameesh-runner-agent@old-worker.service` passes its spelling through
`--agents`; the runner resolves that filter to `worker`, so a simultaneously
started primary instance competes for the same fenced lease. Diagnostics
recognize enabled runner units addressed by aliases. This feature does not
install, rename, enable or restart any unit on an actual host.

A signed message may retain the original alias spellings in its signed payload.
Verification resolves those spellings to the identities stored on the message
and then rebuilds every signed field from that message. Changing the recipient,
body, date, nonce or signature still invalidates it. The signing bytes are never
rewritten. The alias of an agent key remains an agent signature.

Every database resolution that changes the spelling appends an event to
`agent_name_resolutions`: supplied name, primary, operation, database user and
server time. Lease, key and permission paths use this same resolver. The public
resolver also emits the mapping to the service journal, so a refused operation's
transaction rollback does not hide the spelling used. These traces are
observations, not signed approvals. The resolution table refuses updates and
deletes. Failure to write its event fails the resolution.

The mediated API adds the compatible `agents.resolve_name(name,
operation="lookup")` operation on `/op` and `/session/op`. It only exposes the
name mapping, as the reduced directory does; it grants no access to an agent.
The server normalizes inputs before scope and lease checks. Unknown names,
wrong hosts, wrong owners and stale epochs keep their usual refusal. The
executor allowlist accepts aliases too. File fallback resolves declarations
from configured canons; without a canon it keeps the historical spelling.

## Renaming and rollback

This step changes no existing primary name or production canon. The renaming
table and migration of existing identities remain steps 2 and 3, contingent on
the owner's signed approval. Do not use an ordinary `canon sync` to perform a
rename: an existing identity's alias is protected from being taken as a new
primary by another registry row.

The declaration supports reversal by exchanging the primary and an alias:
`title: worker; aliases: [new-worker]` becomes
`title: new-worker; aliases: [worker]`, and conversely. Both directions use the
same resolver, as tested offline. The step 3 migrator must move the existing
identity's references atomically between turns, keep the former primary as an
alias, and preserve the session, worktree, mail and key signing bytes. Reverting
the declaration uses that same migration in the opposite direction. Step 1 does
not supply or run this migration.
