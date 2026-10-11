---
type: Decision
title: "Name: ameesh, buggy logo, repository manaty/ameesh"
description: "The agent-mesh package becomes ameesh; full renaming right away; private repository, then public at the first release."
status: stable
tags: [nom, depot, release]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

"amesh" is taken by three agent projects (tlhc/amesh, samchung95/amesh, code-rabi/amesh) and by the PyPI package amesh (a Tokyo rain radar). "agent-mesh" is the name of a commercial product (Solace Agent Mesh). "ameesh" is free on PyPI, npm, crates.io, Homebrew, AUR and Docker Hub; it is also an Indian first name, with no software project of the same name.

# Decision

- Name: **ameesh** (pronounced like *amish*). Logo: an **Amish buggy** (black buggy with a hood).
- Scope of the renaming: **the whole project** — repository, Python module, commands (a single `ameesh` command with subcommands), environment variables; `agent-mail` remains an alias because the harness hooks call it.
- Timing: **immediate**, without waiting for the end of the ongoing security review.
- Repository: **github.com/manaty/ameesh**, **private** now, **public after the first release**.

# Consequences

- Repository created private on 2026-10-03 (main at c4d11d5).
- Before going public: remove the test bench DSN with a hard-coded password, run the history through a secret scanner, choose a licence (owner decision to come).
