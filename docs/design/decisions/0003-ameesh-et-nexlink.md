---
type: Decision
title: "ameesh is a standalone product, used by Nexlink and natively integrated"
description: "Split of roles between Nexlink (identities, pages, messages, human signatures) and ameesh (execution)."
status: stable
tags: [nexlink, architecture]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

The Nexlink agents-and-teams spec (AM1–AM15) already provided for agent accounts, project conversations, signatures by device keys, relays and leases; ameesh v1 has its own registry, mailbox, leases and signatures. The two overlap.

# Decision

- ameesh is a **standalone product**; **Nexlink uses it** (Nexlink organisations run their agents with ameesh).
- **Native integration**: Nexlink is a communication channel for ameesh and displays projects, modules and lots.
- Split: **Nexlink** = organisations, human members, agent accounts, conversations, notifications, human signatures on device, audit; **ameesh** = per-machine runner, harness adapters, sessions, turns, execution leases, budget, lot lifecycle, authority verification.
- Agent↔agent exchanges **also** go through the messaging, visible to humans, filed in one thread per lot.

# Consequences

- The ameesh core exposes interfaces (directory, messaging, work catalogue, authority, leases); Nexlink is a connector, native and preferred, not a dependency of the core.
- See [0005](0005-canon-okf.md): Nexlink pages are a view of the OKF canon.
