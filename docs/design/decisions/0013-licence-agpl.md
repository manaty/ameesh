---
type: Decision
title: "ameesh licence: AGPL-3.0-only, with a contribution agreement"
description: "ameesh will be published under the GNU AGPL v3, only variant; external contributions go through a contribution agreement."
status: stable
tags: [licence, release]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:00:00+02:00" }
---

# Context

The manaty/ameesh repository is private until the first release
([0001](0001-nom-ameesh.md)); a licence had to be chosen before going
public. mesh-design recommended Apache-2.0 (adoption, consistency with OKF);
the owner chose the AGPL.

# Decision

- ameesh is published under the **GNU Affero General Public License v3**,
  **`AGPL-3.0-only`** variant: the licence version is fixed at v3; only a
  holder of the rights on the code concerned can decide to change it.
- **External contributions** are accepted only under a **contribution
  agreement** (CLA, to be drafted) intended to leave Manaty the possibility of
  relicensing (later move to `or-later`, to a future version, or dual
  licensing).

# Consequences

- Anyone who runs a **modified** version of ameesh as a network-accessible
  service must offer its source code to its users.
- **Intended policy, conditional on the rights actually held**: for the
  code Manaty holds the rights to, Manaty is not bound by its own
  licence (dual licensing, integration into Nexlink). For code by other
  authors, this freedom depends on the rights granted by the CLA, which is not
  yet drafted; **no guarantee of relicensing third-party code** is
  given here. Ownership of contributions produced by agents
  operated for Manaty is to be confirmed in the same work.
- `or-later` was rejected: it would allow choosing a future version of the
  licence that could be more permissive (for example without the network clause).
  With an adequate CLA, `only` leaves a later evolution open.
- **Compatibility of dependencies and integrations: study to be done**, not an
  established result. It will cover the exact licences and versions of each
  distributed dependency (WebAuthn libraries, MCP SDK…) and the topology
  of integrations (API calls to AGPL services such as Zammad or
  Synapse, or combined distribution).
- To do before going public: `LICENSE` file (official text),
  package metadata, SPDX headers, removal of the hard-coded test bench DSN
  (lot in progress at deepseek7); CLA and its verification on PRs; licence
  compatibility study; secret detection on the history.
