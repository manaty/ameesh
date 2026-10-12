---
type: Specification
title: "ameesh requirements (R1–R23)"
description: "The requirements of the original mesh spec (R1–R7), generalized, and those added by the decisions of 2026-10-03 (R8–R17)."
status: draft
tags: [exigences, specification]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
sources:
  - { resource: "../../MESH-SPEC.md", title: "Original mesh spec (§4, R1–R7)" }
  - { resource: "decisions/index.md", title: "Owner decisions" }
---

# Original requirements, generalized

| # | Requirement | Change from the original spec |
|---|---|---|
| R1 | The **members** of a team (humans and agents) work from several machines — devices, PCs, servers, pods — and communicate in real time. | "agents" → "members" ([0002](decisions/0002-portee-equipes-mixtes.md)) |
| R2 | Any harness, through its **standards**: MCP for outbound, driving through stream-json, app-server or **ACP** for wake-up. | adapters → standards ([0007](decisions/0007-standards-des-harnais.md)) |
| R3 | An agent runs in only one place at a time (lease); a **business object** (ticket, order preparation) is handled by only one member at a time. | leases on external objects |
| R4 | A human's authority is **proven** by a signature made on a device the agents do not control, bound to the exact fingerprint of what is approved. | owner → any authorized human; key out of the agents' reach |
| R5 | Nothing is lost: durable mail, replayable turn, lease that expires. An **irreversible action** follows the execution protocol below: it is **never retried until the risk of a duplicate is removed** (guaranteed deduplication or reconciliation) **or explicitly accepted by an authorized human**. | idempotence and unknown outcome |
| R6 | Interchangeable transport: Nexlink (native), open source stack (Matrix, Zulip), common tools (Slack, Teams, Google Chat, e-mail). | [0003](decisions/0003-ameesh-et-nexlink.md), [0004](decisions/0004-open-source-et-outils-courants.md) |
| R7 | Budget and model per agent; view of subscriptions; budget per project. | budget per project |

# Requirements added on 2026-10-03

| # | Requirement | Source |
|---|---|---|
| R8 | Humans and agents are members of equal rank in the model (address, presence, response time); **decision authority is reserved to humans**: an agent never has the `approve` capability. A human can sign a **bounded standing approval** (action class, cap, expiry, revocable) that the gate enforces; the authority then remains the human's signature, never the agent's. | [0002](decisions/0002-portee-equipes-mixtes.md) |
| R9 | Several organizations and several teams, with data isolation. | [0003](decisions/0003-ameesh-et-nexlink.md) |
| R10 | Humans' devices are full access points: notification, reading, signed approval from the phone. | [human authority study](etudes/autorite-humaine.md) |
| R11 | The work domain is not hard-coded: pipelines, deliverable types, review rules and autonomy policy are configured per team, in the canon. | [0002](decisions/0002-portee-equipes-mixtes.md) |
| R12 | **Human readability**: every exchange, including agent to agent, happens in natural language in a thread readable and auditable by the team; no hidden channel. | [0006](decisions/0006-lisibilite-humaine.md) |
| R13 | **Signed messages**: each message carries a provenance signature; each decision an authority signature. | [0009](decisions/0009-paroles-signees.md) |
| R14 | **Human accountability**: every agent, orchestrators included, has a responsible human — declared in its card, or, for an ephemeral agent without a card, **inherited from the agent that created it** and recorded by ameesh at its creation. An agent without a resolved responsible human does not run. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R15 | **Living projects**: a project has responsible humans, one or more orchestrators, members (sometimes shared), a canon and dependencies; it is created, split, merged or archived with traced lineage. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R16 | **Decisions without relay**: a decision made in any human–agent conversation is recorded in the project canon (one decision per file); the orchestrators read it there. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R17 | **Human attention**: each human has a single queue of the decisions awaiting them, across all projects. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R18 | **Governed placement**: which agent runs on which host, and with an API key or a plan, is decided by the project's ameesh lead (or the infra they delegate to), within the limits of the policy set by each host's lead; ameesh facilitates this management and supervises it. | [0014](decisions/0014-orchestrateurs-a-tours-et-placement.md) |
| R19 | **Speed**: review proportionate to the risk class (declared in the canon per file scope); turn interruption by priority message; session rotation with a resumption summary; sessions robust to relocation; per-lot lead times measured. | [0018](decisions/0018-vitesse-des-agents.md) |
| R20 | **Budgets**: model and effort per task; caps per harness, agent and project; never exceeding a plan, read at its source; accounting per turn. | [0019](decisions/0019-budgets-et-routage.md) |
| R21 | **Model catalog**: ameesh lists, discovers and evaluates models and effort levels on reference tasks, and recommends model and effort per task class; the routing policy stays in the canon. | [0020](decisions/0020-catalogue-et-evaluation-des-modeles.md) |
| R22 | **Harness catalog**: harnesses are no longer hard-coded; a centralized service describes them, certifies them with a conformance test bench, evaluates them with the models and makes them available (signed installation, pinned version). | [0021](decisions/0021-catalogue-des-harnais.md) |
| R23 | **Self-repair**: bugs surfaced by interactions are detected, deduplicated, triaged according to a policy in the canon, fixed, verified, shipped and monitored by agents; no human decision is inferred from a text or from silence for an irreversible action. | [0022](decisions/0022-auto-reparation.md) |

