# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-workflow CI evidence for an immutable integration train (#1467).

The required workflows/jobs are explicit project contracts, verified at
qualification. No aggregate green status, skipped job, or another branch's
run can stand in for complete CI. This adapter does not poll or rerun.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from urllib.parse import quote

from . import land, resolutions
from .train import TrainError, digest


def _slurp_unsupported(proc) -> bool:
    """#2295 : le gh 2.45 de l'hôte ne connaît pas `--slurp`."""
    return "unknown flag: --slurp" in ((proc.stderr or "") + (proc.stdout or ""))


def _json_pages(proc) -> list:
    """#2295 : pages concaténées de `gh api --paginate` sans `--slurp`.

    Chaque page doit être un objet JSON, comme les pages slurpées que rend
    `_api` (l'appelant lit `page.get(...)`).
    """
    text, pages, at = proc.stdout or "", [], 0
    decoder = json.JSONDecoder()
    while True:
        while at < len(text) and text[at].isspace():
            at += 1
        if at >= len(text):
            return pages
        try:
            page, at = decoder.raw_decode(text, at)
        except ValueError as exc:
            raise TrainError("cannot read exact train CI evidence: %s" % exc) from exc
        if not isinstance(page, dict):
            raise TrainError("cannot read exact train CI evidence: unexpected page")
        pages.append(page)


def _same_branch(run, workflow, plan):
    """The run belongs to the pushed packet.

    #2446: a reusable workflow has no separate ``workflow_call`` run in the
    API; its jobs live in the caller's run (a ``push`` here), whose branch is
    the packet branch. The exact ``head_sha``, the audited ``path`` and the
    event already bind the run; a direct ``workflow_call`` contract (not used
    by ameesh) keeps the exact branch check unless GitHub labels it as the
    caller's branch.
    """
    if run.get("head_branch") == plan["branch"]:
        return True
    return workflow.event == "workflow_call" and isinstance(run.get("head_branch"), str)


@dataclass(frozen=True)
class Workflow:
    path: str
    jobs: tuple[str, ...]
    event: str = "push"
    #: #2446: the reusable workflow called by this caller run. Its execution
    #: evidence is the caller run's push (jobs prefixed by the caller job),
    #: never a separate `tests.yml?event=workflow_call` run, which does not
    #: exist in the GitHub API. Empty for a direct workflow run.
    called: str = ""
    # Reviewed Git blobs that make the job list a *complete* suite: the
    # workflow itself, its called workflows and its selection/planning code.
    # A workflow-changing lot needs a newly audited contract, never a green
    # job with the same display name and different execution conditions.
    sources: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if self.event not in ("push", "workflow_dispatch", "workflow_call"):
            raise ValueError("train CI event: push | workflow_dispatch | workflow_call")
        if not self.path.startswith(".github/workflows/") or not self.path.endswith((".yml", ".yaml")):
            raise ValueError("explicit workflow path expected")
        if self.called and (not self.called.startswith(".github/workflows/")
                            or not self.called.endswith((".yml", ".yaml"))):
            raise ValueError("explicit called workflow path expected")
        if not self.jobs or len(set(self.jobs)) != len(self.jobs):
            raise ValueError("distinct required executable jobs expected")
        paths = [path for path, _ in self.sources]
        if self.path not in paths or len(set(paths)) != len(paths):
            raise ValueError("audited source blobs including the workflow expected")
        if self.called and self.called not in paths:
            raise ValueError("called workflow must be part of the audited sources")
        for path, blob in self.sources:
            if not re.fullmatch(r"[\w./-]+", path) or any(
                    part in ("", ".", "..") for part in path.split("/")):
                raise ValueError("repository-relative audited source path expected")
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob):
                raise ValueError("full audited source blob expected")


#: #2503: the identified plumbing jobs every lot suite carries. A reusable
#: caller exposes its jobs as ``<caller> / <job>``; only these exact names —
#: read from the audited `lot-plan.yml` / `lot-status.yml` — are excluded
#: from the execution proof. A test job that merely starts with ``lot-`` (or
#: with a plumbing caller's name) is required.
DUE_PLUMBING = (
    "lot-plan",
    "lot-plan / Due parts of the change set",
    "lot-plan / Announce the due parts still missing a status",
    "lot-plan-retry",
    "lot-plan-retry / Due parts of the change set",
    "lot-plan-retry / Announce the due parts still missing a status",
    "lot-pending",
    "lot-status",
)

