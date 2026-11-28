# SPDX-License-Identifier: AGPL-3.0-only
"""Audited train CI contracts. Declaring one never enables a train.

The source blobs describe the execution semantics behind the required jobs.
A new workflow/planner needs a new audit; an unchanged display name does not
transfer full-suite proof. A project-specific contract (the dynamic packet CI
of a project repository, #2503) lives in its module and is reached through
``registry.train_contract`` (#7794); this file only declares ameesh's own.

#2446: ``paquet.yml`` is the caller run of the packet gate. A push to
``integration/**`` creates that run; when no open PR covers the pushed head,
its ``suite`` job calls ``tests.yml`` and the API exposes the called jobs in
the same caller run (prefixed by the caller job). There is **no** separate
``tests.yml?event=workflow_call`` run. The contract therefore pins the caller
workflow, the called suite and the planning blobs, and requires the called
jobs (prefixed by the caller job) — the full 12 psycopg + 12 psql matrix, the
short macOS platform job and the aggregate ``tests`` check. #3198: the full
macOS suite is no longer part of the packet run (it moved to ``macos.yml``,
nightly and on the ``ci-macos`` label); what this contract requires is the
short platform verdict (L63/L66) that stays in ``tests.yml``.
"""
from .train_ci import Workflow


def ameesh() -> tuple[Workflow, ...]:
    """Full packet suite on ``integration/**`` (caller gate + called suite).

    ``paquet.yml`` triggers on ``push integration/**`` and calls ``tests.yml``
    as a reusable workflow when no open PR covers the pushed head. The train
    pushes an immutable ``integration/train-*`` branch without opening a PR,
    so this caller run carries the packet proof: its API jobs are the called
    jobs prefixed by the caller job (e.g. ``suite complète du paquet / plan``).
    A covered push (open PR) leaves ``suite`` skipped, so the called jobs are
    absent and supply no packet CI: the train refuses it.
    The full matrix is 12 psycopg parts, 12 psql parts (#8436, 24 jobs = the
    24 runners), the short macOS platform job and the aggregate ``tests``
    check; doc-only and version lanes
    are conditional outside this contract. #3749: the 12 psql parts run the
    pilot list and the contract, the whole suite under psql is nightly
    (``psql-nocturne.yml``, outside the packet proof).

    #3198: the packet run no longer carries the full macOS suite (8 psycopg
    parts under macOS, hours long); it lives in ``.github/workflows/macos.yml``
    — nightly on ``main`` and on demand with the ``ci-macos`` label — and is
    not part of the packet proof. The ``plateforme macOS`` job required here is
    the short L63/L66 platform verdict that remains in ``tests.yml``.

    #3098: the blobs are also declared by this file at the target's base
    commit, and that versioned copy is the reference for a packet head. The
    installed module supplies the paths; its blobs are only a release
    snapshot and go stale when a packet re-pins a source as it lands.
    #3108: the base also supplies the jobs, added to the installed ones, so a
    job added by a re-pinned workflow is required from the next cut instead
    of the next deployment. The installed jobs stay a floor: a job the base
    removed is still required until a deployment audits the lower contract.
    The jobs expression must stay statically readable (constants, tuples,
    star unpacking, single `range` comprehensions, `%` formatting).
    """
    return (Workflow(
        path=".github/workflows/paquet.yml",
        jobs=("plan",
              *("psycopg %d/12" % n for n in range(1, 13)),
              *("psql %d/12" % n for n in range(1, 13)),
              "plateforme macOS", "tests"),
        event="push",
        called=".github/workflows/tests.yml",
        sources=((".github/workflows/paquet.yml", "fa502a79d4f696b82f77350fbac43b394983e38e"),
                 (".github/workflows/tests.yml", "abcd04991f0f1ac9b07f7d6ad0349380bc6bf8cd"),
                 ("scripts/test-plan.py", "09cc6ef857293ad7045a68829a812585e2bfa2d2"),
                 ("scripts/test-parts.py", "305c23ab2441463ff0bd2d2371f65793b16102c8"),
                 ("scripts/ci-flow.py", "c81e4aff4fcaf1b9ec9a75ba987b3beba50258c2"),
                 ("scripts/ci-test-space.py", "f5535ce3eadcd0b6a6e69567a8c36ed3a121420f"),
                 ("scripts/ci-agregat.py", "b3b84b3d9f40ce567ded8dd446a7d561e9aa5747"),
                 ("scripts/map.py", "f12aac2eaf022b7ebcfabce597616249a6c5ca76")),
        pins_at="src/ameesh/train_contracts.py",
    ),)


def stale_pins(root: str, workflows=None) -> list[tuple[str, str | None, str, str]]:
    """#3606: the contract sources whose blob under ``root`` differs from its pin.

    ``root`` is the root of a built tree, not necessarily a Git worktree: a
    freeze checks a ``git archive`` extraction, so the blob is computed from
    the file with ``git hash-object`` instead of being read from a commit
    object. Returns ``(path, tree blob or None, pinned blob, pin file)`` for
    each stale source; an empty list means every audited source matches its
    pin. A source missing from the tree has no blob and is stale too.

    Only static contracts (``Workflow`` tuples) can be checked this way. A
    module's dynamic contract pins sources of its project repository, which an
    ameesh tree does not carry; this check applies to the project tree that
    declares them.
    """
    from . import plan_git
    if workflows is None:
        workflows = ameesh()
    stale = []
    for workflow in workflows:
        for path, pinned in workflow.sources:
            actual = plan_git._out(root, "hash-object", "--", path)
            if actual != pinned:
                stale.append((path, actual, pinned, workflow.pins_at or "?"))
    return stale
