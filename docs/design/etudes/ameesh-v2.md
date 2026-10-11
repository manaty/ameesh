---
type: Study
title: "Study — ameesh v2: personas across devices and servers, with roles, identity and authorisation"
description: "The v2 plan: agent work independent of human hardware; servers, capacity-based placement and moving personas with their memory; devices coordinated through Nexlink; addressable roles with holders and deputies; OIDC human identity and relationship-based authorisation; isolation and persona memory with access rights. Options, lot order and current delivery status."
status: draft
tags: [v2, servers, multi-host, nexlink, persona, role, identity, authorisation, isolation, memory]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T05:45:00+02:00" }
stale_after: 2027-01-09
sources:
  - { resource: "../decisions/0003-ameesh-et-nexlink.md", title: "Decision 0003 (ameesh and Nexlink)" }
  - { resource: "../decisions/0012-autorite-par-ameesh-approve.md", title: "Decision 0012 (authority)" }
  - { resource: "../decisions/0015-interfaces-des-facades.md", title: "Decision 0015 (public interfaces)" }
  - { resource: "../decisions/0017-approbation-via-page-nexlink.md", title: "Decision 0017 (approval through Nexlink)" }
  - { resource: "../decisions/0028-ressources-des-hotes-et-repartition.md", title: "Decision 0028 (host resources)" }
  - { resource: "../decisions/0029-persona-et-session.md", title: "Decision 0029 (persona and session)" }
  - { resource: "../decisions/0032-persona-roles-and-sessions.md", title: "Decision 0032 (persona, roles, sessions)" }
  - { resource: "../decisions/0033-isolated-meshes-hosting-and-identity.md", title: "Decision 0033 (isolated meshes, hosting, identity)" }
  - { resource: "persona-et-harnais.md", title: "Persona and harness study" }
---

# Purpose

V1 runs on **one device**, its owner's workstation: a local Postgres database,
a runner per agent, canons in git clones and desktop notifications. On
2026-10-09, that workstation had 17 admitted personas for 16 slots, with swap
under pressure: a human's hardware limits the work.

V2 must:

1. **Run personas on servers**, so work no longer depends on human devices.
2. **Coordinate several devices**, workstations, phones and servers, through
   Nexlink ([0003](../decisions/0003-ameesh-et-nexlink.md)).
3. Apply **persona, role and session**
   ([0032](../decisions/0032-persona-roles-and-sessions.md)).
4. Provide what multiple machines and humans require: **identity**,
   **authorisation**, **isolation** and **memory with access rights**.

**Outside v2 scope**:

- Creating personas by cloning or assembly (0032 §3).
- Specialising workers through learning (v3).
- Designing Nexlink's own interface, which belongs to Nexlink (0015).

# What v1 already provides

| V1 capability | Use in v2 |
|---|---|
| **Epoch leases**: one runner leads an agent; takeover is monotonic | Exclusivity across hosts already works with a shared database |
| **`Host` cards**, admission, host policy, pressure readings (L31, 0028) | Foundation for capacity-based placement |
| **Multiple canons** (0031, restricted by 0033) | Several teams in an organisation; different organisations have separate meshes |
| **Cluster profile**: one agent per pod, external database, read-only canon, no incoming port | Server hosting model to extend beyond one agent |
| **ameesh-approve** and signed receipts (0012, 0017) | Remote human authority, including on a phone |
| **Pushed alerts** (L38: desktop, ntfy, Slack) | Notify humans away from their machines |
| **Thread transport interface** | Nexlink and other connectors |

Missing from that baseline:

- Persona/session separation.
- Memory outside transcripts.
- A Postgres role per agent.
- Verified human identity.
- Isolation between agents on one host.
- Moving a persona between hosts.

# Area A — multiple hosts, including servers

**A1. Shared database.** Postgres reachable by every host, managed or on a VM,
over TLS. A role per host, then per agent: an agent must no longer be able to
read or modify another's state (a known cluster-profile limitation). Off-site
backups and a tested restore are required. A workstation's local database
remains possible for a personal mesh.

**A2. Server runners.** Two profiles:

- **VM and systemd**: v1 as it stands, one service per agent. Simple, with
  Unix-user isolation (area E).
- **Cluster**: one pod per active persona. Stronger isolation, more operational work.

Both profiles share the database and lease model.

**A3. Capacity-based placement.** The canon declares **rules**, as required by
0029: admitted hosts or labels, persona constraints, visibility of its
repository. It does not declare a location. ameesh chooses a host when opening
a session, using pressure readings. When a workstation is saturated, the next
session starts on a server.