#: #2503: jobs a suite may legitimately leave skipped on a packet dispatch,
#: scoped to the suite that declares them. A name carries no exception on its
#: own: `changed-paths` classifies a develop push (backend-suite),
#: `github-bridge` needs a develop dispatch recipe (compute), and the hosted
#: desktop matrix only exists when `CI_HOSTED_DESKTOP` is `on`
#: (desktop-suite). The matrix legs are named explicitly so a new leg is
#: required rather than silently tolerated; a whole skipped matrix is exposed
#: by the API with the unexpanded name (`Desktop suite (${{ matrix.os }})`).
DUE_SUITE_TOLERATED = {
    ".github/workflows/backend-suite.yml": ("changed-paths",),
    ".github/workflows/compute.yml": ("github-bridge",),
    ".github/workflows/desktop-suite.yml": ("Desktop suite (${{ matrix.os }})",
                                           "Desktop suite (windows-latest)",
                                           "Desktop suite (macos-latest)"),
}


def _tolerated_job(name, path, context):
    """An identified plumbing job, or a skip-by-design job of *this* suite."""
    if name in DUE_PLUMBING:
        return True
    if name in ("lot-pending / Lot status (%s)" % context,
                "lot-status / Lot status (%s)" % context):
        return True
    return name in DUE_SUITE_TOLERATED.get(path, ())


@dataclass(frozen=True)
class DueWorkflow:
    """A suite selected by the repository's own due-parts selector (#2503).

    The 23 Nexlink suites are not frozen by the contract: the selector names
    the `lot/<suite>` parts the integrated change set makes due, and the run's
    own jobs (minus the identified plumbing jobs and the skip-by-design jobs
    of this suite) are the proof.
    """

    path: str
    context: str
    event: str = "workflow_dispatch"

    def __post_init__(self):
        if not self.path.startswith(".github/workflows/") \
                or not self.path.endswith((".yml", ".yaml")):
            raise ValueError("explicit workflow path expected")
        if not re.fullmatch(r"lot/[A-Za-z0-9_-]+", self.context):
            raise ValueError("lot/<suite> context expected")
        if self.event != "workflow_dispatch":
            raise ValueError("a due part is a dispatched suite")


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


def contract_digest(workflows):
    """The stable identity of a CI contract for the packet reservation.

    A static tuple is digested over all of its workflows. A dynamic contract
    (#2503) supplies `digest()`: the required set varies per packet, but the
    reservation binds the shared audited contract, never the due subset of one
    plan.
    """
    digest_method = getattr(workflows, "digest", None)
    if callable(digest_method):
        return digest_method()
    return digest([asdict(workflow) for workflow in workflows])


def parts_cache(cfg):
    """#2503: the on-disk due-parts proof cache of this host, if it has state."""
    from . import ci_parts
    return ci_parts.cache_dir(cfg)


def _job_match(required, actual):
    """Priority of an API job name against a required suite job, or None.

    GitHub exposes a reusable workflow's jobs in the caller run. Depending on
    the caller and the API representation, the name is exact, prefixed by the
    caller job (`suite complète du paquet / psql 4/16`), or surfaced through
    the part's check-run name (`tests (flux) psql 4/16`). The exact name wins;
    an ambiguous match at the same priority is refused by the caller.
    """
    if not isinstance(actual, str):
        return None
    if actual == required:
        return 0
    if actual.endswith(" / " + required):
        return 1
    if actual.endswith(" " + required):
        return 2
    return None


def required_job(name, jobs):
    """The single job proving a required name, or an empty/ambiguous list."""
    best, found = None, []
    for job in jobs:
        priority = _job_match(name, job.get("name"))
        if priority is None:
            continue
        if best is None or priority < best:
            best, found = priority, [job]
        elif priority == best:
            found.append(job)
    return found


def called_workflow(run, workflow, plan):
    """True when the caller run references the audited called workflow at head."""
    refs = run.get("referenced_workflows")
    if not isinstance(refs, list):
        raise TrainError("caller run does not name its audited reusable workflow")
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        path, sha = ref.get("path"), ref.get("sha")
        if not isinstance(path, str) or not isinstance(sha, str):
            continue
        base, _, pinned = path.rpartition("@")
        if base != workflow.called and not base.endswith("/" + workflow.called):
            continue
        if pinned == plan["head"] and sha == plan["head"]:
            return True
    raise TrainError("caller run does not name the audited reusable workflow at the exact head")


