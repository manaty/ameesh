---
type: Decision
title: "Persona and session: an agent's durable identity is separate from its executions"
description: "A persona (identity, role, person in charge) is described in the canon and has no position; a session is an ephemeral execution of the persona in a harness, on a host, placed by ameesh. Each human has a particular persona, their digital twin."
status: stable
tags: [persona, session, memoire, double-numerique, placement, harnais]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "owner decision in his conversation with mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T18:00:00+02:00" }
---

# Decision

1. **Persona**: the durable identity of an agent — name, role, human in charge,
   capabilities (never `approve`) and policy; it has a memory, kept by
   the harness (see below). The canon
   describes it; it has **no position** (no host, no directory).
2. **Session**: an execution of the persona in a harness, on a host, with
   a model, an account and a lease. It is ephemeral, placed by ameesh
   ([0028](0028-ressources-des-hotes-et-repartition.md)), opened per lot
   ([0025](0025-une-session-par-lot.md)). **A persona can have several
   sessions in parallel** (for example one per lot).
3. **Harness**: third-party harnesses are supported (ACP, L16), notably
   for subscriptions that can only be used through their own tools;
   an in-house harness remains possible but optional.
4. **Digital twin**: each human in the system has a particular persona,
   their digital twin. It never approves
   ([0012](0012-autorite-par-ameesh-approve.md)): authority remains the
   human's passkey.
5. **Placement**: the canon declares rules (persona constraints,
   admitted hosts or host labels, host policy), not the current
   position; a session's host and working directory are runtime
   state. The current `Placement` cards become admissions without
   `cwd`.

# Visibility of a persona (owner's clarification)

**Single rule: a persona only runs on a human's machine if that
human has access to the persona's repository.** The owner of a machine can read
everything on it: control is therefore done at placement, through the
repository rights. For a server or a cluster host, the rule applies to its
person in charge and to those who administer it. A persona reserved for a few
lives in a repository reserved for those people; a human's digital twin,
in a repository private to that human, therefore only runs on their machine — with no
particular rule. This control adds to the placement rules
([0028](0028-ressources-des-hotes-et-repartition.md), lot L31).

# Choices made after the study (2026-10-05)

See [persona and harness](../etudes/persona-et-harnais.md).

1. Persona memory is a **neutral contract** (file format in one
   git repository per persona, persona text); an in-house harness is
   **optional**: it would just be one more harness that reads this contract.
2. The harnesses' native memories are **disabled** for sessions
   launched by ameesh.
3. Visibility is checked through the **forge API**, with an `admins` key
   in the `Host` card; the trial clone remains a fallback for a personal
   workstation.
4. The limited-access credential for a persona's repository is created by the
   **host's person in charge** and stored in the host's secrets.
5. Memory is consolidated by **the session itself**, at fixed points
   (end of lot, handover).
6. The Persona card is the subject of a **separate lot** (P1).

# To be specified later

- The digital twin's powers (what it may answer on behalf of its human,
  emergency signalling).

**Memory belongs to the harness, not to ameesh.** It follows a neutral contract
(see "Choices made": Markdown files in one git repository per persona,
readable by all harnesses); each harness loads it at the start of a
session and enriches it afterwards. An in-house harness is optional. ameesh neither stores nor defines it; it only
passes to the harness the identity of the persona it runs. The digital
twin may also live outside ameesh; the authority rule stays in
ameesh: no module approves
([0012](0012-autorite-par-ameesh-approve.md)).

# Consequences

- Overhaul of the canon profile (`Agent` → persona) in a separate lot (P1);
  `Placement` → admission and visibility check at placement in L31.
- Accounts (L30) and dynamic placement (L31) apply to sessions.
- No memory in the ameesh core: the persona memory component
  (lot P4 of the study) is separate; ameesh designates the persona when launching a
  session and binds the session to the loaded memory versions.
