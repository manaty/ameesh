# SPDX-License-Identifier: AGPL-3.0-only
"""Factual diagnostic boundary shared by packet and post-landing consumers.

CI job failures alone establish neither an environmental cause nor a faulty
lot. Until exact test/impact/local receipts are available, expose the missing
facts on the observed packet instead of inventing an attribution or rerun.
The versioned process policy remains in #1616; this module grants no authority.
"""
from __future__ import annotations

from .train import TrainError

SCHEMA = "ameesh-train-diagnosis/1"


def unresolved(plan, ci):
    """Describe a complete red packet with both causes explicitly unknown.

    ``ci`` comes from the exact-workflow adapter, not an event payload or the
    last run of a branch. The failed-test stream must still bind each test to
    its run attempt/job. A qualified impact map supplies candidates only;
    causality and a local result on this very commit are separate receipts.
    """
    if ci.get("head") != plan["head"] or ci.get("tree") != plan["tree"] \
            or ci.get("status") != "red" or ci.get("complete") is not True:
        raise TrainError("diagnosis requires exact complete red packet CI")
    runs = [dict(workflow=run.get("workflow"), run=run.get("run"),
                 attempt=run.get("attempt"),
                 failed_jobs=[dict(id=job.get("id"), name=job.get("name"))
                              for job in run.get("jobs", ())
                              if job.get("conclusion") == "failure"])
            for run in ci.get("workflows", ())]
    return dict(schema=SCHEMA,
                packet={field: plan[field] for field in
                        ("source", "target", "base", "head", "tree")},
                ci=runs,
                environment=dict(status="unknown", receipts=[]),
                attribution=dict(status="unknown", candidates=[], proven_lots=[]),
                missing=["complete test failures bound to exact workflow/run/attempt/job",
                         "qualified test-to-file impact map and exact member diffs",
                         "trusted local green receipts on the same commit and test command",
                         "causal proof before removing or accusing a lot"])
