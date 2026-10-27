---
type: Exploration
status: exploration
title: "A freeze that tolerates changes without effect"
---

# A freeze that tolerates changes without effect

## Problem

A lot in the merge queue is bound to its frozen commit (`frozen_sha`). On
each radar pass, a lot whose branch head is no longer exactly that commit is
ejected (`merge_queue.py`, "branch moved past its freeze"): its agent must
freeze again, get it reviewed again, then queue the lot again, and it loses
its rank. The rule does not look at what changed: fixing a typo in a comment
costs as much as a change of logic.

The review policy (`ameesh review-class`, the canon's `review_policies`)
already classifies a change as `light`, `normal` or `sensitive`, but **by
file path**: it says where a change happens, not what kind of change it is.

## Intuitions

- **Classify the delta between the freeze and the new head**, not the whole
  lot. When the delta has no effect, the freeze would move forward to the
  new commit without ejection, and that extension would be written to the
  database with its justification (for example "comments only, identical
  AST").
- **Separate what can be proved from what must be judged.**
  - Provable mechanically: comments only or pure formatting (the syntax
    tree is identical; in Python, `ast` ignores comments), documentation
    only under `light` paths, renaming a local variable (identical tree up
    to the renaming, within its scope).
  - Not provable: localized refactoring (extracting a function,
    reordering). Instead of an ejection, a review of the delta alone, with
    the lot keeping its rank.
  - Everything else: ejection, as today.
- **Two dimensions for review**: the class of the path (existing) and the
  nature of the change. This is the idea behind the semantic approval rules
  Anthropic describes for its review tool (see context).

## Open questions

- How many "branch moved past its freeze" ejections does the radar make
  (reason in `merge_queue.reason`, note in the lot's journal), and what
  share has a delta made only of comments, formatting or documentation?
  How much does each ejection cost (new freeze, review, re-queue, lost
  rank)?
- Some comments are directives: `# type: ignore`, `# noqa`,
  `# pragma: no cover`, `# fmt: off`, shebang, encoding declaration. Is a
  closed list of comments that count as code needed?
- Docstrings are part of the Python syntax tree: do they need a rule of
  their own?
- Which proof for each language (SQL, shell, TypeScript, YAML)? Should SQL
  migrations, classified `sensitive`, always eject?
- Can a comment changed in a `sensitive` file extend the freeze, or does
  the path class always win?
- Does the CI certified on the frozen commit stay valid for a delta proved
  to have no effect, or must it run again on the new head (`ci_required`)?
- For the review of the delta alone: the same reviewer as for the freeze,
  or any reviewer?

Existing context: [Landing conditions](../../LANDING-CONDITIONS.md),
[Orchestrator, merge queue per target](../../ORCHESTRATEUR.md),
[Conflict radar](../etudes/l160-radar-de-conflits.md), and
[Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control)
(Scion and the Antique review tool, semantic approval rules). These
references inform the question; they do not promote this card to a study or
to decided work.
