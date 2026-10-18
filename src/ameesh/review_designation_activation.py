# SPDX-License-Identifier: AGPL-3.0-only
"""#2447 — activate the reviewer designation at the `frozen` milestone.

`review_designation` selects reviewers without I/O, `review_designation_pg`
writes the designation and its mails in one transaction. This module is the
missing caller: it turns a `frozen` milestone into the inputs of the selection
and calls the port **inside the milestone transaction**
(`PgPort(tx, after_commit=threads)`), so the freeze and its designation are one
fact.

Inputs, all from machine sources (never a note, an assignment or a name):

- **policy**: `mechanics.load(repo, commit, project)` at the exact audited
  canon commit (`canon_state.last_good_commit` when the host has one, the
  configured ref otherwise). `train_reviews` gives the rules, `reviews.*` the
  model families; a missing or malformed policy designates nobody — it is
  reported, never guessed;
- **frozen diff**: `merge-base(target, frozen_sha)..frozen_sha`, target read
  from `branch_target`, the project's configured target, then `origin/HEAD`;
- **authors**: the lot's assignee at freeze (`review_lot._author_at_freeze`,
  the historical turn trace) and every commit of the frozen range through its
  exact provenance receipt (#1465). A commit without a receipt makes the
  designation wait: an unknown family cannot prove independence;
- **candidates**: the registry rows of the lot's project, real session family
  only (`registry.live_harness`), availability and open-lot load;
- **pilot**: the project's `orchestrateur` in the canon, when registered; an
  empty notify is handled by the port (#1634: the freeze is kept, the
  notification is journaled as undeliverable).

Failure policy: a *data* problem (no repository, no canon policy, unreadable
evidence) never cancels the freeze — it is journaled once per freeze and the
pilot is mailed when one is known. A failed designation admits nothing: the
lot keeps its manual path (`ameesh work reviewer`), which remains the
journaled exception. The adapter also catches unexpected selection errors and
records them rather than losing a freeze to a designation bug; database
errors still cancel the milestone with it (the transaction is the guarantee).

Outside the hook, the same inputs feed the recovery path used by
`ameesh work reviewers <lot> --apply`: it replays the latest freeze, so a
capacity or canon problem fixed later can still designate the same head.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from . import canon as canon_mod
from . import lot_class, mechanics, plan_git, projects, registry, review
from . import review_designation as rd
from . import review_designation_pg as pg
from . import review_lot, review_provenance, storage, work

#: the sender of the journal notes and automatic mails
SENDER = pg.SENDER
#: journal prefix of a policy/evidence problem: one note per freeze
UNAVAILABLE_PREFIX = "review-designation-unavailable:"
#: a frozen range longer than this is not read in one selection (bounded work)
MAX_COMMITS = 500


def enabled(cfg) -> bool:
    """True when the host reads a canon: the activation has a policy to obey."""
    return bool(cfg is not None and str(getattr(cfg, "canon", "") or "").strip())


class ActivationError(Exception):
    """The designation cannot be prepared from the machine inputs."""


@dataclass(frozen=True)
class Unavailable:
    """An expected reason not to designate: journaled, never guessed."""

    why: str
    #: no canon configured at all: a bench/host without a canon is silent
    silent: bool = False


@dataclass(frozen=True)
class Policy:
    """The canon policy at one exact commit, with the need of the frozen diff."""

    rules: tuple
    families: tuple
    risk: str
    required: int
    ref: str
    commit: str
    source: str


@dataclass
class Inputs:
    """Everything the selection needs, read once from the machine sources."""

    item: dict
    freeze: dict
    repo: str
    base: str
    paths: list
    commits: list
    policy: Policy
    authors: list
    unknown_authors: list
    candidates: list
    notify: str
    manual: list = field(default_factory=list)


# --------------------------------------------------------------------------
# the canon policy, at the audited commit
# --------------------------------------------------------------------------

def _audited_commit(db, cfg, canon, source) -> str:
    """The exact commit to read for this canon: the host's last good one.

    `canon_state.last_good_commit` is the commit the host audited; the
    configured ref can be ahead of it (a fetch without a sync). Without a
    recorded state (bench, first start) the source commit is the only exact
    commit known; `mechanics.load` still refuses a non-commit.
    """
    from . import canon_sync

    try:
        key = canon_sync.state_key(db, canon, getattr(cfg, "host", ""))
        state = storage.of(db).canon.state(getattr(cfg, "host", ""), key)
    except Exception:  # noqa: BLE001 - unreadable state: the exact ref remains
        state = None
    if state and state.get("status") == "ok":
        commit = str(state.get("last_good_commit") or "").strip().lower()
        root = str(state.get("root") or "")
        if commit and (not root or root == str(getattr(canon, "root", "") or "")):
            return commit
    return str(source.commit or "").strip().lower()


def _policy(cfg, db, project: str, paths: Sequence[str]) -> Policy | Unavailable:
    """The first configured canon that declares a usable policy for `project`."""
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        return Unavailable("no canon configured: no automatic designation", silent=True)
    errors = []
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001 - no readable canon: explicit wait
        return Unavailable("cannot read the configured canon: %s: %s"
                           % (type(exc).__name__, str(exc)[:200]))
    if not canons:
        return Unavailable("no readable canon configured: no review policy")
    for canon in canons:
        for source in getattr(canon, "sources", ()) or ():
            if source.mode != "git" or not source.repo or not source.commit:
                errors.append("%s: canon source is not an exact Git commit"
                              % (getattr(canon, "root", "") or "canon"))
                continue
            if getattr(source, "prefix", ""):
                errors.append("%s: canon under a sub-directory, mechanics are read"
                              " at the Git root only" % (source.repo,))
                continue
            commit = _audited_commit(db, cfg, canon, source)
            if not commit:
                errors.append("%s: no exact audited commit" % (source.repo,))
                continue
            try:
                loaded = mechanics.load(source.repo, commit, project)
                need = loaded.reviews_for(paths)
            except (mechanics.MechanicsError, review.ReviewError,
                    canon_mod.CanonError) as exc:
                errors.append("%s@%s: %s" % (source.repo, commit[:12],
                                             str(exc)[:240]))
                continue
            try:
                rules = tuple(rd.Rule(paths=rule.paths, reviews=rule.reviews)
                              for rule in loaded.rules)
            except rd.DesignationError as exc:
                errors.append("%s@%s: %s" % (source.repo, commit[:12], exc))
                continue
            return Policy(rules=rules, families=tuple(loaded.families),
                          risk=need.risk, required=need.reviews,
                          ref=str(getattr(loaded, "source", "") or
                                  "%s@%s" % (source.repo, commit)),
                          commit=commit, source=source.repo)
    why = "; ".join(errors[:4]) or "no canon declares a review policy for %r" % (project,)
    return Unavailable("review policy unavailable at the audited canon commit: %s" % why)


# --------------------------------------------------------------------------
# the repository, the target and the frozen diff
# --------------------------------------------------------------------------

def _repository(db, cfg, item: Mapping, frozen_sha: str) -> str:
    """The repository holding the frozen head (assignee's worktree first)."""
    candidates: list[str] = []
    assignee = str((item or {}).get("assignee") or "").strip()
    if assignee:
        try:
            row = registry.get(db, assignee) or {}
        except Exception:  # noqa: BLE001 - unreadable registry: next hint
            row = {}
        candidates.append(str(row.get("cwd") or ""))
    try:
        candidates.append(lot_class.repository(cfg, dict(item or {})) or "")
    except Exception:  # noqa: BLE001 - no radar settings: next hint
        candidates.append("")
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass
    seen = set()
    for path in candidates:
        path = os.path.expanduser(str(path or "").strip())
        if not path or not os.path.isdir(path):
            continue
        repo = canon_mod.git_toplevel(path)
        if not repo or repo in seen:
            continue
        seen.add(repo)
        try:
            if plan_git._commit(repo, frozen_sha):
                return repo
        except Exception:  # noqa: BLE001 - unreadable repository: next hint
            continue
    return ""


def _resolve_base(repo: str, base: str) -> str:
    """The target to diff against: `origin/<branch>` first, then the local ref."""
    text = str(base or "").strip()
    if not text:
        return ""
    if plan_git._commit(repo, text):
        remote = canon_mod.remote_branch(repo, text)
        if remote and plan_git._commit(repo, remote):
            return remote
        return text
    remote = canon_mod.remote_branch(repo, text)
    if remote and plan_git._commit(repo, remote):
        return remote
    return ""


def _range(repo: str, base: str, head: str) -> tuple[str, list, list]:
    """`(start, paths, commits)` of the lot's own frozen change."""
    base = _resolve_base(repo, base)
    if not base:
        raise ActivationError("no readable target branch to diff the freeze against")
    head = str(head or "").strip()
    if not plan_git._commit(repo, head):
        raise ActivationError("the frozen commit %s is not in %s" % (head[:12], repo))
    proc = plan_git._out(repo, "merge-base", base, head)
    if proc is None:
        raise ActivationError("cannot read merge-base(%s, %s)" % (base, head[:12]))
    start = str(proc).strip() or base
    try:
        paths = canon_mod.git_changed_paths_between(repo, start, head)
        commits = (plan_git._out(repo, "rev-list", "--no-merges",
                                 "%s..%s" % (start, head)) or "").split()
    except canon_mod.GitError as exc:
        raise ActivationError("cannot read the frozen diff: %s" % exc) from exc
    if len(commits) > MAX_COMMITS:
        raise ActivationError("the frozen range carries %d commits (limit %d):"
                              " refusing an unbounded provenance read"
                              % (len(commits), MAX_COMMITS))
    return start, paths, commits


# --------------------------------------------------------------------------
# the authors: the assignee at freeze and each commit's exact receipt
# --------------------------------------------------------------------------

def _family(harness) -> str:
    return review_lot.family_of(str(harness or ""))


def _authors(db, item: Mapping, milestone: Optional[Mapping],
             commits: Sequence[str]) -> tuple[list, list]:
    """`(authors, unknown authors)` drawn from the freeze and exact receipts."""
    lot = int((item or {}).get("id") or 0)
    assignee = str((item or {}).get("assignee") or "").strip()
    author = {}
    try:
        author = review_lot._author_at_freeze(db, lot, dict(item or {})) or {}
    except Exception:  # noqa: BLE001 - unreadable trace: unknown family
        author = {}
    name = str(author.get("name") or assignee).strip()
    family = _family(author.get("harness"))
    if not family and milestone is not None:
        try:
            proof = review_provenance.receipt_for_milestone(db, int(milestone["id"]))
        except (review_provenance.ProvenanceError, KeyError, TypeError, ValueError):
            proof = None
        if proof and str(proof.get("actor") or "").strip() == name:
            family = _family(proof.get("harness"))
    if not family and name and (milestone is None
                                or str(milestone.get("actor") or "").strip()
                                in ("", name)):
        try:
            row = registry.get(db, name) or {}
            # the declared harness is not a session: only an opened session
            # proves the family that really ran (L242b, #1037)
            if row.get("session_id") or row.get("session_harness"):
                family = _family(row.get("session_harness"))
        except Exception:  # noqa: BLE001 - no session: the family stays unknown
            family = ""
    by_name: dict[str, set] = {}
    if name:
        by_name.setdefault(name, set()).add(family)
    unknown = []
    for sha in commits or ():
        try:
            receipt = review_provenance.receipt_for_commit(db, sha)
        except review_provenance.ProvenanceError:
            receipt = None
        if receipt is None:
            unknown.append(str(sha)[:12])
            continue
        actor = str(receipt.get("actor") or "").strip()
        if actor:
            by_name.setdefault(actor, set()).add(_family(receipt.get("harness")))
    authors = []
    for actor in sorted(by_name):
        families = {f for f in by_name[actor] if f}
        # one actor with two proven families is an ambiguity: never guess
        family = next(iter(families)) if len(families) == 1 else ""
        authors.append({"name": actor, "family": family})
    return authors, unknown


# --------------------------------------------------------------------------
# the candidates: real session family, availability, open-lot load
# --------------------------------------------------------------------------

def _loads(db) -> dict:
    """Open lots per agent (the load of the selection), in one read."""
    try:
        rows = db.query(
            "SELECT assignee, count(*) AS load FROM work_items"
            " WHERE assignee IS NOT NULL AND assignee <> ''"
            "   AND state NOT IN ('merged', 'promoted', 'closed')"
            " GROUP BY assignee")
    except Exception:  # noqa: BLE001 - unreadable load: zero, never a guess
        return {}
    out = {}
    for row in rows or ():
        try:
            out[str(row["assignee"])] = int(row["load"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _notify(cfg, db, project: str, listing: Sequence[Mapping]) -> str:
    """The project's orchestrator in the registry, when one is declared."""
    from . import sous_utilisation

    try:
        names = sous_utilisation.alert_orchestrators(cfg, db, list(listing))
    except Exception:  # noqa: BLE001 - no readable role: no recipient
        return ""
    by_name = {str(row.get("name") or ""): row for row in listing or ()}
    wanted = str(project or "").strip()
    for name in names:
        row = by_name.get(name)
        if row is None:
            continue
        if not wanted or wanted in projects.agent_projects(dict(row)):
            return name
    return ""


def _manual(item: Mapping) -> list:
    from .work_reviewers import designated

    try:
        return list(designated(dict(item or {})))
    except Exception:  # noqa: BLE001 - unreadable list: no manual exception
        return []


# --------------------------------------------------------------------------
# the inputs of one freeze
# --------------------------------------------------------------------------

def prepare(cfg, db, lot: int, *, freeze: Optional[Mapping] = None) -> Inputs | Unavailable:
    """Reads the machine inputs of a freeze; an expected refusal is `Unavailable`."""
    item = work.get(db, int(lot))
    if item is None:
        return Unavailable("lot #%d not found" % int(lot))
    if freeze is None:
        rows = storage.of(db).work.milestones(int(lot), pg.MILESTONES)
        row = plan_git.latest_freeze(rows)
        if row is None or not str(row.get("sha") or "").strip():
            return Unavailable("lot #%d is not frozen: no `frozen` milestone" % int(lot))
        freeze = {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip(),
                  "at_ts": row.get("at_ts"), "actor": row.get("actor") or ""}
    else:
        freeze = dict(freeze)
    frozen_sha = str(freeze.get("sha") or "").strip()
    if not frozen_sha:
        return Unavailable("freeze #%s names no commit" % (freeze.get("id"),))
    repo = _repository(db, cfg, item, frozen_sha)
    if not repo:
        return Unavailable("no repository holds the frozen head %s: cannot read the"
                           " frozen diff" % frozen_sha[:12])
    base = lot_class.default_base(cfg, item, repo)
    if not base:
        return Unavailable("no target branch known for lot #%d" % int(lot))
    try:
        start, paths, commits = _range(repo, base, str(freeze.get("sha") or ""))
    except ActivationError as exc:
        return Unavailable(str(exc))
    project = str(item.get("app") or "").strip()
    if not project:
        return Unavailable("lot #%d declares no project: the review policy is unknown"
                           % int(lot))
    if not paths:
        # no changed file: no review is due, and no policy needs to be read
        return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=[],
                      commits=commits,
                      policy=Policy(rules=(), families=(), risk="light", required=0,
                                    ref="", commit="", source=""),
                      authors=[], unknown_authors=[], candidates=[], notify="",
                      manual=[])
    policy = _policy(cfg, db, project, paths)
    if isinstance(policy, Unavailable):
        return policy
    listing = registry.overview(db)
    notify = _notify(cfg, db, project, listing)
    candidates = rd.candidates_from_registry(
        listing, loads=_loads(db), project=project,
        families=policy.families or rd.KNOWN_FAMILIES)
    if notify:
        # the pilot receives the capacity alert; the review stays with the
        # project's agents, so the two roles never collapse into one mail
        candidates = [candidate for candidate in candidates
                      if candidate.name != notify]
    authors, unknown = _authors(db, item, freeze, commits)
    return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=paths,
                  commits=commits, policy=policy, authors=authors,
                  unknown_authors=unknown, candidates=candidates, notify=notify,
                  manual=_manual(item))


# --------------------------------------------------------------------------
# the writes
# --------------------------------------------------------------------------

def _freeze_label(freeze: Mapping) -> str:
    return "#%s (%s)" % (freeze.get("id"), str(freeze.get("sha") or "")[:12])


def _note_unavailable(db, item: Mapping, freeze: Mapping, why: str,
                      threads: list, notify: str = "") -> None:
    """One journal note per freeze, plus a mail to the pilot when one is known.

    The note is the explicit waiting state: the freeze is kept, the designation
    is not recorded (a recovery run designates the same head once the cause is
    fixed). `events_noted` is bounded to the freeze, so a replay of the same
    freeze never adds a second note.
    """
    from . import mail

    lot = int((item or {}).get("id") or 0)
    freeze_id = int(freeze.get("id") or 0)
    since = float(freeze.get("at_ts") or 0.0)
    prefix = "%s%d:%d:" % (UNAVAILABLE_PREFIX, lot, freeze_id)
    if storage.of(db).work.events_noted(prefix, since, ids=[lot]):
        return
    text = "%s automatic reviewer designation not computed: %s" % (prefix, why)
    storage.of(db).work.note(lot, str((item or {}).get("state") or ""), text, SENDER)
    if not notify:
        return
    body = "\n".join([
        "Automatic reviewer designation unavailable at freeze (canon §2, #1617/#2447).",
        "",
        "Lot: #%s \"%s\"" % (lot, " ".join(str((item or {}).get("title") or "").split())),
        "- Freeze: frozen milestone %s." % _freeze_label(freeze),
        "- Reason: %s" % why,
        "",
        "Nothing is admitted implicitly. Fix the canon policy or the evidence, then",
        "retry the designation for this same freeze:",
        "  ameesh work reviewers %d --apply" % lot,
        "The manual path remains available and journaled: ameesh work reviewer %d <agent>."
        % lot,
    ])
    mail.send(db, SENDER, notify, body, work_item_id=str(lot),
              payload={"review_designation": {
                  "lot": lot, "freeze": freeze_id,
                  "sha": str(freeze.get("sha") or ""), "state": "unavailable"}},
              after_commit=threads)


def _selection(inputs: Inputs, port) -> dict:
    """The `apply` call, shared by the hook and the recovery path."""
    return rd.apply(
        inputs.item, policy=inputs.policy.rules, paths=list(inputs.paths),
        port=port, freeze_sha=str(inputs.freeze.get("sha") or ""),
        authors=inputs.authors, candidates=inputs.candidates,
        risk=inputs.policy.risk, base=inputs.base, policy_ref=inputs.policy.ref,
        notify=inputs.notify, unknown_authors=inputs.unknown_authors,
        families=inputs.policy.families or rd.KNOWN_FAMILIES)


def designate_in_transaction(cfg, tx, lot: int, *, milestone: Optional[Mapping],
                             threads: list) -> dict:
    """The `on_frozen` body: all writes stay in the freeze's transaction.

    A manual reviewer already designated makes the automatic path an explicit
    no-op (the manual exception is journaled by its own command). Every
    expected data problem is journaled and notified, never raised: a freeze is
    a fact of the lot's timeline and must not be lost to a designation input.
    An unexpected selection error is recorded the same way; only a database
    failure (the note itself cannot be written) rolls the milestone back.
    """
    item = work.get(tx, int(lot)) or {"id": int(lot), "state": "", "title": "", "app": ""}
    freeze = dict(milestone or {})
    freeze.setdefault("id", 0)
    freeze.setdefault("sha", "")
    try:
        if _manual(item):
            storage.of(tx).work.note(
                int(lot), str(item.get("state") or ""),
                "automatic reviewer designation skipped at freeze %s: manual"
                " reviewer(s) already designated (%s); the manual designation"
                " remains the journaled exception"
                % (_freeze_label(freeze), ", ".join(_manual(item))), SENDER)
            return {"state": "manual", "reviewers": [], "freeze": freeze,
                    "why": "manual reviewer designation"}
        inputs = prepare(cfg, tx, int(lot), freeze=freeze)
        if isinstance(inputs, Unavailable):
            if not inputs.silent:
                notify = _notify(cfg, tx, str(item.get("app") or ""),
                                 registry.overview(tx))
                _note_unavailable(tx, item, freeze, inputs.why, threads, notify)
            return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                    "why": inputs.why}
        if not inputs.paths:
            return {"state": rd.NONE, "required": 0, "reviewers": [],
                    "freeze": freeze, "why": "no changed path: no review due"}
        if inputs.policy.required == 0:
            return {"state": rd.NONE, "required": 0, "reviewers": [],
                    "freeze": freeze, "why": "light lot: no review before merge"}
        return _selection(inputs, pg.PgPort(tx, after_commit=threads))
    except (rd.DesignationError, rd.StaleFreeze) as exc:
        notify = _notify(cfg, tx, str(item.get("app") or ""), registry.overview(tx))
        _note_unavailable(tx, item, freeze, str(exc), threads, notify)
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a designation bug never loses a freeze
        why = "internal designation error: %s: %s" % (type(exc).__name__,
                                                      str(exc)[:240])
        try:
            notify = _notify(cfg, tx, str(item.get("app") or ""), registry.overview(tx))
            _note_unavailable(tx, item, freeze, why, threads, notify)
        except Exception:  # noqa: BLE001 - no note: the freeze must still be kept
            pass
        return {"state": "unavailable", "reviewers": [], "freeze": freeze, "why": why}


