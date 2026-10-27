---
type: Exploration
status: exploration
title: "Versioning for agents: explicit dependencies and per-change attestation"
---

# Versioning for agents: explicit dependencies and per-change attestation

## Problem

Lots that are green separately can contradict each other once composed: the
conflict is semantic, not textual (#794; #1266 against `test_l161f`; #928
against `test_l56`), and some tests depend on the number of loaded modules.
A revert can cancel the content of a merge while keeping its ancestry: an
ancestor head therefore no longer proves that its content is present
(#1825). Git records snapshots and their ancestry; it proves neither their
semantic compatibility nor the cause of a red test.

Diagnosis is therefore paid for with `bisect --first-parent` over hourly
packets that are already merged (#1293, #1308, #1346), while the test proof
stays attached to the commit rather than to the content of the change. The
measured test → code map (L161e) already relates each test to the blocks it
exercises, but it does not link that measurement to how lots are composed.

## Intuitions

- **Changes with explicit dependencies that commute.** Pijul and Darcs show
  that a unit of change can declare what it depends on and apply only when
  its dependencies are present: composition becomes a commutation
  computation, and a disagreement becomes a named incompatibility rather
  than a conflict marker. These guarantees concern patches: on their own,
  they do not detect semantic dependencies between lots, as the
  [Darcs motivation](https://darcs.net/Theory/Motivation) points out. The
  [Pijul theory](https://pijul.org/manual/theory.html) also separates
  structural dependencies from semantic ones that must be added. The
  Mergiraf pilot (L160g) measured syntactic merging and did not adopt it;
  the intuition is about the composed unit, not about better line-by-line
  stitching.
- **Test attestation attached to the content of a change.** A proof (green
  tests, scope, environment) would travel with the content it covers; after
  a commutation or a recomposition, it would be replayed or explicitly
  invalidated, instead of being assumed by inheritance from the commit.
- **Pointing directly at the faulty change.** The L161e test → code map
  measures the blocks each test exercises. Crossed with the blocks changed
  by the lots and with their attestations, it could narrow the candidates
  for a red packet. Telling a faulty lot from an interaction between lots
  still needs a proof; coverage alone does not demonstrate it.
- **Hourly composition without reverts.** An hourly packet would become a
  selection of changes that commute, whose withdrawal would be exact and
  would require neither a merge revert nor a full replay.
- **The database as the source of truth for landings.** Today, whether a
  lot was delivered is re-derived from git on every read: `plan_git.probe`
  tries ancestry, then looks for a revert in at most 200 commits
  (`MAX_REVERT_SCAN`, message "This reverts commit"), then compares
  `patch-id`s; `scripts/paquet-inclusion.py` checks the content at a
  packet's head line by line. The database already records the merge (the
  `merged` milestone and its commit, `merge_queue` in `landed`, train
  withdrawals), but neither the revert nor the withdrawal of a composed
  lot. If each transition (composed into a packet, landed, withdrawn or
  reverted) were written by whoever performs it, when they perform it, "is
  this lot delivered?" would become a read, and git a cross-check: a
  mismatch between the database and the tree would be an alert, never a
  silent correction. Anthropic describes the same principle: ask the
  review tool when a change landed rather than re-reading the history (see
  context).

## Open questions

- How can a dependency between changes be represented without imposing a
  full DAG on every author?
- What happens to an attestation whose content has commuted since: replay,
  invalidate, or ask again?
- Which dependency, data and environment context must an attestation cover
  to stay valid after recomposition?
- How does an agent know that a red test comes from its change rather than
  from a neighbour composed in the same packet?
- Can the withdrawal of an already composed change be exact, and what
  happens to its neighbours' attestations?
- Which audit and signature guarantees remain if the unit of proof is no
  longer the commit?
- What is kept from git (review, PR, history) and what changes nature?
- How many reverts escape the current detection (beyond the 200-commit
  window, or made without the standard message), and how many lots were
  closed wrongly, then reopened?
- How many admitted lots were left out of a packet's composition without
  anything recording it, before the inclusion check existed?
- A revert made outside ameesh (a human, a direct `git revert`): should the
  radar record it on its own, or only alert when it cannot tell which lot
  it belongs to?
- Does a reverted lot go back to `build` for its agent, or stay `blocked`
  until a human decides?

Existing context: [Impact-based tests study](../etudes/l161-tests-par-impact.md)
(test → code map), [Conflict radar](../etudes/l160-radar-de-conflits.md),
[Mergiraf pilot](../etudes/l160g-mergiraf-pilot.md), the
[lot process](https://github.com/manaty/home/blob/main/processus/lots.md)
(hourly packets), and
[Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control)
(Scion: changesets, server-side queries, post-merge testing). These
references inform the question; they do not promote this card to a study or
to decided work.
