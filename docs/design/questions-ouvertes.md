---
type: Reference
title: "Open design questions"
description: "What awaits an owner decision or a study, after the 2026-10-03 session."
status: draft
tags: [questions, decisions]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
---

# Awaiting the owner

Q1 to Q8 settled as of 2026-10-04 (see
the [decisions](decisions/)). Open as of 2026-10-05:

- Persona and harness ([study](etudes/persona-et-harnais.md)): amend
  [0029](decisions/0029-persona-et-session.md) to make persona
  memory (one git repository per persona) a neutral format, managed by a
  separate component, that can last without an in-house harness; native harness memories
  disabled by default; verification of the visibility rule (forge
  API or test copy, host administrators); creation of
  credentials limited to the repository; who consolidates; Persona card in L31 or
  a separate lot.

- Resident agent ([study](etudes/agent-resident.md), 2026-10-10): the owner
  accepted the study's 7 recommendations on 2026-10-10 (decision 0035). Still
  open, as the recommendations do not cover them: which agents are resident
  by default (orchestrators and permanent roles?); who can talk to an agent;
  the sleep delay (30 min) and the `attach` maximum duration (2 h); Codex
  `app-server` (experimental) now or later; `!stop`.

# Awaiting a study or a prototype

- Licenses: drafting the CLA and ownership of contributions produced by
  agents; compatibility, version by version, of distributed dependencies
  and integrations with AGPL-3.0-only ([0013](decisions/0013-licence-agpl.md)).

- ameesh profile of OKF Federation: `Agent`, `Team`, `WorkPackage`,
  `Pipeline` types; binding `human:<id>` → passkey / device key; `agent:<id>` → account.
  Part of it could move upstream into okf-federation.
- Agent isolation (one Unix user or one container per agent): cost,
  compatibility with harnesses and their sessions.
- Per-human decision queue (R17): where does it live (Nexlink, ameesh, canon)?
- Allocation of agents shared between projects: declarative rule in the canon.
- Detailed analysis of the namesake projects (tlhc/amesh, samchung95/amesh,
  code-rabi/amesh) to reuse their good ideas.
