# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-workflow CI evidence for an immutable integration train (#1467).

The required workflows/jobs are explicit project contracts, verified at
qualification. No aggregate green status, skipped job, or another branch's
run can stand in for complete CI. This adapter does not poll or rerun.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote

from . import land, resolutions
from .train import TrainError


@dataclass(frozen=True)
class Workflow:
    path: str
    jobs: tuple[str, ...]
    event: str = "push"
    # Reviewed Git blobs that make the job list a *complete* suite: the
    # workflow itself, its called workflows and its selection/planning code.
    # A workflow-changing lot needs a newly audited contract, never a green
    # job with the same display name and different execution conditions.
    sources: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if self.event not in ("push", "workflow_dispatch"):
            raise ValueError("train CI event: push | workflow_dispatch")
        if not self.path.startswith(".github/workflows/") or not self.path.endswith((".yml", ".yaml")):
            raise ValueError("explicit workflow path expected")
        if not self.jobs or len(set(self.jobs)) != len(self.jobs):
            raise ValueError("distinct required executable jobs expected")
        paths = [path for path, _ in self.sources]
        if self.path not in paths or len(set(paths)) != len(paths):
            raise ValueError("audited source blobs including the workflow expected")
        for path, blob in self.sources:
            if not re.fullmatch(r"[\w./-]+", path) or any(
                    part in ("", ".", "..") for part in path.split("/")):
                raise ValueError("repository-relative audited source path expected")
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob):
                raise ValueError("full audited source blob expected")


def audited_sources(workflows):
    if not workflows:
        raise TrainError("complete audited CI contract required")
    sources = {}
    for workflow in workflows:
        for path, blob in workflow.sources:
            if path in sources and sources[path] != blob:
                raise TrainError("conflicting audited CI source contracts")
            sources[path] = blob
    return sources


def verify_local(path, plan, workflows):
    """Check commit objects before a publication can trigger push CI.

    The worktree and remote default branch are not execution evidence for
    this commit. Check the object type too: a symlink is not a workflow file.
    """
    for source, blob in sorted(audited_sources(workflows).items()):
        proc = resolutions._run(path, "ls-tree", plan["head"], "--", source)
        fields = resolutions._text(proc).strip().split()
        if proc.returncode or len(fields) != 4 or fields[0] not in ("100644", "100755") \
                or fields[1:] != ["blob", blob, source]:
            raise TrainError("full-suite source differs from audited CI contract: %s" % source)


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

    def verify(self, plan):
        """Verify the audited execution contract at this exact train head.

        In particular, changing a matrix or making an executable step
        conditional cannot be covered by the old list of green job names.
        This is read-only and performed before dispatch and before using CI.
        """
        for path, blob in sorted(audited_sources(self.workflows).items()):
            pages = self._api("contents/%s?ref=%s" % (quote(path, safe="/"), plan["head"]))
            if len(pages) != 1 or not isinstance(pages[0], dict) \
                    or pages[0].get("type") != "file" or pages[0].get("sha") != blob:
                raise TrainError("full-suite source differs from audited CI contract: %s" % path)

    def start(self, plan):
        """Called once, after durable ci-intent. Push contracts need no dispatch.

        On an interrupted multi-workflow dispatch, the remaining workflows
        are absent and the train waits. It never repeats an uncertain call.
        The pilot can reconcile absent runs before a bounded recovery.
        """
        self.verify(plan)
        for workflow in self.workflows:
            if workflow.event != "workflow_dispatch":
                continue
            proc = self.gh(["workflow", "run", workflow.path, "--repo", self.slug,
                            "--ref", plan["branch"], "-f", "expected_sha=" + plan["head"]])
            if proc.returncode:
                raise TrainError("train dispatch failed or uncertain: %s" % workflow.path)

    def read(self, plan):
        self.verify(plan)
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
            # run supplies no outcome and never asks for an automatic rerun.
            run = max(runs, key=lambda r: (int(r["id"]), int(r.get("run_attempt", 1))))
            attempt = int(run.get("run_attempt", 1))
            if run.get("status") != "completed":
                pending = True
                continue
            if run.get("conclusion") not in ("success", "failure"):
                raise TrainError("complete CI interrupted or cancelled: no culpable lot proved")
            jobs = [j for page in self._api("actions/runs/%s/attempts/%s/jobs?per_page=100"
                                            % (run["id"], attempt)) for j in page.get("jobs", ())]
            failed = False
            for required in workflow.jobs:
                found = [j for j in jobs if j.get("name") == required]
                if len(found) != 1 or found[0].get("status") != "completed" \
                        or found[0].get("conclusion") not in ("success", "failure"):
                    # Success of the workflow with a skipped required job
                    # cannot prove that this suite actually ran.
                    raise TrainError("required full-suite job missing, skipped or interrupted")
                failed = failed or found[0]["conclusion"] == "failure"
            if failed != (run["conclusion"] == "failure"):
                # A failed ancillary job with all test jobs green is not
                # evidence against a lot; nor is an allowed failing test
                # hidden behind a successful workflow.
                raise TrainError("workflow conclusion does not prove full-suite job outcome")
            red = red or failed
            evidence.append(dict(workflow=workflow.path, run=run["id"], attempt=attempt,
                                 conclusion=run["conclusion"],
                                 jobs=[dict(id=j["id"], name=j.get("name"),
                                            conclusion=j.get("conclusion")) for j in jobs]))
        status = "absent" if absent else "pending" if pending else "red" if red else "green"
        return dict(head=plan["head"], tree=plan["tree"], complete=not absent and not pending,
                    status=status, workflows=evidence)
