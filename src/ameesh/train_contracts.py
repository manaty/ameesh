# SPDX-License-Identifier: AGPL-3.0-only
"""Audited train CI contracts. Declaring one never enables a train.

The source blobs describe the execution semantics behind the required jobs.
A new workflow/planner needs a new audit; an unchanged display name does not
transfer full-suite proof. No Nexlink contract is supplied while its backend
and website dispatch can still select impacted tests on integration/train-*.
"""
from .train_ci import Workflow


def ameesh() -> tuple[Workflow, ...]:
    """Full push suite at e4e4c85c: both PostgreSQL drivers, macOS and gates.

    integration/** push always uses the full 4 psycopg + 6 psql parts,
    including for documentation-only content. Doc-only and version jobs are
    conditional lanes outside this contract; the aggregate cannot replace
    the executable parts. No second dispatch is added to this push.
    """
    return (Workflow(
        path=".github/workflows/tests.yml",
        jobs=("plan", *("psycopg %d/4" % n for n in range(1, 5)),
              *("psql %d/6" % n for n in range(1, 7)), "plateforme macOS", "tests"),
        sources=((".github/workflows/tests.yml", "d38a9e6f99ed39bed9b7192bc49cfbb1758968dd"),
                 ("scripts/test-plan.py", "502141ea8fe28d8bcae132ea1b3fa74147f6d283"),
                 ("scripts/test-parts.py", "5379a497802315a53d45bfaf6f6172cd43efe7ca"),
                 ("scripts/ci-flow.py", "c3c8ce59a13fb7f652b3f7b32e533c72a2969d95"),
                 ("scripts/map.py", "12207bd07fb0a38d66ade2e9b6e0cd903979b463")),
    ),)
