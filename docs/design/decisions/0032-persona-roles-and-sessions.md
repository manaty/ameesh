---
type: Decision
title: "Persona, roles and sessions: who acts, in what capacity, in which execution"
description: "Persona = who (distinct personality, bound to one canon: memory, roles, rights, tools and skills, sessions backed up off-device, responsible human with deputies and superiors); role = a function within a scope; session = execution. No generic workers; persona memory takes priority in v2; v1 agents live until their lot ends; one mesh per organisation."
status: stable
tags: [persona, role, session, memory, v2]
decided_by: human:smichea
decision_date: 2026-10-09
attestation: "owner's answers in their conversation with mesh-design (2026-10-09); unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T05:30:00+02:00" }
sources:
  - { resource: "0029-persona-et-session.md", title: "Decision 0029 (persona and session)" }
  - { resource: "0030-pas-de-travail-sans-reveil-possible.md", title: "Decision 0030 (wakeup, delegation)" }
  - { resource: "../etudes/persona-et-harnais.md", title: "Persona and harness study" }
---

# Context

Decision [0029](0029-persona-et-session.md) separates a persona from its
sessions, but v1 does not implement this: a registry row, the “agent”, holds
both identity (responsible human, capabilities) and execution (harness,
session, lease). There is therefore only one session per agent. Names inherited
from v0 (`deepseekN`) combine a harness and a number, and memory is only the
session transcript. The concept of a **role** is also missing: a permanent
agent holding a stable function for a team, such as checking database
migrations, currently has only a name.

# Decision

1. **Three distinct concepts.**
   - **Persona**: *who* acts.
   - **Role**: a *function within a scope* (team, project, service), held by a
     persona or a human.
   - **Session**: an ephemeral *execution* of the persona (harness, model,
     account, host, lease), in accordance with [0029](0029-persona-et-session.md).

   In everyday language, “agent” means a persona that is not a human's digital twin.
2. **A persona has a distinct personality, bound to a single canon.** At a
   given time, it is defined by:
   - its **memory**;
   - its **roles**;
   - its **rights**;
   - its **tools and skills**;
   - its **sessions** (current and past executions);
   - its **responsible human**, with their **deputies** and **superiors**.

   Sessions are part of the persona: they are **backed up outside the device
   running them**, on a mesh server or, between devices, through Nexlink.
   Losing a device does not mean losing a persona.
3. **Creating personas.** New personas may be created **by cloning** or **by
   assembly**. The details are outside the scope of v2.
4. **No generic workers.** A worker specialises in the domain where it works.
   Creating one by cloning or learning belongs to v3.
5. **Memory per persona takes priority in v2.** This is the memory repository
   in [0029](0029-persona-et-session.md) (study lots P1 and P4). It also lets a
   persona change hosts, for example to a server, without losing what it knows.
6. **V1 agents live until their lot ends.** None is renamed. New agents are
   created as personas, with a role name.
7. **One mesh per organisation.** An organisation has its own mesh: database,
   runners and personas. A device can participate in several organisations'
   meshes, with one runner per mesh. Several canons for a single organisation
   are not a v2 requirement.

# To clarify

- **The role model**:
  - addressing by role;
  - the holder and deputies;
  - absences;
  - a role held by a human or an agent;
  - vocabulary shared by all canons and vocabulary specific to each canon.

  This will be investigated in the v2 study.
- **The responsible human's superiors**: the escalation chain, and what a
  superior can do in place of an absent responsible human.

# Consequences

- The canon's `Agent` card becomes `Persona` (lot P1, already planned in
  0029), with roles, tools and skills, and the responsible human with their
  deputies and superiors. The `Agent` card remains readable during transition.
- The registry separates personas from their sessions: several sessions per
  persona, and one lease per session.
- The ameesh v2 study takes these three concepts as its foundation. The study
  from PR #15 is published separately from this decision.