**A4. Moving a persona with what it knows.** This is the difficult part.
Harness sessions are local files: Claude transcripts, Codex threads, DeepSeek
sessions. Three options:

| Option | Principle | Limits |
|---|---|---|
| **A4a — persona memory** (0029, P1/P4) | The persona carries its memory in a git repository; a fresh session on the new host loads it | Detailed ongoing conversation context is lost; move only between lots |
| **A4b — session copy** | Copy the transcript to the new host before resuming | Harness-specific; paths and accounts need rewriting; fragile |
| **A4c — shared storage** | Sessions live on a shared volume | Infrastructure dependency, locking, unsuitable for workstations |

**Recommendation**: A4a as the rule, under 0032 §5. A4b only to finish an
ongoing lot, and only for harnesses that support it.

**A7. Session backups** (0032 §2). Sessions are part of the persona. Each
runner copies its sessions off-device at the end of each turn, to mesh object
storage on a server or at the backup provider. Between devices, copying goes
through Nexlink (area B). Copies are encrypted because sessions contain
everything the agent has read. They also support A4b: resuming elsewhere means
restoring the latest copy.

**A5. Model accounts.** Provider terms must be checked before using a human
subscription on an unattended server. Under 0033 §3:

- Server personas use the organisation's **API keys**, billed by token under
  ameesh's caps (0019).
- Human subscriptions remain on human devices.
- Host policy (`credential_modes`) enforces this.

**A6. Work repositories.** Each host has its clones and worktrees, with deploy
keys limited to the persona's repositories (0029, visibility).

# Area B — multiple devices through Nexlink

Decision 0003 already divides responsibilities:

- **Nexlink**: organisations, members, agent accounts, conversations,
  notifications, on-device signatures and audit.
- **ameesh**: runners, sessions, turns, leases, budgets and lots.

**B1. One authority for leases.** Two architectures:

- **B1a — central ameesh database; Nexlink for humans and transport.** Every
  device running personas has a runner connected to the database. Nexlink
  displays, converses, notifies and collects signatures.
- **B1b — local ameesh instances synchronised by Nexlink.** More resilient
  offline, but still requires a single lease authority.

**Recommendation**: B1a. An offline device can run its own unshared local personas.

**B2. Talk to a persona from any device.** A Nexlink conversation with a
persona or **role** replaces `ameesh attach` in a terminal. ameesh provides a
conversation interface: a human message becomes a turn, the answer returns to
the thread, interactive control goes through the `attach` lease. Decision
0015 requires a public interface so an organisation can build its own without Nexlink.

**B3. Approvals and notifications.** ameesh-approve is relayed through a
Nexlink page (0017), using the human's phone passkey. Alerts use a Nexlink
channel alongside desktop, ntfy and Slack.

**B4. Devices as hosts.** A workstation can remain a host for its
subscriptions and a human's private personas (0029, visibility). A phone is a client.

# Area C — roles

0032 defines a role as a function within a scope, held by a persona or human.
The remaining design covers:

- **C1. Canon declaration.** A `Role` card (name, scope, description, rights).
  Persona cards list held roles: holder or deputy, with dates.
- **C2. Addressing.** `ameesh mail send role:<role>@<scope>` and lots assigned
  to roles resolve at sending time: the available holder, otherwise a deputy.
  Record resolution in the thread so recipients are known.
- **C3. Absences.** A holder, human or stopped persona, is **unavailable** for
  a period. Deadline delegation (L40), currently agent-only, extends to
  humans. The responsible human's **superiors** (0032 §2) form the escalation
  chain when neither holder nor deputies respond.
- **C4. Vocabulary.** The owner-approved common foundation (0033 §8):
  coordinator, author, reviewer, approver, domain expert and watcher. Each
  canon adds its own roles, such as migration checker.
- **C5. Observation.** `ameesh list --role`; find a scope's coordinator by role.

# Area D — identity and authorisation

- **D1. Human identity.** ameesh is an **OIDC client**, with a generic provider
  chosen by the organisation (0033 §4). Trials use Keycloak and Supabase Auth;
  Nexlink must be compatible. The provider establishes *who* the human is;
  the canon establishes their organisational membership, roles and
  responsibilities. The ameesh-approve passkey remains the authority (0012).
