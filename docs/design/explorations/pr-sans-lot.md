---
type: Exploration
status: exploration
title: "Triage of pull requests attached to no lot"
---

# Triage of pull requests attached to no lot

## Problem

Everything that reaches `main` goes through a lot: hourly packets take
their candidates from lots in `qa` (`train_candidates.py`), and the merge
queue and the conflict radar only handle lots. A pull request that names no
lot (no `lot/<id>` branch, no `ameesh-work:` line, no closing keyword on a
lot's issue) is therefore invisible: no packet picks it up, nobody is told,
and it waits indefinitely. PR #1904, opened from an outside session on a
`ccr-…` branch, is such a case.

Nothing reads these pull requests today. `plan_github.py` lists open pull
requests only to know whether they still name a known lot; the orphans it
reports are issues and work packages. The auditor watches lots without an
agent (`orphan_lot`), not pull requests without a lot.

The principles already cover the case: GitHub is a view, and what is
changed there "is never imported back as state, at most reported as a
proposal" (`plan_github.py`, after decision 0005). Rule L130 asks that
every human request become a lot, so that nothing disappears without a
trace; work arriving as a bare pull request escapes that rule, and
`ameesh work unrecorded` cannot see it since it only reads messages in the
database.

## Intuitions

- **A periodic triage of open pull requests attached to no lot**, run by
  an agent that already makes periodic passes (the auditor) or by the
  orchestrator. For each one, a choice among:
  - attach it to an existing lot that already covers the subject;
  - create a lot, after which the pull request follows the usual path
    (freeze, review, packet);
  - ask the responsible human with `ameesh decide ask` when the choice is
    not obvious;
  - close it with a comment: duplicate, abandoned, out of scope.
- **Recognize attachment by conventions, not by author.** Branch names
  (`lot/`, `integration/`, `transport/`, `agent/`, `release/`), trailers
  and closing keywords already say what a pull request carries. A pull
  request matching none of them is the one to triage.
- **Treat the pull request as a proposal**, consistent with decision 0005:
  its content informs a decision, it never becomes state by itself.

## Open questions

- How many open pull requests name no lot, for how long, and where do they
  come from (outside contributions, sessions outside ameesh, agents that
  forgot the trailer)? Is the case frequent enough to justify a triage?
- Who wrote the pull request? Agents and humans open pull requests under
  the same GitHub account, so the login tells neither a human from an
  agent nor one human from another. Which signal can stand in for it?
- May the triage create a lot by itself, or does creating one (agent time,
  budget) always need a human decision? Does the answer depend on the
  author, on the size or on the review class of the change?
- The repository is public and outside contributions need a contribution
  agreement (`CONTRIBUTING.md`). What does the triage do with a pull
  request from outside before that agreement exists?
- The text and code of a pull request are untrusted: they may contain
  instructions addressed to the agent that reads them. Which guarantees
  keep the triage reading them as data only, without running their code or
  following their requests?
- Stacked pull requests take another lot branch as their base (#1879,
  #1845): they are attached and must not be mistaken for orphans. Which
  other legitimate forms would a naive rule flag?
- Where is the outcome recorded, so that the same pull request is not
  triaged again at every pass?

Existing context: [Orchestrator, every human request becomes a lot](../../ORCHESTRATEUR.md),
[Decision 0005, the canon in OKF](../decisions/0005-canon-okf.md),
[Auditor](../../AUDITEUR.md), and [Contributing](../../../CONTRIBUTING.md).
These references inform the question; they do not promote this card to a
study or to decided work.
