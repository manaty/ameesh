# ameesh documentation

**ameesh** coordinates mixed teams of humans and AI agents working on the same
projects, across several agent harnesses: **Claude Code**, **Codex** and
**DeepSeek Harness**. It gives every agent a durable mailbox, runs them under
exclusive leases (one runner per host), writes every message into a thread a
human can read, and keeps irreversible or costly actions behind a gate that
only opens with a receipt signed by a human on their phone.

ameesh is free software under **AGPL-3.0-only**. Source code:
[github.com/manaty/ameesh](https://github.com/manaty/ameesh).

!!! note "Where ameesh runs today"
    v1.0.0 runs on a **test bench**: a local Postgres container, fake harness
    binaries, a fake `gh` and a software passkey. The steps to put it in front
    of real agents are described in [Switch from a v0 setup](guides/switch-from-v0.md).

## What it is made of

| Piece | What it does |
|---|---|
| **Canon** | A git repository in [OKF](concepts/canon.md) format that declares humans, hosts, agents and placements. ameesh reads it only at the merged canonical commit; a host can read several canons. |
| **Registry and mailbox** | Postgres tables for presence, leases, sessions, messages and work items. LISTEN/NOTIFY wakes runners. |
| **Runner** (`ameesh run`, alias `agent-runner`) | One per host. Claims agents under leases, runs turns in the agent's harness, resumes the same session. |
| **Readable threads** (`ameesh fil`) | Every message that goes through ameesh is appended, verbatim, to a Markdown thread per project or lot. |
| **Actions and the gate** (`ameesh action`) | Effects on the outside world are recorded actions with a stable id and a digest; irreversible or costly ones need a receipt. |
| **ameesh-approve** | A separate service, under its own Unix user, that shows the action to a human and has them sign it with a passkey. |
| **Observability** | `ameesh list`, `ameesh alerts`, `ameesh decisions`, `ameesh cost`, `ameesh progress`, `ameesh hosts`; `ameesh notify` pushes alerts to the responsible human. |

## Where to start

- New here: [Getting started](getting-started.md) runs the whole v1 scenario on
  your machine in a few minutes.
- To understand the model: [Concepts](concepts/index.md).
- To set up a project: [Write a canon](guides/write-a-canon.md).
- To evaluate the risk: [Security model](security.md) states what is
  guaranteed and what is not.
- Every command: [CLI reference](reference/cli.md).

!!! info "Language of the command-line output"
    ameesh's built-in help, messages and design documents are currently written
    in French. This documentation is in English and describes the same commands
    and options.
