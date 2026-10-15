# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-workflow CI evidence for an immutable integration train (#1467).

The required workflows/jobs are explicit project contracts, verified at
qualification. No aggregate green status, skipped job, or another branch's
run can stand in for complete CI. This adapter does not poll or rerun.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from . import land
from .train import TrainError


@dataclass(frozen=True)
class Workflow:
    path: str
    jobs: tuple[str, ...]
    event: str = "push"

    def __post_init__(self):
        if self.event not in ("push", "workflow_dispatch"):
            raise ValueError("train CI event: push | workflow_dispatch")
        if not self.path.startswith(".github/workflows/") or not self.path.endswith((".yml", ".yaml")):
            raise ValueError("explicit workflow path expected")
        if not self.jobs or len(set(self.jobs)) != len(self.jobs):
            raise ValueError("distinct required executable jobs expected")


class GitHub:
    def __init__(self, slug: str, workflows: tuple[Workflow, ...], *, gh=land._gh):
        from . import radar
        if not radar._GITHUB_RE.fullmatch(slug) or not workflows:
            raise ValueError("repository and complete required workflow contract expected")
        if len({w.path for w in workflows}) != len(workflows):
            raise ValueError("duplicate workflow contract")
        self.slug, self.workflows, self.gh = slug, workflows, gh

    def _api(self, path):
        proc = self.gh(["api", "--paginate", "--slurp", "repos/%s/%s" % (self.slug, path)])
        if proc.returncode:
            raise TrainError("cannot read exact train CI evidence")
        return land._json(proc, "train CI API", list)

    def start(self, plan):
        """Called once, after durable ci-intent. Push contracts need no dispatch.

        On an interrupted multi-workflow dispatch, the remaining workflows
        are absent and the train waits. It never repeats an uncertain call.
        The pilot can reconcile absent runs before a bounded recovery.
        """
        for workflow in self.workflows:
            if workflow.event != "workflow_dispatch":
                continue
            proc = self.gh(["workflow", "run", workflow.path, "--repo", self.slug,
                            "--ref", plan["branch"], "-f", "expected_sha=" + plan["head"]])
            if proc.returncode:
                raise TrainError("train dispatch failed or uncertain: %s" % workflow.path)

    def read(self, plan):
        evidence, pending, red, absent = [], False, False, False
        for workflow in self.workflows:
            name = quote(workflow.path.rsplit("/", 1)[-1], safe="")
            path = ("actions/workflows/%s/runs?head_sha=%s&event=%s&per_page=100"
                    % (name, plan["head"], workflow.event))
            runs = [r for page in self._api(path) for r in page.get("workflow_runs", ())
                    if r.get("head_sha") == plan["head"] and r.get("head_branch") == plan["branch"]
                    and r.get("path") == workflow.path and r.get("event") == workflow.event]
            if not runs:
                absent = True
                continue
            # A newer attempt invalidates the old run's evidence. A cancelled
            # run is red, never a request for an automatic rerun.
            run = max(runs, key=lambda r: (int(r["id"]), int(r.get("run_attempt", 1))))
            attempt = int(run.get("run_attempt", 1))
            if run.get("status") != "completed":
                pending = True
                continue
            if run.get("conclusion") != "success":
                if run.get("conclusion") != "failure":
                    raise TrainError("complete CI interrupted or cancelled: no culpable lot proved")
                red = True
                evidence.append(dict(workflow=workflow.path, run=run["id"], attempt=attempt,
                                     conclusion=run.get("conclusion")))
                continue
            jobs = [j for page in self._api("actions/runs/%s/attempts/%s/jobs?per_page=100"
                                            % (run["id"], attempt)) for j in page.get("jobs", ())]
            for required in workflow.jobs:
                found = [j for j in jobs if j.get("name") == required]
                if len(found) != 1 or found[0].get("status") != "completed" \
                        or found[0].get("conclusion") != "success":
                    # Success of the workflow with a skipped required job
                    # cannot prove that this suite actually ran.
                    raise TrainError("required full-suite job missing, skipped or not successful")
            evidence.append(dict(workflow=workflow.path, run=run["id"], attempt=attempt,
                                 jobs=[dict(id=j["id"], name=j.get("name"),
                                            conclusion=j.get("conclusion")) for j in jobs]))
        status = "absent" if absent else "pending" if pending else "red" if red else "green"
        return dict(head=plan["head"], tree=plan["tree"], complete=not absent and not pending,
                    status=status, workflows=evidence)