# Execution protocol for an irreversible action (R5)

An irreversible action (sending to the customer, refund, shipping label, pickup
booking, merge) goes through the gate (ameesh-gate) and through states
**durably logged before each effect**:

```
proposée → approuvée → lancée → confirmée | échouée | inconnue
```

Two distinct identifiers:

- the **action identity** (`action_id`): assigned at proposal, **stable
  across all attempts** of the same action. It is the **idempotency
  key** passed to the external system when it allows it;
- the **authorization nonce**: specific to each signed receipt, consumed once.
  A receipt authorizes one attempt of a given action; it never creates a
  new action.

1. **"lancée" (launched) is written before the external call**; the receipt is consumed at that
   moment and can no longer be used.
2. **Unknown outcome** (effect possibly done, response lost): state `inconnue` (unknown),
   **no new automatic attempt**.
3. **Guaranteed deduplication**: if the connector declares that the external system
   deduplicates on the idempotency key (for example an
   `Idempotency-Key` header documented by a payment API), a new attempt
   **of the same action** — same `action_id`, new receipt — carries no risk of a
   duplicate.
4. **Reconciliation**: otherwise, the connector finds the outcome through a read
   (look for the e-mail in the sent items by its `Message-ID`, the shipment by its
   reference). Found → `confirmée` (confirmed) or `échouée` (failed).
5. **Neither deduplication nor conclusive reconciliation**: wait for a human
   (R17), with the context. Only an **explicit decision by an authorized human,
   who accepts the risk of a duplicate**, can create a **new action**
   (new `action_id`); this decision is signed and bound to the
   `inconnue` action it replaces.
6. After `échouée` (certain failure, no effect), a new attempt of the
   same action requires a new receipt; it keeps the same `action_id`.

**`Message-ID` is not a deduplication guarantee**: SMTP servers do not
deduplicate on this header. It only serves as a **reconciliation handle**.

Each irreversible-action connector therefore declares, in the canon: whether the
external system **guarantees** deduplication on a key (with the reference to
its documentation), and how to reconcile an unknown outcome.

# Resulting architecture principles

1. **Three layers**: the **canon** (OKF in git, federated), **communication**
   (readable threads on Nexlink, Matrix, Slack…), **execution** (ameesh).
2. ameesh **writes to the canon only through a reviewed** proposal.
3. The **cards** (agents, teams, lots) are in the canon; their **state** is
   in ameesh.
4. Integrations are **standard MCP servers**; ameesh provides the
   wake-up (runner), the thread (ameesh-fil), the gate (ameesh-gate) and the proof
   (ameesh-approve).
