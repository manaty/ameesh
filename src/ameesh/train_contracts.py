# SPDX-License-Identifier: AGPL-3.0-only
"""Audited train CI contracts. Declaring one never enables a train.

The source blobs describe the execution semantics behind the required jobs.
A new workflow/planner needs a new audit; an unchanged display name does not
transfer full-suite proof. No Nexlink contract is supplied while its backend
and website dispatch can still select impacted tests on integration/train-*.

#2446: ``paquet.yml`` is the caller run of the packet gate. A push to
``integration/**`` creates that run; when no open PR covers the pushed head,
its ``suite`` job calls ``tests.yml`` and the API exposes the called jobs in
the same caller run (prefixed by the caller job). There is **no** separate
``tests.yml?event=workflow_call`` run. The contract therefore pins the caller
workflow, the called suite and the planning blobs, and requires the called
jobs (prefixed by the caller job) — the full 8 psycopg + 16 psql matrix, the
macOS platform job and the aggregate ``tests`` check.
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
    The full matrix is 8 psycopg parts, 16 psql parts, the macOS platform job
    and the aggregate ``tests`` check; doc-only and version lanes are
    conditional outside this contract.
    """
    return (Workflow(
        path=".github/workflows/paquet.yml",
        jobs=("plan",
              *("psycopg %d/8" % n for n in range(1, 9)),
              *("psql %d/16" % n for n in range(1, 17)),
              "plateforme macOS", "tests"),
        event="push",
        called=".github/workflows/tests.yml",
        sources=((".github/workflows/paquet.yml", "0d4e4c9cae703575b33aa393d6a1c95a81e5c896"),
                 (".github/workflows/tests.yml", "8403f902b5b87e52a99f86549822feb92e784035"),
                 ("scripts/test-plan.py", "d37c0a3dfcbb5516550d4015bb073538afbfc815"),
                 ("scripts/test-parts.py", "2265308c9fed248852bfd9ec41ba8f79266f0647"),
                 ("scripts/ci-flow.py", "ac574bd1f38eb788c70800095efde70bd2b8b751"),
                 ("scripts/map.py", "7a7516683643ff7cd66abf3d50147b25446a03bb")),
    ),)
