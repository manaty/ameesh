# Generic core boundary

The generic core owns work tracking, communication, governance, resources and
agent execution. Business modules own their domain vocabulary and behavior.
Git used for the canon, identity and persona memory remains infrastructure;
git branches and pull requests as work products belong to development tooling.

`work_lifecycle` centralizes the current persisted lifecycle spellings and the
compatibility delivery connector. `work` re-exports its public state groups.
Readers should use these groups rather than repeat storage spellings:

- `ACTIVE`: requested work and work in progress.
- `WAITING`: blocked work and work waiting for a human decision.
- `TO_VALIDATE`: work awaiting validation.
- `DELIVERED`: delivery recorded or promoted.
- `COMPLETED`: delivered work and explicitly closed work.
- `TERMINAL`: states without further normal transitions. A recorded delivery
  can still be promoted, so it is completed without being terminal.

The legacy `closed` state also covers delivery outside the target and completed
review missions. Its close reason must be checked before treating it as an
abandonment. Public presentation states, action states, milestone kinds and
remote issue states have separate contracts; their equal spelling does not
make them interchangeable with work-item states.

`tests/test_generic_core.py` scans Python literals and identifiers outside
`modules/`, plus `progress_page.html`. It also detects direct imports of module
implementations, including relative and aliased imports. Python docstrings and
comments are excluded. The existing generic module storage adapter imports
remain counted until that interface is exposed through the registry.

`tests/generic_core_exceptions.txt` records a count and a reason for each
existing file/motif pair. Increasing a count or adding a pair fails the test;
removing coupling requires reducing the corresponding allowance. An exception
does not exempt a whole file. Baseline edits require review, including cases
where a new occurrence replaces an unrelated removed occurrence with the same
motif. This lexical check cannot replace an architectural review.

The boundary check preserves the current runtime behavior. Module activation,
renamed persisted states, validation as a stored state, extraction of development
and forge tooling, vocabulary selection and declarative pipelines are subsequent
parts of the generic-core transition.