- **D2. Relationship-based authorisation.** “The holder of project X's
  production approver role can approve a deployment of X.” The **canon stays
  the source**, changed through reviewed PRs. ameesh evaluates rules itself
  or feeds optional **OpenFGA** during canon sync, like authenticators.
- **D3. Acting on someone's behalf.** When a persona acts for a human, the
  tool sees both identities through token exchange (RFC 8693). Its own rights apply.
- **D4. Database.** A Postgres role per agent, limited to its rows, messages
  and lots. A separate owner role performs migrations, never a runner.

# Area E — isolation

- **E1.** A **Unix user per persona** on a VM, or a **pod** in a cluster. This
  addresses the baseline risk of running everything as the owner's Unix user.
- **E2.** An **MCP proxy** for session tools. It enforces policy: injects
  identity (D3), asks the rule engine (D2) whether a call is allowed, and logs it.
- **E3.** Outbound network access limited to what the persona needs: model
  provider, forge and database.

# Area F — persona memory with access rights

- **F1.** Each persona's memory repository (0029, P1/P4): Markdown files
  consolidated at fixed points (lot end, handoff), loaded by any harness.
  **V2 priority** (0032 §5).
- **F2.** Levels: organisation (reviewed canon), project (threads), persona
  (its repository), person (the private repository of their digital twin).
- **F3.** Reading rights use the same relations as D2: who may read which
  memory. The visibility rule in 0029 is the first case.

# Area G — visibility for the responsible human (0033 §9)

- **G1. Activity log per persona.** Chronological sessions opened/closed,
  turns (trigger, duration, outcome), messages received/sent, lots (states,
  verdicts), actions/receipts and spending. Registry, threads, per-turn costs
  and actions already provide data. Missing: the persona view and retention
  beyond a session's lifetime.
- **G2. Session content.** The responsible human can open current or past
  transcripts from backups (A7), to understand *why* an agent acted.
- **G3. Access.** The responsible human, their deputies and superiors
  (0033 §9), through area D relations (“responsible for” → “may read activity
  of”). Every transcript consultation is recorded.
- **G4. Where.** CLI (`ameesh activity <persona>`), the real-time view in 0024
  and Nexlink, with the same public interface (0015).
- **G5. Retention.** Per organisation, set in its canon; default 90 days for
  transcripts and one year for the activity log (0033 §9).

# Proposed order

| Phase | Content | Depends on |
|---|---|---|
| **1** | Separate persona and session (`Persona` card, registry, multiple sessions); persona memory (F1) | 0029, 0032 |
| **2** | Roles (C1–C5); persona activity log (G1, G3, G4) | 1 |
| **3** | Shared database and Postgres roles (A1, D4); first VM/systemd server (A2) | 1 |
| **4** | Capacity placement and moves between lots (A3, A4a); server API accounts (A5) | 1, 3 |
| **5** | OIDC identity and deployed ameesh-approve (D1, B3) | 3 |
| **6** | Conversation interface and Nexlink connector (B2) | 2, 5 |
| **7** | Relationship-based authorisation, optional OpenFGA (D2, D3, F3) | 2, 5 |
| **8** | Isolation: Unix user or pod, MCP proxy (E1–E3) | 3, 7 |

# Lots (breakdown of 2026-10-09)

