# SPDX-License-Identifier: AGPL-3.0-only
"""#5244: the activation step of a lot, read by the roadmap's progress.

Canon §5: done means active, and for ameesh active means staging (§14). Merging
code therefore only reaches the first step. The steps of a lot that was done:

* `merged`: merged, and its commit is known not to be in the attested staging;
* `active`: its commit is an ancestor of the commit attested on every host
  actually active (`staging_pending.active_hosts`, a reported host holds
  nothing, as in `staging_pending.waiting`);
* `gesture`: an operator gesture (closed by hand, reserved to an external
  executor) with no signed decision on the lot: the closure alone proves nothing;
* `receipt`: the same gesture, with a signed answer to a decision of the lot;
* `delivered`: delivered outside the target, there is nothing to activate;
* `unmeasured`: nothing can tell (a project with no attestation, no readable
  clone, no attestation yet, a commit absent from the clone). It keeps counting
  as before and says so: an absence of evidence is not a negative (§3).

`counted` says whether the lot counts as done. Read only: no fetch, and a git
failure only makes the step `unmeasured`.
"""
from __future__ import annotations

import bisect
import subprocess
from dataclasses import dataclass, field

from . import storage
from . import work_lifecycle as lifecycle

#: The attestations (`actif-<sha>.json`, `staging.attested`) carry no project:
#: they prove the mesh's own staging, like `staging_pending.regressions`.
ATTESTED_APP = "ameesh"
GIT_TIMEOUT_S = 10

OPEN, ABANDONED = "open", "abandoned"
MERGED, ACTIVE, GESTURE, RECEIPT, DELIVERED, UNMEASURED = (
    "merged", "active", "gesture", "receipt", "delivered", "unmeasured")
#: what the page and the Gantt say for each step that is reached
LABELS = {MERGED: "merged, not active yet", ACTIVE: "active in staging",
          GESTURE: "gesture done, no signed receipt", RECEIPT: "signed receipt",
          DELIVERED: "delivered off target", UNMEASURED: "activation not measured"}


@dataclass
class Evidence:
    """What staging proves, for `ATTESTED_APP`. `ancestors` is sorted, for the
    prefix match of a short milestone SHA; None: nothing can be measured."""
    ancestors: list | None = None
    known: set = field(default_factory=set)
    repo: str | None = None

    def contains(self, sha: str) -> bool:
        rows = self.ancestors or []
        at = bisect.bisect_left(rows, sha)
        return at < len(rows) and rows[at].startswith(sha)


def _git(repo: str | None, *args, stdin: str | None = None) -> str | None:
    command = ["git"] + (["-C", repo] if repo else []) + list(args)
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _clones(cfg) -> list:
    """The declared release clone, then the clone this command runs in: the
    release block only exists on the release host. An unrelated clone resolves
    no attested commit, and so measures nothing."""
    from . import release_cutoff as rc
    out = []
    try:
        project = rc.projects(cfg).get(ATTESTED_APP) if cfg is not None else None
    except (rc.ReleaseError, AttributeError, TypeError, ValueError):
        project = None
    if project is not None:
        out.append(project.repo)
    return out + [None]


def _ancestors(active: list, cfg) -> tuple[str | None, list] | None:
    """Commits contained in EVERY attested commit, from the first clone that
    knows them all."""
    for repo in _clones(cfg):
        common = None
        for sha in active:
            listed = _git(repo, "rev-list", "%s^{commit}" % sha)
            if listed is None:
                common = None
                break
            rows = set(listed.split())
            common = rows if common is None else common & rows
        if common is not None:
            return repo, sorted(common)
    return None


def evidence(db, shas=(), *, cfg=None) -> Evidence:
    """The staging evidence; `shas`: the merged commits to tell apart from a
    commit this clone does not have."""
    from . import staging_pending
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    active = sorted({e["sha"] for e in staging_pending.active_hosts(db).values()
                     if not e.get("absent") and e.get("sha")})
    found = _ancestors(active, cfg) if active else None
    if found is None:
        return Evidence()
    repo, ancestors = found
    out = Evidence(ancestors=ancestors, repo=repo)
    wanted = sorted({str(s).lower() for s in shas if s})
    # A commit absent from the clone is not known to be missing from staging.
    listed = _git(repo, "cat-file", "--batch-check=%(objectname)", stdin=
                  "".join(s + "\n" for s in wanted)) if wanted else ""
    answers = (listed or "").splitlines()
    if len(answers) == len(wanted):
        out.known = {s for s, a in zip(wanted, answers) if not a.endswith("missing")}
    return out


def merged_shas(milestones) -> dict:
    """The commit of the latest `merged` milestone of each lot (rows in date
    order, `progress.lot_milestones`)."""
    out = {}
    for row in milestones:
        if row.get("kind") == "merged" and row.get("sha"):
            out[int(row["work_item_id"])] = str(row["sha"]).lower()
    return out


def _view(step: str, counted: bool, **extra) -> dict:
    return dict({"step": step, "counted": counted, "label": LABELS[step]}, **extra)


def step_of(item: dict, sha: str | None, *, signed: dict | None, proof: Evidence) -> dict:
    """`{step, counted, label, decision?}` of one lot."""
    state = item.get("state") or ""
    reason = item.get("close_reason")
    if state == lifecycle.CLOSED:
        if reason not in ("done", "delivered"):
            return {"step": ABANDONED, "counted": False, "label": None}
        if (item.get("external_executor") or "").strip():
            decision = (signed or {}).get(int(item["id"]))
            return (_view(RECEIPT, True, decision=decision) if decision
                    else _view(GESTURE, False))
        if reason == "delivered":
            return _view(DELIVERED, True)
        # A review or an analysis closed by hand: outside the count, as before.
        return {"step": ABANDONED, "counted": False, "label": None}
    if state not in lifecycle.DELIVERED:
        return {"step": OPEN, "counted": False, "label": None}
    if (item.get("app") or item.get("workstream")) != ATTESTED_APP \
            or proof.ancestors is None or not sha:
        return _view(UNMEASURED, True)
    if proof.contains(sha):
        return _view(ACTIVE, True)
    return _view(MERGED, False) if sha in proof.known else _view(UNMEASURED, True)


def compute(db, items, *, cfg=None) -> dict:
    """`{lot id: step view}` for `items` (rows with id, state, close_reason, app,
    external_executor). Open lots cost nothing: only closed ones are read."""
    st = storage.of(db)
    rows = {int(i["id"]): i for i in items}
    merged = [i for i, r in rows.items() if r.get("state") in lifecycle.DELIVERED]
    gestures = [i for i, r in rows.items() if r.get("state") == lifecycle.CLOSED
                and r.get("close_reason") in ("done", "delivered")
                and (r.get("external_executor") or "").strip()]
    shas = merged_shas(st.progress.lot_milestones(merged)) if merged else {}
    signed = st.decisions.signed_for_lots(gestures) if gestures else {}
    proof = evidence(db, shas.values(), cfg=cfg) if merged else Evidence()
    return {i: step_of(r, shas.get(i), signed=signed, proof=proof) for i, r in rows.items()}
