---
type: Decision
title: "Automatic switch to a secondary account, back to the primary after the reset"
description: "For each provider, an ordered list of accounts; when the active account nears its limit, the following turns move to the next account, and return to the primary as soon as its window is reset."
status: stable
amended_by: ["0034-consommer-d-abord-ce-qui-expire.md"]
tags: [budgets, forfaits, comptes, routage]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "owner request in his conversation with mesh-design (80% of the 7-day Claude plan consumed); unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T09:40:00+02:00" }
---

# Decision

1. For each provider (Claude, Codex, DeepSeek, …), a host or a team
   declares an **ordered list of accounts** (primary, secondary, …). Each
   account has its credentials (host secrets, never in the canon) and its
   gauges.
2. **Automatic switch**: when the active account reaches the threshold of the
   budget guard ([0019](0019-budgets-et-routage.md):
   `min(90 %, elapsed share of the window + 10 points)` for a plan, cap or balance for a pay-per-token account), the
   **following turns** use the next account in the list instead of pausing
   the agents. The pause only happens when all accounts are
   at the threshold.
3. **Automatic return** to the primary account as soon as its window is reset
   (or its balance topped up), at the next turn.
4. Never in the middle of a turn; each switch is logged (event, team
   thread) and visible in `ameesh cost` and `ameesh progress`.
5. Session continuity is sought (same session resumed under the other
   account when the harness allows it); otherwise rotation with a resume summary
   ([0025](0025-une-session-par-lot.md)).

# Consequences

- Lot L30. Compliance with each provider's terms of use regarding the
  use of several accounts remains the team's responsibility.
- The account login steps (`/login`, `codex login`) remain
  human; ameesh neither stores nor copies any password.
