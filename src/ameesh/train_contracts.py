# SPDX-License-Identifier: AGPL-3.0-only
"""Audited train CI contracts. Declaring one never enables a train.

The source blobs describe the execution semantics behind the required jobs.
A new workflow/planner needs a new audit; an unchanged display name does not
transfer full-suite proof. No Nexlink contract is supplied while its backend
and website dispatch can still select impacted tests on integration/train-*.

#2446: ``paquet.yml`` is the packet gate that turns a push to
``integration/**`` into exactly one run of ``tests.yml`` (``workflow_call``
when no open PR covers the pushed head), and ``tests.yml`` is the suite that
contract pins: the full 8 psycopg + 16 psql matrix, the macOS platform job and
the aggregate ``tests`` check. The gate's own jobs are not run evidence for the
suite; the audited blob is, and the ``workflow_call`` run is read then.
"""
from .train_ci import Workflow


def ameesh() -> tuple[Workflow, ...]:
    """Full packet suite on ``integration/**`` (gate + tests).

    ``paquet.yml`` triggers on ``push integration/**`` and calls ``tests.yml``
    as a reusable workflow when no open PR covers the pushed head. The train
    pushes an immutable ``integration/train-*`` branch without opening a PR,
    so the packet run is the ``workflow_call`` run of ``tests.yml`` on that
    exact head and branch. The full matrix is 8 psycopg parts, 16 psql parts,
    the macOS platform job and the aggregate ``tests`` check; doc-only and
    version lanes are conditional outside this contract.
    """
    return (Workflow(
        path=".github/workflows/tests.yml",
        jobs=("plan", *("psycopg %d/8" % n for n in range(1, 9)),
              *("psql %d/16" % n for n in range(1, 17)), "plateforme macOS", "tests"),
        event="workflow_call",
        sources=((".github/workflows/tests.yml", "8403f902b5b87e52a99f86549822feb92e784035"),
                 (".github/workflows/paquet.yml", "0d4e4c9cae703575b33aa393d6a1c95a81e5c896"),
                 ("scripts/test-plan.py", "d37c0a3dfcbb5516550d4015bb073538afbfc815"),
                 ("scripts/test-parts.py", "2265308c9fed248852bfd9ec41ba8f79266f0647"),
                 ("scripts/ci-flow.py", "ac574bd1f38eb788c70800095efde70bd2b8b751"),
                 ("scripts/map.py", "7a7516683643ff7cd66abf3d50147b25446a03bb")),
    ),)
