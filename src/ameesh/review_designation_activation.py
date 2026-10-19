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
  the historical turn trace, the same reference family as the merge gate) and
  every commit of the frozen range through its exact provenance receipt
  (#1465), except a designated reviewer's first-round correction under the §2
  threshold (#2638: ≤ ~50 changed lines, no design). A commit without a
  receipt makes the designation wait: an unknown family cannot prove
  independence;
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

import json
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
#: canon §2: a reviewer's first-round correction is a small fix up to ~50
#: changed lines; beyond that (or for a design change) its author becomes an
#: author of the head (#2638)
REVIEWER_FIX_LINES = 50
#: canon §2 "ou de conception": a design document is never a small fix
DESIGN_PREFIXES = ("docs/design/",)


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
    except Exception:  # noqa: BLE001 - unreadable state: no audited commit known
        state = None
    if state is not None:
        # A recorded state is authoritative even when the CURRENT pass is
        # invalid: `last_good_commit` is the last audited commit, while the
        # configured ref may already point at an un-audited head (B2 of the
        # Codex review cx2). Without a last good commit there is no audited
        # policy to read: never fall back to the ref on a known host.
        commit = str(state.get("last_good_commit") or "").strip().lower()
        root = str(state.get("root") or "")
        if commit and (not root or root == str(getattr(canon, "root", "") or "")):
            return commit
        return ""
    # No state at all (bench, first start): the exact configured ref is the
    # only commit known, and `mechanics.load` still refuses a non-commit.
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


def _designated_reviewers(db, item: Mapping, milestone: Optional[Mapping],
                          author: str) -> set[str]:
    """The names designated as reviewers of the lot, automatic or manual.

    The current column holds the previous freeze's automatic names and the
    manual exception; the journal marks keep the names a later freeze retired.
    A reviewer who pushed a first-round correction must stay recognizable after
    the freeze that retired them (#2638). The lot author is never a reviewer.
    """
    names: list[str] = []
    lot = int((item or {}).get("id") or 0)
    freeze_id = int((milestone or {}).get("id") or 0)
    try:
        names.extend(_previous_reviewers(db, lot, freeze_id))
    except Exception:  # noqa: BLE001 - unreadable journal: no automatic name
        pass
    try:
        from .work_reviewers import designated

        names.extend(designated(dict(item or {})))
    except Exception:  # noqa: BLE001 - unreadable column: no manual name
        pass
    return {str(value).strip() for value in names
            if str(value).strip() and str(value).strip() != author}


def _change(repo: str, sha: str) -> Optional[tuple[int, bool]]:
    """`(changed lines, touches a design path)` of one commit, or None.

    `None` means the diff is unreadable: the caller must not guess a small fix.
    A binary change is never a small fix, and neither is a design document
    (canon §2: a correction "> ~50 lignes ou de conception" goes back to the
    author, #2638)."""
    if not repo:
        return None
    text = plan_git._out(repo, "show", "--numstat", "-z", "--format=", "--no-renames", sha)
    if text is None:
        return None
    lines, design = 0, False
    for row in str(text).split("\0"):
        if not row:
            continue
        fields = row.split("\t", 2)
        if len(fields) < 3:
            return None
        added, removed, path = fields
        if added == "-" or removed == "-":
            return REVIEWER_FIX_LINES + 1, design
        try:
            lines += int(added) + int(removed)
        except ValueError:
            return None
        if path.startswith(DESIGN_PREFIXES):
            design = True
    return lines, design


def _authors(db, item: Mapping, milestone: Optional[Mapping],
             commits: Sequence[str], repo: str = "") -> tuple[list, list]:
    """`(authors, unknown authors)` drawn from the freeze and exact receipts.

    The reference author is the lot's author at freeze (the assignee, exactly
    as `merge_queue.review_gate` reads it). Every other commit counts as an
    author, with one exception: a **designated reviewer's** first-round
    correction under the §2 threshold (`REVIEWER_FIX_LINES`, no design change)
    does not make its author an author of the head — their family stays
    eligible, and the same reviewer may be designated again on the new head.
    A larger or design change makes them an author like any other (#2638).
    """
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
    reviewers = _designated_reviewers(db, item, milestone, name)
    by_name: dict[str, set] = {}
    if name:
        by_name.setdefault(name, set()).add(family)
    unknown = []
    review_commits: dict[str, list[tuple[str, str]]] = {}
    for sha in commits or ():
        try:
            receipt = review_provenance.receipt_for_commit(db, sha)
        except review_provenance.ProvenanceError:
            receipt = None
        if receipt is None:
            unknown.append(str(sha)[:12])
            continue
        actor = str(receipt.get("actor") or "").strip()
        if not actor:
            continue
        proven = _family(receipt.get("harness"))
        if actor != name and actor in reviewers:
            # held until the §2 threshold is known for their whole correction
            review_commits.setdefault(actor, []).append((str(sha), proven))
            continue
        by_name.setdefault(actor, set()).add(proven)
    for actor, entries in sorted(review_commits.items()):
        total, design = 0, False
        for sha, _proven in entries:
            change = _change(repo, sha)
            if change is None or change[1]:
                design = True
                break
            total += change[0]
        if design or total > REVIEWER_FIX_LINES:
            # beyond the small first-round fix: they authored the head
            by_name.setdefault(actor, set()).update(
                proven for _sha, proven in entries if proven)
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


def _manual(db, item: Mapping) -> list:
    """The reviewer names that were designated by hand, not by a past freeze.

    `work_items.reviewers` holds both. The automatic names are exactly the ones
    a `review-designation:` mark already carries; subtracting them keeps the
    manual exception visible on a replays and lets a new freeze clear only the
    names it wrote itself.
    """
    from .work_reviewers import designated

    try:
        names = list(designated(dict(item or {})))
    except Exception:  # noqa: BLE001 - unreadable list: no manual exception
        return []
    if not names:
        return []
    try:
        automatic = set(_previous_reviewers(db, int(item.get("id") or 0), 0))
    except Exception:  # noqa: BLE001 - unreadable marks: treat all as manual
        return names
    return [name for name in names if name not in automatic]


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
    authors, unknown = _authors(db, item, freeze, commits, repo)
    return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=paths,
                  commits=commits, policy=policy, authors=authors,
                  unknown_authors=unknown, candidates=candidates, notify=notify,
                  manual=_manual(db, item))


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


def _marks(db, lot: int, *, freeze_id: Optional[int] = None,
           since_ts: Optional[float] = None) -> Optional[list]:
    """Les marques automatiques du lot, dans l'ordre du journal (#2487).

    Rend `[(row_id, kind, payload, created_ts)]`, `kind` valant
    `"designation"` (une sélection, y compris `waiting`) ou `"carried"` (une
    marque gardée par un report). `freeze_id` restreint à un gel. None quand le
    journal est illisible : l'appelant ne retire alors aucun nom et ne devine
    pas la marque effective.
    """
    from . import verdict_carry
    lot = int(lot)
    if freeze_id is None:
        prefixes = (("%s%d:" % (pg.MARK_PREFIX, lot), "designation"),
                    (verdict_carry.carried_mark_prefix(lot), "carried"))
    else:
        prefixes = ((pg.mark_prefix(lot, int(freeze_id)), "designation"),
                    ("%s%d:" % (verdict_carry.carried_mark_prefix(lot), int(freeze_id)),
                     "carried"))
    since = 0.0 if since_ts is None else float(since_ts or 0.0)
    found = []
    for prefix, kind in prefixes:
        try:
            rows = storage.of(db).work.events_noted(prefix, since, ids=[lot])
        except Exception:  # noqa: BLE001 - journal illisible : rien n'est retiré
            return None
        for row in rows or ():
            note = str(row.get("note") or "")
            if kind == "designation":
                try:
                    payload = pg.designation_of(note)
                except rd.DesignationError:
                    continue
            else:
                payload = verdict_carry.carried_mark_of(note)
            if payload is None:
                continue
            try:
                noted_freeze = int((payload.get("freeze") or {}).get("id") or 0)
            except (TypeError, ValueError):
                continue
            if freeze_id is not None and noted_freeze != int(freeze_id):
                continue
            found.append((int(row.get("id") or 0), kind, payload,
                          float(row.get("created_ts") or 0.0)))
    return sorted(found, key=lambda entry: entry[0])


def _effective_mark(db, lot: int, freeze_id: int, *,
                    since_ts: Optional[float] = None) -> Optional[tuple]:
    """La dernière marque automatique de CE gel (ordre du journal), ou None.

    Sélection ou report : la plus récente décrit seule l'état du gel (#2487,
    arbitrage du pilote). Une marque illisible ou d'un autre gel est ignorée.
    """
    marks = _marks(db, lot, freeze_id=int(freeze_id), since_ts=since_ts)
    return marks[-1] if marks else None


def _previous_reviewers(db, lot: int, freeze_id: int) -> list:
    """The reviewers of the lot's automatic designations outside `freeze_id`.

    Only the journal marks are read: a name that never appeared in a mark is a
    manual designation and must survive a new freeze. A `waiting` designation
    names no reviewer. A carried mark (#2487) is an automatic designation too.
    """
    names: list[str] = []
    for _row_id, kind, noted, _ts in _marks(db, lot) or ():
        try:
            noted_freeze = int((noted.get("freeze") or {}).get("id") or 0)
        except (TypeError, ValueError):
            continue
        if noted_freeze == int(freeze_id):
            continue
        if kind == "designation" and noted.get("state") != rd.DESIGNATED:
            continue
        for reviewer in noted.get("reviewers") or ():
            name = str((reviewer or {}).get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _carried_mark(db, lot: int, freeze_id: int, *, since_ts: Optional[float] = None) -> Optional[dict]:
    """La marque `review-designation-carried` de CE gel, ou None (#2487).

    Une marque d'un autre gel, ou d'un autre lot, ne compte pas. La lecture est
    bornée à l'instant du gel quand il est connu, comme les marques de
    désignation (#1775, N3). Une marque illisible est ignorée : la reprise
    repasse par la désignation normale, jamais par une devinette.
    """
    carried = [payload for _row_id, kind, payload, _ts in
               _marks(db, lot, freeze_id=int(freeze_id), since_ts=since_ts) or ()
               if kind == "carried"]
    return carried[-1] if carried else None


def _carried_shape(mark: Mapping) -> dict:
    """La forme rendue par le hook et la reprise pour une désignation gardée."""
    reviewers = []
    for row in (mark or {}).get("reviewers") or ():
        name = str((row or {}).get("name") or "").strip()
        if name:
            reviewers.append({"name": name,
                              "family": str((row or {}).get("family") or "")})
    return {"state": "carried", "required": len(reviewers),
            "reviewers": reviewers,
            "freeze": dict((mark or {}).get("freeze") or {}),
            "carried": [dict(entry or {}) for entry in (mark or {}).get("carried") or ()],
            "why": str((mark or {}).get("why") or
                       "verdicts carried: the reported reviewers are kept")}


def _latest_freeze(db, lot: int) -> Optional[dict]:
    """The latest `frozen` milestone of the lot, as `{id, sha}`, or None."""
    row = plan_git.latest_freeze(
        storage.of(db).work.milestones(int(lot), pg.MILESTONES))
    if row is None:
        return None
    return {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip()}


def _lock_current_freeze(db, lot: int, freeze: Mapping) -> bool:
    """Locks the lot row and says whether `freeze` is still the latest one.

    `add_milestone` takes the same `FOR NO KEY UPDATE` lock of the lot row
    before inserting a `frozen` milestone: under this lock no new freeze can
    commit between the check and the writes that follow in the transaction.
    A recovery of an old freeze mutates nothing, not even the reviewer column
    of the newer head (B/P1 of the Codex review cx2 on #2447).
    """
    rows = db.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                    (int(lot),))
    if not rows:
        return False
    last = _latest_freeze(db, int(lot))
    if last is None:
        return False
    try:
        return (int(last["id"]) == int(freeze.get("id") or 0)
                and str(last.get("sha") or "").strip().lower()
                == str(freeze.get("sha") or "").strip().lower())
    except (TypeError, ValueError):
        return False


def _sync_reviewers(db, item: Mapping, freeze: Mapping, names, *, text: str = "") -> None:
    """Makes the automatic designation visible to the gate and to progress.

    The journal mark is the authoritative record, but the reviewers read by
    the merge gate, `progress` and the stale-reviewer alert are the
    `work_items.reviewers` column (`work_reviewers.designated`). Every
    automatic name is retired except the ones of the **effective** mark of the
    freeze — the last one in journal order, selection or carried (#2487): a
    late carry therefore removes the selection it supersedes, even in the same
    freeze. A name that never appeared in a mark is a manual designation and is
    preserved. A manual waiver (`-none`) is never overridden.

    `text`, when given, is the journal line of this sync (a verdict carry keeps
    the reported reviewers instead of designating, #2487)."""
    from .storage.postgres.work import has_reviewers

    if not has_reviewers(db):
        return
    lot = int((item or {}).get("id") or 0)
    freeze_id = int((freeze or {}).get("id") or 0)
    if not _lock_current_freeze(db, lot, freeze):
        # A stale replay never writes the column of the newer head (B/P1 of
        # the Codex review cx2): no designation is recorded for this freeze.
        return
    rows = db.query(
        "SELECT reviewers FROM work_items WHERE id = %s FOR NO KEY UPDATE",
        (lot,))
    if not rows:
        return
    current = [str(name) for name in (rows[0].get("reviewers") or []) if name]
    if "-none" in current:
        return
    names = [str(name or "").strip() for name in names or ()]
    names = [name for name in names if name]
    marks = _marks(db, lot)
    if marks is None:
        return                    # journal illisible : aucun nom retiré
    effective = _effective_mark(db, lot, freeze_id) if names else None
    keep_marks = {effective[0]} if effective else set()
    retired = set()
    for row_id, _kind, noted, _ts in marks:
        if row_id in keep_marks:
            continue
        for reviewer in noted.get("reviewers") or ():
            name = str((reviewer or {}).get("name") or "").strip()
            if name:
                retired.add(name)
    keep = [name for name in current if name not in retired]
    wanted = [str((reviewer or {}).get("name") or "").strip()
              for reviewer in (effective[2].get("reviewers") or () if effective else ())]
    wanted.extend(names)
    for name in wanted:
        if name and name not in keep:
            keep.append(name)
    if keep == current:
        return
    db.query(
        "UPDATE work_items SET reviewers ="
        " ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) WHERE id = %s"
        " RETURNING reviewers",
        (json.dumps(keep), lot))
    if names:
        text = text or "reviewers designated automatically at freeze #%d: %s" % (
            freeze_id, ", ".join(names))
    elif text:
        pass
    elif retired:
        text = ("automatic reviewer designation of freeze #%d cleared"
                " (previous head): %s" % (freeze_id, ", ".join(sorted(retired))))
    else:
        return
    storage.of(db).work.note(lot, str((item or {}).get("state") or ""), text, SENDER)


_SAVEPOINT = "ameesh_review_designation"


def _savepoint(tx) -> None:
    tx.execute("SAVEPOINT " + _SAVEPOINT)


def _release(tx) -> None:
    tx.execute("RELEASE SAVEPOINT " + _SAVEPOINT)


def _rollback(tx) -> None:
    tx.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
    tx.execute("RELEASE SAVEPOINT " + _SAVEPOINT)


def _internal_failure(cfg, tx, item: Mapping, freeze: Mapping, threads: list,
                      exc: BaseException) -> dict:
    """Journal/notify an unexpected selection failure and return the wait.

    Called only AFTER a savepoint rollback (or before any write): no partial
    mail, mark or reviewer column can survive from the failed attempt. A
    failure to write this note propagates, which rolls the freeze back: the
    milestone and its designation are one fact, never a half-mailed freeze.
    """
    why = "internal designation error: %s: %s" % (type(exc).__name__,
                                                  str(exc)[:240])
    notify = _notify(cfg, tx, str((item or {}).get("app") or ""),
                     registry.overview(tx))
    _note_unavailable(tx, item, freeze, why, threads, notify)
    _sync_reviewers(tx, item, freeze, [])
    return {"state": "unavailable", "reviewers": [], "freeze": dict(freeze or {}),
            "why": why}


class _ForcedPort:
    """Vue du port qui ignore la sélection déjà écrite du gel (#2487).

    `force` rejoue une désignation **réelle** après un report qui a refusé : la
    sélection supplantée du même gel ne doit pas être renvoyée telle quelle par
    `recorded` (`rd.apply`), et le port doit écrire une marque plus récente que
    le report, pas rejouer l'ancienne (`PgPort.commit(force=True)`). Le verrou
    et le contrôle du gel courant restent ceux du port réel.
    """

    def __init__(self, port) -> None:
        self.port = port

    def latest_freeze(self, lot: int):
        return self.port.latest_freeze(lot)

    def recorded(self, lot: int, freeze_id: int):
        return None

    def commit(self, lot: int, **kwargs) -> dict:
        return self.port.commit(lot, force=True, **kwargs)


def _selection(inputs: Inputs, port, *, force: bool = False) -> dict:
    """The `apply` call, shared by the hook and the recovery path."""
    if force:
        port = _ForcedPort(port)
    return rd.apply(
        inputs.item, policy=inputs.policy.rules, paths=list(inputs.paths),
        port=port, freeze_sha=str(inputs.freeze.get("sha") or ""),
        authors=inputs.authors, candidates=inputs.candidates,
        risk=inputs.policy.risk, base=inputs.base, policy_ref=inputs.policy.ref,
        notify=inputs.notify, unknown_authors=inputs.unknown_authors,
        families=inputs.policy.families or rd.KNOWN_FAMILIES)


def carried_plan(cfg, db, lot: int, sha: str) -> Optional[dict]:
    """Le plan du report pour la tête qu'on s'apprête à geler, ou None (#2487).

    Lu **avant** le gel : `verdict_carry.would_carry` ne lit que les jalons et
    le dépôt, sans écrire. Un plan qui ne reporte pas la totalité des verdicts
    en attente (`refused`) ne fait pas s'abstenir la désignation : les sièges
    manquants doivent être désignés normalement. Une erreur de lecture ne fait
    jamais perdre le gel ni la désignation : None, le chemin normal.
    """
    text = str(sha or "").strip()
    if not text:
        return None
    from . import verdict_carry
    try:
        plan = verdict_carry.would_carry(cfg, db, int(lot), text)
    except Exception:  # noqa: BLE001 - aucune preuve : désignation normale
        return None
    if plan.get("result") != "would-carry" or plan.get("refused"):
        return None
    return plan


def _previous_freeze(db, lot: int, freeze_id: int) -> Optional[dict]:
    """Le gel qui précède `freeze_id` dans la frise du lot, ou None."""
    rows = storage.of(db).work.milestones(int(lot), pg.MILESTONES)
    earlier = [row for row in rows or ()
               if row.get("kind") == "frozen"
               and int(row.get("id") or 0) < int(freeze_id)]
    return plan_git.latest_freeze(earlier)


def _previous_designees(db, lot: int, freeze_id: int, current=None) -> list:
    """Les relecteurs désignés au gel précédent, voté ou non (#2487).

    Sur un gel réaligné, le report ne connaît que les verdicts rendus
    (`pending`) : un siège désigné au gel précédent dont le verdict n'est pas
    encore rendu doit rester dans la colonne, sinon `progress` ne l'attend plus
    et il n'est plus relancé (B/P1 de dev1). La marque **effective** du gel
    précédent seule fait foi, sélection ou report. `current` (la colonne avant
    le gel) écarte un siège que le pilote a retiré à la main.
    """
    prior = _previous_freeze(db, lot, freeze_id)
    if not prior:
        return []
    mark = _effective_mark(db, lot, int(prior["id"]),
                           since_ts=float(prior.get("at_ts") or 0.0))
    if not mark:
        return []
    kept = (None if current is None
            else {str(name or "").strip() for name in current or () if name})
    return [dict(reviewer) for reviewer in (mark[2].get("reviewers") or ())
            if str((reviewer or {}).get("name") or "").strip()
            and (kept is None
                 or str((reviewer or {}).get("name") or "").strip() in kept)]


def _carried_in_transaction(tx, item: Mapping, freeze: Mapping,
                            carried: Mapping) -> dict:
    """Garde les relecteurs reportés au gel, sans sélection ni courrier (#2487).

    Le report n'est pas encore écrit (il suit le COMMIT du gel) : la marque
    `review-designation-carried` dit dès le gel quels relecteurs sont gardés et
    la colonne `reviewers` les rend visibles à `progress` et à l'alerte. Les
    sièges du gel précédent qui n'ont pas encore voté restent désignés (B/P1 de
    dev1) : le report ne reporte que des verdicts, pas une attente. Le report
    lui-même, quand il aboutit, recopie les verdicts et retrouve la même marque
    (idempotente). Aucune exception attendue ici : un échec d'écriture annule le
    gel avec sa marque, comme la désignation.
    """
    from . import verdict_carry
    lot = int((item or {}).get("id") or 0)
    freeze = dict(freeze or {})
    freeze_id = int(freeze.get("id") or 0)
    effective = _effective_mark(tx, lot, freeze_id,
                                since_ts=float(freeze.get("at_ts") or 0.0))
    if effective is not None and effective[1] == "carried":
        return _carried_shape(effective[2])
    plan = dict(carried or {})
    plan.setdefault("detail", "verdicts carried: the reported reviewers are kept")
    note = verdict_carry.carried_mark_note(
        lot, freeze, plan,
        extra_reviewers=_previous_designees(tx, lot, freeze_id,
                                            (item or {}).get("reviewers")))
    storage.of(tx).work.note(lot, str((item or {}).get("state") or ""), note, SENDER)
    mark = verdict_carry.carried_mark_of(note) or {}
    out = _carried_shape(mark)
    names = [row.get("name") for row in out.get("reviewers") or ()]
    _sync_reviewers(
        tx, item, freeze, names,
        text="reviewers kept from carried verdicts at freeze #%d: %s (#2442/#2487)"
             % (freeze_id, ", ".join(str(name) for name in names)))
    return out


def designate_in_transaction(cfg, tx, lot: int, *, milestone: Optional[Mapping],
                             threads: list, carried: Optional[Mapping] = None) -> dict:
    """The `on_frozen` body: all writes stay in the freeze's transaction.

    `carried` (#2487) : le plan du report quand il couvre la tête gelée ; la
    désignation s'abstient alors (marque reportée, relecteurs gardés, aucun
    courrier). Il est calculé avant le gel par `carried_plan` — la transaction
    ne fait aucune lecture réseau — et absent pour tout gel non réaligné.

    A manual reviewer already designated makes the automatic path an explicit
    no-op (the manual exception is journaled by its own command). Every
    expected data problem is journaled and notified, never raised: a freeze is
    a fact of the lot's timeline and must not be lost to a designation input.

    The selection writes inside a **savepoint**: an exception while the mails,
    the mark or the reviewer column are being written rolls the attempt back
    (and truncates its deferred thread writes) before the explicit waiting
    note is written. No first mail survives without its mark, and a replay
    cannot mail the same reviewer twice (B3 of the Codex review cx2). A
    failure to write the waiting note itself propagates: the milestone and
    its designation are one fact.
    """
    item = work.get(tx, int(lot)) or {"id": int(lot), "state": "", "title": "", "app": ""}
    freeze = dict(milestone or {})
    freeze.setdefault("id", 0)
    freeze.setdefault("sha", "")
    manual = _manual(tx, item)
    if manual:
        storage.of(tx).work.note(
            int(lot), str(item.get("state") or ""),
            "automatic reviewer designation skipped at freeze %s: manual"
            " reviewer(s) already designated (%s); the manual designation"
            " remains the journaled exception"
            % (_freeze_label(freeze), ", ".join(manual)), SENDER)
        return {"state": "manual", "reviewers": [], "freeze": freeze,
                "why": "manual reviewer designation"}
    if carried:
        return _carried_in_transaction(tx, item, freeze, carried)
    try:
        inputs = prepare(cfg, tx, int(lot), freeze=freeze)
    except Exception as exc:  # noqa: BLE001 - recorded below, never guessed
        return _internal_failure(cfg, tx, item, freeze, threads, exc)
    if isinstance(inputs, Unavailable):
        if not inputs.silent:
            notify = _notify(cfg, tx, str(item.get("app") or ""),
                             registry.overview(tx))
            _note_unavailable(tx, item, freeze, inputs.why, threads, notify)
            _sync_reviewers(tx, item, freeze, [])
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": inputs.why}
    checkpoint = len(threads)
    _savepoint(tx)
    try:
        if not inputs.paths or inputs.policy.required == 0:
            out = {"state": rd.NONE, "required": 0, "reviewers": [],
                   "freeze": freeze,
                   "why": ("light lot: no review before merge" if inputs.paths
                           else "no changed path: no review due")}
            _sync_reviewers(tx, item, freeze, [])
        else:
            out = _selection(inputs, pg.PgPort(tx, after_commit=threads))
            if out.get("state") == rd.DESIGNATED:
                _sync_reviewers(tx, item, freeze,
                                [r.get("name") for r in out.get("reviewers") or ()])
            else:
                _sync_reviewers(tx, item, freeze, [])
        _release(tx)
        return out
    except (rd.DesignationError, rd.StaleFreeze) as exc:
        try:
            _rollback(tx)
        except Exception:  # noqa: BLE001 - no rollback: the freeze must roll back
            raise exc
        del threads[checkpoint:]
        notify = _notify(cfg, tx, str(item.get("app") or ""), registry.overview(tx))
        _note_unavailable(tx, item, freeze, str(exc), threads, notify)
        _sync_reviewers(tx, item, freeze, [])
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": str(exc)}
    except Exception as exc:  # noqa: BLE001 - recorded after the savepoint rollback
        try:
            _rollback(tx)
        except Exception:  # noqa: BLE001 - no rollback: the freeze must roll back
            raise exc
        del threads[checkpoint:]
        return _internal_failure(cfg, tx, item, freeze, threads, exc)


def hook(cfg, outcome: Optional[dict] = None, *, carried: Optional[Mapping] = None):
    """The callback installed on the CLI's `frozen` milestone.

    `outcome`, when given, receives the designation shape for the command's
    message; the callback itself has no other return channel. `carried` is the
    read-only plan of the verdict carry when it covers the head being frozen
    (#2487): the callback then keeps the reported reviewers and mails nobody.
    """
    def on_frozen(tx, milestone, threads):
        result = designate_in_transaction(cfg, tx, int(milestone["work_item_id"]),
                                          milestone=milestone, threads=threads,
                                          carried=carried)
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


def _stale(freeze: Mapping, why: str = "") -> dict:
    """A recovery of a freeze that is no longer the latest: nothing written."""
    default = ("freeze #%s is no longer the latest: nothing written"
               % (dict(freeze or {}).get("id"),))
    return {"state": "stale", "reviewers": [], "freeze": dict(freeze or {}),
            "why": str(why or default)}


def designate(cfg, db, lot: int, *, freeze: Optional[Mapping] = None,
              force: bool = False) -> dict:
    """Replays the latest freeze outside the hook (recovery, CLI).

    The selection's mark, its mails and the reviewer column are written in ONE
    transaction, under the lot row lock that `add_milestone` also takes: a new
    freeze cannot slip between the designation and its column, and a replay of
    an old freeze writes nothing at all (B/P1 of the Codex review cx2). A
    policy/evidence problem is journaled in its own transaction; a selection
    refusal is returned, never raised to the caller.

    `force` (#2487) : rejoue la désignation réelle même quand un plan de report
    couvrait la tête (le report a refusé après le gel : les sièges manquants
    doivent être désignés). Sans lui, une désignation reportée déjà écrite est
    rendue telle quelle, et un plan de report encore valable garde les
    relecteurs reportés sans courrier.
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
    manual = _manual(db, item)
    if manual:
        return {"state": "manual", "reviewers": [], "freeze": freeze,
                "why": "manual reviewer(s) designated: %s" % ", ".join(manual)}
    if not force:
        effective = _effective_mark(db, int(lot), int(freeze.get("id") or 0),
                                    since_ts=float(freeze.get("at_ts") or 0.0))
        if effective is not None:
            # #2487 : la dernière marque du gel fait foi, sélection ou report.
            # Une sélection `waiting` est retentée : la capacité peut être
            # revenue, et la même marque devient `designated`.
            if effective[1] == "carried":
                return _carried_shape(effective[2])
            if effective[2].get("state") == rd.DESIGNATED:
                return dict(effective[2])
        plan = carried_plan(cfg, db, int(lot), str(freeze.get("sha") or ""))
        if plan:
            with db.transaction() as tx:
                if not _lock_current_freeze(tx, int(lot), freeze):
                    return _stale(freeze)
                return _carried_in_transaction(tx, item, freeze, plan)
    threads = _threads()
    checkpoint = len(threads)
    try:
        inputs = prepare(cfg, db, int(lot), freeze=freeze)
        if isinstance(inputs, Unavailable):
            if not inputs.silent:
                notify = _notify(cfg, db, str(item.get("app") or ""),
                                 registry.overview(db))
                with db.transaction() as tx:
                    if not _lock_current_freeze(tx, int(lot), freeze):
                        return _stale(freeze)
                    _note_unavailable(tx, item, freeze, inputs.why, threads, notify)
                    _sync_reviewers(tx, item, freeze, [])
                _drain(db, threads)
            return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                    "why": inputs.why}
        if not inputs.paths or inputs.policy.required == 0:
            with db.transaction() as tx:
                if not _lock_current_freeze(tx, int(lot), freeze):
                    return _stale(freeze)
                _sync_reviewers(tx, item, freeze, [])
            return {"state": rd.NONE, "required": 0, "reviewers": [],
                    "freeze": freeze,
                    "why": "light lot: no review before merge" if inputs.paths
                    else "no changed path: no review due"}
        with db.transaction() as tx:
            out = _selection(inputs, pg.PgPort(tx, after_commit=threads,
                                                at_freeze=False), force=force)
            names = ([r.get("name") for r in out.get("reviewers") or ()]
                     if out.get("state") == rd.DESIGNATED else [])
            _sync_reviewers(tx, item, freeze, names)
        _drain(db, threads)
        return out
    except rd.StaleFreeze as exc:
        # The port or the column guard refused an old freeze before any
        # write: never clear or re-register the newer head's reviewers.
        del threads[checkpoint:]
        return _stale(freeze, str(exc))
    except rd.DesignationError as exc:
        del threads[checkpoint:]
        with db.transaction() as tx:
            if not _lock_current_freeze(tx, int(lot), freeze):
                return _stale(freeze)
            _note_unavailable(tx, item, freeze, str(exc), threads,
                              _notify(cfg, db, str(item.get("app") or ""),
                                      registry.overview(db)))
            _sync_reviewers(tx, item, freeze, [])
        _drain(db, threads)
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": str(exc)}
    except BaseException:
        del threads[checkpoint:]
        raise


def reviewers(cfg, db, lot: int, *, apply: bool = False) -> dict:
    """Read the recorded designation of the latest freeze, optionally retrying it."""
    item = work.get(db, int(lot))
    if item is None:
        raise work.WorkError("lot %s not found" % int(lot))
    rows = storage.of(db).work.milestones(int(lot), pg.MILESTONES)
    row = plan_git.latest_freeze(rows)
    out = {"lot": int(lot), "freeze": None, "designation": None,
           "manual": _manual(db, item), "applied": None}
    if row is None:
        return out
    freeze = {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip(),
              "at_ts": row.get("at_ts"), "actor": row.get("actor") or ""}
    out["freeze"] = freeze
    marks = _marks(db, int(lot), freeze_id=int(freeze["id"]),
                   since_ts=float(freeze.get("at_ts") or 0.0))
    if marks is None:
        out["error"] = "the review designation journal cannot be read"
    elif marks:
        # #2487 : la dernière marque du gel fait foi, sélection ou report.
        effective = marks[-1]
        out["designation"] = (_carried_shape(effective[2])
                              if effective[1] == "carried"
                              else dict(effective[2]))
    if apply:
        out["applied"] = designate(cfg, db, int(lot), freeze=freeze)
    return out
