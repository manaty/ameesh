---
type: Decision
title: "A centralised service evaluates harnesses and makes them available, instead of hard-coding them"
description: "Harnesses (Claude Code, Codex, DeepSeek Harness, and the ones to come) become entries in a central catalogue: declarative descriptors, generic adapters, conformance test bench, harness × model × effort evaluation."
status: stable
tags: [harnais, catalogue, evaluation, adaptateurs, acp, mcp]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:55:00+02:00" }
sources:
  - { resource: "../etudes/standards-des-harnais.md", title: "Study of harness standards" }
---

# Context

The runner currently knows three hard-coded harnesses (`claude`, `codex`,
`deepseek`), with three command lines and three stream formats. New
harnesses are appearing (Gemini CLI, goose, OpenHands, agent SDKs…),
and each one evolves (versions, options, protocols).

# Decision

A **centralised service** **evaluates the different types of harness** and **makes
them available** to ameesh, **instead of hard-coding them**. It complements the
model catalogue ([0020](0020-catalogue-et-evaluation-des-modeles.md)).

# Consequences

- **Harness descriptor** (`Harness` concept of the canon or of the central
  catalogue): how to launch it headless, resume a session, read its
  stream, inject mail (hooks), which protocols it speaks (MCP and version,
  ACP, app-server), permission model, source of its gauges and its cost,
  accepted models and efforts, installation method and pinned version.
- **Generic adapters** in the runner: an **ACP** adapter (all the
  harnesses that speak it), a **descriptor-driven** adapter for
  command-line harnesses, and specific adapters only when
  indispensable (Claude stream-json, Codex app-server); no more closed
  list of harnesses in the code or the schema.
- **Conformance test bench**: a suite that checks that a harness can do a
  turn, resume the session, receive mail, respect the tool and permission
  policy, report its cost — hence a conformance level
  published in the catalogue.
- **Evaluation** harness × model × effort on the reference tasks of 0020,
  with the same safeguards (costly action under the gate, plans).
- **Centralised service = sensitive supply-chain point**: a
  host installs a harness only from a **signed** entry, with a **pinned
  version** and a **verified checksum**, admitted by the host policy
  ([0014](0014-orchestrateurs-a-tours-et-placement.md)); installation is an
  action subject to the gate.
- Requirement R22; lots L16 (descriptors and generic adapters, in
  the runner) and L17 (central catalogue, conformance test bench, harness
  evaluation).
