---
type: Decision
title: "Consume first the capacity that expires soonest"
description: "Among a provider's plan accounts, ameesh picks for each new session the account furthest behind its pace (amendment of 2026-10-10), on a tie the one whose unused capacity expires soonest, within the allowed pace, instead of keeping the secondary accounts as backup. Amends 0027."
status: stable
tags: [budgets, forfaits, comptes, routage]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "owner decision in his conversation with claude3 (mesh-design); unsigned"
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T08:00:00+02:00" }
---

# Context

[0027](0027-bascule-automatique-entre-comptes.md) treats a provider's accounts
as an ordered list: the primary serves alone, and the next one
only takes over at the threshold. On 2026-10-10, the primary Codex account was
at 20% of its 7-day window, for an allowed pace of 52%. Meanwhile,
the secondary account's 5-hour window, unused, was reset
without having been used. A plan not consumed before its reset is
lost. The same day, an estimate showed that the plans were sleeping
while pay-per-token models were doing the work.

On top of that there was a defect: a gauge reading whose window had expired
kept its last value (93%). The account therefore looked saturated, and
ameesh would never have picked it.

# Decision

1. **A provider's plan accounts form a pool**, not
   a backup list. For each turn, ameesh picks, among the usable
   accounts (credentials present, under their pace threshold from
   [0019](0019-budgets-et-routage.md)), the one whose **unused capacity
   expires soonest**. It uses the account's most constraining window,
   and on a tie, the declared order. *Amended on 2026-10-10, see
   below: the primary criterion is now the lag behind pace.*
2. **Pace remains the guard.** An account is never pushed beyond
   `min(90 %, elapsed share of the window + 10 points)` of its window. The pause only happens
   when all accounts are at the threshold.
3. **A stale reading does not block.** A reading whose window has expired
   counts as 0%. An account with no recent reading is **re-read** before being
   set aside, or tried, since the first turn reports its gauge.
4. **Session continuity.** The account is not changed during a
   session for a marginal gain. A session stays on its account as long as
   that account is under its threshold. The choice is made when a session is
   opened (0025: one session per lot) and at rotation, with the
   resumption or summary rules of 0027 §5. A Codex session is never portable
   from one account to another.
5. **Transparency.** Each account choice is logged with its reason
   (« expire dans 52 min, 0 % utilisé », "expires in 52 min, 0% used").
   `ameesh accounts list` shows, for each account, the capacity that will be
   lost at the next reset if nothing changes.
6. **Human override unchanged.** `ameesh accounts use` still takes precedence,
   and `ameesh accounts auto` hands control back to this rule.

# Consequences

- Amends 0027 §2–3: "switch at the threshold" and "return to the primary" are
  replaced by this choice. The pause when all accounts are at the threshold, the
  logging and the continuity remain.
- Lot L74 (implementation), after the fix for stale readings (L71).
- Compliance with each provider's terms of use regarding the use
  of several accounts remains the team's responsibility (0027).

# Amendment of 2026-10-10: spread across all accounts

Owner's approval on 2026-10-10; lot L117.

## Finding

That same evening, `ameesh accounts list` showed the primary Claude account at
37% of its 5-hour window (pace 83%) and at 35% of its 7-day window
(pace 55%). The secondary and tertiary accounts showed « — »: never
used. Their weekly plans were being lost, although three
subscriptions are paid for.

The rule of §1 was the cause. The primary always has an open window,
hence a capacity "that expires", and it stays under its pace. An account with no
open window has nothing that expires and came after. The primary therefore always
won, and session continuity (§4) froze the situation.

## Amended rule (§1)

**A never-used account must not stay unused.** For a new
session, or at rotation, ameesh picks, among the usable accounts
(credentials present, under their pace threshold in **all** their
windows), the one that is **furthest behind its pace**:

- an account's lag is the ratio between what is used and what is allowed at this
  point of the window, `utilisé / min(90 %, elapsed share of the window + 10 points)`. The
  denominator is the guard ceiling of [0019](0019-budgets-et-routage.md),
  the one shown by the « rythme » ("pace") column. The smallest ratio wins;
- the ratio is taken on the account's **most constraining window** (the
  largest ratio among its windows), as §1 already did. It is the
  account's actual distance to the guard, which sets it aside as soon as a single window
  reaches its ceiling. When the 7-day window is the most advanced, which
  is the case where a weekly plan gets lost, it is the one that decides. A
  fresh session is not sent to an account whose 5-hour window is
  almost at its threshold, even if its week is behind;
- an account with no open window (expired reading) or with no reading counts as 0%
  used. It therefore goes first, and its first turn reports its gauge;
- on an equal ratio, the unused capacity that **expires soonest** (the
  original §1 rule), then the declared order.

Each new session adds usage to the chosen account, which thus moves back
in the ranking. Sessions are spread across the accounts instead of
piling up on the primary.

## Unchanged

The override (`ameesh accounts use`, §6), continuity as long as the session's
account is under its threshold (§4), the pause when all accounts are at the threshold
(§2) and the Codex rule, under which a session is never portable from one account to
another.

## Transparency (§5)

The logged reason states the lag of the chosen account, then that of the accounts
that follow it: « compte claude : secondaire — sans relevé : 0 % utilisé, le
plus en retard sur son rythme ; avant : tertiaire sans relevé : 0 % utilisé,
primaire seven_day 35 % utilisé (rythme 55 %) » ("claude account: secondary —
no reading: 0% used, furthest behind its pace; ahead of: tertiary no reading:
0% used, primary seven_day 35% used (pace 55%)"). `ameesh accounts list` gives
the same reason for the next choice of a new session.
