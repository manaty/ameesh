# SPDX-License-Identifier: AGPL-3.0-only
"""Audited Nexlink train CI contract (#2503), outside the generic core (#7794).

Nexlink's packet CI is dynamic: the parts a packet makes due are recomputed
from the integrated change set by the repository's own `lot-plan` selector,
and only those parts are dispatched and proved. The selector and the shared
plan/status workflows are audited; the 23 suite definitions are not frozen.
The core reaches this contract only through the Module interface
(``registry.train_contract``), never by importing this package.
"""
import re
import types

from ... import ci_parts, merge_queue, radar, resolutions, train_ci
from ...train import PublicationRefused, TrainError, digest


#: Nexlink target commit whose selector was audited. Only the shared
#: plan/status workflows, their fetch helper and the due-parts selector are
#: frozen; the 23 suite definitions are the reviewed project code and are
#: selected per packet.
NEXLINK_AUDIT_COMMIT = "c1277f0681e261e3a71d987eb34b6c01f252e1f9"

_NEXLINK_LOT_PLAN = (".github/workflows/lot-plan.yml",
                     "13e91b5d27dd9f09f33938717f29d4f72e18a436")
_NEXLINK_LOT_STATUS = (".github/workflows/lot-status.yml",
                       "237d75fc555ca37281ea562ed49aca47857d6e7f")
_NEXLINK_LOT_SELECTOR = ("scripts/ci/lot-plan.mjs",
                         "7b9f01de9ab4075b16c5bb80032c762b45299d0c")
#: #7246: lot-plan delegates its fetch to this bounded retry helper (#6589).
#: Pin the executable too: auditing only its caller would leave the due-parts
#: computation dependent on an unchecked script at the dispatched head.
_NEXLINK_FETCH_RETRY = ("scripts/ci/git-fetch-retry.sh",
                        "d68cfb30adb894a59206ccfc9f5367a0ad898349")

#: The shared execution contract of a Nexlink packet: the selector that
#: computes the due parts, the reusable plan job and the reusable status
#: publisher, including the plan's fetch helper. The suite workflows are not
#: pinned: they are the project's reviewed code, selected per packet by the selector.
_NEXLINK_SOURCES = {
    _NEXLINK_LOT_PLAN[0]: _NEXLINK_LOT_PLAN[1],
    _NEXLINK_LOT_STATUS[0]: _NEXLINK_LOT_STATUS[1],
    _NEXLINK_LOT_SELECTOR[0]: _NEXLINK_LOT_SELECTOR[1],
    _NEXLINK_FETCH_RETRY[0]: _NEXLINK_FETCH_RETRY[1],
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
    list is accepted only when the real diff proves a documentation-only
    packet (canon §3, #5428, #8175); any other empty selection is refused.
    """

    ci_parts = radar.CiParts(
        selector="scripts/ci/lot-plan.mjs",
        argv=("node", "{selector}", "--files", "{files}", "--out", "{out}"),
        schema="nexlink-lot-plan/1",
        due="due")

    def __init__(self):
        self._resolved_key = None
        self._resolved = ()
        #: #8175: (head, base) of a packet accepted on a proven doc-only diff.
        self._doc_only_key = None
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
            "dispatch_tolerated": list(train_ci.DUE_DISPATCH_TOLERATED),
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
            raise PublicationRefused(
                "the target selector differs from the audited Nexlink contract: %s"
                % _NEXLINK_LOT_SELECTOR[0])

    def _doc_only(self, plan, repo):
        """#8175: le paquet ne change que la documentation sans CI.

        Une liste due vide n'est une preuve que si le diff réel `base..head`
        ne touche que la liste blanche de `merge_queue.doc_only_changes`,
        la même que #5428 / land.py:1529. Sinon le refus reste terminal :
        une sélection vide peut aussi venir d'un sélecteur ou d'un change set
        qui ne prouve rien. Lecture locale, jamais une déclaration.
        """
        try:
            changes = ci_parts.raw_changes(repo, plan["base"], plan["head"])
        except ci_parts.PartsError as exc:
            raise PublicationRefused(
                "the change set of the packet cannot be read for a doc-only proof: %s"
                % exc) from exc
        return merge_queue.doc_only_changes(changes)

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
            # #8175 (canon §3): a packet whose real diff only touches the
            # documentation whitelist awaits no CI, like a doc-only queue
            # packet (#5428). Any other empty selection stays a refusal.
            if not self._doc_only(plan, repo):
                raise PublicationRefused(
                    "no due part: a packet CI cannot be proven (doc-only packet?)")
            self._doc_only_key = key
            self._resolved_key = key
            self._resolved = ()
            return ()
        workflows = []
        for context in due:
            match = re.fullmatch(r"lot/([A-Za-z0-9_-]+)", context)
            if match is None:
                raise PublicationRefused("malformed due part: %r" % context)
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
                raise PublicationRefused("due part %s is not declared by %s at the train head"
                                         % (context, path))
            workflows.append(train_ci.DueWorkflow(path=path, context=context))
        self._resolved_key = key
        self._resolved = tuple(workflows)
        return self._resolved

    def due_for(self, plan, *, repo, cache=None):
        """The due suites, for the local existence check before publication."""
        return self.resolve(plan, repo=repo, cache=cache)

    def doc_only(self, plan):
        """#8175: ce plan a été accepté sur un diff doc seule prouvé."""
        return self._doc_only_key == (plan["head"], plan["base"])

    def sources_for(self, plan, *, repo, cache=None):
        """The shared sources pinned by this contract, for one valid plan."""
        self.resolve(plan, repo=repo, cache=cache)
        return dict(_NEXLINK_SOURCES)


def nexlink() -> Nexlink:
    """The Nexlink packet contract: due suites only, dispatched with expected_sha."""
    return Nexlink()
