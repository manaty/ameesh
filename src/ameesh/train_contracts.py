# SPDX-License-Identifier: AGPL-3.0-only
"""Audited train CI contracts. Declaring one never enables a train.

The source blobs describe the execution semantics behind the required jobs.
A new workflow/planner needs a new audit; an unchanged display name does not
transfer full-suite proof. Nexlink's packet CI is dynamic (#2503): the parts
a packet makes due are recomputed from the integrated change set by the
repository's own `lot-plan` selector, and only those parts are dispatched and
proved. The selector and the shared plan/status workflows are audited; the 23
suite definitions are not frozen — each due suite is the project's reviewed
code at the integrated head, and every non-plumbing/non-tolerated job it runs
must be green. The audited selector runs twice: over the base tree (the plan
the target already published, so the head cannot narrow it away) and over the
head tree (the suites the packet adds or widens). A declarations tree whose
selector blob differs from the base is refused, never executed.

#2446: ``paquet.yml`` is the caller run of the packet gate. A push to
``integration/**`` creates that run; when no open PR covers the pushed head,
its ``suite`` job calls ``tests.yml`` and the API exposes the called jobs in
the same caller run (prefixed by the caller job). There is **no** separate
``tests.yml?event=workflow_call`` run. The contract therefore pins the caller
workflow, the called suite and the planning blobs, and requires the called
jobs (prefixed by the caller job) — the full 8 psycopg + 16 psql matrix, the
short macOS platform job and the aggregate ``tests`` check. #3198: the full
macOS suite is no longer part of the packet run (it moved to ``macos.yml``,
nightly and on the ``ci-macos`` label); what this contract requires is the
short platform verdict (L63/L66) that stays in ``tests.yml``.
"""
import re
import types

from . import ci_parts, radar, resolutions, train_ci
from .train import TrainError, digest
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
    The full matrix is 8 psycopg parts, 16 psql parts, the short macOS
    platform job and the aggregate ``tests`` check; doc-only and version lanes
    are conditional outside this contract. #3749: the 16 psql parts run the
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
              *("psycopg %d/8" % n for n in range(1, 9)),
              *("psql %d/16" % n for n in range(1, 17)),
              "plateforme macOS", "tests"),
        event="push",
        called=".github/workflows/tests.yml",
        sources=((".github/workflows/paquet.yml", "0d4e4c9cae703575b33aa393d6a1c95a81e5c896"),
                 (".github/workflows/tests.yml", "c8b5ca77a253c41ebc869081fee7618382dcefd3"),
                 ("scripts/test-plan.py", "2dbcecd40110f3a10f3551c312779cbefb03bb3c"),
                 ("scripts/test-parts.py", "f5a86e090e97e945b52f77fba344828394635d2a"),
                 ("scripts/ci-flow.py", "b633b95258f03db4d0760d3f47855a0e832ac8a4"),
                 ("scripts/ci-agregat.py", "d10a3b6d11f9abf4b9a4e16de68b1ba1a6ccc6f1"),
                 ("scripts/map.py", "7a7516683643ff7cd66abf3d50147b25446a03bb")),
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

    Only static contracts (``Workflow`` tuples) can be checked this way. The
    Nexlink contract is dynamic and pins sources of the Nexlink repository,
    which an ameesh tree does not carry; this check applies to the project
    tree that declares them.
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


#: Nexlink target commit whose selector was audited. Only the shared
#: plan/status workflows and the due-parts selector are frozen; the 23 suite
#: definitions are the reviewed project code and are selected per packet.
NEXLINK_AUDIT_COMMIT = "9103c1673dc451396db0202ff3fca7f87cd87bf8"

_NEXLINK_LOT_PLAN = (".github/workflows/lot-plan.yml",
                     "ae94763e5fcec34cf92cf0e3cadc6c4ec780afde")
_NEXLINK_LOT_STATUS = (".github/workflows/lot-status.yml",
                       "237d75fc555ca37281ea562ed49aca47857d6e7f")
_NEXLINK_LOT_SELECTOR = ("scripts/ci/lot-plan.mjs",
                         "7b9f01de9ab4075b16c5bb80032c762b45299d0c")

#: The shared execution contract of a Nexlink packet: the selector that
#: computes the due parts, the reusable plan job and the reusable status
#: publisher. The suite workflows themselves are not pinned: they are the
#: project's reviewed code, selected per packet by the selector.
_NEXLINK_SOURCES = {
    _NEXLINK_LOT_PLAN[0]: _NEXLINK_LOT_PLAN[1],
    _NEXLINK_LOT_STATUS[0]: _NEXLINK_LOT_STATUS[1],
    _NEXLINK_LOT_SELECTOR[0]: _NEXLINK_LOT_SELECTOR[1],
}