def verify_local(path, plan, workflows):
    """Check commit objects before a publication can trigger push CI.

    The worktree and remote default branch are not execution evidence for
    this commit. Check the object type too: a symlink is not a workflow file.
    A dynamic contract (#2503) supplies the sources of the parts this plan
    makes due; its undued suites are not executed, so their files are not
    part of this packet's proof.
    """
    sources_for = getattr(workflows, "sources_for", None)
    sources = (sources_for(plan, repo=path) if callable(sources_for)
               else audited_sources(workflows))
    for source, blob in sorted(sources.items()):
        proc = resolutions._run(path, "ls-tree", plan["head"], "--", source)
        fields = resolutions._text(proc).strip().split()
        if proc.returncode or len(fields) != 4 or fields[0] not in ("100644", "100755") \
                or fields[1:] != ["blob", blob, source]:
            raise TrainError("full-suite source differs from audited CI contract: %s" % source)
    due_for = getattr(workflows, "due_for", None)
    if callable(due_for):
        # The suite's own workflow is the reviewed project code, not a frozen
        # blob: the run and its jobs are the proof. It must exist at the head
        # the branch will publish.
        for workflow in due_for(plan, repo=path):
            proc = resolutions._run(path, "ls-tree", plan["head"], "--", workflow.path)
            fields = resolutions._text(proc).strip().split()
            if proc.returncode or len(fields) != 4 or fields[0] not in ("100644", "100755") \
                    or fields[1] != "blob" or fields[3] != workflow.path:
                raise TrainError("due suite workflow missing at the train head: %s"
                                 % workflow.path)


