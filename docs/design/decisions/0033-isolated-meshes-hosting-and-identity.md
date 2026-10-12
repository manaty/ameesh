---
type: Decision
title: "v2: isolated meshes per organisation, hosting, identity and human roles"
description: "A persona never works for another organisation; each organisation hosts its mesh (existing infrastructure or a VM); server personas use the organisation's API keys; a generic OIDC identity provider chosen by the organisation; human roles declared in its OKF canon; a responsible human or deputy can do everything the responsible human can do; a common role foundation for all knowledge work."
status: stable
tags: [v2, mesh, organisation, hosting, identity, oidc, role, deputies]
decided_by: human:smichea
decision_date: 2026-10-09
attestation: "owner's answers in their conversation with mesh-design (2026-10-09); unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T06:15:00+02:00" }
sources:
  - { resource: "0031-plusieurs-canons.md", title: "Decision 0031 (multiple canons)" }
  - { resource: "0032-persona-roles-and-sessions.md", title: "Decision 0032 (persona, roles, sessions)" }
---

# Decision

1. **Isolation between organisations.** A persona of one organisation
   **never** works for another organisation, and vice versa. There is no
   invitation of a persona into another organisation's mesh, nor a bridge
   between meshes. A device serving two organisations runs **two separate
   meshes**: two databases, two sets of runners, two sets of accounts and
   secrets. No state is shared.
2. **Hosting chosen by the organisation.** Each organisation hosts its mesh
   on its existing infrastructure or on a VM. The first server profile is a
   **VM with systemd**; the cluster profile waits until there are several servers.
3. **Model accounts belong to the organisation.** Server personas use the
   **organisation's API keys**, billed by token, under ameesh's caps. Human
   subscriptions remain on their devices.
4. **A generic OIDC identity provider, chosen by the organisation.** For
   example Google, restricted to the organisation's email domain. Trials use
   Keycloak and Supabase Auth. Nexlink must be compatible: this is a ticket
   in the Nexlink repository. ameesh does not depend on Nexlink for identity (0015).
5. **Order.** Start with the first server and shared database (study phase 3),
   alongside persona/session separation, persona memory and session backups
   (phase 1). Roles follow (phase 2).
6. **Human roles.** They are declared in the **organisation's OKF canon**
   (its `home` repository), just like persona roles.
7. **Full substitution by deputies.** A persona's responsible human, or one
   of their deputies, can do **everything** the responsible human can do,
   including validation and approval, so a decision is never blocked. Each
   acts under their own identity: a deputy approves with **their own** passkey
   (0012). The action is recorded as performed by the deputy on behalf of the
   responsible human.
8. **A common role foundation for all knowledge work**, beyond software
   development. Each canon adds its own roles. Foundation validated by the owner:
   - **coordinator**: allocates and tracks work;
   - **author**: produces a deliverable;
   - **reviewer**: checks another person's deliverable;
   - **approver**: decides and commits the organisation;
   - **domain expert**: provides authority in a domain;
   - **watcher**: monitors sources and reports findings.
9. **Visibility for the responsible human.** A human sees the **current and
   past** activity of the personas they are responsible for: sessions, turns,
   messages, lots, actions and spending. This also applies to their deputies,
   who can act in their place (§7), and their superiors, to whom situations
   escalate. Past activity relies on session backups
   ([0032](0032-persona-roles-and-sessions.md) §2) and threads. This is the
   view in [0024](0024-vue-temps-reel.md), filtered by responsibility and
   readable on a phone. Default retention, unless the organisation's canon
   sets it: **90 days** for session transcripts, **one year** for the activity log.

# Consequences

- **This decision restricts 0031.** Several canons on the same host remain
  possible, but **a mesh has only one organisation**. A host serving two
  organisations must separate their meshes. A shared database for the canons
  of two organisations on one workstation is a transitional v1 arrangement
  to split in v2.
- The canon profile gains human members' roles, deputies and superiors.
- The v2 study removes cooperation between meshes, and its identity and
  authorisation section follows this decision. The study from PR #15 is
  published separately.