class Nexlink:
    """Dynamic Nexlink packet contract (#2503).

    A Nexlink packet is not one caller run over the 23 suites and the contract
    does not freeze them. Each lot suite is a `workflow_dispatch` workflow
    that publishes its `lot/<suite>` status; the parts a packet makes due are
    computed by the repository's own `scripts/ci/lot-plan.mjs` from the
    integrated change set. This contract pins the selector and the shared
    plan/status workflows, resolves the due contexts through the audited
    `ci_parts` sandbox over both the base and the head declarations, and
    dispatches those suites with `expected_sha`. The suite workflow itself
    must exist at the head; its own jobs, minus the identified plumbing jobs
    and the skip-by-design jobs of that suite, are the proof. An empty due
    list is refused: a doc-only packet is not a train proof.
    """

    ci_parts = radar.CiParts(
        selector="scripts/ci/lot-plan.mjs",
        argv=("node", "{selector}", "--files", "{files}", "--out", "{out}"),
        schema="nexlink-lot-plan/1",
        due="due")

    def __init__(self):
        self._resolved_key = None
        self._resolved = ()
        #: #2503: successful remote source checks by exact (head, base); the
        #: due set and the sources are resolved against the base, so a changed
        #: base is a new proof. A long packet CI is re-read at each service
        #: step and must not re-fetch the shared blobs at every poll.
        self.verified_heads = set()

    def digest(self):
        """The shared contract identity, never a frozen list of the 23 suites."""
        return digest({
            "schema": "nexlink-train-contract/1",
            "ci_parts": {"selector": self.ci_parts.selector,
                         "argv": list(self.ci_parts.argv),
                         "schema": self.ci_parts.schema,
                         "due": self.ci_parts.due},
            "sources": sorted(_NEXLINK_SOURCES.items()),
            "plumbing": list(train_ci.DUE_PLUMBING),
            "tolerated": {path: list(names) for path, names
                          in sorted(train_ci.DUE_SUITE_TOLERATED.items())},
        })

    def _project(self):
        return types.SimpleNamespace(name="nexlink", ci_parts=self.ci_parts)

    def _audit_base(self, repo, base):
        """The selector that computes the due list must be the audited one.

        `ci_parts.due` extracts the base tree and runs that selector there; a
        target that changed the selector would compute a different due list,
        so it needs a new contract, never a silently different selection. The
        head-declarations proof additionally refuses a selector blob that
        differs at the head (`ci_parts.due`), and the head's own blob is
        checked by `GitHub.verify`. The suite workflows are deliberately not
        checked here: they are the project's reviewed code and the selector
        reads them at the target and at the head.
        """
        proc = resolutions._run(repo, "ls-tree", base, "--", _NEXLINK_LOT_SELECTOR[0])
        fields = resolutions._text(proc).strip().split()
        if proc.returncode or len(fields) != 4 or fields[0] not in ("100644", "100755") \
                or fields[1] != "blob" or fields[2] != _NEXLINK_LOT_SELECTOR[1] \
                or fields[3] != _NEXLINK_LOT_SELECTOR[0]:
            raise TrainError("the target selector differs from the audited Nexlink contract: %s"
                             % _NEXLINK_LOT_SELECTOR[0])

    def _proof(self, plan, *, repo, cache, declarations_sha=None):
        """One due-parts proof: the audited selector over base or head tree."""
        try:
            return ci_parts.due(self._project(), plan["head"], plan["base"], repo,
                                cache=cache, declarations_sha=declarations_sha)
        except ci_parts.PartsError as exc:
            label = "base" if declarations_sha is None else "head declarations"
            raise TrainError("the due parts of the packet cannot be proven (%s): %s"
                             % (label, exc)) from exc

    def resolve(self, plan, *, repo, cache=None):
        """The dispatched suites of the `lot/<suite>` parts this plan makes due.

        #2503: two proofs. The base selector over the base tree preserves the
        plan the target already published (a head cannot narrow a due part
        away); the same audited selector over the **head** tree covers the
        workflows the packet adds or widens. The union is required, and each
        declaration is read at the dispatched head.
        """
        key = (plan["head"], plan["base"])
        if key == self._resolved_key:
            return self._resolved
        self._audit_base(repo, plan["base"])
        base_proof = self._proof(plan, repo=repo, cache=cache)
        head_proof = self._proof(plan, repo=repo, cache=cache,
                                 declarations_sha=plan["head"])
        due = tuple(base_proof["due"]) + tuple(
            context for context in head_proof["due"] if context not in base_proof["due"])
        if not due:
            raise TrainError("no due part: a packet CI cannot be proven (doc-only packet?)")
        workflows = []
        for context in due:
            match = re.fullmatch(r"lot/([A-Za-z0-9_-]+)", context)
            if match is None:
                raise TrainError("malformed due part: %r" % context)
            path = ".github/workflows/%s.yml" % match.group(1)
            # The context and the workflow file are two declarations at the
            # dispatched head; the selector names the context, the dispatch
            # needs the file. Refuse a due context the named file does not
            # declare at the head (a removed, renamed or divergent suite),
            # rather than dispatching an unrelated workflow.
            proc = resolutions._run(repo, "show", "%s:%s" % (plan["head"], path))
            declared = re.search(r"(?m)^\s*context:\s*(lot/[A-Za-z0-9_-]+)\s*$",
                                 resolutions._text(proc) if not proc.returncode else "")
            if proc.returncode or declared is None or declared.group(1) != context:
                raise TrainError("due part %s is not declared by %s at the train head"
                                 % (context, path))
            workflows.append(train_ci.DueWorkflow(path=path, context=context))
        self._resolved_key = key
        self._resolved = tuple(workflows)
        return self._resolved

    def due_for(self, plan, *, repo, cache=None):
        """The due suites, for the local existence check before publication."""
        return self.resolve(plan, repo=repo, cache=cache)

    def sources_for(self, plan, *, repo, cache=None):
        """The shared sources pinned by this contract, for one valid plan."""
        self.resolve(plan, repo=repo, cache=cache)
        return dict(_NEXLINK_SOURCES)


def nexlink() -> Nexlink:
    """The Nexlink packet contract: due suites only, dispatched with expected_sha."""
    return Nexlink()
