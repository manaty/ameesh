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
  from `branch_target`, the project's configured target, then `origin/HEAD`.
  The repository is the assignee's checkout, the host's radar repository, the
  process cwd, then (#2561) a worktree tracked by housekeeping or a sibling of
  those paths named after the lot/agent — the freeze command may run from the
  agent's session directory, another repository. The exact SHA and the issue's
  repository origin decide. The exact target branch is refreshed outside the
  freeze transaction and pinned to a commit for the whole reading (#3250);
- **authors**: the lot's assignee at freeze (`review_lot._author_at_freeze`,
  the historical turn trace, the same reference family as the merge gate) and
  every commit of the frozen range through its exact provenance receipt
  (#1465). A designated reviewer's correction is an authored commit of the
  head, whatever its size (#2888): it makes their family an author, and the
  §2 threshold only decides whether the correction should have been pushed at
  all (too big or a design change returns to the author). The authors of the
  lot's earlier freezes whose content subsists stay authors (#2924): an
  identical-content history rewrite does not make their families independent.
  A commit without a receipt is attributed by its agent committer's session
  family in the turn journal (`review_authors`, #2681): the reference author
  stays the author, a designated reviewer's fix remains their correction and an
  author; any other commit makes the designation wait, because an unknown
  family cannot prove independence. #2721 names that fallback in the mark
  (`proof: turn-journal`, `fallback_commits`), never as an exact receipt;
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

A **light** lot has no pre-merge reviewer: canon §2 gives it tests and CI before
merge, and the other-family review after merge. The freeze journals that
outcome once (`review-designation-none:<lot>:<freeze>:`) so the absence of a
reviewer is explained, never mistaken for a failed designation (#2561).

Outside the hook, the same inputs feed the recovery path used by
`ameesh work reviewers <lot> --apply`: it replays the latest freeze, so a
capacity or canon problem fixed later can still designate the same head. The
runner also retries the journaled unavailable freezes after a successful canon
sync (`retry_pending`, bounded and idempotent), so the command is optional and
an older pilot installation is no obstacle.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import dataclasses
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from . import canon as canon_mod
from . import db as db_mod
from . import lot_class, mechanics, plan_git, projects, registry, review
from . import review_designation as rd
from . import review_designation_pg as pg
from . import review_authors, review_floor, review_target, storage, work

#: the sender of the journal notes and automatic mails
SENDER = pg.SENDER
#: journal prefix of a policy/evidence problem: one note per freeze
UNAVAILABLE_PREFIX = "review-designation-unavailable:"
#: journal prefix of a light/no-review outcome: one note per freeze (#2561)
NONE_PREFIX = "review-designation-none:"
#: journal prefix of an unreadable mark ignored by the reviewer load (#3047):
#: the line names the offending lot and the id of the note it ignored
UNREADABLE_PREFIX = "review-designation-unreadable:"
#: the first release that carries `ameesh work reviewers` (#2447)
RECOVERY_FROM = "1.12.0"
#: why a light lot keeps no pre-merge reviewer (canon §2, full table)
LIGHT_WHY = ("light lot: no pre-merge reviewer is due (the other-family review"
             " happens after merge)")
#: why an empty frozen diff keeps no reviewer
NO_PATH_WHY = "no changed path: no review due"
#: a frozen range longer than this is not read in one selection (bounded work)
MAX_COMMITS = 500
#: #2681 : le seuil et la lecture des auteurs sont celles de la porte et du
#: train (`review_authors`), pas une seconde règle locale.
REVIEWER_FIX_LINES = review_authors.REVIEWER_FIX_LINES
DESIGN_PREFIXES = review_authors.DESIGN_PREFIXES

#: a run of `retry_pending` reads at most this many unavailable notes
RETRY_SCAN = 200
#: and retries at most this many lots
RETRY_LIMIT = 20
#: the retry queue's cursor, under the host state directory (#2561): the last
#: unavailable note examined. A pass resumes after it and wraps at the end, so
#: the same recent lots cannot take every pass's slots and starve the older
#: ones (a resolved lot is skipped, not counted against the pass's budget).
RETRY_CURSOR = "review-designation-retry.cursor"
#: a missing frozen head is fetched from the lot's origin at most this often
FETCH_TRIES = 2
#: and each fetch is bounded (network is not allowed to hold the freeze)
FETCH_TIMEOUT = 10.0

_RETRY_NOTE = re.compile(r"^review-designation-unavailable:(\d+):(\d+):")
_SHA_TEXT = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


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
    #: designated reviewers' small first-round corrections (#2739): they are
    #: authors of the head (#2888), so their family is excluded; the list keeps
    #: their exact names for the personal exclusion and the trace
    correctors: list
    candidates: list
    notify: str
    manual: list = field(default_factory=list)
    #: the manual reviewers that prove an independent family (#2627)
    manual_seats: list = field(default_factory=list)
    #: the raw `-none` waiver of the lot (`work reviewer --no-verdict`)
    waiver: bool = False
    #: {family: advance in points over its plan pace} (#2668); empty when no
    #: gauge reading is current, which leaves the load to decide
    advances: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# the canon policy, at the audited commit
# --------------------------------------------------------------------------

def _audited_commit(db, cfg, canon, source) -> str:
    """The exact commit to read for this canon: the host's last good one.

    The single implementation lives in `canon_sync.audited_commit` (moved for
    #2664, which reads the same commit for the model families): a recorded
    state is authoritative even when the current pass is invalid, and with no
    state at all the exact source commit is the only one known.
    """
    from . import canon_sync

    return canon_sync.audited_commit(db, cfg, canon, source)


class NoCanon(RuntimeError):
    """No configured canon can be read: the policy sources are unknown."""


def policy_sources(cfg, db, project: str, errors: list):
    """The configured canons whose compiled policy for `project` loads.

    Yields `(source, commit, loaded)` in the designation's order: the
    configured canons, the default one first, each Git source at its audited
    commit. A refused source is named in `errors` and skipped. Raises
    `NoCanon` (with the waiting reason) when no canon can be read at all.
    The train's canon watch (#2914) reads the same sources, so the commit it
    compares with its pin is the one the designation reads.
    """
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        raise NoCanon("no canon configured: no automatic designation")
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001 - no readable canon: explicit wait
        raise NoCanon("cannot read the configured canon: %s: %s"
                      % (type(exc).__name__, str(exc)[:200])) from exc
    if not canons:
        raise NoCanon("no readable canon configured: no review policy")
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
            try:
                commit = _audited_commit(db, cfg, canon, source)
            except Exception as exc:  # noqa: BLE001 - état illisible : refus, jamais de repli
                errors.append("%s: cannot read the audited state: %s"
                              % (source.repo, str(exc)[:240]))
                continue
            if not commit:
                errors.append("%s: no exact audited commit" % (source.repo,))
                continue
            try:
                loaded = mechanics.load(source.repo, commit, project)
            except (mechanics.MechanicsError, canon_mod.CanonError) as exc:
                errors.append("%s@%s: %s" % (source.repo, commit[:12],
                                             str(exc)[:240]))
                continue
            yield source, commit, loaded


def _policy(cfg, db, project: str, paths: Sequence[str]) -> Policy | Unavailable:
    """The first configured canon that declares a usable policy for `project`."""
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        return Unavailable("no canon configured: no automatic designation", silent=True)
    errors = []
    try:
        for source, commit, loaded in policy_sources(cfg, db, project, errors):
            try:
                need = loaded.reviews_for(paths)
                # #2627 : le plancher critique de la porte de fusion
                # (`merge_queue.required_verdicts`) fait partie du besoin. Un
                # canon qui déclare un avis sur `exploitation.py` ou `db.py`
                # n'abaisse pas les deux avis que la porte exigera au feu.
                from . import merge_queue
                classified = review.classify(loaded.review_policy, list(paths))
                required = max(need.reviews, merge_queue.required_verdicts(classified))
                # même classe que le lecteur compilé : le max des deux lectures
                risk = max((need.risk, str(classified.get("class") or "light")),
                           key=review.RANK.__getitem__)
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
                          risk=risk, required=required,
                          ref=str(getattr(loaded, "source", "") or
                                  "%s@%s" % (source.repo, commit)),
                          commit=commit, source=source.repo)
    except NoCanon as exc:
        return Unavailable(str(exc))
    why = "; ".join(errors[:4]) or "no canon declares a review policy for %r" % (project,)
    return Unavailable("review policy unavailable at the audited canon commit: %s" % why)


# --------------------------------------------------------------------------
# the repository, the target and the frozen diff
# --------------------------------------------------------------------------

def _candidate_paths(db, cfg, item: Mapping) -> list[str]:
    """Local paths the agent may have frozen from, in reading order."""
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
    return candidates


def _tracked_worktrees(db, cfg, item: Mapping) -> list[str]:
    """The worktrees housekeeping already attributed to this lot on this host.

    #2561: `managed_worktrees` is a machine source, cheaper and more exact than
    scanning a directory; its absence (old schema, no pass yet) is not an error.
    """
    try:
        lot = int((item or {}).get("id") or 0)
    except (TypeError, ValueError):
        return []
    if not lot:
        return []
    try:
        rows = db.query(
            "SELECT path FROM managed_worktrees"
            " WHERE host = %s AND lot = %s AND status = 'active'"
            " ORDER BY id DESC",
            (str(getattr(cfg, "host", "") or ""), str(lot)))
    except Exception:  # noqa: BLE001 - no tracking table/readable row: next hint
        return []
    return [str(row.get("path") or "") for row in rows or ()
            if str(row.get("path") or "").strip()]


def _search_roots(paths: Sequence[str]) -> list[str]:
    """The parent directories of candidate checkouts, de-duplicated.

    A worktree created from another checkout lives beside it (the agent's
    `~/work/<project>-<agent>-<lot>` convention). When the freeze command runs
    from the agent's other repository, that sibling is the only local copy of
    the frozen head.
    """
    roots: list[str] = []
    for path in paths:
        text = os.path.expanduser(str(path or "").strip())
        if not text:
            continue
        real = os.path.realpath(text)
        if not os.path.isdir(real):
            continue
        root = os.path.dirname(real)
        if not root or root == real or root in roots:
            continue
        if root == os.path.dirname(root):  # never scan the filesystem root
            continue
        roots.append(root)
    return roots


def _neighbour_paths(roots: Sequence[str], needles: Sequence[str]) -> list[str]:
    """Immediate children of `roots` whose name carries one of `needles`."""
    out: list[str] = []
    seen = set()
    wanted = [str(needle) for needle in needles if str(needle or "").strip()]
    if not wanted:
        return out
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            if not any(needle in entry.name for needle in wanted):
                continue
            real = os.path.realpath(entry.path)
            if real in seen:
                continue
            seen.add(real)
            out.append(real)
    return out


def _configured_origin(repo: str) -> str:
    """The configured GitHub `owner/repo` of `repo`, without URL rewrites.

    `git remote get-url` applies `url.<base>.insteadOf`, which can hide the
    forge identity; the configuration value is what the issue reference names.
    """
    try:
        from . import migrations
    except Exception:  # noqa: BLE001 - no parser: no identity
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return migrations.github_repo(proc.stdout.strip()) or ""


def _origin_matches(repo: str, slug: str) -> bool:
    """A discovered neighbour is trusted only for the lot's own repository.

    The exact SHA makes content forgery impossible, not a forged *target*: the
    base refs are read from this checkout, so a sibling of another project (or a
    local clone with no GitHub origin) must never be chosen. The issue reference
    is the machine identity; without one, the fallback keeps the old hints only.
    """
    return bool(slug) and _configured_origin(repo) == slug


def _valid_branch(branch: str) -> str:
    """A branch name that is safe as a fetch refspec, or ""."""
    branch = str(branch or "").strip()
    if not branch or branch.startswith("-") or ".." in branch \
            or any(ch.isspace() for ch in branch):
        return ""
    try:
        proc = subprocess.run(["git", "check-ref-format", "refs/heads/" + branch],
                              capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return branch if proc.returncode == 0 else ""


def _fetch_frozen(repo: str, frozen_sha: str, branch: str) -> bool:
    """Fetch the exact frozen head into a local clone of the lot's repository.

    #2561: a freeze declared after a push can name a head that no *local*
    checkout holds yet. The fetch is bounded, never shallow (canon §7), only
    writes a private `refs/ameesh/frozen/<sha>` ref, and the exact commit id is
    checked afterwards. The shallow option is never used.
    """
    refspecs: list[str] = []
    if _SHA_TEXT.fullmatch(str(frozen_sha or "")):
        refspecs.append(str(frozen_sha))
    branch = _valid_branch(branch)
    if branch and _SHA_TEXT.fullmatch(str(frozen_sha or "")):
        refspecs.append("+refs/heads/%s:refs/ameesh/frozen/%s" % (branch, frozen_sha))
    for refspec in refspecs:
        try:
            proc = subprocess.run(
                ["git", "-C", repo, "fetch", "--no-tags", "--quiet", "origin", refspec],
                capture_output=True, timeout=FETCH_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        try:
            if plan_git._commit(repo, frozen_sha):
                return True
        except Exception:  # noqa: BLE001 - unreadable clone: next refspec
            continue
    return False


def _fetch_from_local_clone(paths: Sequence[str], slug: str, frozen_sha: str,
                            branch: str, seen: set) -> str:
    """The first local clone of `slug` into which the frozen head can be fetched."""
    if not slug:
        return ""
    tries = 0
    for path in paths:
        path = os.path.expanduser(str(path or "").strip())
        if not path or not os.path.isdir(path):
            continue
        repo = canon_mod.git_toplevel(path)
        if not repo or repo in seen or not _origin_matches(repo, slug):
            continue
        seen.add(repo)
        tries += 1
        if _fetch_frozen(repo, frozen_sha, branch):
            return repo
        if tries >= FETCH_TRIES:
            break
    return ""


def _repo_holding(path: str, frozen_sha: str, seen: set) -> str:
    """The git top level of `path` when it holds the exact frozen commit."""
    path = os.path.expanduser(str(path or "").strip())
    if not path or not os.path.isdir(path):
        return ""
    repo = canon_mod.git_toplevel(path)
    if not repo or repo in seen:
        return ""
    seen.add(repo)
    try:
        return repo if plan_git._commit(repo, frozen_sha) else ""
    except Exception:  # noqa: BLE001 - unreadable repository: next hint
        return ""


def _repository(db, cfg, item: Mapping, frozen_sha: str, *,
                fetch: bool = True) -> str:
    """The repository holding the frozen head (assignee's worktree first).

    #2561: a freeze command often runs from the agent's *session* directory
    (`~/work/nexlink-<agent>`), not from the lot's worktree. The first three
    hints then all miss the ameesh commit even though the pushed head exists in
    the agent's other checkout. The fallback reads the worktrees attributed by
    housekeeping (a trusted machine source) and the siblings of the known paths
    whose name cites the lot or its assignee; the exact commit id decides, and a
    discovered sibling must also carry the lot's own GitHub origin.

    `fetch=False` (the freeze transaction) never reaches the network: the head
    found only on origin is journaled unavailable, then fetched by the retry
    that runs outside any transaction (`retry_pending`).

    #2786: `frozen_sha` must be a full commit id. A symbolic ref (`HEAD`, a
    branch) is never resolved in this host's clone: it would read a commit
    that nobody froze (#2712).
    """
    if not _SHA_TEXT.fullmatch(str(frozen_sha or "").strip().lower()):
        return ""
    candidates = _candidate_paths(db, cfg, item)
    seen: set[str] = set()
    for path in candidates:
        repo = _repo_holding(path, frozen_sha, seen)
        if repo:
            return repo
    try:
        slug = work.lot_repository(dict(item or {}), cfg)
    except Exception:  # noqa: BLE001 - no issue reference: no discovered clone
        slug = ""
    try:
        lot = str(int((item or {}).get("id") or 0))
    except (TypeError, ValueError):
        lot = ""
    hints = _tracked_worktrees(db, cfg, item)
    for path in hints:
        repo = _repo_holding(path, frozen_sha, seen)
        if repo:
            return repo
    if not slug:
        return ""
    needles = [needle for needle in
               (lot, str((item or {}).get("assignee") or "").strip())
               if needle and needle != "0"]
    roots = _search_roots(list(candidates) + list(hints))
    neighbours = _neighbour_paths(roots, needles)
    for path in neighbours:
        repo = _repo_holding(path, frozen_sha, seen)
        if repo and _origin_matches(repo, slug):
            return repo
    # The head may have been pushed but not fetched into any local clone
    # (#2517); fetch it from the lot's own origin, bounded, and verify the SHA.
    if not fetch:
        return ""
    return _fetch_from_local_clone(
        list(candidates) + list(hints) + list(neighbours), slug, frozen_sha,
        str((item or {}).get("branch") or "").strip(), set())


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


def _range(repo: str, base: str, head: str, *, bounded: bool = True) -> tuple[str, list, list]:
    """`(start, paths, commits)` of the lot's own frozen change."""
    base = _resolve_base(repo, base)
    if not base:
        raise ActivationError("no readable target branch to diff the freeze against")
    head = str(head or "").strip().lower()
    if not _SHA_TEXT.fullmatch(head):
        # #2786: never resolve a symbolic ref (`HEAD`, a branch) in this
        # clone: the frozen diff would be another commit's diff (#2712).
        raise ActivationError(
            "the frozen head %r is not a full commit id: the designation never "
            "resolves a ref in its own clone" % (head[:24],))
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
    if bounded and len(commits) > MAX_COMMITS:
        raise ActivationError("the frozen range carries %d commits (limit %d):"
                              " refusing an unbounded provenance read"
                              % (len(commits), MAX_COMMITS))
    return start, paths, commits


# --------------------------------------------------------------------------
# the authors: one reading shared with the train and the merge gate (#2681)
# --------------------------------------------------------------------------

def _designation_authority(db, actor: str) -> bool:
    """May `actor` have written a `reviewer X designated` journal line (#2739)?

    The line is only a text: any agent can copy it in a free note, so the text
    alone proves nothing. The write path (`work.designate_reviewer`, and the
    designation `work move qa --reviewer` makes in the same gesture) admits the
    system, a human, or an orchestrator/design agent **including the recent
    assigner history `corrector` uses**. The journal read applies the same
    authority, so an ordinary agent's note cannot make itself a designated
    reviewer (and excuse its own commit as a fix), while a designation the
    write path accepted is read back (#2776: with `history=False`, a
    `work move qa --reviewer` made by an ordinary agent was written and then
    ignored, and its retired seat was treated as an author).
    """
    actor = str(actor or "").strip()
    if not actor:
        return False
    if actor == work.SYSTEM_SENDER or actor.startswith(work.HUMAN_PREFIX):
        return True
    try:
        from . import assignments
        return assignments.is_orchestrator(
            getattr(db, "cfg", None), db, actor,
            roles=work.CORRECTOR_ROLES, history=True)
    except Exception:  # noqa: BLE001 - unreadable authority: no name counted
        return False


def _journal_reviewers(db, lot: int, *, until=None) -> list:
    """The reviewer names designated by hand in the lot journal (#2739).

    `work_items.reviewers` is the live column; `work reviewer --remove` retires
    a manual seat and the column no longer carries the name. The journal keeps
    the designation (`reviewer X designated`), so a manual reviewer who pushed
    a first-round correction stays recognizable after their seat is retired,
    exactly like the automatic names of the marks (#2638). Only a line written
    by a designation authority counts (`_designation_authority`): a free note
    carrying the same words is not a designation. The read is bounded by the
    note prefix and the lot, never by the last 500 events: a designation older
    than a talkative lot's recent journal stays read (#2776, same reason as
    #1448 N2). With `until` (the freeze instant), a reviewer named after the
    freeze is ignored: they did not review that head.
    """
    names: list[str] = []
    try:
        events = storage.of(db).work.events_noted(
            "reviewer ", 0.0, ids=[int(lot)]) or []
    except Exception:  # noqa: BLE001 - unreadable journal: no manual name
        return names
    for event in events:
        note = str((event or {}).get("note") or "").strip()
        if not note.startswith("reviewer ") or not note.endswith(" designated"):
            continue
        if not _designation_authority(db, (event or {}).get("actor")):
            continue
        if until is not None:
            try:
                if float((event or {}).get("created_ts") or 0.0) > float(until):
                    continue
            except (TypeError, ValueError):
                continue
        name = note[len("reviewer "):-len(" designated")].strip()
        if name and name not in names:
            names.append(name)
    return names


def _designated_reviewers(db, item: Mapping, milestone: Optional[Mapping],
                          author: str) -> set[str]:
    """The names designated as reviewers of the lot, automatic or manual.

    The current column holds the previous freeze's automatic names and the
    manual exception; the journal marks keep the names a later freeze retired.
    A reviewer who pushed a first-round correction must stay recognizable after
    the freeze that retired them (#2638) — including a **manual** reviewer
    whose seat was retired from the column (#2739), read from the journal. The
    lot author is never a reviewer.
    """
    names: list[str] = []
    lot = int((item or {}).get("id") or 0)
    freeze_id = int((milestone or {}).get("id") or 0)
    try:
        names.extend(_previous_reviewers(db, lot, freeze_id))
    except Exception:  # noqa: BLE001 - unreadable journal: no automatic name
        pass
    try:
        names.extend(_journal_reviewers(db, lot,
                                        until=(milestone or {}).get("at_ts")))
    except Exception:  # noqa: BLE001 - unreadable journal: no manual name
        pass
    try:
        from .work_reviewers import designated

        names.extend(designated(dict(item or {})))
    except Exception:  # noqa: BLE001 - unreadable column: no manual name
        pass
    return {str(value).strip() for value in names
            if str(value).strip() and str(value).strip() != author}


def attribution_of_head(db, item: Mapping, milestone: Optional[Mapping],
                        repo: str, base: str, head: str, *, families=(),
                        commits=None, strict: bool = False,
                        families_out: Optional[dict] = None) -> tuple[list, list, list]:
    """`(auteurs, inconnus, correcteurs)` de la plage gelée `base..head` (#2681/#2739).

    La même lecture partagée que la désignation (`review_authors.reading`),
    offerte à la porte de fusion : quand l'appelant ne passe pas les auteurs,
    elle les lit elle-même sur la plage exacte et exclut tous les auteurs
    reconnus, pas seulement l'assigné (#2726). `milestone` est le gel du lot
    quand la tête est celle-là ; il laisse `_designated_reviewers` reconnaître
    un correcteur retiré par une désignation ultérieure. Ne lève pas sur un
    inconnu : l'appelant décide (la porte refuse fermé, le train lève).

    #2961 : `strict=True` (la porte) fait d'un reçu de provenance contradictoire
    une erreur au lieu d'une absence — la famille de la référence ne se
    remplace alors pas au registre courant. `families_out` reçoit les familles
    prouvées par acteur : une famille vide est une absence **ou** une
    ambiguïté, et la porte distingue les deux.

    #2803 : `commits` remplace la plage `base..head` quand l'appelant l'a
    déjà calculée — le delta propre d'un véhicule de paquet, seul contenu que
    sa porte relit (`packet_reviews.need`). La lecture des auteurs et des
    correcteurs porte alors exactement sur ce delta, jamais sur les commits
    des membres déjà relus sur leur propre tête.
    """
    if commits is None:
        _start, _paths, commits = _range(repo, base, head)
    return review_authors.reading(db, item, milestone, commits, repo,
                                  families=families, strict=strict,
                                  families_out=families_out,
                                  base=base, head=head)


def corrections_of_head(db, item: Mapping, milestone: Optional[Mapping],
                        repo: str, base: str, head: str, *, families=()) -> list:
    """The designated reviewers' small first-round corrections of `base..head`.

    Read from the exact frozen commits and their provenance receipts, never
    from a designation mark: a head re-integrated under a new SHA is read as it
    stands (#2739). A corrector is an author of this head (#2888): the caller
    excludes their family like any author's, and this list keeps their exact
    names for the personal exclusion. `milestone` is the lot's freeze when it
    is this head; it lets `_designated_reviewers` recognise a corrector retired
    by a later designation. The caller (the merge gate) handles an unreadable
    range as it must; this reader invents no name.
    """
    _authors, unknown, correctors = attribution_of_head(
        db, item, milestone, repo, base, head, families=families)
    if unknown:
        raise ActivationError("author provenance unknown for frozen commit(s): %s"
                              % ", ".join(unknown))
    return correctors


# --------------------------------------------------------------------------
# the candidates: real session family, availability, open-lot load
# --------------------------------------------------------------------------

def _mark_reviewers(note: str) -> Optional[list]:
    """The reviewer names a designation mark carries, or None.

    The JSON is parsed **in Python**, before any cast: a hand-written note with
    a valid mark prefix and invalid JSON is an unreadable mark, not a database
    error that aborts the freeze transaction (#3047). `designation_of` refuses
    an unreadable selection mark and `carried_mark_of` returns None; both are
    the same refusal here, and the caller journals it.
    """
    from . import verdict_carry

    text = str(note or "")
    if text.startswith(pg.MARK_PREFIX):
        try:
            payload = pg.designation_of(text)
        except rd.DesignationError:
            return None
    elif text.startswith(verdict_carry.CARRIED_MARK_PREFIX):
        payload = verdict_carry.carried_mark_of(text)
    else:
        return []
    if not isinstance(payload, dict):
        return None
    reviewers = payload.get("reviewers")
    if reviewers is None:
        return []
    if not isinstance(reviewers, (list, tuple)):
        return None
    names = []
    for row in reviewers:
        if not isinstance(row, dict):
            return None
        names.append(str(row.get("name") or "").strip())
    return names


#: #3033 / #3047: the journal facts of every open lot that the pending-review
#: and fix calculation (`_owed_read`) needs, in **one** statement: the latest
#: freeze, its latest mark, every designation mark of the lot, the manual
#: designations since that freeze and the verdicts (actor, head, verdict and
#: instant — #3288 reads the blocks from the same rows). Every note comes back
#: as **text** and is parsed in Python: no note is ever cast by PostgreSQL, so
#: no bad note can abort the freeze transaction this read runs in.
_PENDING_FACTS_SQL = """
    WITH open_items AS (
        SELECT id, assignee, reviewers, title, state, app
          FROM work_items
         WHERE state NOT IN ('merged', 'promoted', 'closed')
    ), latest_freeze AS (
        SELECT DISTINCT ON (m.work_item_id)
               m.work_item_id AS lot, m.id AS freeze_id, m.sha AS sha, m.at AS at
          FROM work_item_milestones m
          JOIN open_items o ON o.id = m.work_item_id
         WHERE m.kind = 'frozen'
         ORDER BY m.work_item_id, m.at DESC, m.id DESC
    ), marks AS (
        SELECT e.work_item_id AS lot,
               array_agg(e.id ORDER BY e.id) AS ids,
               array_agg(e.note ORDER BY e.id) AS notes
          FROM work_item_events e
          JOIN latest_freeze f ON f.lot = e.work_item_id
         WHERE starts_with(e.note, 'review-designation:' || e.work_item_id || ':')
            OR starts_with(e.note, 'review-designation-carried:'
                                   || e.work_item_id || ':')
         GROUP BY e.work_item_id
    ), manual AS (
        SELECT e.work_item_id AS lot,
               array_agg(e.note ORDER BY e.id) AS notes,
               array_agg(extract(epoch FROM e.created_at)::float8
                         ORDER BY e.id) AS ts
          FROM work_item_events e
          JOIN latest_freeze f ON f.lot = e.work_item_id
         WHERE e.created_at >= f.at
           AND starts_with(e.note, 'reviewer ')
           AND right(e.note, length(' designated')) = ' designated'
         GROUP BY e.work_item_id
    ), verdicts AS (
        SELECT v.work_item_id AS lot,
               array_agg(v.actor ORDER BY v.id) AS actors,
               array_agg(v.sha ORDER BY v.id) AS shas,
               array_agg(v.verdict ORDER BY v.id) AS kinds,
               array_agg(extract(epoch FROM v.at)::float8 ORDER BY v.id) AS ts
          FROM work_item_milestones v
          JOIN latest_freeze f ON f.lot = v.work_item_id
         WHERE v.kind = 'verdict'
         GROUP BY v.work_item_id
    )
    SELECT o.id AS lot, o.assignee, o.reviewers, o.title, o.state, o.app,
           f.sha AS freeze_sha, extract(epoch FROM f.at)::float8 AS freeze_ts,
           mark.id AS note_id, extract(epoch FROM mark.created_at)::float8 AS mark_ts,
           mk.ids AS mark_ids, mk.notes AS mark_notes,
           mn.notes AS manual_notes, mn.ts AS manual_ts,
           vd.actors AS verdict_actors, vd.shas AS verdict_shas,
           vd.kinds AS verdict_kinds, vd.ts AS verdict_ts
      FROM open_items o
      LEFT JOIN latest_freeze f ON f.lot = o.id
      LEFT JOIN LATERAL (
          SELECT e.id, e.created_at
            FROM work_item_events e
           WHERE e.work_item_id = f.lot
             AND e.created_at >= f.at
             AND (starts_with(e.note,
                              'review-designation:' || f.lot
                              || ':' || f.freeze_id || ':')
                  OR starts_with(e.note,
                                 'review-designation-carried:' || f.lot
                                 || ':' || f.freeze_id || ':'))
           ORDER BY e.id DESC
           LIMIT 1
      ) mark ON true
      LEFT JOIN marks mk ON mk.lot = o.id
      LEFT JOIN manual mn ON mn.lot = o.id
      LEFT JOIN verdicts vd ON vd.lot = o.id
"""


def _owed_read(db, *, unreadable: Optional[list] = None) -> tuple[list, list, list]:
    """The open lots, the reviews still owed and the fixes due, in **one** read.

    It is the **only** calculation of a pending review (#3033) and of the fix
    that follows a blocking verdict (#3288): the selection load (`_loads`,
    #3004), the reading of an agent's queue (`pending_reviews`,
    `due_fixes`: resume brief, idle turn) and the §9 `green_verdict` /
    `blocked_freeze` relaunches all read it, so they cannot disagree. Returns
    `(lots, pending, fixes)`: `lots` is every open lot as `(lot, assignee)`;
    `pending` one row per (lot, reviewer) — `lot`, `name`, `sha` (the frozen
    head), `designated_ts`, `title`, `state`, `app` — oldest designation
    first; `fixes` one row per (lot, debtor) — `lot`, `name` (the assignee),
    `sha`, `blocked_ts` (earliest still-standing block), `blocked_by`,
    `title`, `state`, `app` — oldest block first.

    A review is pending when an open lot's **latest** freeze designates the
    agent and no verdict of theirs names that head. The designees are the
    gate's `reviewers` column (what the merge gate waits for), restricted to
    the current head:

    - a name of the **effective** mark of the latest freeze
      (`review-designation:` selection or `review-designation-carried:`
      report, the last one in journal order — the mark `_sync_reviewers`
      derives the column from);
    - a manual designation: a name that no automatic mark of the lot ever
      carried (kept across freezes by `_sync_reviewers`), or one designated by
      hand (`reviewer X designated`, `ameesh work reviewer`) since the latest
      freeze.

    An automatic name of an older freeze is therefore not a pending review of
    the new head — including during the selection itself, when the column has
    not been synced yet (#3004) — and a reviewer the pilot removed (#3004:
    dev1 removed, dev2 designated) is no longer one. Only a verdict naming the
    frozen head discharges it, full or abbreviated SHA, the `_same_freeze`
    rule; a `blocked` verdict counts, the reviewer has rendered it. A closed,
    merged or promoted lot owes nothing. `designated_ts` is when the agent was
    designated for this head (latest of the freeze, the mark naming them, a
    manual designation since the freeze): the start of the §9 clock.

    #3288: a fix is due when the latest freeze carries a `blocked` verdict
    that no later verdict of the **same actor** on the same head cancelled,
    and no newer freeze moved the head (a re-freeze of the same SHA is the
    same content: the block still stands). The debtor is the assignee, and
    `blocked_ts` is the **earliest** still-standing block — « blocking
    verdict → new freeze » starts at the first `blocked` of the actor's
    uninterrupted run, not at the last (#17656 B1: a second `blocked` from
    the same actor on the same head must not push the escalation back); an
    `ok` (any non-`blocked` verdict) between two blocks restarts the clock at
    the next one. With
    designated seats, only a designated reviewer's block counts and a later GO
    from another reviewer does not erase it (#2231); without a seat (empty
    column or `-none`), the head's **last** verdict decides, exactly as
    `progress._work_item_milestones` reads it. The block that discharges a
    review (`pending`) is the one that opens its fix clock.

    #3047: the marks are read as **text** and parsed by `_mark_reviewers`.
    An unreadable mark carries no name — the latest one designates nobody, an
    older one proves no automatic name — and is reported in `unreadable` as
    `(lot, note id)` for the caller to journal; a valid mark next to it is
    still read. The read raises when the base cannot be read: each caller
    decides (zero load, « unreadable » queue).
    """
    rows = db.query(_PENDING_FACTS_SQL)
    lots: list = []
    pending: list = []
    fixes: list = []
    for row in rows or ():
        try:
            lot_id = int(row["lot"])
        except (KeyError, TypeError, ValueError):
            continue
        assignee = str(row.get("assignee") or "").strip()
        lots.append((lot_id, assignee))
        frozen_sha = str(row.get("freeze_sha") or "").strip()
        if not frozen_sha:
            continue
        freeze_ts = row.get("freeze_ts")
        marks: dict = {}
        for note_id, note in zip(row.get("mark_ids") or (), row.get("mark_notes") or ()):
            names = _mark_reviewers(note)
            marks[int(note_id)] = names
            if names is None and unreadable is not None:
                unreadable.append((lot_id, int(note_id)))
        effective = (set(marks.get(int(row["note_id"])) or ())
                     if row.get("note_id") is not None else set())
        ever = {name for names in marks.values() for name in names or ()}
        # `reviewer X designated` since the freeze: X, at its latest line
        manual: dict = {}
        for note, ts in zip(row.get("manual_notes") or (), row.get("manual_ts") or ()):
            name = str(note or "")[len("reviewer "):-len(" designated")]
            manual[name] = max(float(ts), manual.get(name, float(ts)))
        verdicts = list(zip(row.get("verdict_actors") or (),
                            row.get("verdict_shas") or (),
                            row.get("verdict_kinds") or (),
                            row.get("verdict_ts") or ()))
        rendered = [(actor, sha) for actor, sha, _kind, _at in verdicts]
        seen: set = set()
        for raw in row.get("reviewers") or ():
            name = str(raw or "").strip()
            if name in ("", "-none") or name in seen:
                continue
            seen.add(name)
            if name not in effective and name in ever and name not in manual:
                continue
            if any(str(actor or "").strip().lower() == name.lower()
                   and rd._same_freeze(frozen_sha, sha)
                   for actor, sha in rendered):
                continue
            since = [float(ts) for ts in (
                freeze_ts,
                row.get("mark_ts") if name in effective else None,
                manual.get(name)) if ts is not None]
            pending.append({"lot": lot_id, "name": name, "sha": frozen_sha,
                            "designated_ts": max(since) if since else None,
                            "title": row.get("title") or "",
                            "state": row.get("state") or "",
                            "app": row.get("app") or ""})
        # #3288 : la correction due, calculée sur les mêmes verdicts.
        if not assignee:
            continue
        # #17656 (B1) : le début du blocage est le premier `blocked` d'une
        # série ininterrompue du même acteur sur la tête gelée ; un `ok`
        # entre deux blocages repart l'horloge au blocage suivant. Le dernier
        # verdict de chaque acteur tranche (un ok lève son blocage).
        streaks: dict = {}
        overall = None
        for actor, sha, kind, at in verdicts:
            actor = str(actor or "").strip()
            if not actor:
                continue
            at = float(at) if at is not None else None
            sha = str(sha or "").strip()
            applies = (rd._same_freeze(frozen_sha, sha) if sha
                       else (freeze_ts is not None and at is not None
                             and at >= float(freeze_ts)))
            if not applies:
                continue
            kind = str(kind or "").strip().lower()
            entry = streaks.setdefault(actor.lower(),
                                       {"actor": actor, "kinds": [], "at": []})
            entry["kinds"].append(kind)
            entry["at"].append(at)
            overall = (actor, kind, at)

        def _block_start(entry: dict) -> float | None:
            """Premier `blocked` de la série finale de cet acteur."""
            index = len(entry["kinds"]) - 1
            while index > 0 and entry["kinds"][index - 1] == "blocked":
                index -= 1
            return entry["at"][index]

        seats = {str(name or "").strip().lower()
                 for name in (row.get("reviewers") or ())
                 if str(name or "").strip() not in ("", "-none")}
        if seats:
            blockers = [(_entry["actor"], _block_start(_entry))
                        for key, _entry in streaks.items()
                        if key in seats and _entry["kinds"][-1] == "blocked"]
        else:
            blockers = ([(streaks[overall[0].lower()]["actor"],
                          _block_start(streaks[overall[0].lower()]))]
                        if overall is not None and overall[1] == "blocked"
                        else [])
        if blockers:
            first = min(blockers, key=lambda v: (v[1] is None, v[1] or 0.0, v[0]))
            fixes.append({"lot": lot_id, "name": assignee, "sha": frozen_sha,
                          "blocked_ts": first[1], "blocked_by": first[0],
                          "title": row.get("title") or "",
                          "state": row.get("state") or "",
                          "app": row.get("app") or ""})
    pending.sort(key=lambda r: (r["designated_ts"] is None, r["designated_ts"] or 0.0,
                                r["lot"], r["name"]))
    fixes.sort(key=lambda r: (r["blocked_ts"] is None, r["blocked_ts"] or 0.0,
                              r["lot"]))
    return lots, pending, fixes


def _pending_read(db, *, unreadable: Optional[list] = None) -> tuple[list, list]:
    """`(lots, pending)` of `_owed_read`, the reviews side (#3033)."""
    lots, pending, _fixes = _owed_read(db, unreadable=unreadable)
    return lots, pending


def _loads(db, *, unreadable: Optional[list] = None) -> dict:
    """Open lots per agent (the load of the selection), in **one** read.

    The charge of a reviewer is every open lot they carry: the lots assigned to
    them, plus the reviews they still owe (#3004) — `_pending_read`, the same
    calculation as their queue (`pending_reviews`, #3033). The second part is
    what was missing: a reviewer with an empty assignment queue but a full
    review queue looked free, so every freeze of their family picked them
    again. An unreadable read is zero, as before: never a guess and never a
    failed freeze. A lot where the agent is both assignee and designated
    reviewer counts once: `charges` is a set of (lot, agent).

    #3047: an unreadable mark charges nobody and is reported in `unreadable`
    as `(lot, note id)`; the caller journals it (`_note_unreadable`).
    """
    try:
        lots, pending = _pending_read(db, unreadable=unreadable)
    except Exception:  # noqa: BLE001 - unreadable load: zero, never a guess
        return {}
    charges = {(lot_id, assignee) for lot_id, assignee in lots if assignee}
    charges.update((row["lot"], row["name"]) for row in pending)
    out: dict = {}
    for _lot_id, name in charges:
        out[name] = out.get(name, 0) + 1
    return out


def pending_reviews(db, agent: Optional[str] = None) -> list:
    """The reviews still owed, oldest designation first (#3033), in one read.

    One row per (lot, reviewer): `lot`, `name`, `sha` (the frozen head),
    `designated_ts` (epoch of the designation for this head), `title`,
    `state`, `app`. `agent` restricts to one reviewer (names compared without
    case, like the verdict). The calculation is `_pending_read`, shared with
    the selection load (#3004); an unreadable mark designates nobody (#3047).
    The read raises when the base cannot be read: the caller says
    « unreadable », it never shows an empty queue.
    """
    _lots, pending = _pending_read(db)
    if agent is None:
        return pending
    wanted = str(agent).strip().lower()
    return [row for row in pending if row["name"].lower() == wanted]


def due_fixes(db, agent: Optional[str] = None) -> list:
    """The fixes still owed after a blocking verdict, oldest block first (#3288).

    One row per (lot, debtor): `lot`, `name` (the assignee who owes the fix),
    `sha` (the frozen head that was blocked), `blocked_ts` (epoch of the
    earliest still-standing block), `blocked_by` (its author), `title`,
    `state`, `app`. `agent` restricts to one debtor (names compared without
    case). The calculation is the `fixes` half of `_owed_read`, the same read
    as the pending reviews (#3033): a block discharges the reviewer's queue
    and opens the assignee's fix clock, one source for both. The read raises
    when the base cannot be read: the caller says « unreadable », it never
    shows an empty queue.
    """
    _lots, _pending, fixes = _owed_read(db)
    if agent is None:
        return fixes
    wanted = str(agent).strip().lower()
    return [row for row in fixes if row["name"].lower() == wanted]


def _note_unreadable(db, lot: int, state: str,
                     unreadable: Sequence[tuple]) -> None:
    """Journal unreadable designation marks once, without failing a freeze.

    The trace is written on the lot whose load observed the mark, the row the
    freeze transaction already holds: writing on the foreign lot would take a
    second lot's lock and two concurrent freezes could journal on each other.
    The line names the offending lot and the note id (#3047). The write is
    best effort, under a savepoint when a transaction is open: a failed trace
    must never abort the freeze it explains.
    """
    if not unreadable:
        return
    in_tx = db_mod.in_transaction(db)
    for bad_lot, note_id in unreadable:
        try:
            bad_lot, note_id = int(bad_lot), int(note_id)
        except (TypeError, ValueError):
            continue
        prefix = "%s%d:%d:" % (UNREADABLE_PREFIX, bad_lot, note_id)
        text = ("%s unreadable review designation mark ignored in the reviewer"
                " load (lot #%d, note #%s)" % (prefix, bad_lot, note_id))
        try:
            if storage.of(db).work.events_noted(prefix, 0.0, ids=[int(lot)]):
                continue
        except Exception:  # noqa: BLE001 - no duplicate proof: write the trace
            pass
        try:
            if in_tx:
                db.execute("SAVEPOINT review_designation_load_trace")
            try:
                storage.of(db).work.note(int(lot), state, text, SENDER)
            except Exception:  # noqa: BLE001 - a trace never blocks a freeze
                if in_tx:
                    db.execute("ROLLBACK TO SAVEPOINT review_designation_load_trace")
                    db.execute("RELEASE SAVEPOINT review_designation_load_trace")
                continue
            if in_tx:
                db.execute("RELEASE SAVEPOINT review_designation_load_trace")
        except Exception:  # noqa: BLE001 - best effort only
            continue


def _advances(db) -> dict:
    """The raw plan advance of each family, from the shared gauge history (#2668).

    The history (`quota_gauge_readings`) is read by every host of the mesh, so
    a DeepSeek host can compare the Claude and Codex plans even when it
    declares neither account. The reading is the raw pace: nominal multiplier
    and default ceiling, the same scale for every family; the declared
    `pace_multiplier`/`pace_ceiling` are deliberately not read (B of
    mesh-design on 4123b99). The gauge's own window and reset are the exact
    ones. Unreadable: an empty mapping, the selection keeps its load rule —
    never a failed freeze.
    """
    try:
        from . import review_pace, sous_utilisation

        rows = storage.of(db).operations.latest_gauges(
            since_s=sous_utilisation.PLAN_GAUGES_SINCE_S)
        return review_pace.family_advances(rows, now=time.time())
    except Exception:  # noqa: BLE001 - unreadable gauges: neutral, load decides
        return {}


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


def _candidates(listing: Sequence[Mapping], *, loads: Mapping, project: str, families,
                pilot: str) -> list:
    """The project's candidates, its pilot removed.

    The project's agents plus the agents of another team that declare it under
    `relit:` (#3185). The pilot receives the capacity alert; the review stays
    with the others, so the two roles never collapse into one mail — even when
    the pilot's card would make it a candidate.
    """
    candidates = rd.candidates_from_registry(listing, loads=loads, project=project,
                                             families=families)
    if pilot:
        candidates = [candidate for candidate in candidates if candidate.name != pilot]
    return candidates


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


def _waiver(item: Mapping) -> bool:
    """The raw `-none` waiver of the lot, never overridden by a designation.

    `work_reviewers.designated` filters `-none` out: the waiver is an authority
    gesture (#1979), so it is read on the raw column here. A new freeze of
    another head lapses it in storage before the hook (#2281); when it is still
    there, no reviewer is added and no mail is sent (#2627).
    """
    from .storage.postgres.work import REVIEWER_NONE

    item = item or {}
    if str(item.get("reviewer") or "").strip() == REVIEWER_NONE:
        return True
    names = item.get("reviewers")
    if names is None:
        names = [item.get("reviewer")]
    return any(str(name or "").strip() == REVIEWER_NONE for name in names or ())


def _manual_seats(manual: Sequence[str], listing: Sequence[Mapping],
                  authors: Sequence[Mapping], families, *,
                  correctors: Sequence[Mapping] = ()) -> list:
    """The manual reviewers that cover a required seat (#2627).

    A manual reviewer is always kept in the column; this only says which ones
    count: a known family of the compiled policy, distinct from every author's
    family and from the other manual reviewers. An unknown family, or the
    author's family, does not prove independence: the seat remains missing and
    is designated automatically. A corrector cannot count its own seat either
    (#2739/#2888): a corrector is an author of this head, so its family is
    forbidden like any author's and its name does not cover a seat. The list is
    ordered like the column.
    """
    forbidden_names, forbidden_families = rd.families_of(authors)
    _names, corrector_families = rd.families_of(correctors)
    forbidden_families = forbidden_families | corrector_families
    forbidden_names = forbidden_names | rd.names_of(correctors)
    rows = {str((row or {}).get("name") or ""): row for row in listing or ()}
    covered = set(forbidden_families)
    seats = []
    for name in manual or ():
        name = str(name or "").strip()
        row = rows.get(name) or {}
        family = rd.live_family(row, families=families)
        if (not name or name in forbidden_names or not family
                or family in covered):
            continue
        covered.add(family)
        seats.append({"name": name, "family": family})
    return seats


def _manual_covers(inputs: Inputs) -> bool:
    """Whether the manual reviewers alone prove every required opinion (#2627).

    A manual reviewer of an unknown family does not count, and an unknown
    author family or an unknown commit provenance means no independence can be
    proven at all: the designation then goes through the normal waiting path
    (and its capacity alert), never a manual no-op that would look sufficient.
    """
    if not inputs.manual:
        return False
    required = int(inputs.policy.required)
    if required == 0:
        return True
    if len(inputs.manual_seats) < required or inputs.unknown_authors:
        return False
    return not rd.unknown_families(inputs.authors)


# --------------------------------------------------------------------------
# the inputs of one freeze
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """The target certified outside the transaction, for one frozen head."""
    repo: str
    declared: str
    sha: str
    head: str


def prepare_target(cfg, db, item: Mapping, head: str, *,
                   fetch: bool = True) -> Target | Unavailable:
    repo = _repository(db, cfg, item, head, fetch=fetch)
    if not repo:
        return Unavailable("no repository holds the frozen head %s: cannot read the"
                           " frozen diff" % head[:12])
    base = lot_class.default_base(cfg, item, repo)
    if not base:
        return Unavailable("no target branch known for lot #%d" % int(item["id"]))
    try:
        sha = review_target.resolve(repo, base, fetch=fetch)
    except (review_target.TargetError, canon_mod.GitError) as exc:
        return Unavailable(str(exc))
    return Target(repo, base, sha, head)


def prepare(cfg, db, lot: int, *, freeze: Optional[Mapping] = None,
            fetch: bool = True, target=None) -> Inputs | Unavailable:
    """Reads the machine inputs of a freeze; an expected refusal is `Unavailable`.

    `fetch=False` keeps the read local (no `git fetch`), as the freeze
    transaction requires. A branch target then needs the `target` snapshot
    prepared outside the transaction; otherwise the read is unavailable."""
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
    # #2786: a freeze must name a full commit id. Never resolve `HEAD` or a
    # branch in this host's clone to compute the frozen diff: that is how the
    # automatic designation read "no changed path" on the #2712 freeze.
    if not _SHA_TEXT.fullmatch(frozen_sha.lower()):
        return Unavailable(
            "freeze #%s names %r, not a full commit id: the designation never "
            "resolves a ref in its own clone — freeze again with the full head "
            "(`ameesh work milestone %d frozen --sha <commit>`)"
            % (freeze.get("id"), frozen_sha, int(lot)))
    if target is None:
        target = prepare_target(cfg, db, item, frozen_sha, fetch=fetch)
    if isinstance(target, Unavailable):
        return target
    if (target.head != frozen_sha
            or target.declared != lot_class.default_base(cfg, item, target.repo)):
        return Unavailable("the target changed since refresh: retry outside the freeze transaction")
    repo, base = target.repo, target.sha
    from . import packet_reviews

    try:
        start, paths, commits = _range(repo, base, frozen_sha,
                                       bounded=not packet_reviews.is_vehicle(item))
    except ActivationError as exc:
        return Unavailable(str(exc))
    if packet_reviews.is_vehicle(item):
        # #2766 : un véhicule ne désigne que pour son delta propre ; les
        # membres portent leurs avis sur leur propre tête gelée.
        try:
            own = packet_reviews.designation_changes(db, int(lot), item, repo, start,
                                                     frozen_sha)
        except packet_reviews.VehicleError as exc:
            return Unavailable("vehicle review unreadable: %s" % exc)
        if own is not None:
            paths, commits = own["paths"], own["commits"]
    project = str(item.get("app") or "").strip()
    if not project:
        return Unavailable("lot #%d declares no project: the review policy is unknown"
                           % int(lot))
    manual = _manual(db, item)
    waiver = _waiver(item)
    if not paths:
        # no changed file: no review is due, and no policy needs to be read
        return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=[],
                      commits=commits,
                      policy=Policy(rules=(), families=(), risk="light", required=0,
                                    ref="", commit="", source=""),
                      authors=[], unknown_authors=[], correctors=[], candidates=[],
                      notify="", manual=manual, waiver=waiver)
    policy = _policy(cfg, db, project, paths)
    if isinstance(policy, Unavailable):
        return policy
    # #3183 : le plancher posé par un pilote (`review_floor`) relève le compte
    # de la désignation comme celui de la porte ; un journal illisible attend.
    floor = review_floor.apply({}, db, int(lot), cfg=cfg)
    if floor.get("reviews_floor_unreadable"):
        return Unavailable("review floor unreadable: %s" % floor["reviews_floor_unreadable"])
    if int(floor.get("reviews_floor") or 0) > policy.required:
        policy = dataclasses.replace(policy, required=int(floor["reviews_floor"]))
    listing = registry.overview(db)
    notify = _notify(cfg, db, project, listing)
    families = policy.families
    unreadable: list = []
    candidates = _candidates(listing, loads=_loads(db, unreadable=unreadable),
                             project=project, families=families, pilot=notify)
    if unreadable:
        _note_unreadable(db, int(lot), str(item.get("state") or ""), unreadable)
    authors, unknown, correctors = review_authors.reading(
        db, item, freeze, commits, repo, families=policy.families, trace=True,
        base=start, head=frozen_sha)
    return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=paths,
                  commits=commits, policy=policy, authors=authors,
                  unknown_authors=unknown, correctors=correctors,
                  candidates=candidates, notify=notify,
                  manual=manual, waiver=waiver,
                  manual_seats=_manual_seats(manual, listing, authors, families,
                                             correctors=correctors),
                  advances=_advances(db))


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
        "Nothing is admitted implicitly. If this lot is light, no pre-merge reviewer",
        "is due (the other-family review happens after merge, canon §2). For a",
        "normal or sensitive lot, fix the canon policy or the evidence, then retry",
        "this same freeze:",
        "  ameesh work reviewers %d --apply    (ameesh >= %s)" % (lot, RECOVERY_FROM),
        "The manual path remains available and journaled on every installed version:",
        "  ameesh work reviewer %d <agent>." % lot,
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



def _note_none(db, item: Mapping, freeze: Mapping, why: str) -> None:
    """Journal a light/no-review outcome once per freeze (#2561).

    A light lot has no pre-merge reviewer (canon §2: the other family reviews
    after merge). Without a mark the freeze kept no trace, and a pilot reading
    "no reviewer" could take it for a failed designation and designate by hand.
    The note is the explicit, machine-written explanation; no mail is sent
    because nothing is expected from anyone.
    """
    lot = int((item or {}).get("id") or 0)
    freeze_id = int(freeze.get("id") or 0)
    since = float(freeze.get("at_ts") or 0.0)
    prefix = "%s%d:%d:" % (NONE_PREFIX, lot, freeze_id)
    if storage.of(db).work.events_noted(prefix, since, ids=[lot]):
        return
    storage.of(db).work.note(
        lot, str((item or {}).get("state") or ""),
        "%s automatic reviewer designation: none — %s" % (prefix, why), SENDER)


def _none_outcome(db, lot: int, freeze: Mapping) -> Optional[dict]:
    """The journaled light/no-review outcome of this freeze, or None."""
    freeze_id = int((freeze or {}).get("id") or 0)
    since = float((freeze or {}).get("at_ts") or 0.0)
    prefix = "%s%d:%d:" % (NONE_PREFIX, int(lot), freeze_id)
    try:
        rows = storage.of(db).work.events_noted(prefix, since, ids=[int(lot)])
    except Exception:  # noqa: BLE001 - unreadable journal: no recorded outcome
        return None
    if not rows:
        return None
    text = str((rows[-1] or {}).get("note") or "")
    why = text.split("none — ", 1)[1] if "none — " in text else LIGHT_WHY
    return {"state": rd.NONE, "required": 0, "reviewers": [], "why": why,
            "freeze": dict(freeze or {})}


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
    """The latest `frozen` milestone of the lot, or None."""
    row = plan_git.latest_freeze(
        storage.of(db).work.milestones(int(lot), pg.MILESTONES))
    if row is None:
        return None
    return {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip(),
            "at_ts": row.get("at_ts"), "actor": row.get("actor") or ""}


def frozen_authors(db, item: Mapping) -> Optional[list]:
    """Les auteurs de la tête gelée, tels que la désignation de son gel les a lus.

    #2864 : la proposition de relecture (#1677) doit écarter les familles de
    **tous** les auteurs de la plage gelée, comme la désignation au gel
    (#1617) et la porte (#2726), et pas seulement celle de l'assigné au gel.
    La marque `review-designation` du dernier gel garde la lecture exacte de
    `review_authors.reading` (noms, familles, preuves) : c'est cette lecture
    qui fait foi ici, sans relire la plage ni deviner une famille.

    La lecture n'est complète que si la marque nomme aussi les commits qu'elle
    n'a pas pu attribuer (`unknown_authors`, #2864) : des auteurs connus ne
    prouvent pas qu'aucun autre commit ne l'est pas. Une marque sans cette
    liste (antérieure au champ), ou qui la porte non vide, ne prouve donc rien.

    Rend la liste `{name, family}`, ou None quand aucune lecture **complète**
    n'est disponible : pas de gel, pas de marque pour ce gel, journal
    illisible, marque antérieure à #2721 (sans `authors`), liste
    `unknown_authors` absente ou non vide, ou liste d'auteurs vide. L'appelant
    échoue alors fermé — aucune proposition de relecture.
    """
    lot = int((item or {}).get("id") or 0)
    if not lot:
        return None
    freeze = _latest_freeze(db, lot)
    if freeze is None:
        return None
    try:
        marks = _marks(db, lot, freeze_id=int(freeze["id"]),
                       since_ts=float(freeze.get("at_ts") or 0.0))
    except Exception:  # noqa: BLE001 - journal illisible : lecture inconnue
        return None
    for _row_id, kind, noted, _ts in reversed(marks or ()):
        if kind != "designation":
            continue
        rows = noted.get("authors")
        unknown = noted.get("unknown_authors")
        # #2864 : la complétude de la provenance se prouve ; une marque d'avant
        # le champ ne la prouve pas, et un inconnu explicite l'interdit.
        if not isinstance(rows, list) or not isinstance(unknown, list):
            return None
        if [value for value in unknown if str(value).strip()]:
            return None
        authors = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("name") or "").strip()
            if name:
                authors.append({"name": name,
                                "family": str(row.get("family") or "").strip().lower()})
        return authors or None
    return None


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
        families=inputs.policy.families,
        already=inputs.manual_seats, fixers=inputs.correctors,
        required=inputs.policy.required, advances=inputs.advances)


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


def _manual_outcome(db, item: Mapping, freeze: Mapping, manual: Sequence[str], *,
                    waiver: bool = False, unknown: str = "",
                    note: bool = True) -> dict:
    """The explicit manual no-op: keep the manual reviewers, add nobody (#2627).

    The note says why no automatic reviewer was added. The column is synced
    with an empty selection so a previous freeze's automatic names are retired
    while the manual names survive (`_sync_reviewers` never removes a name that
    no mark carries) and a `-none` waiver is never overridden.

    `unknown` (#2637): the selection could not compute the coverage (prepare
    failed, or the policy is unavailable). The note no longer claims the manual
    reviewers cover the required reviews; it keeps them and says the coverage
    is unknown, with the reason. `note=False` lets the caller write the single
    unavailable note and its alert instead of duplicating it.
    """
    lot = int((item or {}).get("id") or 0)
    if waiver:
        text = ("automatic reviewer designation skipped at freeze %s: manual"
                " waiver (-none) is not overridden" % _freeze_label(freeze))
        why = "manual waiver (-none)"
    elif unknown:
        text = ("automatic reviewer designation skipped at freeze %s: manual"
                " reviewer(s) kept (%s); their coverage of the required"
                " review(s) is unknown because %s"
                % (_freeze_label(freeze), ", ".join(manual), unknown))
        why = "manual reviewer designation (coverage unknown)"
    else:
        text = ("automatic reviewer designation skipped at freeze %s: manual"
                " reviewer(s) already cover the required review(s) (%s); the"
                " manual designation remains the journaled exception"
                % (_freeze_label(freeze), ", ".join(manual)))
        why = "manual reviewer designation"
    if note:
        storage.of(db).work.note(lot, str((item or {}).get("state") or ""), text, SENDER)
    _sync_reviewers(db, item, freeze, [])
    return {"state": "manual", "reviewers": [], "freeze": dict(freeze or {}),
            "why": why}


def _manual_unavailable(cfg, tx, item: Mapping, freeze: Mapping,
                        manual: Sequence[str], threads: list, why: str, *,
                        silent: bool = False) -> dict:
    """Keep the manual reviewers when the selection cannot compute coverage.

    #2637: with manual seats present, an unavailable policy or a `prepare`
    failure used to take `_manual_outcome`, which claimed the required reviews
    were covered and skipped `_note_unavailable` and its pilot alert. The manual
    seats are still kept and no automatic reviewer is added, but the waiting
    state and the alert are now explicit. A silent source (no canon configured)
    stays mail-free, and its manual note carries the unknown coverage reason.
    """
    if silent:
        return _manual_outcome(tx, item, freeze, manual, unknown=why)
    out = _manual_outcome(tx, item, freeze, manual, unknown=why, note=False)
    coverage = ("manual reviewer(s) kept (%s); their coverage of the required"
                " review(s) is unknown: %s" % (", ".join(manual), why))
    notify = _notify(cfg, tx, str((item or {}).get("app") or ""),
                     registry.overview(tx))
    _note_unavailable(tx, item, freeze, coverage, threads, notify)
    return out


def _designate_in_transaction(cfg, tx, lot: int, *, milestone: Optional[Mapping],
                              threads: list, carried: Optional[Mapping] = None,
                              reading: Optional[dict] = None, target=None) -> dict:
    """The `on_frozen` body: all writes stay in the freeze's transaction.

    `carried` (#2487) : le plan du report quand il couvre la tête gelée ; la
    désignation s'abstient alors (marque reportée, relecteurs gardés, aucun
    courrier). Il est calculé avant le gel par `carried_plan` — la transaction
    ne fait aucune lecture réseau — et absent pour tout gel non réaligné.

    The manual reviewers already designated are **kept** (#2627). The automatic
    path completes them: it counts the manual seats that prove an independent
    family, then designates only the missing ones with the same selection and
    the same mail. It stays an explicit, journaled no-op only when the manual
    reviewers already cover the required reviews, or when a `-none` waiver is
    in force. Every expected data problem is journaled and notified, never
    raised: a freeze is a fact of the lot's timeline and must not be lost to a
    designation input.

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
    if _waiver(item):
        return _manual_outcome(tx, item, freeze, manual, waiver=True)
    if carried:
        return _carried_in_transaction(tx, item, freeze, carried)
    try:
        # no network read inside the freeze transaction (see the docstring)
        inputs = prepare(cfg, tx, int(lot), freeze=freeze, fetch=False, target=target)
    except Exception as exc:  # noqa: BLE001 - recorded below, never guessed
        if manual:
            return _manual_unavailable(
                cfg, tx, item, freeze, manual, threads,
                "internal designation error: %s: %s"
                % (type(exc).__name__, str(exc)[:240]))
        return _internal_failure(cfg, tx, item, freeze, threads, exc)
    if isinstance(inputs, Unavailable):
        if manual:
            return _manual_unavailable(cfg, tx, item, freeze, manual, threads,
                                       inputs.why, silent=inputs.silent)
        if not inputs.silent:
            notify = _notify(cfg, tx, str(item.get("app") or ""),
                             registry.overview(tx))
            _note_unavailable(tx, item, freeze, inputs.why, threads, notify)
            _sync_reviewers(tx, item, freeze, [])
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": inputs.why}
    if reading is not None:
        # #2812 : la même lecture que la désignation, réutilisée par la
        # notification des blocages levés (aucune lecture git redoublée).
        reading["excluded"] = {
            str(row.get("name") or "").strip().lower()
            for row in list(inputs.authors or ()) + list(inputs.correctors or ())
            if str(row.get("name") or "").strip()}
    if _manual_covers(inputs):
        return _manual_outcome(tx, item, freeze, inputs.manual)
    checkpoint = len(threads)
    _savepoint(tx)
    try:
        if not inputs.paths or inputs.policy.required == 0:
            out = {"state": rd.NONE, "required": 0, "reviewers": [],
                   "freeze": freeze,
                   "why": LIGHT_WHY if inputs.paths else NO_PATH_WHY}
            _note_none(tx, item, freeze, out["why"])
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


#: journal prefix of the "your earlier block is superseded" notification
SUPERSEDED_PREFIX = "review-block-superseded:"


def _excluded_on_freeze(cfg, db, item: Mapping, freeze: Mapping) -> set[str]:
    """Les noms que la porte écarte sur cette tête gelée (#2812).

    Même lecture que la porte et la désignation (`review_authors.reading`) :
    les auteurs de la plage gelée, correcteurs de relecteur compris (ils sont
    auteurs de la tête, #2888). Le gel n'envoie donc pas l'invitation à un
    agent qui ne peut pas rendre un avis indépendant. Une lecture impossible
    rend l'ensemble vide : la notification garde son comportement historique et
    ne perd jamais un gel.
    """
    sha = str((freeze or {}).get("sha") or "").strip().lower()
    if cfg is None or db is None or not sha:
        return set()
    try:
        item = dict(item or {})
        repo = _repository(db, cfg, item, sha, fetch=False)
        if not repo:
            return set()
        base = lot_class.default_base(cfg, item, repo)
        if not base:
            return set()
        _start, _paths, commits = _range(repo, base, sha)
        authors, _unknown, correctors = review_authors.reading(
            db, item, dict(freeze or {}), commits, repo, families=(),
            base=base, head=sha)
    except Exception:  # noqa: BLE001 - plage illisible : comportement historique
        return set()
    return {str(row.get("name") or "").strip().lower()
            for row in list(authors or ()) + list(correctors or ())
            if str(row.get("name") or "").strip()}


def _notify_superseded_blocks(db, item: Mapping, freeze: Mapping, outcome: Mapping,
                              threads: list, *, cfg=None,
                              excluded: set[str] | None = None) -> None:
    """Notifie le relecteur dont le blocage portait sur une tête antérieure.

    #2681 : la porte ne lit que les verdicts de la tête gelée ; un blocage
    posé sur une tête antérieure ne bloque plus, et un `ok` indépendant sur la
    nouvelle tête suffit. Le relecteur qui a bloqué doit le savoir : un
    `notify` par (lot, gel, relecteur), jamais un doublon, et pas de courrier
    quand sa désignation du gel le prévient déjà. Une lecture impossible ne
    perd jamais un gel : la notification est au mieux.

    #2812 : on n'invite pas un agent que la porte écarte sur cette tête — un
    auteur de la plage gelée, y compris un relecteur dont la correction est un
    commit de la tête (#2888). `excluded` est la lecture déjà faite par la
    désignation ; à défaut (`None`), elle est refaite ici, et son échec laisse
    le courrier historique."""
    lot = int((item or {}).get("id") or 0)
    freeze_id = int((freeze or {}).get("id") or 0)
    sha = str((freeze or {}).get("sha") or "").strip().lower()
    state = str((outcome or {}).get("state") or "")
    if not lot or not freeze_id or not sha or state in ("unavailable", "stale"):
        return
    rows = storage.of(db).work.milestones(lot, None)
    latest: dict = {}
    for row in rows or ():
        if row.get("kind") != "verdict":
            continue
        actor = str(row.get("actor") or "").strip()
        if not actor:
            continue
        key = actor.lower()
        order = (float(row.get("at_ts") or 0.0), int(row.get("id") or 0))
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, row)
    designated = {str(row.get("name") or "").strip().lower()
                  for row in (outcome or {}).get("reviewers") or ()}
    designated.update(str(name or "").strip().lower()
                      for name in (outcome or {}).get("manual") or ())
    author = str((item or {}).get("assignee") or "").strip().lower()
    candidates = []
    for key, (_order, row) in sorted(latest.items()):
        if (row.get("verdict") or "") != "blocked" or key == author:
            continue
        mark_sha = str(row.get("sha") or "").strip().lower()
        current = bool(mark_sha) and (mark_sha == sha
                                      or (len(mark_sha) >= 7 and sha.startswith(mark_sha)))
        if current or key in designated:
            # un blocage sur la tête gelée reste en vigueur ; une désignation
            # du gel envoie déjà son propre courrier au relecteur
            continue
        candidates.append((key, row, mark_sha))
    if not candidates:
        return
    if excluded is None:
        excluded = _excluded_on_freeze(cfg, db, item, freeze)
    for key, row, mark_sha in candidates:
        if key in excluded:
            # #2812/#2888 : auteur de la tête ou correcteur de relecteur, son
            # avis ne compterait pas sur cette tête ; ne pas l'inviter.
            continue
        prefix = "%s%d:%d:%s:" % (SUPERSEDED_PREFIX, lot, freeze_id, key)
        if storage.of(db).work.events_noted(prefix, 0.0, ids=[lot]):
            continue
        actor = str(row.get("actor") or "").strip()
        body = "\n".join([
            "Blocage levé par une tête plus récente (canon §2, #2681).",
            "",
            "Lot: #%s" % lot,
            "- Votre dernier verdict sur une tête antérieure (%s) était BLOQUANT." % mark_sha[:12],
            "- La tête gelée courante est %s : la porte ne lit que les verdicts de" % sha[:12],
            "  cette tête. Un `ok` indépendant sur elle suffit ; votre blocage antérieur",
            "  ne la refuse pas.",
            "",
            "Si vous voulez re-relire cette tête :",
            "  ameesh work milestone %d verdict ok|blocked --sha %s --actor %s"
            % (lot, sha, actor),
        ])
        storage.of(db).work.note(
            lot, str((item or {}).get("state") or ""), prefix + sha,
            SENDER)
        from . import mail
        mail.send(db, SENDER, actor, body, work_item_id=str(lot),
                  payload={"review_block_superseded": {
                      "lot": lot, "freeze": freeze_id, "sha": sha,
                      "blocked_sha": mark_sha}},
                  after_commit=threads)


def designate_in_transaction(cfg, tx, lot: int, *, milestone: Optional[Mapping],
                             threads: list, carried: Optional[Mapping] = None,
                             target=None) -> dict:
    """The `on_frozen` body, plus the superseded-block notification (#2681).

    The notification is best-effort: it runs after the designation writes, and
    an error there never loses the freeze or its designation. The gate rule
    (only the frozen head's verdicts) is unchanged; this only tells a reviewer
    whose earlier block no longer blocks the new head."""
    reading: dict = {}
    result = _designate_in_transaction(cfg, tx, lot, milestone=milestone,
                                       threads=threads, carried=carried,
                                       reading=reading, target=target)
    try:
        item = work.get(tx, int(lot)) or {"id": int(lot)}
        _notify_superseded_blocks(tx, item, milestone or {}, result, threads,
                                  cfg=cfg, excluded=reading.get("excluded"))
    except Exception:  # noqa: BLE001 - une notification ne perd pas un gel
        pass
    return result


def hook(cfg, outcome: Optional[dict] = None, *, carried: Optional[Mapping] = None,
         target=None):
    """The callback installed on the CLI's `frozen` milestone.

    `outcome`, when given, receives the designation shape for the command's
    message; the callback itself has no other return channel. `carried` is the
    read-only plan of the verdict carry when it covers the head being frozen
    (#2487): the callback then keeps the reported reviewers and mails nobody.
    `target` (#3250) is prepared before opening the transaction; without it a
    branch target defers to recovery instead of trusting the clone's old ref.
    """
    def on_frozen(tx, milestone, threads):
        result = designate_in_transaction(cfg, tx, int(milestone["work_item_id"]),
                                          milestone=milestone, threads=threads,
                                          carried=carried, target=target)
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
    # #2786: validate the freeze BEFORE any read that resolves it — including
    # the carry plan and the effective-mark replay. A `HEAD` or a branch is
    # never resolved in this clone, and nothing is written for it.
    frozen_sha = str(freeze.get("sha") or "").strip().lower()
    if not _SHA_TEXT.fullmatch(frozen_sha):
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": ("freeze #%s names %r, not a full commit id: the designation "
                        "never resolves a ref in its own clone, nothing written"
                        % (freeze.get("id"), str(freeze.get("sha") or "")))}
    manual = _manual(db, item)
    if _waiver(item):
        return {"state": "manual", "reviewers": [], "freeze": freeze,
                "why": "manual waiver (-none)"}
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
            if manual:
                with db.transaction() as tx:
                    if not _lock_current_freeze(tx, int(lot), freeze):
                        return _stale(freeze)
                    out = _manual_unavailable(cfg, tx, item, freeze, manual,
                                              threads, inputs.why,
                                              silent=inputs.silent)
                _drain(db, threads)
                return out
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
        if _manual_covers(inputs):
            return {"state": "manual", "reviewers": [], "freeze": freeze,
                    "why": "manual reviewer(s) already cover the required review(s): %s"
                           % ", ".join(inputs.manual)}
        if not inputs.paths or inputs.policy.required == 0:
            why = LIGHT_WHY if inputs.paths else NO_PATH_WHY
            with db.transaction() as tx:
                if not _lock_current_freeze(tx, int(lot), freeze):
                    return _stale(freeze)
                _note_none(tx, item, freeze, why)
                _sync_reviewers(tx, item, freeze, [])
            return {"state": rd.NONE, "required": 0, "reviewers": [],
                    "freeze": freeze, "why": why}
        with db.transaction() as tx:
            out = _selection(inputs, pg.PgPort(tx, after_commit=threads,
                                                at_freeze=False), force=force)
            names = ([r.get("name") for r in out.get("reviewers") or ()]
                     if out.get("state") == rd.DESIGNATED else [])
            _sync_reviewers(tx, item, freeze, names)
            try:
                _notify_superseded_blocks(tx, item, freeze, out, threads)
            except Exception:  # noqa: BLE001 - une notification ne perd rien
                pass
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
    else:
        # #2561: a light freeze has no mark; its journaled no-review outcome
        # is the explanation the CLI and the pilot read.
        none = _none_outcome(db, int(lot), freeze)
        if none is not None:
            out["designation"] = none
    if apply:
        out["applied"] = designate(cfg, db, int(lot), freeze=freeze)
    return out


def _retry_resolved(db, lot: int, freeze: Mapping) -> bool:
    """True when this freeze already has a final outcome.

    A manual designation, a `none` note or a designated/light mark is final. A
    `waiting` mark is not: capacity or evidence may have returned, which is
    exactly what a retry is for (#1617).
    """
    try:
        item = work.get(db, int(lot))
        if item is not None and _manual(db, item):
            return True
    except Exception:  # noqa: BLE001 - unreadable manual list: retry the freeze
        pass
    freeze_id = int((freeze or {}).get("id") or 0)
    since = float((freeze or {}).get("at_ts") or 0.0)
    try:
        if storage.of(db).work.events_noted(
                "%s%d:%d:" % (NONE_PREFIX, int(lot), freeze_id), since, ids=[int(lot)]):
            return True
    except Exception:  # noqa: BLE001 - unreadable journal: let `designate` decide
        pass
    # #2487: the effective mark of the freeze can be a carried one (verdicts
    # reported from an earlier head); it is final too, exactly like a
    # designation. A waiting selection is retried.
    marks = _marks(db, int(lot), freeze_id=freeze_id, since_ts=since)
    if not marks:
        return False
    effective = marks[-1]
    if effective[1] == "carried":
        return True
    return str((effective[2] or {}).get("state") or "") in (rd.DESIGNATED, rd.NONE)


def _retry_cursor_path(cfg) -> str:
    """The retry cursor file, under the host state directory (never a repo)."""
    state = str(getattr(cfg, "state_dir", "") or "").strip()
    return os.path.join(state, RETRY_CURSOR) if state else ""


def _read_retry_cursor(cfg) -> int:
    """The last unavailable note examined, or 0 when absent/unreadable."""
    path = _retry_cursor_path(cfg)
    if not path:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = int(str(handle.read(64)).strip() or 0)
    except (OSError, ValueError):
        return 0
    return max(0, value)


def _write_retry_cursor(cfg, cursor: int) -> None:
    """Atomically records the cursor; a state problem never fails the retry."""
    path = _retry_cursor_path(cfg)
    if not path:
        return
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("%d\n" % max(0, int(cursor)))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _retry_notes(db, after: int, *, scan: int = RETRY_SCAN) -> list:
    """The unavailable notes after `after`, oldest first (a bounded window).

    Ordering oldest first is what gives the cursor a direction: a pass resumes
    after the last examined note and pages forward, wrapping once. The window
    is bounded, so a large backlog is walked over several passes instead of
    reading the whole journal at once."""
    return db.query(
        "SELECT e.id, e.work_item_id, e.note FROM work_item_events e"
        " JOIN work_items w ON w.id = e.work_item_id"
        " WHERE e.note LIKE %s"
        "   AND e.created_at >= now() - interval '30 days'"
        "   AND w.state NOT IN ('merged', 'promoted', 'closed')"
        "   AND e.id > %s"
        " ORDER BY e.id ASC LIMIT %s",
        (UNAVAILABLE_PREFIX + "%", int(after), int(scan)))


def retry_pending(cfg, db, *, limit: int = RETRY_LIMIT) -> dict:
    """Retries the latest freeze of lots whose designation was journaled unavailable.

    #2561: the policy and the frozen head can become readable *after* the freeze
    (canon publication then sync; the worktree found once the agent's other
    checkout exists). The freeze is kept, so the recovery replays the same head
    — never a selection on a newer one. Bounded, idempotent, and it never raises:
    the caller (the runner after a successful canon sync) only logs the summary.
    A waiting mark is retried (capacity may be back); a designated or light
    outcome is skipped.

    The pass is a **fair slice** of the queue, not the newest 20 notes again:
    `RETRY_CURSOR` keeps the last note examined, the pass resumes after it and
    wraps at the end. A resolved lot (manual, `none`, carried or designated) is
    skipped without consuming an attempt, and a note that no longer matches the
    lot's latest freeze is skipped too; neither can hold a slot forever. A
    blocked or unavailable lot itself does not vanish: it is seen again on the
    next turn of the cursor. A new freeze's note is a new id, so the cursor
    reaches it on its next turn; the link to the freeze is re-read on every
    pass, never stored in the cursor.
    """
    out = {"attempted": 0, "designated": 0, "waiting": 0, "none": 0,
           "unavailable": 0, "manual": 0, "stale": 0, "skipped": 0,
           "errors": [], "summary": ""}
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = RETRY_LIMIT
    cursor = _read_retry_cursor(cfg)
    try:
        rows = _retry_notes(db, cursor)
        if not rows and cursor:
            # end of the queue: start the next turn at the oldest note
            cursor = 0
            rows = _retry_notes(db, 0)
    except Exception as exc:  # noqa: BLE001 - a retry never breaks the caller
        out["errors"].append("%s: %s" % (type(exc).__name__, str(exc)[:200]))
        out["summary"] = "no retry: journal unreadable"
        return out
    seen: set[int] = set()
    last = cursor
    attempted = 0
    for row in rows or ():
        try:
            lot = int(row.get("work_item_id") or 0)
            note_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        match = _RETRY_NOTE.match(str(row.get("note") or ""))
        if not match or int(match.group(1)) != lot or lot in seen:
            last = max(last, note_id)
            continue
        freeze_id = int(match.group(2))
        latest = _latest_freeze(db, lot)
        if latest is None or int(latest.get("id") or 0) != freeze_id:
            out["skipped"] += 1
            last = max(last, note_id)
            continue
        seen.add(lot)
        try:
            resolved = _retry_resolved(db, lot, latest)
        except Exception as exc:  # noqa: BLE001 - one lot never stops the pass
            out["errors"].append("#%d: %s: %s"
                                 % (lot, type(exc).__name__, str(exc)[:160]))
            last = max(last, note_id)
            continue
        if resolved:
            out["skipped"] += 1
            last = max(last, note_id)
            continue
        if attempted >= limit:
            # budget spent: this note stays for the next turn of the cursor
            break
        attempted += 1
        out["attempted"] = attempted
        last = max(last, note_id)
        try:
            result = designate(cfg, db, lot, freeze=latest)
        except Exception as exc:  # noqa: BLE001 - one lot never stops the pass
            out["errors"].append("#%d: %s: %s"
                                 % (lot, type(exc).__name__, str(exc)[:160]))
            continue
        state = str((result or {}).get("state") or "")
        if state in out:
            out[state] += 1
    # Always move past what this pass examined, even when no lot was attempted:
    # the next pass must not re-read the same resolved/stale notes.
    if last > cursor or cursor == 0:
        _write_retry_cursor(cfg, last)
    out["summary"] = ("%d attempted: %d designated, %d waiting, %d none,"
                      " %d unavailable, %d manual, %d stale, %d skipped"
                      % (attempted, out["designated"], out["waiting"],
                         out["none"], out["unavailable"], out["manual"],
                         out["stale"], out["skipped"]))
    return out