The server lot goes first: once delivered, personas work there to accelerate
the remaining lots (owner's instruction).

| Lot | Phase | Content |
|---|---|---|
| **L55** | 3 | **VM server profile**: Terraform, cloud-init installation of TLS Postgres, ameesh, systemd and backups; private device network (WireGuard); procedure and rollback |
| **L56** | 3 | **Postgres roles** per host and agent (D4); separate migration owner role |
| **L57** | 3 | **Mesh split**: export a canon's personas and state to its organisation's database (0033 §1) |
| **L51** | 1 | **`Persona` card** (also read `Agent`): ordered `harnesses`, `roles`, `memory.repository`; `Member` card: `deputies`, `superiors`, roles |
| **L52** | 1 | **Personas and sessions in the registry**: several sessions per persona, one lease per session |
| **L53** | 1 | **Session backups** (A7): encrypted end-of-turn copy to mesh object storage; restore elsewhere |
| **L54** | 1 | **Persona memory** (F1, P3/P4): git repository, startup loading, end-of-lot consolidation |
| **L58** | 2 | **Roles** (C1–C5): `Role` card, `role:` addressing, deputies, absences, escalation |
| **L59** | 2 | **Activity log** (G1–G5): `ameesh activity`, responsibility-based access, retention |

## Lot status as of 2026-10-11

This replaces the source study's 2026-10-09 readiness snapshot. “Open” means
awaiting rebase and review on current main, not current CI approval. The
triage is recorded on lot #139.

| Lot | PR | Status | Depends on |
|---|---|---|---|
| L55 VM server profile | [#148](https://github.com/manaty/ameesh/pull/148) (replaces #16) | Merged into main | — |
| L57 mesh split | [#148](https://github.com/manaty/ameesh/pull/148) (replaces #22) | Merged into main; mesh split applied on 2026-10-10 | L55 |
| L51 Persona card, deputies and superiors | [#17](https://github.com/manaty/ameesh/pull/17) | Open; rebase after decisions 0032/0033 | Decisions |
| L58 addressable roles | [#20](https://github.com/manaty/ameesh/pull/20) | Closed; replacement lot to follow L51 | L51 |
| L53 session backups | [#18](https://github.com/manaty/ameesh/pull/18) | Open; rebase after L52 | L52 integration |
| L54 persona memory | [#21](https://github.com/manaty/ameesh/pull/21) | Open; rebase after L53 (shared runner changes) | L53 integration |
| L52 session history (first part) | [#24](https://github.com/manaty/ameesh/pull/24) | Open; rebase after L51; parallel sessions remain separate | L51 integration |
| L59 activity log | [#19](https://github.com/manaty/ameesh/pull/19) | Open; rebase after L54; responsibility filtering remains | L54 integration |
| L56 runner Postgres role | [#23](https://github.com/manaty/ameesh/pull/23) | Open; separate lot #140, in parallel after L51 | Current main |
| D1 OIDC identity | [#25](https://github.com/manaty/ameesh/pull/25) | Closed; deferred | — |
| D2 relationship-based activity access | [#27](https://github.com/manaty/ameesh/pull/27) | Closed; deferred | L59, L51, D1 |
| E1 Unix user per persona | [#26](https://github.com/manaty/ameesh/pull/26) | Closed; not merged; container trial is not delivery | L57 |
| E2 MCP proxy | [#28](https://github.com/manaty/ameesh/pull/28) | Closed; deferred | — |

Review/merge order from lot #139: decisions 0032/0033 (#135), L51 #17, L56
#23 in parallel (#140), then L52 #24, L53 #18, L54 #21 and L59 #19.
Mesh-design reviews and merges. Old PR #14 is closed; its work is being
redone under lot #138. PRs #29–#33 are closed because parallel-session option
B was dropped. Historical readiness and test counts from 2026-10-09 do not
establish CI results for the rebased PRs.

Remaining design or owner work:

- **Parallel sessions** (rest of L52): the source proposal remains in
  [PR #15's parallel-session study](https://github.com/manaty/ameesh/blob/108de7f6098a6a936b86ec6366291d86a6c85caa/docs/design/etudes/sessions-paralleles.md).
  Its option B recommendation was dropped in the 2026-10-11 triage; it is
  historical, not an approved implementation plan.
- **D2**, relationship-based authorisation, depends on L51, L59 and D1.
- **E**, isolation: E1 #26 is closed; a delivery plan is still required,
  followed by the MCP proxy.
- **B2**, conversation interface, with Nexlink (issue #47 in that repository).

# Owner questions already answered

1. **One mesh per organisation**, isolated: no persona works for another
   organisation (0032 §7, 0033 §1).
2. **The organisation chooses the first server**, existing infrastructure or
   a VM; VM and systemd first (0033 §2).
3. **Server personas use the organisation's token-billed API keys**; human
   subscriptions remain on workstations (0033 §3).
4. **Generic OIDC chosen by the organisation**; trials with Keycloak and
   Supabase Auth; Nexlink compatible (0033 §4).
5. A persona's “agents” are **its sessions**, backed up off-device (0032 §2, A7).
6. **Phase 3 and phase 1 in parallel, then roles** (0033 §5).

# Risks

- **Provider terms** for server subscriptions (A5): verify before deployment.
- **Context loss** when moving a persona (A4): acceptable between lots,
  not in the middle of one.
- **Database exposure** (A1): TLS, per-agent roles, restricted network.
  Without D4, a compromised agent can read everything.
- **Nexlink dependency** is prohibited by 0015. Every function using Nexlink
  keeps its public interface.
- **V1 → v2 migration load**: v1 agents live until their lot ends (0032 §6).
  Reading both `Agent` and `Persona` lasts through the transition.