class GitHub:
    def __init__(self, slug: str, workflows, *, gh=land._gh, repo=None, cache=None):
        from . import radar
        if not radar._GITHUB_RE.fullmatch(slug) or not workflows:
            raise ValueError("repository and complete required workflow contract expected")
        if callable(getattr(workflows, "resolve", None)):
            # #2503: a dynamic contract selects its due suites per plan; there
            # is no static 23-suite tuple to freeze or de-duplicate.
            self.workflows = workflows
        else:
            items = tuple(workflows)
            if not items:
                raise ValueError("repository and complete required workflow contract expected")
            if len({w.path for w in items}) != len(items):
                raise ValueError("duplicate workflow contract")
            self.workflows = items
        self.slug, self.gh = slug, gh
        #: #2503: the private train clone and the due-parts proof cache. A
        #: static contract ignores both; a dynamic contract resolves its due
        #: suites from the repository's own selector at each plan.
        self.repo, self.cache = repo, cache

    def _due(self, plan):
        """The workflows this plan makes due (all of a static contract)."""
        resolve = getattr(self.workflows, "resolve", None)
        if not callable(resolve):
            return tuple(self.workflows)
        if not self.repo:
            raise TrainError("dynamic CI contract requires the train repository")
        return resolve(plan, repo=self.repo, cache=self.cache)

    def _sources(self, plan):
        """The audited sources of the parts this plan actually executes."""
        sources_for = getattr(self.workflows, "sources_for", None)
        if not callable(sources_for):
            return audited_sources(self.workflows)
        if not self.repo:
            raise TrainError("dynamic CI contract requires the train repository")
        return sources_for(plan, repo=self.repo, cache=self.cache)

    def _api(self, path):
        proc = self.gh(["api", "--paginate", "--slurp", "repos/%s/%s" % (self.slug, path)])
        if proc.returncode and _slurp_unsupported(proc):
            # #2295 : sans --slurp, `--paginate` concatène les pages JSON.
            proc = self.gh(["api", "--paginate", "repos/%s/%s" % (self.slug, path)])
            if not proc.returncode:
                return _json_pages(proc)
        if proc.returncode:
            raise TrainError("cannot read exact train CI evidence")
        return land._json(proc, "train CI API", list)

    def verify(self, plan):
        """Verify the audited execution contract at this exact train head.

        In particular, changing a matrix or making an executable step
        conditional cannot be covered by the old list of green job names.
        This is read-only and performed before dispatch and before using CI.
        A long packet CI re-reads the same plan; the successful check of an
        exact (head, base) is remembered on the contract, never re-fetched per
        poll. The base is part of the key: the due set and its sources are
        resolved against it (#2503, N1), so a changed base is a new proof.
        """
        verified = getattr(self.workflows, "verified_heads", None)
        key = (plan["head"], plan["base"]) if verified is not None else None
        if verified is not None and key in verified:
            return
        for path, blob in sorted(self._sources(plan).items()):
            pages = self._api("contents/%s?ref=%s" % (quote(path, safe="/"), plan["head"]))
            if len(pages) != 1 or not isinstance(pages[0], dict) \
                    or pages[0].get("type") != "file" or pages[0].get("sha") != blob:
                raise TrainError("full-suite source differs from audited CI contract: %s" % path)
        for workflow in self._due(plan):
            if not isinstance(workflow, DueWorkflow):
                continue
            pages = self._api("contents/%s?ref=%s"
                              % (quote(workflow.path, safe="/"), plan["head"]))
            if len(pages) != 1 or not isinstance(pages[0], dict) \
                    or pages[0].get("type") != "file":
                raise TrainError("due suite workflow missing at the train head: %s"
                                 % workflow.path)
        if verified is not None:
            verified.add(key)

    def start(self, plan):
        """Called once, after durable ci-intent. Push contracts need no dispatch.

        On an interrupted multi-workflow dispatch, the remaining workflows
        are absent and the train waits. It never repeats an uncertain call.
        The pilot can reconcile absent runs before a bounded recovery.
        """
        self.verify(plan)
        for workflow in self._due(plan):
            if workflow.event != "workflow_dispatch":
                continue
            proc = self.gh(["workflow", "run", workflow.path, "--repo", self.slug,
                            "--ref", plan["branch"], "-f", "expected_sha=" + plan["head"]])
            if proc.returncode:
                raise TrainError("train dispatch failed or uncertain: %s" % workflow.path)

    def read(self, plan):
        self.verify(plan)
        evidence, pending, red, absent = [], False, False, False
        for workflow in self._due(plan):
            name = quote(workflow.path.rsplit("/", 1)[-1], safe="")
            path = ("actions/workflows/%s/runs?head_sha=%s&event=%s&per_page=100"
                    % (name, plan["head"], workflow.event))
            runs = [r for page in self._api(path) for r in page.get("workflow_runs", ())
                    if r.get("head_sha") == plan["head"]
                    and _same_branch(r, workflow, plan)
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
            if getattr(workflow, "called", ""):
                # #2446: the reusable workflow has no `workflow_call` run of
                # its own; the caller push must reference it at this exact
                # head, and its called jobs are read below.
                called_workflow(run, workflow, plan)
            jobs = [j for page in self._api("actions/runs/%s/attempts/%s/jobs?per_page=100"
                                            % (run["id"], attempt)) for j in page.get("jobs", ())]
            if isinstance(workflow, DueWorkflow):
                # #2503: the suite is not frozen; every job it actually ran
                # that is not an identified plumbing job, or a skip-by-design
                # job of this suite, must be green. A job the API does not
                # name cannot be proved, and a name alone never exempts it.
                for job in jobs:
                    if not isinstance(job.get("name"), str):
                        raise TrainError("due suite job without a name: %s"
                                         % workflow.path)
                required = [job for job in jobs
                            if not _tolerated_job(job["name"], workflow.path,
                                                  workflow.context)]
                if not required:
                    raise TrainError("due suite has no executable job: %s" % workflow.path)
                for job in required:
                    if job.get("status") != "completed" \
                            or job.get("conclusion") not in ("success", "failure"):
                        raise TrainError("required suite job missing, skipped or interrupted")
                # A tolerated job is tolerated when *skipped by design*; if it
                # actually ran and failed, the run is red and this suite is the
                # faulty one (N2): a generic conclusion mismatch would hide it.
                failed = any(job.get("conclusion") == "failure" for job in jobs)
            else:
                failed = False
                for required in workflow.jobs:
                    found = required_job(required, jobs)
                    if len(found) != 1 or found[0].get("status") != "completed" \
                            or found[0].get("conclusion") not in ("success", "failure"):
                        # Success of the workflow with a skipped or ambiguous
                        # required job cannot prove that this suite actually ran.
                        raise TrainError("required full-suite job missing, skipped or interrupted")
                    failed = failed or found[0]["conclusion"] == "failure"
            if failed != (run["conclusion"] == "failure"):
                # A red run with no proved failing job (an ancillary failure
                # for a static suite) names no culpable lot; nor is a failed
                # required job hidden behind a successful workflow.
                raise TrainError("workflow conclusion does not prove full-suite job outcome")
            red = red or failed
            evidence.append(dict(workflow=workflow.path, run=run["id"], attempt=attempt,
                                 conclusion=run["conclusion"],
                                 jobs=[dict(id=j["id"], name=j.get("name"),
                                            conclusion=j.get("conclusion")) for j in jobs]))
        status = "absent" if absent else "pending" if pending else "red" if red else "green"
        return dict(head=plan["head"], tree=plan["tree"], complete=not absent and not pending,
                    status=status, workflows=evidence)