def hook(cfg, outcome: Optional[dict] = None):
    """The callback installed on the CLI's `frozen` milestone.

    `outcome`, when given, receives the designation shape for the command's
    message; the callback itself has no other return channel.
    """
    def on_frozen(tx, milestone, threads):
        result = designate_in_transaction(cfg, tx, int(milestone["work_item_id"]),
                                          milestone=milestone, threads=threads)
        if outcome is not None:
            outcome.clear()
            outcome.update(result)
        return result
    return on_frozen


# --------------------------------------------------------------------------
# recovery and read view
# --------------------------------------------------------------------------

def _threads() -> list:
    return []


def _drain(db, threads: list) -> None:
    for record in threads:
        record(db.cfg, db)


def designate(cfg, db, lot: int, *, freeze: Optional[Mapping] = None) -> dict:
    """Replays the latest freeze outside the hook (recovery, CLI).

    Uses `PgPort(db)`, which owns its transaction and drains the readable
    thread after its commit. A policy/evidence problem is journaled in its own
    transaction; a selection refusal is returned, never raised to the caller.
    """
    item = work.get(db, int(lot)) or {"id": int(lot), "state": "", "title": "",
                                      "app": ""}
    if freeze is None:
        rows = storage.of(db).work.milestones(int(lot), pg.MILESTONES)
        row = plan_git.latest_freeze(rows)
        if row is None:
            return {"state": "unfrozen", "reviewers": [], "freeze": None,
                    "why": "lot #%d is not frozen" % int(lot)}
        freeze = {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip(),
                  "at_ts": row.get("at_ts"), "actor": row.get("actor") or ""}
    freeze = dict(freeze)
    manual = _manual(item)
    if manual:
        return {"state": "manual", "reviewers": [], "freeze": freeze,
                "why": "manual reviewer(s) designated: %s" % ", ".join(manual)}
    threads = _threads()
    try:
        inputs = prepare(cfg, db, int(lot), freeze=freeze)
        if isinstance(inputs, Unavailable):
            if not inputs.silent:
                notify = _notify(cfg, db, str(item.get("app") or ""),
                                 registry.overview(db))
                with db.transaction() as tx:
                    _note_unavailable(tx, item, freeze, inputs.why, threads, notify)
                _drain(db, threads)
            return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                    "why": inputs.why}
        if not inputs.paths or inputs.policy.required == 0:
            return {"state": rd.NONE, "required": 0, "reviewers": [],
                    "freeze": freeze,
                    "why": "light lot: no review before merge" if inputs.paths
                    else "no changed path: no review due"}
        return _selection(inputs, pg.PgPort(db))
    except (rd.DesignationError, rd.StaleFreeze) as exc:
        with db.transaction() as tx:
            _note_unavailable(tx, item, freeze, str(exc), threads,
                              _notify(cfg, db, str(item.get("app") or ""),
                                      registry.overview(db)))
        _drain(db, threads)
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": str(exc)}


def reviewers(cfg, db, lot: int, *, apply: bool = False) -> dict:
    """Read the recorded designation of the latest freeze, optionally retrying it."""
    item = work.get(db, int(lot))
    if item is None:
        raise work.WorkError("lot %s not found" % int(lot))
    rows = storage.of(db).work.milestones(int(lot), pg.MILESTONES)
    row = plan_git.latest_freeze(rows)
    out = {"lot": int(lot), "freeze": None, "designation": None,
           "manual": _manual(item), "applied": None}
    if row is None:
        return out
    freeze = {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip(),
              "at_ts": row.get("at_ts"), "actor": row.get("actor") or ""}
    out["freeze"] = freeze
    try:
        out["designation"] = pg.PgPort(db).recorded(int(lot), int(freeze["id"]))
    except rd.DesignationError as exc:
        out["error"] = str(exc)
    if apply:
        out["applied"] = designate(cfg, db, int(lot), freeze=freeze)
    return out
