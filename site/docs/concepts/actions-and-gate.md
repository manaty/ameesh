# Actions and the gate

An **action** is an effect on the outside world: merging a pull request,
sending something, spending money. In ameesh, such an effect is not a command
an agent runs directly; it is a **recorded action** that goes through a
**gate**.

## The model

Each action has a stable id (`act_` + 26 characters), a project, an optional
work item, who proposed it, a **connector** and an **operation**, a **target**
(the account or object affected), canonical JSON **arguments**, an optional
**amount** and currency, and a **class**:

| Class | Receipt required by default |
|---|---|
| `read` | no |
| `reversible` | no |
| `irreversible` | **yes** |
| `costly` | **yes** |

`--class` can raise a connector's class, never lower it.

The **digest** of an action is what a human signs:

```
SHA-256("ameesh-action/1\0" || JCS({action_id, project, connector, operation,
                                     target, args, amount, currency, policy_version}))
```

(JCS is RFC 8785 canonical JSON.) Every state transition writes an event and an
entry in the project's [readable thread](threads.md).

## The protocol

```
proposed ──receipt──► approved ──written before the call──► launched ──► confirmed
    │                                                           │    └──► failed
    └──► cancelled                                              └──► unknown ──reconcile──► confirmed | failed | human decision
```

1. **`proposed` → `approved`** only with a valid receipt whose digest is the
   action's digest, or a bounded standing approval that covers it.
2. **`approved` → `launched`** is written and committed **before** the external
   call. In the same transaction the receipt's nonce is consumed (single use),
   or the amount is reserved on the standing grant.
3. The call passes the **idempotency key = `action_id`** to the connector.
4. A certain result gives `confirmed` or `failed`. No result (cut, timeout)
   gives **`unknown`**.
5. **`unknown` is never retried automatically.** If the connector guarantees
   deduplication, a new attempt of the same action is allowed (new receipt,
   same `action_id`). Otherwise `ameesh action reconcile` asks the connector
   what happened; if it cannot tell, the action waits for a human. Only a
   signed human decision that **assumes the duplicate** creates a new action
   (`replaces` = the old one).

## Connectors in v1

| Connector | What it does | Deduplication |
|---|---|---|
| `shell-noop` | writes a file, idempotent by key (tests and demonstration) | guaranteed |
| `git-merge` | merges a GitHub pull request with `gh`; reconciles from the pull request's state | none |

## Commands

```bash
ameesh action propose --connector git-merge --operation merge --project acme-web \
    --target acme/acme-web#7 --args '{"method": "squash"}' --approver human:alice
ameesh action execute <id>          # refused with [receipt_required] until approved
ameesh action request <id> --approver human:alice   # ask ameesh-approve; link written to the thread
ameesh action fetch-receipt <id>    # fetch, verify and attach the signed receipt
ameesh action execute <id>          # runs once; replaying the receipt fails
ameesh action reconcile <id>        # after an unknown outcome
ameesh action show|list|cancel|retry|replace|recover …
ameesh decisions [--for human:ID]   # what waits for a human: approvals, unknown outcomes
```

Exit codes used by the demo: `execute` returns **1** when refused (no receipt,
already consumed) and **4** when the outcome is unknown; `fetch-receipt`
returns **5** while the receipt is not signed yet.

!!! warning "What the gate protects against"
    The gate protects against **mistakes and prompt injection**: an agent that
    is told to merge cannot do it through ameesh without a human's receipt. It
    does **not** protect against a malicious agent that shares the Unix user of
    ameesh: such an agent can read business credentials, modify ameesh or its
    database. See the [Security model](../security.md).
