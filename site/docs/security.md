# Security model

This page states what ameesh v1 guarantees and what it does **not**, without
rounding up. It follows §11 of the specification.

## Guaranteed in v1, and not yet

| Guaranteed in v1 | Not yet (planned for v1.x) |
|---|---|
| An agent without a responsible human, or misplaced against its host's policy, does not run. | Isolation of agents from each other: in the local profile they share the same Unix user. |
| No irreversible or costly action without a valid receipt **when it goes through ameesh's gate**. This protects against mistakes and prompt injection, **not** against a malicious agent that shares ameesh's Unix user: such an agent can read business credentials, modify ameesh or its database. | The gate and business credentials under a Unix user distinct from the agents'; a network sandbox; an MCP proxy. |
| Receipts are bound to the action's digest, single use, and expire. | Strong non-repudiation for **synced** passkeys (the high level means a hardware key). |
| The registry of authenticators can be changed only by a reviewed pull request to the canon. | Provenance signatures on agents' messages. |
| A readable thread of every message that goes through ameesh. | Messages exchanged outside ameesh. |

## Design choices behind these guarantees

- **Authority is proven, never read.** No text, from an agent or claiming to
  come from a human, carries authority. A human's approval is a signature over
  the exact digest of the action, made on their own device.
- **The signing key is out of the agents' reach.** Passkeys live in the human's
  authenticator; ameesh-approve runs under another Unix user or on another
  host; the token ameesh holds can request and fetch receipts, never sign
  them.
- **The service recomputes what it shows.** The approval page displays the
  action as read from the database, never the agent's description of it.
- **Enrolment is a proposal.** A new passkey becomes active only after a
  reviewed pull request to the canon and a sync from the trusted canonical
  branch. A sync never goes backwards and never re-activates a removed passkey.
- **Fail closed.** An unreadable or invalid canon, a refused or unevaluated
  placement, an unresolved responsible human, an accounting marker that cannot
  be written: each one closes new claims or new turns, never silently opens
  them. Running leases and turns are not killed by a canon problem.
- **Unknown is not failure.** An action whose outcome is unknown is never
  retried automatically; it is reconciled with the connector or decided by a
  human who explicitly assumes the risk of a duplicate.
- **Approved source only.** The canon is read at the merged commit of the
  canonical branch, through git objects; local edits and unpushed commits are
  reported, never used.

## Known limits

- In the local profile, agents share ameesh's Unix user: they can alter the
  canon clone or the git configuration. Only a check made by an isolated
  component (ameesh-approve, under another user) is enforceable against a
  malicious agent. The database's monotonicity keeps the passkey registry from
  going backwards.
- The **lease invariant** (no process of an agent alive after its lease
  deadline) holds within a scheduling margin, measured by the tests; arbitrary
  suspension of a process by the operating system is outside the guarantee.
- In the [cluster profile](guides/cluster-profile.md), each agent has its own
  pod, file system and processes, but agents still share ameesh's database
  role.
- Using **subscription** credentials for unattended agents on a server must be
  checked against each provider's terms; a host policy can refuse that
  credential mode.
