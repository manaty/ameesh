---
type: Study
title: "Study — several canons on the same host (two organisations)"
description: "Why ameesh can only read one canon, what breaks if two are synchronised, and the chosen model: a canon identified by its federation, synchronisation effects bounded to that canon; lots L42–L45."
status: stable
tags: [canon, federation, multi-canon, synchronisation, placement]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T15:30:00+02:00" }
sources:
  - { resource: "decisions/0005-canon-okf.md", title: "Decision 0005 (federated OKF canon)" }
  - { resource: "decisions/0008-agents-dans-le-canon.md", title: "Decision 0008 (agents in the canon)" }
  - { resource: "decisions/0029-persona-et-session.md", title: "Decision 0029 (persona, admissions)" }
---

# Context

The owner works with two canons at once: manaty
(`~/canon`, federation `manaty`) and that of a second organisation, called
here "Acme" (`~/development/acme/home`, federation `acme-ingenierie`). Request from an agent of that organisation, 2026-10-07: one of its agents
cannot be declared, and `ameesh agent spawn … --by <agent>` fails (« pas
d'humain responsable résolu », "no responsible human resolved", R14). The owner rates the need "very
important".

# What the code assumes

A single canon, everywhere, and no trace of a federation's identity.

* **Configuration**: `canon` and `canon_ref` are strings
  (`config.py:141-147`). All callers go through
  `canon.from_config(cfg)`: runner, `canon` CLI, `mesh`, `exploitation`.
* **Identity**: the `id` in `federation.yaml` is only read for display.
  A card is referenced as `membre:chemin@commit`. But both federations
  name their root `home`: the references collide.
* **Synchronisation** (`canon_sync.sync`):
  - It reads all rows of the host's registry, whatever their
    origin.
  - It **stops** (`stop_removed`) any row whose card is not in
    the synchronised canon and whose member is read. Synchronising A therefore stops
    B's agents, and vice versa, on every runner pass.
  - `sync_packages` likewise removes the work packages of the other canon.
* **State**: `canon_state` is keyed by the host alone, and claiming requires
  `status = 'ok'` for the host. An invalid canon therefore blocks **all**
  agents of the host, whatever their canon.
* **Authenticators**: the synchronisation log is a global sequence
  that requires the root commits to descend from one another. B is refused after A.
  `revoke_absent` revokes humans absent from the synchronised canon.
* **Names**: `agent_registry.name` is the primary key. Hosts, members,
  teams and packages also have global names.
* **The Acme canon is invalid for ameesh.** Its
  `org/agent-harness/agents/*.md` carry `type: Agent`, but they are
  Claude Code sub-agent definitions (`name`, `tools`), without `title`.
  `fiche-title-missing` is a global blocking error.

# Proposed decision

1. **List of canons.** The host configuration carries
   `canons: [{path, ref, untrusted}]` (or `AMEESH_CANONS`). `canon` and
   `canon_ref` remain accepted as a one-element list. The **first** canon
   is the default canon.
2. **A canon is identified by its federation** (`federation.yaml` `id`).
   * References become `<fédération>/<membre>:<chemin>@<commit>`.
   * The registry, work packages, canon state and the authenticator
     log carry a `canon` column (migration 0032).
   * Existing rows, without a canon, belong to the default canon.
3. **Each synchronisation touches only its own canon.**
   * Stops, responsible-human clearings, package removals and revocations
     target only the rows of that canon.
   * State is kept per `(hôte, canon)`, and claiming looks at the state
     of the agent's canon: closure **per canon**, no longer global.
4. **Names remain global; the first canon that declares a name keeps it.**
   An agent name declared by a second canon becomes there an error limited to
   that card (`canon-name-conflict`), never a takeover. Same
   rule for package identifiers.
   A **released** name (card removed from its canon, agent stopped) can be
   taken over by another canon: this is how an agent changes canon without
   losing its mailbox.
5. **Humans per canon.** `human:<id>` resolves in the agent's canon.
   * A human who works in both canons has a `Member` card in
     each.
   * Their authenticators are synchronised per canon and are only revoked
     by the canon that declared them.
6. **Host described by each canon.**
   * Each canon that places agents on `pc-smichea` has its `Host` card there.
   * An agent's admission is judged against the card of its canon.
   * The physical limits the runner applies (`max_agents`, resource
     thresholds) are **the strictest** of the host's cards.
7. **ameesh scope in a shared canon.** `federation.yaml` may declare
   `extensions: {ameesh: {scope: <dossier>}}` (L47: the only free key of the
   OKF Federation schema). Cards of the ameesh profile (`Agent`, `Host`,
   `Placement`, `Member`, `WorkPackage`) are then read only under that
   folder. Foreign `type: Agent` elsewhere in the canon, such as Acme's
   Claude Code sub-agents, are ignored. Without this key,
   the whole canon is read, as today for manaty.

# Lot plan

| Lot | Content | Risk |
|---|---|---|
| **L42** — identity and bounded synchronisation | list of canons; `canon` on registry, packages and state (migration 0032); qualified references; deletions and removals limited to the synchronised canon; `canon_state` per `(hôte, canon)` and claim predicate per canon; name conflict local to the card; the runner synchronises each canon | high (core) |
| **L43** — scope and host per canon | `ameesh.scope`; admission with the `Host` card of the agent's canon; physical limits = the strictest; `spawn` looks for the creator in all canons | medium |
| **L44** — authenticators per canon | log per canon, revocation limited to the declaring canon, trusted `ref` per canon | high (trust) |
| **L45** — tooling and onboarding of Acme | multi-canon `canon check/show/sync` (`--canon <id>`); documentation; PR on acme/home: `ameesh: {scope: ameesh}`, `Member` card smichea, `Host` pc-smichea, `Agent` acme-docs and acme-securite-docs, admissions | low |

Order: L42, then L43 and L44 in parallel, then L45.

# In the meantime

No agent of another organisation is declared in the manaty canon, even
provisionally: each organisation declares its agents in its own
canon, once L42–L45 are deployed. Do not create an agent with
`--by mesh-design`: it would inherit the ameesh responsible human and team, which
skews R14.
