# Concepts

ameesh separates three kinds of information, and keeps each in its own place:

| Kind | Where it lives | Who changes it |
|---|---|---|
| **Declarations**: who is responsible for what, which agent runs on which host, with which credentials, which passkeys are trusted | the **canon**, a git repository in OKF format | humans, by reviewed pull request |
| **State**: leases, sessions, statuses, messages, actions, spend | the ameesh **database** (Postgres in v1) | the runner and the CLI |
| **Secrets**: API keys, subscription tokens, service token | the host's keychain or secret manager | the host's operator; never the canon, never the database |

```
            OKF canon (git, federated)                 readable threads
   Agent / Host / Placement cards, roles,        (Markdown files in v1)
   review policies, trusted passkeys                       ▲ write / read
                 │ read (merged commit only)               │
                 ▼                                         │
  ┌──────────────────────────── ameesh ─────────────────────────────┐
  │ canon ── placement ── runner (one per host, leases, turns)       │
  │ threads (transports) ── mailbox (queue, v0-compatible hooks)     │
  │ actions (gate, registry, reconciliation)                         │
  │ receipts (verification) ◄── ameesh-approve (separate service)    │
  │ storage (named operations) ── Postgres driver (psycopg | psql)   │
  └──────────────────────────────────────────────────────────────────┘
```

Architecture rules: ameesh **reads** the canon and writes to it only by
proposal (branch or pull request); **state** is in the database; **secrets**
are neither in the canon nor in the database; `ameesh-approve` runs **out of
the agents' reach** (another Unix user or another host) and shares only
receipts with ameesh.

## Vocabulary

| Term | Meaning |
|---|---|
| **canon** | the federated OKF repositories describing teams, members, hosts, placements, lots and decisions |
| **member** | a human (`human:<id>`) or an agent (`agent:<id>`) |
| **card** | the declaration of a member, a host or a placement in the canon |
| **host** | a machine or a cluster, with a responsible human and a policy |
| **placement** | "this agent runs on this host, with this credential mode" |
| **lot** | a unit of work (`work_items` in the database), with milestones requested → frozen → verdict → merged |
| **action** | an effect on the outside world, classed `read`, `reversible`, `irreversible` or `costly` |
| **receipt** | the signed proof that a human approved one attempt of an action |
| **thread** | the readable sequence of messages of a lot or a project |

## Pages

- [The canon](canon.md)
- [Members, agents, hosts, placements](members-agents-hosts.md)
- [Runner and leases](runner-and-leases.md)
- [Sessions](sessions.md)
- [Readable threads](threads.md)
- [Actions and the gate](actions-and-gate.md)
- [Receipts and ameesh-approve](receipts-and-approve.md)
- [Budgets and cost](budgets.md)
