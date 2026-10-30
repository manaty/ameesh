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
  #4343 : quand le lot déclare un prérequis (`work dependencies`) dont une tête
  gelée précède sa tête, la lecture d'auteurs commence à cette tête : les
  commits du parent sont relus dans le parent, et leur provenance inconnue ne
  bloque pas l'enfant;
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
an older pilot installation is no obstacle. #3328: the same runner pass
reattributes a seat whose designated reviewer became unreachable (host closed,
stopped/dead): the same freeze is replayed with the host evidence, the move is
journaled once per (freeze, unreachable names), and the pilot gets the capacity
alert when no replacement family can be designated.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import json
import os
import re
import subprocess
import sys
import time
import dataclasses
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from . import alert_briefs
from . import canon as canon_mod
from . import canon_lowering
from . import db as db_mod
from . import lot_class, mechanics, plan_git, registry, review
from . import review_designation as rd
from . import review_designation_pg as pg
from . import review_authors, review_floor, review_target, storage, work

#: the sender of the journal notes and automatic mails
SENDER = pg.SENDER
#: journal prefix of a policy/evidence problem: one note per freeze
UNAVAILABLE_PREFIX = "review-designation-unavailable:"
#: journal prefix of a light/no-review outcome: one note per freeze (#2561)
NONE_PREFIX = "review-designation-none:"
#: journal prefix of a floor completion (#3557): `<lot>:<freeze>:<floor>:`. One
#: note per (freeze, floor): a replay that finds the seats already kept writes
#: nothing, and a waiting outcome cannot be re-noted by a concurrent pass.
FLOOR_PREFIX = "review-designation-floor:"
#: journal prefix of a manual designation that covers the required reviews
#: (#3596): the retry queue treats it as final without re-reading the policy
MANUAL_PREFIX = "review-designation-manual:"
#: journal prefix of an unreadable mark ignored by the reviewer load (#3047):
#: the line names the offending lot and the id of the note it ignored
UNREADABLE_PREFIX = "review-designation-unreadable:"
#: journal prefix of a reviewer profile that cannot be read (#4779):
#: `<agent>:<reason>:`. One note per (agent, reason) on the lot: the refusal
#: is a fact, and a replay must not write it again.
PROFILE_UNREADABLE_PREFIX = "review-profile-unreadable:"
#: the first release that carries `ameesh work reviewers` (#2447)
RECOVERY_FROM = "1.12.0"
#: why a light lot keeps no pre-merge reviewer (canon §2, full table)
LIGHT_WHY = ("light lot: no pre-merge reviewer is due (the other-family review"
             " happens after merge)")
#: why an empty frozen diff keeps no reviewer
NO_PATH_WHY = "no changed path: no review due"
#: journal line of the waiver posed on a vehicle without own delta (#4109)
VEHICLE_WAIVER_EVENT = ("verdict not required: vehicle without own delta, members "
                        "reviewed (#1979, #4109)")
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
    #: #3554 : la comparaison base/tête d'un lot du canon, quand elle conclut
    #: à une baisse d'exigence (`required` vaut alors au moins 2)
    canon_lowering: Optional[dict] = None
    #: #3742 : les drapeaux d'indépendance compilés au même commit que les
    #: familles, sous la forme `(exclude_author_family, distinct_families)`.
    #: Le tuple n'est pas un littéral mécanique recopié : les deux valeurs
    #: viennent du document compilé, lues par `_policy`. Vide (lecture qui n'a
    #: pas de politique, `required == 0`) : le gel n'a rien à en faire.
    review_independence: tuple = ()


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
    #: #4582 : les acteurs qui ont déjà un verdict sur la tête gelée (SHA
    #: identique ou préfixe) : un siège manuel qui y figure n'est pas relancé.
    answered: tuple = ()
    #: the raw `-none` waiver of the lot (`work reviewer --no-verdict`)
    waiver: bool = False
    #: {family: advance in points over its plan pace} (#2668); empty when no
    #: gauge reading is current, which leaves the load to decide
    advances: dict = field(default_factory=dict)
    #: #3742 : SHA attribués à chaque acteur par la même lecture que les
    #: auteurs (plage courante et gels antérieurs reportés). Le refus de gel
    #: les nomme ; aucune décision ne les lit.
    author_commits: dict = field(default_factory=dict)
    #: #3948 : marques de charge illisibles, lues avec les entrées. La note
    #: est écrite dans la transaction du gel (hors de la lecture préalable),
    #: pour ne pas séparer un diagnostic de son gel.
    unreadable: tuple = ()
    #: #3948 : état lu avec les entrées (assignation, cible, dernier gel).
    #: Relu sous le verrou du gel : une préparation longue ne doit pas désigner
    #: sur une cible ou une frise qui a bougé. Le plancher d'avis est relu à
    #: part, sous le même verrou.
    basis: dict = field(default_factory=dict)
    #: #4679 B/P2 : reçus de correctifs introuvables **sans rattachement de
    #: lot** ({name, sha, freeze}) : le gel les dit, ils n'excluent personne.
    doubts: list = field(default_factory=list)


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


def _declared_reviews(rules, paths) -> tuple[dict, int]:
    """La lecture `train_reviews` du canon compilé, par chemin (#3542).

    `mechanics.ReviewRule` porte les motifs (`.paths`) et leur nombre
    (`.reviews`). Rend `(nommés, attrape-tout)` comme
    `review.declared_reviews` : une règle qui nomme le chemin fixe son nombre ;
    une règle `**` couvre tout sans nommer et ne peut que relever le compte.
    """
    return review.declared_reviews(
        [(rule.paths, rule.reviews) for rule in rules or ()], paths)


def _policy(cfg, db, project: str, paths: Sequence[str]) -> Policy | Unavailable:
    """The first configured canon that declares a usable policy for `project`."""
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        return Unavailable("no canon configured: no automatic designation", silent=True)
    errors = []
    try:
        for source, commit, loaded in policy_sources(cfg, db, project, errors):
            try:
                need = loaded.reviews_for(paths)
                # #2627 / #3542 : le besoin que la porte de fusion calculera
                # au feu. Le nombre d'une règle `train_reviews` qui nomme le
                # chemin fait foi ; `CRITICAL_SCOPE` ne relève que les chemins
                # qu'aucune règle ne nomme, et `**` ne l'abaisse jamais.
                from . import merge_queue
                classified = review.classify(loaded.review_policy, list(paths))
                named, wide = _declared_reviews(loaded.rules, paths)
                classified["train_reviews_paths"] = named
                classified["train_reviews_wide"] = wide
                classified["project"] = project
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
                          commit=commit, source=source.repo,
                          # Un objet de lecture minimal (témoins, ancien
                          # appelant) peut ne pas porter les drapeaux : le
                          # repli garde la politique stricte du canon plutôt
                          # que de supposer l'inverse (#3742).
                          review_independence=(
                              bool(getattr(loaded, "exclude_author_family", True)),
                              bool(getattr(loaded, "distinct_families", True))))
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


def _declared_parent_heads(db, item: Mapping, repo: str, head: str) -> tuple[str, ...]:
    """Les têtes gelées des parents d'empilement qui précèdent `head` (#4343/#4478).

    Un lot empilé sur la tête gelée d'un parent — prérequis déclaré
    (`work dependencies`) ou membre du même paquet (#3589) — porte dans la
    plage lue depuis la cible les commits du parent. Ces commits ont été relus
    sur la tête du parent : ils ne sont pas des auteurs de l'enfant, et la
    provenance inconnue d'un commit du parent ne bloque pas l'enfant.

    La lecture est celle du garde d'empilement (`freeze_stack.candidates`),
    partagée avec la porte de fusion (`review_authors.stacked_parent_heads`) :
    mêmes parents, même preuve d'ascendance, même doute qui conserve l'auteur.
    """
    return review_authors.stacked_parent_heads(db, item, repo, head)


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


def _target_tip(repo: str, base: str, *, fetch: bool = True) -> str:
    """Le commit de la cible au moment de la lecture (#3116), ou "".

    `base` est la cible déclarée (`develop`, `origin/main`, un commit) ; on la
    résout d'abord comme `_range` (la ref distante fait foi) puis en commit.
    #4673 : avec `fetch=True`, la pointe publiée est relue à la forge avant la
    lecture ; `fetch=False` (transaction de gel) garde la ref locale. Une
    cible illisible rend "" : la lecture garde son comportement d'avant."""
    resolved = _resolve_base(repo, base)
    if not resolved:
        return ""
    return review_authors.target_commit(repo, resolved, fetch=fetch)


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


def _manual_designations(db, lot: int, *, until=None) -> Optional[dict]:
    """The manual reviewer designations of the lot, by name (#2739/#3602).

    Returns `{name: (journal id, created_ts)}` for the latest
    `reviewer X designated` line of each name. `work_items.reviewers` is the
    live column; `work reviewer --remove` retires a manual seat and the column
    no longer carries the name. The journal keeps the designation, so a manual
    reviewer who pushed a first-round correction stays recognizable after
    their seat is retired, exactly like the automatic names of the marks
    (#2638). Only a line written by a designation authority counts
    (`_designation_authority`): a free note carrying the same words is not a
    designation. The read is bounded by the note prefix and the lot, never by
    the last 500 events: a designation older than a talkative lot's recent
    journal stays read (#2776, same reason as #1448 N2).

    The journal id is what orders the gesture against the designation marks:
    a manual line written after a mark that carries the same name means the
    pilot re-designated it (#3602), whatever the name's automatic past. With
    `until` (the freeze instant), a line after the freeze is ignored here.
    None when the journal cannot be read: the caller decides, never a guess.
    """
    try:
        events = storage.of(db).work.events_noted(
            "reviewer ", 0.0, ids=[int(lot)]) or []
    except Exception:  # noqa: BLE001 - unreadable journal: read unknown
        return None
    found: dict = {}
    for event in events:
        note = str((event or {}).get("note") or "").strip()
        if not note.startswith("reviewer ") or not note.endswith(" designated"):
            continue
        if not _designation_authority(db, (event or {}).get("actor")):
            continue
        name = note[len("reviewer "):-len(" designated")].strip()
        if not name:
            continue
        try:
            row_id = int((event or {}).get("id") or 0)
            stamp = float((event or {}).get("created_ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if until is not None and stamp > float(until):
            continue
        if name not in found or row_id > found[name][0]:
            found[name] = (row_id, stamp)
    return found


def _journal_reviewers(db, lot: int, *, until=None) -> list:
    """The reviewer names designated by hand in the lot journal (#2739).

    The names of `_manual_designations`, oldest designation first. With
    `until` (the freeze instant), a reviewer named after the freeze is
    ignored: they did not review that head.
    """
    return list(_manual_designations(db, lot, until=until) or ())


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
                        families_out: Optional[dict] = None,
                        target: str = "", admitted_heads=(),
                        exclusions_out: Optional[list] = None) -> tuple[list, list, list]:
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
                                  base=base, head=head, target=target,
                                  admitted_heads=admitted_heads,
                                  exclusions_out=exclusions_out)


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
        # #3835 : nommer le gel antérieur et le lot qui portent l'inconnu.
        raise ActivationError(
            review_authors.unknown_refusal(unknown, head_sha=head))
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
#: designations (their journal id orders them against the marks, #3602) and
#: the verdicts (actor, head, verdict and instant — #3288 reads the blocks
#: from the same rows). Every note comes back as **text** and is parsed in
#: Python: no note is ever cast by PostgreSQL, so no bad note can abort the
#: freeze transaction this read runs in.
_PENDING_FACTS_SQL = f"""
    WITH open_items AS (
        SELECT id, assignee, reviewers, title, state, app
          FROM work_items
         WHERE state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')
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
               array_agg(e.id ORDER BY e.id) AS ids,
               array_agg(e.note ORDER BY e.id) AS notes,
               array_agg(extract(epoch FROM e.created_at)::float8
                         ORDER BY e.id) AS ts
          FROM work_item_events e
          JOIN latest_freeze f ON f.lot = e.work_item_id
         WHERE starts_with(e.note, 'reviewer ')
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
           mn.ids AS manual_ids,
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
      carried (kept across freezes by `_sync_reviewers`), or a name whose
      latest `reviewer X designated` line (`ameesh work reviewer`) follows
      the latest mark carrying it (#3602: the pilot re-designated a name an
      earlier selection had already carried).

    An automatic name of an older freeze is therefore not a pending review of
    the new head — including during the selection itself, when the column has
    not been synced yet (#3004) — and a reviewer the pilot removed (#3004:
    dev1 removed, dev2 designated) is no longer one. Only a verdict naming the
    frozen head discharges it, full or abbreviated SHA, the `_same_freeze`
    rule; a `blocked` verdict counts, the reviewer has rendered it. A closed,
    merged or promoted lot owes nothing. A validated lot owes no further
    review, even if a surplus designated seat has no verdict (#4851).
    `designated_ts` is when the agent was
    designated for this head (latest of the freeze, the mark naming them, a
    manual designation after the mark): the start of the §9 clock.

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
        # The latest mark carrying each name: #3602 compares it with the
        # manual gesture that may have re-designated the same name later.
        ever: dict = {}
        for note_id, names in marks.items():
            for name in names or ():
                ever[name] = max(ever.get(name, 0), int(note_id))
        # `reviewer X designated`: X, at its latest line (journal id, instant)
        manual: dict = {}
        for note, note_id, ts in zip(row.get("manual_notes") or (),
                                     row.get("manual_ids") or (),
                                     row.get("manual_ts") or ()):
            name = str(note or "")[len("reviewer "):-len(" designated")]
            try:
                event_id, stamp = int(note_id), float(ts)
            except (TypeError, ValueError):
                continue
            if name not in manual or event_id > manual[name][0]:
                manual[name] = (event_id, stamp)
        verdicts = list(zip(row.get("verdict_actors") or (),
                            row.get("verdict_shas") or (),
                            row.get("verdict_kinds") or (),
                            row.get("verdict_ts") or ()))
        rendered = [(actor, sha) for actor, sha, _kind, _at in verdicts]
        seen: set = set()
        # #4851: validation has already established the review quorum. Keep
        # the lot's assignment and fix debts, but discharge surplus reviews
        # for every reader (queue, designation load and SLO) together.
        pending_seats = (() if row.get("state") == lifecycle.VALIDATED
                         else row.get("reviewers") or ())
        for raw in pending_seats:
            name = str(raw or "").strip()
            if name in ("", "-none") or name in seen:
                continue
            seen.add(name)
            manual_seat = name in manual and manual[name][0] > ever.get(name, 0)
            if name not in effective and name in ever and not manual_seat:
                continue
            if any(str(actor or "").strip().lower() == name.lower()
                   and rd._same_freeze(frozen_sha, sha)
                   for actor, sha in rendered):
                continue
            since = [float(ts) for ts in (
                freeze_ts,
                row.get("mark_ts") if name in effective else None,
                manual[name][1] if manual_seat else None) if ts is not None]
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


def owed_work(db) -> tuple[list, list]:
    """`(pending, fixes)` of `_owed_read`, both halves in **one** read (#3333).

    For a caller that needs the reviews still owed and the fixes due together
    (the §9 pass: `green_verdict` and `blocked_freeze`): the rows are those of
    `pending_reviews` and `due_fixes` without an agent filter, from a single
    query instead of two. The read raises when the base cannot be read.
    """
    _lots, pending, fixes = _owed_read(db)
    return pending, fixes


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


def _note_profile_unreadable(db, lot: int, state: str,
                             unreadable: Sequence[tuple]) -> None:
    """Journal an unreadable reviewer profile once, without failing a freeze.

    #4779: a registry row whose `capabilities` or `roles` cannot be read is
    not evidence that the agent may review — the candidacy is refused. The
    doubt is written on the lot being designated, deduplicated by
    `(agent, reason)`, so a replay or a second freeze does not repeat it. The
    write is best effort, under a savepoint when a transaction is open: a
    failed trace must never abort the freeze it explains.
    """
    if not unreadable:
        return
    in_tx = db_mod.in_transaction(db)
    for name, reason in unreadable:
        name = str(name or "").strip()
        reason = str(reason or "").strip()
        if not name or reason not in rd.UNREADABLE_PROFILE_REASONS:
            continue
        prefix = "%s%s:%s:" % (PROFILE_UNREADABLE_PREFIX, name, reason)
        text = ("%s reviewer %s refused as candidate: %s (canon §2, #4779)"
                % (prefix, name, rd.PROFILE_REASONS.get(reason, reason)))
        try:
            if storage.of(db).work.events_noted(prefix, 0.0, ids=[int(lot)]):
                continue
        except Exception:  # noqa: BLE001 - no duplicate proof: write the trace
            pass
        try:
            if in_tx:
                db.execute("SAVEPOINT review_designation_profile_trace")
            try:
                storage.of(db).work.note(int(lot), state, text, SENDER)
            except Exception:  # noqa: BLE001 - a trace never blocks a freeze
                if in_tx:
                    db.execute("ROLLBACK TO SAVEPOINT review_designation_profile_trace")
                    db.execute("RELEASE SAVEPOINT review_designation_profile_trace")
                continue
            if in_tx:
                db.execute("RELEASE SAVEPOINT review_designation_profile_trace")
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
    """The project's orchestrator, by the shared pilot rule (#2935).

    `alert_briefs.project_pilots` restricts the declared orchestrators to the
    project and, when none declares it, keeps the declared ones rather than
    naming nobody. The stable order names the first, exactly like a base
    failure. A project is never matched to a hard-coded pilot: the role comes
    from the canon and the project from the registry.
    """
    try:
        pilots = alert_briefs.project_pilots(
            str(project or "").strip(), list(listing), cfg=cfg, db=db)
    except Exception:  # noqa: BLE001 - no readable role: no recipient
        return ""
    return pilots[0] if pilots else ""


def _candidates(listing: Sequence[Mapping], *, loads: Mapping, project: str, families,
                pilot: str, closed_hosts: Sequence[str] = (),
                unreadable_profiles: Optional[list] = None) -> list:
    """The project's candidates, its pilot removed.

    The project's agents plus the agents of another team that declare it under
    `relit:` (#3185). The pilot receives the capacity alert; the review stays
    with the others, so the two roles never collapse into one mail — even when
    the pilot's card would make it a candidate. `closed_hosts` (#3328) is the
    host liveness evidence: an agent on a `draining`/`lost` host is not a
    candidate, like any recipient agent-mail cannot reach. #4779: an agent
    whose card does not prove the `propose` capacity and a role is not a
    candidate; an unreadable profile is reported through
    `unreadable_profiles` so the caller journals the doubt.
    """
    candidates = rd.candidates_from_registry(listing, loads=loads, project=project,
                                             families=families,
                                             closed_hosts=closed_hosts,
                                             unreadable_profiles=unreadable_profiles)
    if pilot:
        candidates = [candidate for candidate in candidates if candidate.name != pilot]
    return candidates


def _manual(db, item: Mapping) -> list:
    """The reviewer names that were designated by hand, not by a past freeze.

    `work_items.reviewers` holds both. An automatic name is one a
    `review-designation:` mark carries; subtracting them keeps the manual
    exception visible on a replay and lets a new freeze clear only the names
    it wrote itself. #3602: the name alone is not enough — a pilot may
    re-designate a name an earlier mark already carried. The manual journal
    line (`reviewer X designated`) is dated by its journal id: the name is
    manual again when that gesture follows the latest mark carrying it. A
    later selection that names the same reviewer makes it automatic again.
    An unreadable mark set keeps every current name manual (no gesture is
    dropped on a read we cannot prove).
    """
    from .work_reviewers import designated

    try:
        names = list(designated(dict(item or {})))
    except Exception:  # noqa: BLE001 - unreadable list: no manual exception
        return []
    if not names:
        return []
    lot = int((item or {}).get("id") or 0)
    marks = _marks(db, lot)
    if marks is None:
        return names
    carried: dict = {}
    for row_id, _kind, noted, _ts in marks:
        for reviewer in noted.get("reviewers") or ():
            name = str((reviewer or {}).get("name") or "").strip()
            if name:
                carried[name] = max(carried.get(name, 0), int(row_id))
    manual = _manual_designations(db, lot)
    if manual is None:
        return [name for name in names if name not in carried]
    return [name for name in names
            if name not in carried
            or (name in manual and manual[name][0] > carried[name])]


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
    forbidden like any author's and its name does not cover a seat. #4779
    (B1 cx2): a manual seat whose card does not prove the `propose` capacity
    and a role covers nothing either — otherwise a canary designated before
    the guard, or by a path that did not read it, would keep the automatic
    selection from naming a real reviewer and the gate would stay held. The
    seat stays in the column (a pilot gesture); only its counting stops. The
    list is ordered like the column.
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
        # #4779 (B1 cx2) : un siège manuel sans `propose` ou sans rôle ne
        # couvre aucun avis, sinon le filtre serait contourné par un siège
        # posé avant la garde (le siège reste dans la colonne).
        eligible, _reason = rd.reviewer_eligibility(row)
        if not eligible:
            continue
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


def _held_manual(inputs: Inputs, reported: Sequence[str] = ()) -> list:
    """Les sièges manuels gardés qui doivent être relancés sur cette tête.

    #4582 : ils couvrent un avis (`missing: 0`) mais n'ont pas de verdict sur
    la tête gelée et leur avis antérieur n'est pas reporté. `reported` sont les
    acteurs dont le verdict va être reporté (#2442), même quand le plan est
    partiel. Les sièges qui y figurent, comme ceux qui ont déjà rendu un avis
    sur la tête (`inputs.answered`), ne sont pas relancés."""
    answered = {str(name or "").strip().lower()
                for name in inputs.answered or () if str(name or "").strip()}
    answered |= {str(name or "").strip().lower()
                 for name in reported or () if str(name or "").strip()}
    return [dict(seat) for seat in inputs.manual_seats or ()
            if str((seat or {}).get("name") or "").strip().lower()
            not in answered]


def _verdicts_on(db, lot: int, sha: str) -> tuple:
    """Les acteurs qui ont déjà un verdict sur `sha` (tête gelée), #4582.

    Un siège manuel gardé ne se relance pas quand son avis porte déjà sur la
    tête gelée : le regel de la même tête, ou un verdict rendu avant le gel,
    ne redemande rien. La lecture est celle des jalons du lot (`_same_freeze`,
    les SHA abrégés compris) ; un journal illisible ne prouve aucun verdict —
    le siège sera relancé, jamais laissé sans courrier sur un doute.
    """
    names: list = []
    try:
        rows = storage.of(db).work.milestones(int(lot), None) or []
    except Exception:  # noqa: BLE001 - lecture impossible : aucun verdict prouvé
        return ()
    target = str(sha or "").strip().lower()
    if not target:
        return ()
    for row in rows:
        if str((row or {}).get("kind") or "") != "verdict":
            continue
        if not rd._same_freeze(target, (row or {}).get("sha")):
            continue
        name = str((row or {}).get("actor") or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


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


def _latest_freeze_marker(db, lot: int, *, before_id: Optional[int] = None) -> Optional[list]:
    """`[id, sha]` du `frozen` de plus grand id (strictement avant `before_id`).

    #3948 : la lecture hors transaction capture cette marque ; le hook la relit
    sous le verrou. Un gel concurrent écrit pendant la lecture rend la
    préparation incomplète (ses auteurs ne sont pas dans la lecture) : la
    désignation attend alors le rejeu de récupération au lieu de désigner sur
    une frise périmée. La borne est l'**id**, comme le filtre `id < gel` de la
    lecture des gels antérieurs — jamais `at` : l'horodatage est celui du début
    de la transaction, et un gel concurrent peut avoir commencé plus tôt.
    None = frise illisible, jamais un zéro supposé.
    """
    try:
        rows = storage.of(db).work.milestones(int(lot), None) or []
        if before_id is not None:
            rows = [row for row in rows
                    if int(row.get("id") or 0) < int(before_id)]
        rows = [row for row in rows if row.get("kind") == "frozen"]
        row = max(rows, key=lambda value: int(value.get("id") or 0),
                  default=None)
    except Exception:  # noqa: BLE001 - frise illisible : l'appelant attend
        return None
    if row is None:
        return []
    return [int(row.get("id") or 0), str(row.get("sha") or "").strip().lower()]


def _prepared_basis(item: Mapping, target, latest) -> dict:
    """L'état dont dépend la lecture préparée, à revérifier sous le verrou.

    La politique d'avis vient du contenu gelé et du commit de canon audité :
    elle ne dépend pas des verrous du lot et n'est pas relue ici (ce serait une
    lecture Git sous le verrou). Le plancher d'avis est un journal du lot : il
    est relu à part, car seul un plancher qui dépasse le compte préparé change
    le besoin (un plancher plus bas ne retire aucun avis dû).
    """
    return {
        "assignee": str((item or {}).get("assignee") or ""),
        "target_declared": str(getattr(target, "declared", "") or ""),
        "latest_freeze": latest,
    }


def prepare(cfg, db, lot: int, *, freeze: Optional[Mapping] = None,
            fetch: bool = True, target=None, note_unreadable: bool = True,
            milestone_proof: Optional[Mapping] = None) -> Inputs | Unavailable:
    """Reads the machine inputs of a freeze; an expected refusal is `Unavailable`.

    `fetch=False` keeps the read local (no `git fetch`), as the freeze
    transaction requires. A branch target then needs the `target` snapshot
    prepared outside the transaction; otherwise the read is unavailable.

    `note_unreadable=False` (#3948) skips the charge note written by the
    reading: the caller reads outside the freeze transaction and carries the
    list in `Inputs.unreadable`, so the note stays in the freeze's
    transaction. `milestone_proof` is the already-captured proof of the
    `frozen` milestone being written: it serves as the reference author's
    receipt when the milestone does not exist yet."""
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

    # #3250 : `base` est déjà la cible exacte épinglée hors transaction ; c'est
    # elle que la lecture de provenance reçoit (#3116), sans la re-résoudre.
    read_target = base
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
    answered = _verdicts_on(db, int(lot), frozen_sha)
    if not paths:
        # no changed file: no review is due, and no policy needs to be read
        # #3948 : la marque est bornée par l'id provisoire du gel, comme la
        # lecture des auteurs (`id < gel`) : un gel concurrent arrivé après la
        # préparation du jalon doit rester « nouveau » sous le verrou.
        latest = _latest_freeze_marker(db, int(lot),
                                       before_id=int(freeze.get("id") or 0))
        return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=[],
                      commits=commits,
                      policy=Policy(rules=(), families=(), risk="light", required=0,
                                    ref="", commit="", source=""),
                      authors=[], unknown_authors=[], correctors=[], candidates=[],
                      notify="", manual=manual, waiver=waiver, answered=answered,
                      basis=_prepared_basis(item, target, latest))
    policy = _policy(cfg, db, project, paths)
    if isinstance(policy, Unavailable):
        return policy
    # #3554 : un lot du canon qui touche sa politique est comparé à la base ;
    # une baisse d'exigence (avis ou classe) exige deux avis. Le résultat est
    # porté par `Policy` pour que la désignation et son journal le lisent.
    lowering = canon_lowering.for_lot(cfg, repo, start, frozen_sha, paths)
    if lowering is not None and lowering.floor:
        policy = dataclasses.replace(
            policy, required=max(int(policy.required), lowering.floor),
            canon_lowering=lowering.to_dict())
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
    # #3328 : l'hôte perdu est la preuve de non-remise que lit agent-mail
    # (#3036). Une lecture en échec ne ferme aucun hôte : on ne devine pas
    # qu'un hôte est mort, et le doute ne désigne personne d'autre.
    closed_hosts = alert_briefs.claims_closed_hosts(db)
    # #4779 : un profil de relecture illisible (capacités ou rôles) n'est pas
    # une preuve ; la candidature est refusée et le doute journalisé, même
    # quand la note de charge est différée (`note_unreadable=False`) : ce n'est
    # pas la même lecture et une donnée absente ne doit pas disparaître.
    profile_unreadable: list = []
    candidates = _candidates(listing, loads=_loads(db, unreadable=unreadable),
                             project=project, families=families, pilot=notify,
                             closed_hosts=closed_hosts,
                             unreadable_profiles=profile_unreadable)
    if unreadable and note_unreadable:
        _note_unreadable(db, int(lot), str(item.get("state") or ""), unreadable)
    if profile_unreadable:
        _note_profile_unreadable(db, int(lot), str(item.get("state") or ""),
                                 profile_unreadable)
    # #3948 : capturée AVANT la lecture des auteurs, la marque doit être
    # antérieure à ce que cette lecture voit ; sinon un gel arrivé entre les
    # deux serait réputé déjà lu et ses auteurs manqueraient. La borne est
    # l'id provisoire du gel, exactement le filtre `id < gel` de la lecture
    # des gels antérieurs : sous le verrou, la même marque est relue avant le
    # vrai id du jalon, et tout gel concurrent apparaît entre les deux.
    latest = _latest_freeze_marker(db, int(lot),
                                   before_id=int(freeze.get("id") or 0))
    author_commits: dict = {}
    # #4343 : un lot empilé sur un prérequis déclaré ne relit pas les commits
    # du parent (gelé et relu sur sa propre tête) ; la provenance inconnue d'un
    # commit du parent ne bloque donc pas l'enfant.
    parent_heads = _declared_parent_heads(db, item, repo, frozen_sha)
    #: #4679 B/P2 : reçus de correctifs introuvables sans lot — dits au gel,
    #: jamais des correcteurs (la porte ne les lit pas).
    doubts: list = []
    authors, unknown, correctors = review_authors.reading(
        db, item, freeze, commits, repo, families=policy.families, trace=True,
        base=start, head=frozen_sha, target=read_target,
        commits_out=author_commits, fetch=fetch,
        admitted_heads=parent_heads,
        milestone_proof=milestone_proof, doubts_out=doubts)
    return Inputs(item=item, freeze=freeze, repo=repo, base=start, paths=paths,
                  commits=commits, policy=policy, authors=authors,
                  unknown_authors=unknown, correctors=correctors,
                  candidates=candidates, notify=notify,
                  manual=manual, waiver=waiver, answered=answered,
                  manual_seats=_manual_seats(manual, listing, authors, families,
                                             correctors=correctors),
                  advances=_advances(db), author_commits=author_commits,
                  unreadable=tuple(unreadable),
                  basis=_prepared_basis(item, target, latest), doubts=doubts)


@dataclass(frozen=True)
class PrepareFailure:
    """A `prepare` exception captured before the freeze transaction (#3948).

    `_designate_in_transaction` journals it like an in-transaction failure;
    the exception never escapes before the milestone is written, so a read
    error still leaves the freeze to the journal instead of losing it."""

    error: Exception


def _provisional_freeze(db, lot: int, head: str, actor: str) -> dict:
    """Le jalon lu avant l'INSERT : identifiant au-delà des marques du lot.

    La lecture hors transaction précède l'INSERT : elle doit voir exactement
    les marques que le futur jalon verra. Un identifiant au-delà de tous ceux
    du lot donne le même ensemble `id < gel` ; la base attribuera le vrai.
    `at_ts` est l'heure de la base, comme le jalon (jamais l'horloge de
    l'hôte, qui peut diverger). Une lecture impossible garde une borne haute
    et l'heure locale : la désignation lira alors toutes les marques
    antérieures, ce qui est le comportement sûr.
    """
    next_id = 2 ** 31 - 1
    at_ts = time.time()
    try:
        rows = db.query(
            "SELECT coalesce(max(id), 0) + 1 AS next_id,"
            "       extract(epoch from now())::float8 AS now_ts"
            "  FROM work_item_milestones WHERE work_item_id = %s", (int(lot),))
        row = (rows or [{}])[0]
        next_id = int(row.get("next_id") or 1)
        at_ts = float(row.get("now_ts") or at_ts)
    except Exception:  # noqa: BLE001 - lecture impossible : borne haute
        pass
    return {"id": next_id, "sha": str(head or "").strip(),
            "at_ts": at_ts, "actor": str(actor or "")}


def prepare_for_freeze(cfg, db, item: Mapping, head: str, *, target=None,
                       actor: str = "",
                       proof: Optional[Mapping] = None) -> Inputs | Unavailable | PrepareFailure:
    """Lit les entrées de la désignation **avant** la transaction du gel (#3948).

    `prepare` fait des lectures Git (plage gelée, politique du canon, et
    surtout `_carried_from_previous` sur tous les gels antérieurs). Appelée
    dans la transaction du jalon, elle la laissait `idle in transaction` plus
    de 180 s sur un lot à nombreux gels (incident #3889) ; la garde
    `idle_in_transaction_session_timeout` tuait alors la connexion, et le gel
    échouait sans que rien ne soit écrit.

    La lecture se fait donc ici, hors transaction, avec la cible déjà épinglée
    (`prepare_target`) et les objets des gels antérieurs déjà rapatriés
    (`prefetch_freeze_objects`). Le hook reçoit le résultat et ne fait plus que
    les écritures, sous le verrou du lot.

    `freeze` est provisoire : son identifiant est au-delà de tous les jalons du
    lot (même ensemble de marques antérieures), son `at_ts` est l'instant de
    la lecture (le jalon sera daté par la base), et `proof` tient lieu de reçu
    pour `reference_author` puisque le jalon n'existe pas encore. Hors tour
    (`proof is None`), la preuve est **vide** et non absente : sans cette
    distinction, `reference_author` lirait le reçu du jalon provisoire, dont
    l'id — `max(id du lot) + 1` — est un bigserial global qui désigne en
    général le jalon d'un autre lot (#3948, B2 dev4). Une exception est
    capturée dans `PrepareFailure` : le gel reste écrit, la désignation attend
    et le journal le dit, comme avant.
    """
    lot = int(item["id"])
    freeze = _provisional_freeze(db, lot, head, actor)
    try:
        return prepare(cfg, db, lot, freeze=freeze, fetch=False, target=target,
                       note_unreadable=False,
                       milestone_proof=proof if proof is not None else {})
    except Exception as exc:  # noqa: BLE001 - journalisé, jamais perdu
        return PrepareFailure(exc)


def prefetch_freeze_objects(db, item: Mapping, target, head: str) -> list[str]:
    """Rapatrie hors transaction les objets des gels antérieurs (#3836).

    `prepare(fetch=False)` refuse tout accès réseau dans la transaction du gel.
    Cette passe, appelée **avant** l'ouverture de cette transaction, demande à
    la forge du dépôt de lecture les objets absents des gels antérieurs : la
    désignation retrouve alors les mêmes auteurs qu'avant, sans jamais tenir
    les verrous du gel pendant un `git fetch` (180 s par objet au plus).

    `target` est le `Target` épinglé par `prepare_target` (ou `Unavailable`) ;
    une cible illisible ne rapatrie rien, la désignation attendra. Jamais
    d'exception : une forge injoignable est une absence, pas un échec du gel.
    """
    if target is None or isinstance(target, Unavailable):
        return []
    try:
        return review_authors.prefetch_prior_freezes(db, item, target.repo, head)
    except Exception:  # noqa: BLE001 - rapatriement au mieux : le gel continue
        return []


#: #3572 : le texte de la note et l'erreur du bloc, passés par réglages locaux
#: à la transaction (jamais interpolés dans le corps PL/pgSQL)
_LOWERING_NOTE = "ameesh.canon_lowering_note"
_LOWERING_ERROR = "ameesh.canon_lowering_error"


def _note_canon_lowering(db, item: Mapping, freeze: Mapping, inputs: Inputs) -> None:
    """#3554 : la liste des fichiers abaissés au journal, une fois par tête.

    Best-effort : un journal qui refuse ne perd ni le gel ni sa désignation.
    La ligne porte le préfixe `canon-review-lowering:` et les commits exacts ;
    un rejeu de la même tête ne la double pas.

    #3572 : dans la transaction du gel, l'écriture passe par un bloc PL/pgSQL
    à exception, une sous-transaction côté serveur sur les deux pilotes (sous
    psql, la première erreur ferme la session : un SAVEPOINT client ne
    rattraperait rien). La note est écrite, ou l'échec est consigné sur stderr
    et la transaction du gel continue.
    """
    lowering = getattr(inputs.policy, "canon_lowering", None)
    if not lowering or not int(lowering.get("floor") or 0):
        return
    text = canon_lowering.journal_text(lowering)
    if not text:
        return
    try:
        if db_mod.in_transaction(db):
            _note_in_transaction(db, int(item["id"]), text)
        else:
            work.note(db, int(item["id"]), text, actor=SENDER, once=True)
    except Exception:  # noqa: BLE001 - la trace au mieux, le gel reste
        pass


def _note_in_transaction(tx, lot: int, text: str) -> None:
    """La note `once` du journal de baisse, isolée dans un bloc à exception."""
    tx.query("SELECT set_config(%s, %s, true) AS note, set_config(%s, '', true)"
             " AS error", (_LOWERING_NOTE, text, _LOWERING_ERROR))
    body = (
        "DECLARE v_note text := current_setting(%(note)s); "
        "BEGIN "
        "PERFORM 1 FROM work_items WHERE id = %(lot)d FOR NO KEY UPDATE; "
        "INSERT INTO work_item_events (work_item_id, state, note, actor) "
        "SELECT w.id, CASE WHEN w.state = 'delivered' THEN coalesce(to_jsonb(w)->>'legacy_state', w.state) ELSE w.state END, v_note, %(actor)s FROM work_items w "
        "WHERE w.id = %(lot)d AND NOT EXISTS (SELECT 1 FROM work_item_events e "
        "WHERE e.work_item_id = w.id AND e.note = v_note); "
        "IF FOUND THEN "
        "UPDATE work_items SET updated_at = now() WHERE id = %(lot)d; "
        "END IF; "
        "EXCEPTION WHEN query_canceled OR OTHERS THEN "
        "PERFORM set_config(%(error)s, SQLERRM, true); "
        "END" % {"note": db_mod.sql_literal(_LOWERING_NOTE), "lot": int(lot),
                 "actor": db_mod.sql_literal(SENDER),
                 "error": db_mod.sql_literal(_LOWERING_ERROR)})
    tx.execute("DO " + db_mod.sql_literal(body))
    rows = tx.query("SELECT current_setting(%s) AS error", (_LOWERING_ERROR,))
    if rows and rows[0]["error"]:
        print("ameesh: canon lowering journal of lot #%d not recorded: %s"
              % (int(lot), rows[0]["error"]), file=sys.stderr)


# --------------------------------------------------------------------------
# the writes
# --------------------------------------------------------------------------

def _freeze_label(freeze: Mapping) -> str:
    return "#%s (%s)" % (freeze.get("id"), str(freeze.get("sha") or "")[:12])


def _floor_prefix(lot: int, freeze_id: int, floor: int) -> str:
    """The idempotent journal prefix of one floor completion (#3557)."""
    return "%s%d:%d:%d:" % (FLOOR_PREFIX, int(lot), int(freeze_id), int(floor))


def _floor_of(db, lot: int, cfg) -> int:
    """The lot's current review floor; raises when the journal cannot be read.

    #3592 : an unreadable journal must not read as a floor of 0, otherwise a
    recorded designation looks complete and its completion is skipped without
    a trace. The caller reports the failure and writes nothing."""
    return int(review_floor.of_lot(db, int(lot), cfg=cfg).get("floor") or 0)


def _mark_covers(mark: Mapping, floor: int, *, column=None) -> bool:
    """True when a recorded designation already covers `floor` opinions.

    Both the recorded required count and the **distinct families** of its
    reviewers must reach the floor: a mark that declares two opinions but
    names two agents of the same family still owes an independent one.
    `column` (the live reviewer names) excludes a seat the pilot removed: the
    mark alone would otherwise pretend the seat is still designated — and
    that holds even when no floor is set (#3685), otherwise `--apply` echoed
    a removed seat as the designation of the freeze."""
    mark = dict(mark or {})
    rows = [row for row in mark.get("reviewers") or ()]
    if column is not None:
        kept = [row for row in rows
                if str((row or {}).get("name") or "").strip() in column]
        if len(kept) < len(rows):
            # #3685 : un siège retiré à la main n'est plus désigné. La marque
            # ne le couvre pas, même sans plancher : le rejeu doit compléter
            # la désignation vivante, jamais renvoyer l'ancienne.
            return False
        rows = kept
    if floor <= 0:
        return True
    try:
        required = int(mark.get("required") or 0)
    except (TypeError, ValueError):
        required = 0
    families = {str((row or {}).get("family") or "").strip().lower()
                for row in rows}
    families.discard("")
    return required >= floor and len(families) >= floor and len(rows) >= floor


def _removed_seats(db, lot: int, freeze: Mapping, live) -> set:
    """The automatic seats of THIS freeze absent from the live column (#3617).

    Every mark of the freeze is read, not only the effective one: a floor
    completion that could only wait partially writes a `waiting` mark naming
    the live seats, so a seat the pilot removed before it appears only in an
    older mark of the same freeze. An unreadable journal removes nobody."""
    names = set()
    for _row_id, _kind, noted, _ts in _marks(
            db, int(lot), freeze_id=int((freeze or {}).get("id") or 0),
            since_ts=float((freeze or {}).get("at_ts") or 0.0)) or ():
        for row in (noted or {}).get("reviewers") or ():
            name = str((row or {}).get("name") or "").strip()
            if name:
                names.add(name)
    return names - {str(name or "").strip() for name in live or ()}


def _kept_seats(shape: Mapping, item: Mapping) -> list:
    """The designated seats of a mark still present in the reviewer column.

    A name the pilot removed from the column is **not** written back: the
    floor completion completes the live designation, it does not undo a manual
    removal. The families come from the mark, the same reading the freeze
    used."""
    from .work_reviewers import designated

    rows = (shape or {}).get("reviewers") or ()
    if item is None or (item.get("reviewers") is None
                        and item.get("reviewer") is None):
        # no readable column (old schema): the mark is the only evidence
        column = {str((row or {}).get("name") or "").strip() for row in rows}
    else:
        try:
            column = {str(name or "").strip() for name in designated(dict(item))}
        except Exception:  # noqa: BLE001 - unreadable column: keep nobody
            column = set()
    seats = []
    for row in rows:
        name = str((row or {}).get("name") or "").strip()
        if name and name in column:
            seats.append({"name": name,
                          "family": str((row or {}).get("family") or "").strip()})
    return seats


def _live_column(item: Mapping) -> Optional[set]:
    """The live reviewer names of a lot, or None when the column is unreadable.

    `work_reviewers.designated` filters `-none` out: a waiver is an authority
    gesture, not a removal. An old schema (no column) and an unreadable column
    both return None, so the mark alone is trusted instead of reading every
    seat as removed (#3557/#3685)."""
    item = item or {}
    if item.get("reviewers") is None and item.get("reviewer") is None:
        return None
    try:
        from .work_reviewers import designated
        return {str(name or "").strip() for name in designated(dict(item))}
    except Exception:  # noqa: BLE001 - unreadable column: the mark alone
        return None


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


def _waive_ownless_vehicle(db, item: Mapping, inputs: "Inputs") -> None:
    """#4109 : un véhicule sans delta propre reçoit la dispense de re-verdict.

    La désignation conclut NONE (« no changed path ») : aucun avis n'est dû,
    les membres portent les leurs. Sans la dispense (#1979), les portes
    attendaient pourtant « verdict from a reviewer (none designated) » et un
    agent devait se désigner. Un véhicule avec une résolution ou un commit
    propre, ou un lot ordinaire sans chemin, ne change pas."""
    from . import packet_reviews

    if inputs.paths or inputs.commits or not packet_reviews.is_vehicle(item):
        return
    work.waive_ownless_vehicle(db, int(item["id"]), SENDER, VEHICLE_WAIVER_EVENT)


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
    may name the seats already provided (#3596): they are automatic too, so
    they are read here — a new freeze retires them like any automatic name
    unless the pilot re-designated them after the mark (#3602, `_manual` and
    `_sync_reviewers` compare the dated manual lines themselves). A carried
    mark (#2487) is an automatic designation too.
    """
    names: list[str] = []
    for _row_id, _kind, noted, _ts in _marks(db, lot) or ():
        try:
            noted_freeze = int((noted.get("freeze") or {}).get("id") or 0)
        except (TypeError, ValueError):
            continue
        if noted_freeze == int(freeze_id):
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

    Rend la liste `{name, family}`, plus `families` pour un acteur prouvé sous
    plusieurs familles (#3584), ou None quand aucune lecture **complète**
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
            if not name:
                continue
            author = {"name": name,
                      "family": str(row.get("family") or "").strip().lower()}
            families = row.get("families")
            if isinstance(families, (list, tuple, set, frozenset)):
                values = sorted({str(value).strip().lower() for value in families
                                 if str(value or "").strip()})
                if values:
                    # #3584 : une marque écrite après ce lot garde l'ensemble
                    # prouvé ; la proposition de relecture en exclut chacune.
                    author["families"] = values
            authors.append(author)
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
    preserved. #3602: a name a mark carried, then re-designated by hand after
    that mark, is preserved too — the journal id of the manual gesture orders
    it against each carrying mark, so a later selection naming the same
    reviewer makes it automatic again. A manual waiver (`-none`) is never
    overridden. An unreadable manual journal retires nothing: a pilot's
    gesture is never dropped on a read we cannot prove.

    `text`, when given, is the journal line of this sync (a verdict carry keeps
    the reported reviewers instead of designating, #2487).

    #3661: a `waiting` effective mark keeps only its seats still in the column
    (or named by the caller). A seat the pilot removed is never written back,
    whatever the path: a selection, a replay, an unavailable or light outcome."""
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
    # The effective mark of this freeze is authoritative even when the caller
    # names no new reviewer: a partial waiting designation (#3596) keeps the
    # seats it already provided, exactly like a carried mark (#2487).
    effective = _effective_mark(db, lot, freeze_id)
    keep_marks = {effective[0]} if effective else set()
    manual = _manual_designations(db, lot)
    retired = set()
    if manual is not None:
        for row_id, _kind, noted, _ts in marks:
            if row_id in keep_marks:
                continue
            for reviewer in noted.get("reviewers") or ():
                name = str((reviewer or {}).get("name") or "").strip()
                if not name:
                    continue
                gesture = manual.get(name)
                if gesture is not None and gesture[0] > row_id:
                    # #3602: the pilot re-designated this name after the mark
                    # that carries it; the dated journal gesture wins over the
                    # automatic past.
                    continue
                retired.add(name)
    keep = [name for name in current if name not in retired]
    wanted = [str((reviewer or {}).get("name") or "").strip()
              for reviewer in (effective[2].get("reviewers") or () if effective else ())]
    if (effective is not None and effective[1] != "carried"
            and effective[2].get("state") == rd.WAITING):
        # #3661 : les sièges d'une attente sont écrits avec leur marque ; un
        # siège absent de la colonne a été retiré par le pilote.
        wanted = [name for name in wanted if name in current]
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


class _FloorPort(_ForcedPort):
    """#3557 : le port d'une désignation complétée par un plancher d'avis.

    Les sièges déjà désignés pour ce gel sont **gardés** et ne reçoivent aucun
    second courrier ; seuls les sièges manquants sont sélectionnés et notifiés.
    La marque écrite les nomme tous, avec le compte requis `max(barème,
    plancher)`.

    Une attente (pas assez de familles indépendantes) qui ne pourvoit aucun
    siège n'écrit **aucune** marque : la désignation reste inchangée et
    l'attente est journalisée sous le verrou, une fois par `(gel, plancher)`.
    Une attente **partielle** (#3617) pourvoit les familles disponibles comme
    au gel (#3596) : la marque `waiting` nomme les sièges gardés et les
    nouveaux, seuls les nouveaux reçoivent leur demande, et la colonne garde
    tous les sièges (`_sync_reviewers` suit la marque effective). L'avis de
    capacité n'est pas envoyé : le plancher vient du pilote, l'attente est
    journalisée, et la reprise du même gel complète les sièges manquants.
    """

    def __init__(self, port, *, kept, examined=(), mark_id: int, floor: int) -> None:
        super().__init__(port)
        self.kept = [{"name": str((seat or {}).get("name") or "").strip(),
                      "family": str((seat or {}).get("family") or "").strip()}
                     for seat in kept or ()
                     if str((seat or {}).get("name") or "").strip()]
        #: les sièges du gel **vivants au début de la passe** (la marque
        #: filtrée par la colonne) : si la colonne ne les porte plus, un retrait
        #: du pilote s'est produit pendant la passe et rien ne doit le
        #: réécrire (#3367). Un siège retiré avant la passe n'en fait pas
        #: partie : c'est un retrait acquis, pas un déplacement concurrent
        #: (B/P2 cx1).
        self.examined = {str(name or "").strip() for name in examined or ()
                         if str(name or "").strip()}
        self.mark_id = int(mark_id)
        self.floor = int(floor)
        #: les sièges ajoutés par cette complétion (trace rendue à l'appelant)
        self.added: list = []

    def commit(self, lot: int, **kwargs) -> dict:
        db = self.port.db
        freeze = dict(kwargs.get("freeze") or {})
        freeze_id = int(freeze.get("id") or 0)
        since = float(freeze.get("at_ts") or 0.0)
        rows = db.query("SELECT state, reviewers FROM work_items WHERE id = %s"
                        " FOR NO KEY UPDATE", (int(lot),))
        effective = _effective_mark(db, int(lot), freeze_id, since_ts=since)
        if effective is None or int(effective[0]) != self.mark_id:
            raise _SeatsMoved(
                "the designation of freeze #%s moved during the floor completion:"
                " nothing written" % (freeze.get("id"),))
        column = {str(name or "").strip()
                  for name in ((rows[0].get("reviewers") if rows else None) or ())}
        examined = self.examined or {seat["name"] for seat in self.kept}
        gone = sorted(examined - column)
        if gone:
            raise _SeatsMoved(
                "reviewer column changed during the floor completion (%s no longer"
                " designated): nothing written" % ", ".join(gone))
        state = str(rows[0].get("state") or "") if rows else ""
        prefix = _floor_prefix(int(lot), freeze_id, self.floor)
        names = {seat["name"] for seat in self.kept}
        new = [dict(row) for row in kwargs.get("reviewers") or ()
               if str((row or {}).get("name") or "").strip() not in names]
        if kwargs.get("state") == rd.WAITING and new:
            # #3617 : attente partielle, les familles disponibles sont pourvues
            # sans attendre le retour de la capacité manquante.
            kwargs["reviewers"] = self.kept + new
            kwargs["messages"] = [message for message in kwargs.get("messages") or ()
                                  if not (message or {}).get("capacity")]
            out = super().commit(lot, **kwargs)
            self.added = [str((row or {}).get("name") or "").strip() for row in new]
            out["completed"] = True
            out["added"] = list(self.added)
            if not storage.of(db).work.events_noted(prefix, since, ids=[int(lot)]):
                storage.of(db).work.note(
                    int(lot), state,
                    "%s review floor %d partially completed the freeze #%s"
                    " designation: added %s; kept %s; %d missing: %s"
                    % (prefix, self.floor, freeze_id, ", ".join(self.added),
                       ", ".join(seat["name"] for seat in self.kept) or "none",
                       int(kwargs.get("missing") or 0),
                       str(kwargs.get("why") or "waiting")),
                    SENDER)
            return out
        if kwargs.get("state") == rd.WAITING:
            if not storage.of(db).work.events_noted(prefix, since, ids=[int(lot)]):
                storage.of(db).work.note(
                    int(lot), state,
                    "%s review floor %d not covered at freeze #%s: %s; kept: %s"
                    % (prefix, self.floor, freeze_id,
                       str(kwargs.get("why") or "waiting"),
                       ", ".join(seat["name"] for seat in self.kept) or "none"),
                    SENDER)
            return {"state": rd.WAITING,
                    "required": int(kwargs.get("required") or 0),
                    "reviewers": [], "missing": int(kwargs.get("missing") or 0),
                    "why": str(kwargs.get("why") or ""), "freeze": freeze}
        kwargs["reviewers"] = self.kept + new
        why = str(kwargs.get("why") or "")
        kwargs["why"] = ("%s; review floor %d completed at freeze #%s"
                         % (why, self.floor, freeze_id) if why
                         else "review floor %d completed at freeze #%s"
                              % (self.floor, freeze_id))
        out = super().commit(lot, **kwargs)
        self.added = [str((row or {}).get("name") or "").strip() for row in new]
        out["completed"] = True
        out["added"] = list(self.added)
        if not storage.of(db).work.events_noted(prefix, since, ids=[int(lot)]):
            storage.of(db).work.note(
                int(lot), state,
                "%s review floor %d completed the freeze #%s designation:"
                " added %s; kept %s"
                % (prefix, self.floor, freeze_id,
                   ", ".join(self.added) or "none",
                   ", ".join(seat["name"] for seat in self.kept) or "none"),
                SENDER)
        return out


def _selection(inputs: Inputs, port, *, force: bool = False,
               kept: Sequence[Mapping] = (), reported: Sequence[str] = ()) -> dict:
    """The `apply` call, shared by the hook and the recovery path.

    `kept` (#3367) : the seats a reassignment keeps (live, or verdict acquired
    on the frozen head). They count like the manual seats: only the missing
    seats are selected.

    `reported` (#4582) : les acteurs dont le verdict va être reporté sur la
    tête par le report (#2442), quand il ne couvre qu'une partie des sièges.
    Leur siège ne reçoit pas de nouvelle demande : l'avis est déjà sur la
    tête. `inputs.answered` écarte de même les avis déjà rendus sur elle."""
    if force:
        port = _ForcedPort(port)
    held = _held_manual(inputs, reported)
    return rd.apply(
        inputs.item, policy=inputs.policy.rules, paths=list(inputs.paths),
        port=port, freeze_sha=str(inputs.freeze.get("sha") or ""),
        authors=inputs.authors, candidates=inputs.candidates,
        risk=inputs.policy.risk, base=inputs.base, policy_ref=inputs.policy.ref,
        notify=inputs.notify, unknown_authors=inputs.unknown_authors,
        families=inputs.policy.families,
        already=list(inputs.manual_seats) + [dict(seat) for seat in kept or ()],
        fixers=inputs.correctors, held=held, doubts=inputs.doubts,
        required=inputs.policy.required, advances=inputs.advances)


def carried_plan(cfg, db, lot: int, sha: str) -> Optional[dict]:
    """Le plan du report pour la tête qu'on s'apprête à geler, ou None (#2487).

    Lu **avant** le gel : `verdict_carry.would_carry` ne lit que les jalons et
    le dépôt, sans écrire. Un plan qui ne reporte pas la totalité des verdicts
    en attente (`refused`) ne fait pas s'abstenir la désignation : les sièges
    manquants doivent être désignés normalement. Il est rendu quand même, car
    ses entrées `carried` disent quels sièges n'ont pas à être relancés sur la
    nouvelle tête (#4582) ; l'appelant teste `refused` avant de s'abstenir.
    Une erreur de lecture ne fait jamais perdre le gel ni la désignation :
    None, le chemin normal.
    """
    text = str(sha or "").strip()
    if not text:
        return None
    from . import verdict_carry
    try:
        plan = verdict_carry.would_carry(cfg, db, int(lot), text)
    except Exception:  # noqa: BLE001 - aucune preuve : désignation normale
        return None
    if plan.get("result") != "would-carry":
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
    while the manual names survive (`_sync_reviewers` keeps a name no mark
    carries, and now also a name the pilot re-designated after the mark that
    carried it, #3602) and a `-none` waiver is never overridden.

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
        # #3596: the retry queue reads this marker instead of re-reading the
        # policy for every manual seat; the freeze id keeps it bounded.
        text = "%s%d:%d: %s" % (MANUAL_PREFIX, lot,
                                int((freeze or {}).get("id") or 0), text)
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


def _excluded_names(inputs: Inputs) -> set[str]:
    """Les noms que la porte écarte sur la tête lue (#2812/#2888/#3836).

    La lecture de la désignation les a déjà calculés (`authors` +
    `correctors`) ; la notification des blocages levés les réutilise au lieu
    de refaire une lecture git dans la transaction du gel.
    """
    return {str(row.get("name") or "").strip().lower()
            for row in list(inputs.authors or ()) + list(inputs.correctors or ())
            if str(row.get("name") or "").strip()}


def _fresh_prepared(cfg, tx, item: Mapping, freeze: Mapping,
                    inputs: Inputs) -> Inputs | Unavailable:
    """Recolle la lecture préalable sur l'état pris sous le verrou (#3948).

    Les entrées viennent d'une lecture faite hors transaction : l'assignation,
    la cible, la frise des gels et le plancher d'avis ont pu bouger pendant ses
    ~140 s. Ils sont revérifiés ici, sous le verrou du lot ; un changement rend
    la préparation périmée et la désignation attend le rejeu de récupération
    (le gel, lui, reste écrit). Les gestes manuels (colonne `reviewers`,
    dispense `-none`) sont relus de la même façon. Le reste de la lecture
    (plage, politique, auteurs) est celui de la cible et de la frise vérifiées.
    """
    basis = dict(inputs.basis or {})
    if basis:
        lot = int(item.get("id") or 0)
        if str(item.get("assignee") or "") != str(basis.get("assignee") or ""):
            return Unavailable(
                "the reading is stale: the assignee changed during the "
                "pre-transaction reading; the designation is left to the "
                "recovery path")
        try:
            current_target = lot_class.default_base(cfg, item, inputs.repo) or ""
        except Exception:  # noqa: BLE001 - cible illisible : périmé par doute
            current_target = ""
        if str(current_target) != str(basis.get("target_declared") or ""):
            return Unavailable(
                "the reading is stale: the target changed during the "
                "pre-transaction reading (%s -> %s); the designation is left "
                "to the recovery path"
                % (basis.get("target_declared") or "?", current_target or "?"))
        expected = basis.get("latest_freeze")
        if expected is None:
            return Unavailable(
                "the reading cannot be validated: the prior freezes were "
                "unreadable when it was prepared; the designation is left to "
                "the recovery path")
        current = _latest_freeze_marker(
            tx, lot, before_id=int(freeze.get("id") or 0))
        if current is None:
            return Unavailable(
                "the reading cannot be validated: the prior freezes are "
                "unreadable under the lock; the designation is left to the "
                "recovery path")
        if list(current) != list(expected or []):
            return Unavailable(
                "the reading is stale: a freeze was written during the "
                "pre-transaction reading; the designation is left to the "
                "recovery path")
        # #3557 : un plancher posé pendant la lecture complète la désignation
        # de son côté ; la préparation, elle, a compté les avis avec l'ancien.
        # Seul un plancher qui dépasse le compte préparé manque des avis : un
        # plancher plus bas ne retire rien et laisse la lecture valable.
        floor = review_floor.apply({}, tx, lot, cfg=cfg)
        if floor.get("reviews_floor_unreadable"):
            return Unavailable(
                "the reading cannot be validated: the review floor is "
                "unreadable under the lock; the designation is left to the "
                "recovery path")
        if inputs.paths and int(floor.get("reviews_floor") or 0) > int(
                inputs.policy.required):
            return Unavailable(
                "the reading is stale: the review floor was raised during the "
                "pre-transaction reading (%s review(s) prepared, %s now "
                "required); the designation is left to the recovery path"
                % (int(inputs.policy.required),
                   int(floor.get("reviews_floor") or 0)))
    manual = _manual(tx, item)
    waiver = _waiver(item)
    # #4582 : un verdict a pu être rendu pendant la lecture préalable ; sous le
    # verrou, il est relu pour ne pas relancer un siège qui a déjà son avis.
    answered = _verdicts_on(tx, int(item.get("id") or 0),
                            str(freeze.get("sha") or ""))
    if list(manual) == list(inputs.manual) and bool(waiver) == bool(inputs.waiver):
        return dataclasses.replace(inputs, item=dict(item), freeze=dict(freeze),
                                   answered=answered)
    listing = registry.overview(tx)
    return dataclasses.replace(
        inputs, item=dict(item), freeze=dict(freeze), manual=list(manual),
        waiver=bool(waiver), answered=answered,
        manual_seats=_manual_seats(manual, listing, inputs.authors,
                                   inputs.policy.families,
                                   correctors=inputs.correctors))


def _unavailable_in_transaction(cfg, tx, item: Mapping, freeze: Mapping,
                                manual: Sequence[str], threads: list,
                                why: str, *, silent: bool = False) -> dict:
    """Journalise et notifie une désignation indisponible, sans écriture Git."""
    if manual:
        return _manual_unavailable(cfg, tx, item, freeze, manual, threads,
                                   why, silent=silent)
    if not silent:
        notify = _notify(cfg, tx, str(item.get("app") or ""),
                         registry.overview(tx))
        _note_unavailable(tx, item, freeze, why, threads, notify)
        _sync_reviewers(tx, item, freeze, [])
    return {"state": "unavailable", "reviewers": [], "freeze": dict(freeze),
            "why": why}


def _designate_in_transaction(cfg, tx, lot: int, *, milestone: Optional[Mapping],
                              threads: list, carried: Optional[Mapping] = None,
                              reading: Optional[dict] = None, target=None,
                              freezing: bool = False,
                              prepared: Optional[object] = None,
                              reported: Sequence[str] = ()) -> dict:
    """The `on_frozen` body: all writes stay in the freeze's transaction.

    `prepared` (#3948) est le résultat de `prepare_for_freeze`, lu **avant**
    l'ouverture de la transaction : `Inputs`, `Unavailable` ou
    `PrepareFailure`. Il évite de refaire dans la transaction les lectures Git
    des gels antérieurs, qui l'ont laissée inactive plus de 180 s et l'ont
    fait tuer par la garde. Absent (rejeu, appelant qui fournit son propre
    hook), la lecture se fait ici comme avant.

    `freezing` (#3742) : l'appel vient du jalon `frozen` lui-même, pas d'un
    rejeu de désignation. Après la lecture qui précède la sélection, le gel est
    refusé si les familles d'auteurs prouvés laissent moins de familles
    éligibles que d'avis requis (canon §2) ; l'exception annule le jalon avec
    la transaction. Un rejeu (`designate`) garde son comportement : il ne peut
    pas refuser un gel déjà écrit et se contente de journaliser l'attente.

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
    designation input. Seule exception (#3742) : une tête dont les familles
    d'auteurs prouvés rendent les avis requis impossibles est refusée avant
    toute écriture de désignation, avec les commits à renvoyer à l'assigné.

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
    if prepared is None:
        try:
            # no network read inside the freeze transaction (see the docstring)
            inputs = prepare(cfg, tx, int(lot), freeze=freeze, fetch=False,
                             target=target)
        except Exception as exc:  # noqa: BLE001 - recorded below, never guessed
            if manual:
                return _manual_unavailable(
                    cfg, tx, item, freeze, manual, threads,
                    "internal designation error: %s: %s"
                    % (type(exc).__name__, str(exc)[:240]))
            return _internal_failure(cfg, tx, item, freeze, threads, exc)
    elif isinstance(prepared, PrepareFailure):
        exc = prepared.error
        if manual:
            return _manual_unavailable(
                cfg, tx, item, freeze, manual, threads,
                "internal designation error: %s: %s"
                % (type(exc).__name__, str(exc)[:240]))
        return _internal_failure(cfg, tx, item, freeze, threads, exc)
    else:
        inputs = prepared
    if isinstance(inputs, Unavailable):
        return _unavailable_in_transaction(
            cfg, tx, item, freeze, manual, threads, inputs.why,
            silent=inputs.silent)
    if prepared is not None:
        # #3948 : la lecture est antérieure à la transaction ; l'état du lot
        # (assignation, cible, frise, colonne des relecteurs, dispense) est
        # revérifié sous le verrou. Une préparation périmée ne désigne pas.
        refreshed = _fresh_prepared(cfg, tx, item, freeze, inputs)
        if isinstance(refreshed, Unavailable):
            return _unavailable_in_transaction(
                cfg, tx, item, freeze, manual, threads, refreshed.why,
                silent=refreshed.silent)
        inputs = refreshed
        if inputs.unreadable:
            _note_unreadable(tx, int(lot), str(item.get("state") or ""),
                             list(inputs.unreadable))
    if freezing:
        # #3742 : refus du gel quand les familles d'auteurs prouvés
        # (correcteurs compris) laissent moins de familles éligibles que
        # d'avis requis. Avant toute écriture de désignation ; l'exception
        # annule le jalon et sa transaction. Un `-none` ou un report de
        # verdicts (#2487) sont sortis plus haut et gardent leur cours : un
        # report réutilise les avis d'une tête de contenu identique, dont les
        # auteurs ne peuvent pas avoir augmenté.
        from . import freeze_families

        freeze_families.enforce(inputs)
    # #3554 : la baisse du canon est un fait du gel, journalisée même quand la
    # couverture manuelle dispense de désigner.
    _note_canon_lowering(tx, item, freeze, inputs)
    if reading is not None:
        # #2812 : la même lecture que la désignation, réutilisée par la
        # notification des blocages levés (aucune lecture git redoublée).
        reading["excluded"] = _excluded_names(inputs)
    if _manual_covers(inputs) and not _held_manual(inputs, reported):
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
            _waive_ownless_vehicle(tx, item, inputs)
        else:
            out = _selection(inputs, pg.PgPort(tx, after_commit=threads),
                             reported=reported)
            if _manual_covers(inputs) and out.get("state") == "manual":
                # #4582 : les demandes nommaient déjà cette tête (ou le siège
                # est introuvable) : aucune marque, le journal de la
                # couverture manuelle reste l'issue du gel (comme #3596).
                out = _manual_outcome(tx, item, freeze, inputs.manual)
            if out.get("state") in (rd.DESIGNATED, rd.WAITING):
                # #3596 : une désignation partielle reste `waiting` et nomme
                # les sièges déjà pourvus ; la colonne les garde.
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


def _excluded_on_freeze(cfg, db, item: Mapping, freeze: Mapping, *,
                        fetch: bool = True) -> set[str]:
    """Les noms que la porte écarte sur cette tête gelée (#2812).

    Même lecture que la porte et la désignation (`review_authors.reading`) :
    les auteurs de la plage gelée, correcteurs de relecteur compris (ils sont
    auteurs de la tête, #2888). Le gel n'envoie donc pas l'invitation à un
    agent qui ne peut pas rendre un avis indépendant. Une lecture impossible
    rend l'ensemble vide : la notification garde son comportement historique et
    ne perd jamais un gel.

    #3836 : `fetch=False` quand la lecture tourne dans la transaction du gel —
    la notification ne doit pas y rapatrier un objet par le réseau.
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
            base=base, head=sha, target=_target_tip(repo, base, fetch=fetch),
            fetch=fetch,
            admitted_heads=review_authors.stacked_parent_heads(
                db, item, repo, sha))
    except Exception:  # noqa: BLE001 - plage illisible : comportement historique
        return set()
    return {str(row.get("name") or "").strip().lower()
            for row in list(authors or ()) + list(correctors or ())
            if str(row.get("name") or "").strip()}


def _superseded_candidates(db, item: Mapping, freeze: Mapping,
                           outcome: Mapping) -> tuple:
    """`(lot, gel, sha, candidats)` de la notification #2681, sans lecture Git.

    Un candidat est le dernier verdict d'un acteur qui a bloqué une **autre**
    tête et que la désignation du gel ne prévient pas déjà. La lecture est
    purement base de données : l'appelant décide quand la preuve d'exclusion
    (Git) est nécessaire, et ne la lance jamais sous le verrou du gel (#3948
    B cx3)."""
    lot = int((item or {}).get("id") or 0)
    freeze_id = int((freeze or {}).get("id") or 0)
    sha = str((freeze or {}).get("sha") or "").strip().lower()
    state = str((outcome or {}).get("state") or "")
    if not lot or not freeze_id or not sha or state in ("unavailable", "stale"):
        return lot, freeze_id, sha, []
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
    return lot, freeze_id, sha, candidates


def _notify_superseded_blocks(db, item: Mapping, freeze: Mapping, outcome: Mapping,
                              threads: Optional[list], *, cfg=None,
                              excluded: set[str] | None = None,
                              fetch: bool = True) -> None:
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
    le courrier historique.

    `fetch=False` (#3836) interdit le rapatriement par la forge : la
    notification tourne dans la transaction du gel et ne doit jamais y attendre
    le réseau. `threads=None` écrit le courrier dans la transaction courante
    (chemin différé après commit, #3948) ; une liste le diffère à son tour."""
    lot, freeze_id, sha, candidates = _superseded_candidates(
        db, item, freeze, outcome)
    if not candidates:
        return
    if excluded is None:
        excluded = _excluded_on_freeze(cfg, db, item, freeze, fetch=fetch)
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


def _deferred_superseded(cfg, lot: int, freeze: Mapping, result: Mapping):
    """La notification #2681 rejouée **après** le COMMIT du gel (#3948 B cx3).

    Les chemins `carried` et `waiver` de `_designate_in_transaction` sortent
    avant de renseigner la lecture d'exclusion. La notification ne doit pas
    refaire `_excluded_on_freeze` sous le verrou du gel : un lot réaligné à
    nombreux gels y relancerait `_carried_from_previous` plus de deux minutes,
    le défaut que ce lot corrige. La preuve d'exclusion est donc lue après
    commit (autocommit, aucun verrou tenu), puis la note et le courrier
    s'écrivent ensemble dans une transaction courte.
    """
    def notify(db_cfg, db):
        try:
            item = work.get(db, int(lot)) or {"id": int(lot)}
            candidates = _superseded_candidates(db, item, freeze, result)[3]
            if not candidates:
                return
            excluded = _excluded_on_freeze(cfg, db, item, freeze, fetch=False)
            with db.transaction() as tx:
                _notify_superseded_blocks(tx, item, freeze, result, None,
                                          cfg=cfg, excluded=excluded,
                                          fetch=False)
        except Exception:  # noqa: BLE001 - notification au mieux, gel gardé
            pass
    return notify


def designate_in_transaction(cfg, tx, lot: int, *, milestone: Optional[Mapping],
                             threads: list, carried: Optional[Mapping] = None,
                             target=None, freezing: bool = False,
                             prepared: Optional[object] = None,
                             reported: Sequence[str] = ()) -> dict:
    """The `on_frozen` body, plus the superseded-block notification (#2681).

    The notification is best-effort: it runs after the designation writes, and
    an error there never loses the freeze or its designation. The gate rule
    (only the frozen head's verdicts) is unchanged; this only tells a reviewer
    whose earlier block no longer blocks the new head. `freezing` (#3742) is
    passed to the body: from the `frozen` milestone, a head its author families
    make unreviewable raises before any designation write and rolls the
    milestone back; a replay of an existing freeze leaves it False.
    `prepared` (#3948) is the pre-transaction reading, handed to the body
    unchanged so no Git reading runs under the freeze's lock. Quand le corps
    sort par un chemin sans lecture préparée (`carried`, `waiver`), la
    notification #2681 est différée après le COMMIT : `_excluded_on_freeze`
    relit les gels antérieurs et ne doit jamais tourner sous le verrou du gel
    (#3948 B cx3)."""
    reading: dict = {}
    result = _designate_in_transaction(cfg, tx, lot, milestone=milestone,
                                       threads=threads, carried=carried,
                                       reading=reading, target=target,
                                       freezing=freezing, prepared=prepared,
                                       reported=reported)
    if (reading.get("excluded") is None
            and str(result.get("state") or "") not in ("unavailable", "stale")):
        # #3948 B cx3 : pas de lecture préparée sur ces chemins ; la
        # notification est au mieux et se rejoue hors transaction.
        threads.append(_deferred_superseded(cfg, int(lot),
                                            dict(milestone or {}),
                                            dict(result)))
        return result
    try:
        item = work.get(tx, int(lot)) or {"id": int(lot)}
        _notify_superseded_blocks(tx, item, milestone or {}, result, threads,
                                  cfg=cfg, excluded=reading.get("excluded"),
                                  fetch=False)
    except Exception:  # noqa: BLE001 - une notification ne perd pas un gel
        pass
    return result


def hook(cfg, outcome: Optional[dict] = None, *, carried: Optional[Mapping] = None,
         target=None, prepared: Optional[object] = None,
         reported: Sequence[str] = ()):
    """The callback installed on the CLI's `frozen` milestone.

    `outcome`, when given, receives the designation shape for the command's
    message; the callback itself has no other return channel. `carried` is the
    read-only plan of the verdict carry when it covers the head being frozen
    (#2487): the callback then keeps the reported reviewers and mails nobody.
    `target` (#3250) is prepared before opening the transaction; without it a
    branch target defers to recovery instead of trusting the clone's old ref.
    `prepared` (#3948) is the machine reading prepared before the transaction;
    the hook only writes under the lock."""
    def on_frozen(tx, milestone, threads):
        result = designate_in_transaction(cfg, tx, int(milestone["work_item_id"]),
                                          milestone=milestone, threads=threads,
                                          carried=carried, target=target,
                                          freezing=True, prepared=prepared,
                                          reported=reported)
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

    #3557 : quand le plancher d'avis du lot dépasse le compte enregistré pour
    le dernier gel, la désignation de ce gel est **complétée** — les sièges
    déjà désignés sont gardés, les manquants sont choisis par la même sélection
    déterministe et notifiés, et une marque plus récente porte le nouveau
    compte. `work reviewers --apply` et `work reviewer --floor` passent ici.
    Une attente (pas assez de familles) ne retire aucun siège : la marque et la
    colonne restent la désignation valide, l'attente est journalisée. Une
    attente partielle (#3617) pourvoit déjà les familles disponibles : une
    marque `waiting` nomme les sièges gardés et les nouveaux.
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
    # #3557 : un plancher posé après le gel relève le besoin ; la désignation
    # enregistrée doit être **complétée** pour ce même gel, pas rejouée telle
    # quelle. #3592 : un journal illisible n'est pas un plancher nul — une
    # marque enregistrée n'est pas rendue comme si un plancher de 0 la
    # couvrait ; la complétion rapporte l'échec et n'écrit rien (marque et
    # colonne restent la désignation valide du gel).
    floor_error = ""
    try:
        floor = _floor_of(db, int(lot), cfg)
    except Exception as exc:  # noqa: BLE001 - fail closed, never guess 0
        floor_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        floor = 0
    if floor_error and force:
        return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                "why": ("review floor unreadable: %s; the designation of "
                        "freeze #%s is left untouched"
                        % (floor_error, freeze.get("id")))}
    # the live reviewer column, the same reading `_kept_seats` and the port
    # use: a seat the pilot removed is not part of the designation any more
    live = _live_column(item)
    completion = None
    waiting = False
    reported: list = []
    if not force:
        effective = _effective_mark(db, int(lot), int(freeze.get("id") or 0),
                                    since_ts=float(freeze.get("at_ts") or 0.0))
        waiting = (effective is not None and effective[1] != "carried"
                   and effective[2].get("state") == rd.WAITING)
        if effective is not None:
            # #2487 : la dernière marque du gel fait foi, sélection ou report.
            # Une sélection `waiting` est retentée : la capacité peut être
            # revenue, et la même marque devient `designated`.
            if floor_error:
                # #3592 : ne pas rendre la marque ; `prepare` relit le
                # plancher et échoue fermé, la branche « complétion » rend
                # `unavailable` sans écrire.
                shape = (_carried_shape(effective[2])
                         if effective[1] == "carried" else dict(effective[2]))
                completion = {"mark_id": int(effective[0]), "shape": shape}
            elif effective[1] == "carried":
                carried = _carried_shape(effective[2])
                if _mark_covers(carried, floor, column=live):
                    return carried
                completion = {"mark_id": int(effective[0]), "shape": carried}
            elif effective[2].get("state") == rd.DESIGNATED:
                existing = dict(effective[2])
                if _mark_covers(existing, floor, column=live):
                    return existing
                # #3557 : le plancher dépasse le compte enregistré. Les sièges
                # de la marque sont gardés ; seuls les manquants seront choisis
                # par la même sélection déterministe (famille opposée, charge,
                # disponibilité) et notifiés.
                completion = {"mark_id": int(effective[0]), "shape": existing}
        if completion is None and not floor_error:
            plan = carried_plan(cfg, db, int(lot), str(freeze.get("sha") or ""))
            # #4582 : un plan partiel ne couvre pas la tête — la désignation
            # réelle doit tourner — mais ses verdicts reportés ne sont pas
            # relancés sur elle.
            reported = [str((entry or {}).get("actor") or "").strip()
                        for entry in (plan or {}).get("carried") or ()
                        if str((entry or {}).get("actor") or "").strip()]
            if plan and not plan.get("refused"):
                with db.transaction() as tx:
                    if not _lock_current_freeze(tx, int(lot), freeze):
                        return _stale(freeze)
                    return _carried_in_transaction(tx, item, freeze, plan)
    threads = _threads()
    checkpoint = len(threads)
    try:
        inputs = prepare(cfg, db, int(lot), freeze=freeze)
        if isinstance(inputs, Unavailable):
            if completion is not None:
                # #3557 : une complétion qui ne peut pas lire ses entrées ne
                # touche à rien. La marque et la colonne restent la
                # désignation valide du gel ; le plancher reste posé et un
                # `--apply` ultérieur réessaiera.
                return {"state": "unavailable", "reviewers": [],
                        "freeze": freeze,
                        "why": "review floor completion: %s" % inputs.why}
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
        # #3554 : même journal hors du hook (reprise), une fois par tête.
        _note_canon_lowering(db, item, freeze, inputs)
        if _manual_covers(inputs) and not _held_manual(inputs, reported):
            # #3596 : la couverture manuelle finale est marquée au journal
            # (`MANUAL_PREFIX`) pour que la boucle de reprise ne relise pas la
            # politique à chaque passe.
            return _manual_outcome(db, item, freeze, inputs.manual)
        if completion is not None:
            # #3557 : la désignation du gel est complétée, jamais remplacée.
            if not inputs.paths or inputs.policy.required == 0:
                return dict(completion["shape"])
            # Le snapshot **vivant** du début de passe : seuls les sièges du
            # gel encore présents dans la colonne sont examinés et gardés. Un
            # siège retiré à la main **avant** la passe n'est ni réexaminé
            # (sinon chaque essai refuse en `stale`, B/P2 cx1) ni choisi de
            # nouveau comme candidat (sinon la complétion réinscrit le retrait
            # sans courrier, B/P1 dev4) : il est exclu, et l'attente est
            # journalisée si la couverture manque.
            live_seats = _kept_seats(completion["shape"], item)
            live_names = {seat["name"] for seat in live_seats}
            mark_names = {str((row or {}).get("name") or "").strip()
                          for row in (completion["shape"] or {}).get("reviewers") or ()}
            mark_names.discard("")
            removed = mark_names - live_names
            seats = rd.manual_seats(
                list(inputs.manual_seats) + live_seats,
                authors=inputs.authors, fixers=inputs.correctors,
                families=inputs.policy.families)
            kept = [seat for seat in seats if seat["name"] in live_names]
            if len(seats) >= int(inputs.policy.required):
                # the live seats already cover the raised floor: nothing to
                # designate, no mail, no new mark
                return dict(completion["shape"])
            if removed:
                inputs = dataclasses.replace(
                    inputs,
                    candidates=[candidate for candidate in inputs.candidates
                                if str(getattr(candidate, "name", "") or "").strip()
                                not in removed])
            with db.transaction() as tx:
                port = _FloorPort(
                    pg.PgPort(tx, after_commit=threads, at_freeze=False),
                    kept=kept, examined=live_names,
                    mark_id=int(completion["mark_id"]), floor=floor)
                out = _selection(inputs, port, kept=port.kept)
                if out.get("state") == rd.DESIGNATED or out.get("completed"):
                    _sync_reviewers(tx, item, freeze,
                                    [r.get("name") for r in out.get("reviewers") or ()])
            _drain(db, threads)
            return out
        removed = set()
        if waiting and live is not None:
            # #3617 : la reprise d'une attente (partielle ou non) ne réinscrit
            # pas un siège de CE gel que le pilote a retiré de la colonne,
            # comme la complétion du plancher (B/P1 dev4) : après une attente
            # partielle écrite par le plancher, la reprise passe ici. #3661 :
            # ni comme candidat, ni comme siège acquis de la marque `waiting`
            # (le port la lit sans eux, `retired`) ; `_sync_reviewers` ne le
            # réécrit sur aucun chemin.
            removed = _removed_seats(db, int(lot), freeze, live)
            if removed:
                inputs = dataclasses.replace(
                    inputs,
                    candidates=[candidate for candidate in inputs.candidates
                                if str(getattr(candidate, "name", "") or "").strip()
                                not in removed])
        if not inputs.paths or inputs.policy.required == 0:
            why = LIGHT_WHY if inputs.paths else NO_PATH_WHY
            with db.transaction() as tx:
                if not _lock_current_freeze(tx, int(lot), freeze):
                    return _stale(freeze)
                _note_none(tx, item, freeze, why)
                _sync_reviewers(tx, item, freeze, [])
                _waive_ownless_vehicle(tx, item, inputs)
            return {"state": rd.NONE, "required": 0, "reviewers": [],
                    "freeze": freeze, "why": why}
        with db.transaction() as tx:
            out = _selection(inputs, pg.PgPort(tx, after_commit=threads,
                                                at_freeze=False, retired=removed),
                             force=force, reported=reported)
            if _manual_covers(inputs) and out.get("state") == "manual":
                # #4582 : couverture manuelle complète sans nouvelle demande
                # (les courriers nomment déjà cette tête) : le journal #3596
                # reste l'issue du gel, comme au hook.
                out = _manual_outcome(tx, item, freeze, inputs.manual)
            names = ([r.get("name") for r in out.get("reviewers") or ()]
                     if out.get("state") in (rd.DESIGNATED, rd.WAITING) else [])
            _sync_reviewers(tx, item, freeze, names)
            try:
                _notify_superseded_blocks(tx, item, freeze, out, threads,
                                          excluded=_excluded_names(inputs),
                                          fetch=False)
            except Exception:  # noqa: BLE001 - une notification ne perd rien
                pass
        _drain(db, threads)
        return out
    except (rd.StaleFreeze, _SeatsMoved) as exc:
        # The port or the column guard refused an old freeze before any
        # write: never clear or re-register the newer head's reviewers.
        del threads[checkpoint:]
        return _stale(freeze, str(exc))
    except rd.DesignationError as exc:
        del threads[checkpoint:]
        if completion is not None:
            # #3557 : a failed completion never clears the valid designation
            # of the freeze (unlike a first designation, which admits nothing).
            return {"state": "unavailable", "reviewers": [], "freeze": freeze,
                    "why": "review floor completion: %s" % exc}
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


def _replay_designation(cfg, db, lot: int, freeze: Mapping, item: Mapping,
                        effective: Optional[tuple]) -> tuple[dict, str]:
    """The `--apply` replay, never echoing a seat that is no longer live.

    #3685 : the recorded mark is returned as it is only when every seat it
    names is still in the reviewer column and reachable (or its verdict is
    acquired on the frozen head). A seat the pilot removed is left out of the
    recovery, which completes the live designation; a seat whose host is lost
    is moved by the same `reseat` the runner pass uses, keeping the live seats.
    Returns `(applied, historical_reason)`; the reason is empty when the mark
    still describes the live designation.
    """
    dead: list = []
    kept: list = []
    removed: list = []
    if (effective is not None and effective[1] == "designation"
            and str((effective[2] or {}).get("state") or "") == rd.DESIGNATED):
        live = _live_column(item)
        if live is not None:
            try:
                by_name = _reviewer_rows(
                    storage.of(db).agents.reachability())
                closed = alert_briefs.claims_closed_hosts(db)
                dead, kept = _dead_seats(cfg, db, int(lot), freeze,
                                         effective[2], sorted(live), by_name,
                                         closed)
            except Exception as exc:  # noqa: BLE001 - unreadable evidence
                # #3685 (B2) : on ne peut pas dire quels sièges sont morts.
                # Poursuivre `designate` afficherait la marque enregistrée
                # comme vivante, avec son siège mort : on rend `unavailable`
                # sans rien écrire ni affirmer de siège vivant.
                return ({"state": "unavailable", "reviewers": [],
                         "freeze": freeze,
                         "why": "reviewer evidence unreadable: %s: %s"
                                % (type(exc).__name__, str(exc)[:200])}, "")
            removed = _removed_seats_from_mark(effective[2], live)
    if dead:
        reason = ("freeze #%s named unreachable reviewer(s): %s"
                  % (freeze.get("id"),
                     ", ".join("%s (%s)" % (name, why) for name, why in dead)))
        if removed:
            reason += ("; removed by the pilot: %s" % ", ".join(removed))
        # #3685 (B1) : un siège retiré par le pilote reste exclu de la
        # sélection, même quand un autre siège de la marque est mort : la
        # branche `reseat` ne doit pas le réinscrire.
        return reseat(cfg, db, int(lot), freeze=freeze, kept=kept, dead=dead,
                      mark_id=int(effective[0]), excluded=removed), reason
    reason = ""
    if removed:
        reason = ("freeze #%s named reviewer(s) removed by the pilot: %s"
                  % (freeze.get("id"), ", ".join(removed)))
    applied = designate(cfg, db, int(lot), freeze=freeze)
    # A live set that already covers the need can leave the recorded mark
    # untouched: never display one of its removed names as freshly designated.
    if isinstance(applied, Mapping) and applied.get("state") == rd.DESIGNATED:
        fresh = _live_column(work.get(db, int(lot)) or {})
        if fresh is not None:
            seats = [seat for seat in applied.get("reviewers") or ()
                     if str((seat or {}).get("name") or "").strip() in fresh]
            if len(seats) != len(applied.get("reviewers") or ()):
                applied = dict(applied)
                applied["reviewers"] = seats
                if not reason:
                    reason = ("freeze #%s named reviewer(s) no longer in the"
                              " reviewer column" % (freeze.get("id"),))
    return applied, reason


def reviewers(cfg, db, lot: int, *, apply: bool = False) -> dict:
    """Read the recorded designation of the latest freeze, optionally retrying it."""
    item = work.get(db, int(lot))
    if item is None:
        raise work.WorkError("lot %s not found" % int(lot))
    rows = storage.of(db).work.milestones(int(lot), pg.MILESTONES)
    row = plan_git.latest_freeze(rows)
    out = {"lot": int(lot), "freeze": None, "designation": None,
           "manual": _manual(db, item), "applied": None, "historical": ""}
    if row is None:
        return out
    freeze = {"id": int(row["id"]), "sha": str(row.get("sha") or "").strip(),
              "at_ts": row.get("at_ts"), "actor": row.get("actor") or ""}
    out["freeze"] = freeze
    marks = _marks(db, int(lot), freeze_id=int(freeze["id"]),
                   since_ts=float(freeze.get("at_ts") or 0.0))
    effective = None
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
        applied, reason = _replay_designation(cfg, db, int(lot), freeze, item,
                                              effective)
        out["applied"] = applied
        out["historical"] = reason
    return out


def _retry_resolved(db, lot: int, freeze: Mapping) -> bool:
    """True when this freeze already has a final outcome.

    A `none` note, a manual designation whose coverage is journaled
    (`MANUAL_PREFIX`, #3596), a carried/designated mark or a light outcome is
    final. A **manual seat alone** is not trusted as final: the retry reads
    the coverage once, and `designate` either writes the manual marker (it
    covers) or provides the missing automatic seat beside it. A `waiting`
    mark is not final: capacity or evidence may have returned, which is
    exactly what a retry is for (#1617).
    """
    freeze_id = int((freeze or {}).get("id") or 0)
    since = float((freeze or {}).get("at_ts") or 0.0)
    for prefix in (NONE_PREFIX, MANUAL_PREFIX):
        try:
            if storage.of(db).work.events_noted(
                    "%s%d:%d:" % (prefix, int(lot), freeze_id), since,
                    ids=[int(lot)]):
                return True
        except Exception:  # noqa: BLE001 - unreadable journal: retry the freeze
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
    state = str((effective[2] or {}).get("state") or "")
    return state in (rd.DESIGNATED, rd.NONE)


def _cursor_path(cfg, name: str) -> str:
    """A cursor file, under the host state directory (never a repo)."""
    state = str(getattr(cfg, "state_dir", "") or "").strip()
    return os.path.join(state, name) if state else ""


def _read_cursor(cfg, name: str) -> int:
    """The last id examined for this cursor, or 0 when absent/unreadable."""
    path = _cursor_path(cfg, name)
    if not path:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = int(str(handle.read(64)).strip() or 0)
    except (OSError, ValueError):
        return 0
    return max(0, value)


def _write_cursor(cfg, name: str, cursor: int) -> None:
    """Atomically records the cursor; a state problem never fails the pass."""
    path = _cursor_path(cfg, name)
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
        f"   AND w.state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
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
    wraps at the end. A resolved lot (manual coverage journaled, `none`,
    carried or designated) is skipped without consuming an attempt, and a note
    that no longer matches the lot's latest freeze is skipped too; neither can
    hold a slot forever. A blocked or unavailable lot itself does not vanish:
    it is seen again on the
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
    cursor = _read_cursor(cfg, RETRY_CURSOR)
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
        _write_cursor(cfg, RETRY_CURSOR, last)
    out["summary"] = ("%d attempted: %d designated, %d waiting, %d none,"
                      " %d unavailable, %d manual, %d stale, %d skipped"
                      % (attempted, out["designated"], out["waiting"],
                         out["none"], out["unavailable"], out["manual"],
                         out["stale"], out["skipped"]))
    return out


#: #3328 : journal prefix of a seat reattributed because its reviewer became
#: unreachable (host closed, stopped/dead, absent). The examined names are in
#: the prefix, so a later loss of the replacement is a new reassignment while a
#: pass that could not replace the same names never loops.
REASSIGN_PREFIX = "review-designation-reassign:"
#: #3328 : the bounded scan of lots whose automatic designation is examined.
REASSIGN_SCAN = 200
#: #3328 : the reassignment scan's cursor, under the host state directory. A
#: pass resumes after the last lot examined and wraps once at the end, so a
#: backlog of non-actionable seats cannot pin the bounded window.
REASSIGN_CURSOR = "review-designation-reassign.cursor"
#: #3685 : journal prefix of a reassignment attempt that could not conclude
#: (frozen head unreadable, policy or target read failure). A transient
#: failure must not consume the `(freeze, names)` mark: the next pass retries.
REASSIGN_RETRY_PREFIX = "review-designation-reassign-retry:"
#: #3685 : transient failures journaled and retried before the seat is left to
#: the pilot (the final attempt consumes the mark, one explicit note).
REASSIGN_RETRY_MAX = 3


def _unreachable_reviewer(row: Optional[Mapping], closed_hosts: set) -> str:
    """Why a designated reviewer will not read its request, or "" (#3328).

    The evidence is the one agent-mail uses for a lost recipient
    (`alert_briefs._unavailable`, #3036): the host's claims are closed, or the
    agent is stopped/dead/blocked or no longer run by the executor. A missing
    lease is deliberately not read: an idle reviewer is woken by the request
    mail. A budget/operator pause is not read either — the agent comes back and
    its seat is not lost. #4491: a `blocked` pause with a **named long cause**
    (authentication, exhausted provider credits, host not ready, account
    block) and an `auth_blocked` text are lost seats, like a closed host, and
    are handed to `reseat` (dropped when the kept seats cover the reviews,
    otherwise replaced by an available agent). The account's short-window
    pause (#3876) and the end-of-turn failure marker / `429` episode (#1268,
    #1270, a minute-scale retry) stay live seats: `is_blocked_pause` reads the
    named causes, not the bare `blocked` status (auditeur's B1 on #4491).
    """
    if row is None:
        return "absent from the registry"
    # #3460 : un agent retiré ne reçoit plus de courrier, son siège est mort
    if rd.is_retired(row):
        return "retired"
    if (row.get("mode") or "execute") != "execute":
        return "mode %s" % (row.get("mode"),)
    status = str(row.get("status") or "").strip().lower()
    # #3415 : le même ensemble que la désignation (`NON_LIVE_STATUS`), pour
    # qu'un statut non vivant ne soit ni désigné ni laissé en siège mort.
    if status in rd.NON_LIVE_STATUS:
        return "status %s" % status
    text = str(row.get("status_text") or "").strip().lower()
    # #4491 : le préfixe est nommé, comme pour « accounts : » (#3973), même
    # quand le statut n'a pas encore été rafraîchi en `blocked`.
    if text.startswith(rd.AUTH_BLOCKED_PREFIX):
        return "authentication blocked"
    if rd.is_blocked_pause(row):
        return "status blocked"
    host = str(row.get("host") or "").strip()
    if host and host in closed_hosts:
        return "host %s: claims closed" % host
    return ""


def _reviewer_rows(listing: Iterable[Mapping]) -> dict:
    """The registry rows by canonical name, aliases included (#3328).

    An alias resolves to the current row: a renamed agent is still reachable
    at its new host, and its old name in an old mark must not look dead."""
    by_name: dict = {}
    for row in listing or ():
        row = dict(row or {})
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        by_name.setdefault(name, row)
        aliases = row.get("aliases") or ()
        if isinstance(aliases, str):
            aliases = [aliases]
        try:
            for alias in aliases:
                alias = str(alias or "").strip()
                if alias:
                    by_name.setdefault(alias, row)
        except TypeError:
            continue
    return by_name


def _exact_receipts_required(cfg, db, lot: int, sha: str) -> bool:
    """Whether the merge gate counts only verdicts with their exact receipt (#3369).

    In a **sensitive** class the gate filters out a verdict without its #1465
    proof (`merge_queue.review_gate`), so that verdict cannot keep a seat: the
    seat would stay dead and the head would wait for an opinion the gate will
    never read. The class is the one the gate reads (`lot_class.of_lot`): a
    doubt and an unreadable class are sensitive, exactly like the gate.
    """
    try:
        item = work.get(db, int(lot))
        reading = lot_class.of_lot(db, int(lot), cfg=cfg, item=item,
                                   head=str(sha or "").strip())
    except Exception:  # noqa: BLE001 - an unreadable class is a doubt: sensitive
        return True
    if not isinstance(reading, Mapping) or reading.get("doubt"):
        return True
    return str(reading.get("class") or "") == lot_class.SENSITIVE


def _head_voters(cfg, db, lot: int, sha: str, by_name: Mapping) -> set:
    """The names whose verdict names the exact frozen head, case-folded (#3367).

    A seat whose reviewer already gave its verdict (`ok` or `blocked`) on the
    frozen head is not lost when its host closes: the gate reads that verdict,
    and a replacement would only owe a second, useless one. Only a verdict on
    that exact commit counts (the full id, or an abbreviation of at least 7
    characters): a verdict on an earlier head, or without a commit, leaves the
    seat to replace. An actor known by an alias also counts under its
    registry name.

    #3369: in a sensitive class the seat is kept only on a verdict the gate
    will actually count, i.e. one carrying its exact #1465 receipt (the gate
    discards an unproven one). Keeping such a seat would leave a dead seat
    that is never replaced.
    """
    head = str(sha or "").strip().lower()
    voters: set = set()
    if not head:
        return voters
    rows = []
    for row in storage.of(db).work.milestones(int(lot), None) or ():
        if row.get("kind") != "verdict":
            continue
        noted = str(row.get("sha") or "").strip().lower()
        if not noted or not (noted == head
                             or (len(noted) >= 7 and head.startswith(noted))):
            continue
        if not str(row.get("actor") or "").strip():
            continue
        rows.append(row)
    if not rows:
        return voters
    from .merge_queue import _exact_verdict_receipt
    exact = _exact_receipts_required(cfg, db, int(lot), head)
    for row in rows:
        actor = str(row.get("actor") or "").strip()
        if exact and not actor.startswith(work.HUMAN_PREFIX) \
                and _exact_verdict_receipt(db, row) is None:
            continue
        voters.add(actor.casefold())
        primary = str((by_name.get(actor) or {}).get("name") or "").strip()
        if primary:
            voters.add(primary.casefold())
    return voters


def _acquired(name: str, voters: set, by_name: Mapping) -> bool:
    """Whether `name` (or its registry name) is among the head's voters."""
    if name.casefold() in voters:
        return True
    primary = str((by_name.get(name) or {}).get("name") or "").strip()
    return bool(primary) and primary.casefold() in voters


def _dead_seats(cfg, db, lot: int, freeze: Mapping, payload: Mapping,
                column: Sequence[str], by_name: Mapping,
                closed: set) -> tuple[list, list]:
    """The dead seats of a designation mark, and the ones kept (#3685).

    Only a seat still present in the reviewer column is examined: one the
    pilot removed is history, not a live designation to move. A seat whose
    reviewer is unreachable but has already voted on the exact frozen head is
    kept (#3367). Returns `(dead, kept)`, `dead` being `[(name, why)]` and
    `kept` `[{"name", "family"}]`."""
    live = {str(name or "").strip() for name in column or ()
            if str(name or "").strip()}
    dead: list = []
    kept: list = []
    voters = None
    for reviewer in (payload or {}).get("reviewers") or ():
        name = str((reviewer or {}).get("name") or "").strip()
        if not name or name not in live:
            continue
        why = _unreachable_reviewer(by_name.get(name), closed)
        if why:
            if voters is None:
                voters = _head_voters(cfg, db, int(lot),
                                      str((freeze or {}).get("sha") or ""),
                                      by_name)
            if not _acquired(name, voters, by_name):
                dead.append((name, why))
                continue
        kept.append({"name": name,
                     "family": str((reviewer or {}).get("family") or "").strip()})
    return dead, kept


def _removed_seats_from_mark(payload: Mapping, column: Sequence[str]) -> list:
    """The seats a designation mark names but the pilot removed (#3685).

    The live column is authoritative: a name the mark still carries but the
    column no longer does is history, and a recovery must not choose it again
    — even when another seat of the same mark is dead and moves by `reseat`.
    """
    live = {str(name or "").strip() for name in column or ()
            if str(name or "").strip()}
    recorded = {str((row or {}).get("name") or "").strip()
                for row in (payload or {}).get("reviewers") or ()}
    recorded.discard("")
    return sorted(recorded - live)


def _unreachable_lots(db, *, after: int, hosts: Sequence[str],
                      scan: int = REASSIGN_SCAN) -> list:
    """Open lots whose reviewer column names an unreachable agent (#3328).

    The filter runs **before** the bounded window: 200 older healthy lots
    cannot hide the 201st whose seat is lost. Each name of the column is
    unnested, so a name absent from the registry is seen even beside a live
    one — the former `r.name = ANY(w.reviewers)` join kept the live row and
    never produced the missing one. A name is reachable when a registry row
    (or one of its aliases) is `execute`, not `stopped`/`dead`/`blocked` and
    not under an `auth_blocked` text (#4491), on a host whose claims are open.

    The window is ordered by lot id and starts after `after`; the caller
    wraps to 0 at the end and persists the last examined id, so a backlog of
    rows that need no action cannot starve the rows behind it.
    """
    host_sql = ""
    params: list = [int(after)]
    hosts = [str(host).strip() for host in hosts or () if str(host).strip()]
    if hosts:
        host_sql = (" AND r.host NOT IN (%s)"
                    % ", ".join(["%s"] * len(hosts)))
        params.extend(hosts)
    params.append(int(scan))
    return db.query(
        "SELECT w.id, w.state, w.reviewers FROM work_items w"
        f" WHERE w.state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
        "   AND w.reviewers IS NOT NULL AND cardinality(w.reviewers) > 0"
        "   AND w.id > %s"
        "   AND EXISTS ("
        "     SELECT 1 FROM unnest(w.reviewers) AS rv(name)"
        "     WHERE NOT EXISTS ("
        "       SELECT 1 FROM agent_registry r"
        "       WHERE (r.name = rv.name"
        "              OR rv.name = ANY(coalesce(r.aliases, '{}'::text[])))"
        "         AND r.status NOT IN ('stopped', 'dead', 'blocked')"
        "         AND coalesce(r.status_text, '') NOT LIKE 'auth_blocked%%'"
        "         AND r.mode = 'execute'"
        + host_sql +
        "     )"
        "   )"
        " ORDER BY w.id ASC LIMIT %s", tuple(params))


#: #3367 : the journal reason of a reseat whose kept seats cover every opinion
COVERED_WHY = ("unreachable seat(s) dropped: the kept seats cover the required"
               " review(s)")
#: #4491 : the journal reason of a reseat whose dead seat is dropped because
#: the merge gate is already open on the frozen head (the two families of the
#: report had rendered their `ok`; only the blocked seat stayed in the
#: designation). Removing it does not withdraw an opinion and sends no mail.
GATE_COVERED_WHY = ("unreachable seat(s) dropped: the required reviews are already"
                    " ok on the frozen head")


class _SeatsMoved(Exception):
    """#3367 : the mark the pass read is no longer the freeze's effective mark."""


class _AlreadyNoted(Exception):
    """#3369 : a concurrent pass already journaled this reassignment."""


class _UnreadableInputs(Exception):
    """#3631 : a read the reassignment pass needs could not be made.

    The message names the read (`agents.reachability`, `work_items.unreachable`
    …, so the runner log says WHICH query lost the pass) and carries the
    driver's statement detail when the failure is a statement timeout.
    """


def _named_read(name: str, call):
    """Run `call`, naming the read on failure (`_UnreadableInputs`)."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the caller reports and skips
        raise _UnreadableInputs(
            "%s: %s" % (name, db_mod.timeout_detail(exc))) from exc


def _reassign_prefix(lot: int, freeze_id: int, dead: Sequence[tuple]) -> str:
    """The journal prefix that makes one reassignment pass idempotent (#3328).

    It names the examined dead seats, so a later loss of a replacement is a
    new reassignment while a pass that cannot replace the same names never
    loops.
    """
    return "%s%d:%d:%s:" % (REASSIGN_PREFIX, int(lot), int(freeze_id),
                            ",".join(sorted(name for name, _why in dead or ())))


def _reassign_note(lot: int, freeze_id: int, dead: Sequence[tuple], tail: str,
                   kept: Sequence[str] = ()) -> str:
    """The journal note of a reassignment, shared by every outcome (#3369)."""
    why_text = "; ".join("%s (%s)" % (name, why) for name, why in dead or ())
    if kept:
        tail += "; kept: %s" % ", ".join(kept)
    return ("%s reviewer(s) unreachable: %s; %s"
            % (_reassign_prefix(lot, freeze_id, dead), why_text, tail))


class _ReseatPort(_ForcedPort):
    """#3367 : the port of a reassignment, which replaces only the dead seats.

    `kept` (live seats, and seats whose verdict is acquired on the frozen head)
    stays in the written mark, before the replacements, and receives no new
    mail. Under the lock of the lot row, the effective mark must still be the
    one the pass read (`mark_id`): a concurrent pass (another host) that
    already moved the seats makes this one write nothing. Every seat the pass
    examined (kept and dead) must also still be in the reviewer column: a
    pilot's `work reviewer --remove` writes no mark, and `prepare` can take
    seconds, so a removal in that window would otherwise be written back
    (review of impl-02 on #3367). #3685: a seat the mark names but the pilot
    removed (`excluded`) is never selected again; a selection that still
    names it after a concurrent removal refuses the write instead of
    resurrecting the seat. Without a
    replacement (`waiting`), no **designation** mark is written: a waiting mark
    would retire the live seats from the column, so the designation stays as
    it is. The journal note of that waiting outcome is written under this same
    lock (#3369), before the caller alerts the pilot, so a second host's
    concurrent pass sees it and sends nothing.
    """

    def __init__(self, port, *, kept: Sequence[Mapping], mark_id: int,
                 dead: Sequence[str] = (),
                 excluded: Sequence[str] = ()) -> None:
        super().__init__(port)
        self.kept = [{"name": str((seat or {}).get("name") or "").strip(),
                      "family": str((seat or {}).get("family") or "").strip()}
                     for seat in kept or () if str((seat or {}).get("name") or "").strip()]
        self.mark_id = int(mark_id)
        self.seats = ({seat["name"] for seat in self.kept}
                      | {str(name or "").strip() for name in dead or ()
                         if str(name or "").strip()})
        self.excluded = {str(name or "").strip() for name in excluded or ()
                         if str(name or "").strip()}

    def commit(self, lot: int, **kwargs) -> dict:
        db = self.port.db
        freeze = dict(kwargs.get("freeze") or {})
        rows = db.query("SELECT reviewers FROM work_items WHERE id = %s"
                        " FOR NO KEY UPDATE", (int(lot),))
        effective = _effective_mark(db, int(lot), int(freeze.get("id") or 0))
        if effective is None or int(effective[0]) != self.mark_id:
            raise _SeatsMoved("the designation of freeze #%s moved during the pass:"
                              " nothing written" % (freeze.get("id"),))
        column = {str(name or "").strip()
                  for name in ((rows[0].get("reviewers") if rows else None) or ())}
        gone = sorted(self.seats - column)
        if gone:
            raise _SeatsMoved("reviewer column changed during the pass (%s no longer"
                              " designated): nothing written" % ", ".join(gone))
        if kwargs.get("state") == rd.WAITING:
            return {"state": rd.WAITING, "required": int(kwargs.get("required") or 0),
                    "reviewers": [], "missing": int(kwargs.get("missing") or 0),
                    "why": str(kwargs.get("why") or ""), "freeze": freeze}
        names = {seat["name"] for seat in self.kept}
        kwargs["reviewers"] = self.kept + [
            dict(row) for row in kwargs.get("reviewers") or ()
            if str((row or {}).get("name") or "").strip() not in names]
        chosen = {str((row or {}).get("name") or "").strip()
                  for row in kwargs.get("reviewers") or ()}
        clash = sorted(self.excluded & chosen)
        if clash:
            raise _SeatsMoved("reviewer column changed during the pass (%s removed)"
                              " nothing written" % ", ".join(clash))
        return super().commit(lot, **kwargs)


def _reseat_alert(db, item: Mapping, freeze: Mapping, notify: str,
                  kept: Sequence[Mapping], dead: Sequence[tuple], out: Mapping,
                  threads: list) -> None:
    """Tells the pilot that a lost seat has no replacement (#3367)."""
    if not notify or registry.get(db, notify) is None:
        return
    from . import mail

    lot = int((item or {}).get("id") or 0)
    body = "\n".join([
        "Unreachable reviewer seat without a replacement (canon §2, #3328/#3367).",
        "",
        "Lot: #%s \"%s\"" % (lot, " ".join(str((item or {}).get("title") or "").split())),
        "- Freeze: frozen milestone %s." % _freeze_label(freeze),
        "- Unreachable: %s." % "; ".join("%s (%s)" % (name, why) for name, why in dead),
        "- Kept: %s." % (", ".join(seat["name"] for seat in kept) or "none"),
        "- No replacement: %s" % (out.get("why") or "no eligible reviewer"),
        "",
        "The designation is unchanged: the unreachable reviewer keeps its request",
        "and reviews when its host comes back. To move the seat now:",
        "  ameesh work reviewer %d <agent>" % lot,
    ])
    mail.send(db, SENDER, notify, body, work_item_id=str(lot),
              payload={"review_designation": {
                  "lot": lot, "freeze": int((freeze or {}).get("id") or 0),
                  "sha": str((freeze or {}).get("sha") or ""),
                  "state": rd.WAITING, "reassign": True}},
              after_commit=threads)


def _gate_open(cfg, db, lot: int, freeze: Mapping) -> bool:
    """#4491: is the merge gate already open on this frozen head?

    The report's two lots had their two independent `ok` on the head while the
    only remaining seat was a blocked reviewer: the seat no longer held a
    review, it only kept `progress` in "waiting: verdict by <blocked>". The
    gate is the reference for that fact — it proves the opinions independent,
    with their exact receipts (#1468/#2928) — so this helper reuses it instead
    of re-reading verdicts locally. An unreadable gate is `False`: the caller
    replaces the seat as before, never drops it on a doubt.
    """
    from . import merge_queue
    try:
        item = work.get(db, int(lot))
        if not item:
            return False
        marks = storage.of(db).work.milestones(int(lot), None) or ()
        gate = merge_queue.review_gate_for_item(db, cfg, item, marks)
    except Exception:  # noqa: BLE001 - an unreadable gate is not an admission
        return False
    return bool(isinstance(gate, Mapping) and gate.get("ok"))


def reseat(cfg, db, lot: int, *, freeze: Mapping, kept: Sequence[Mapping],
           dead: Sequence[tuple], mark_id: int,
           excluded: Sequence[str] = ()) -> dict:
    """Replaces only the dead seats of a freeze's designation (#3367).

    `reassign_unreachable` used to replay the whole designation (`force`): a
    live reviewer could lose its seat to a less loaded agent of its family, and
    a seat whose verdict was already acquired on the frozen head was handed to
    someone else. Here the `kept` seats count like manual seats, only the
    missing ones are selected and mailed, and the new mark names the kept
    seats then the replacements. When the kept seats already cover the
    required opinions, the dead seats are simply dropped (no mail). #4491: the
    same drop happens when the **merge gate is already open** on the head —
    the missing opinions were rendered by other agents, and the blocked seat
    must not keep the lot waiting; this case sends no mail either.

    #3685: `excluded` names the seats the mark records but the pilot removed.
    They are never candidates again, even when the same mark has a dead seat to
    move (#3328/#3367); the port refuses a write that would bring one back.

    Nothing is written when the replay cannot conclude: a head or policy that
    cannot be read, no review due, or a freeze that moved returns the outcome
    with the designation untouched — the former replay cleared the whole
    column in that case. Without a replacement, the designation is unchanged
    and the pilot gets one alert, its journal note written under the lot lock
    before the mail (#3369).
    """
    freeze = dict(freeze or {})

    def unchanged(state: str, why: str) -> dict:
        return {"state": state, "reviewers": [], "freeze": freeze, "why": why}

    if not _SHA_TEXT.fullmatch(str(freeze.get("sha") or "").strip().lower()):
        return unchanged("unavailable", "freeze #%s names %r, not a full commit id"
                         % (freeze.get("id"), str(freeze.get("sha") or "")))
    item = work.get(db, int(lot)) or {"id": int(lot), "state": "", "title": "",
                                      "app": ""}
    if _waiver(item):
        return unchanged("manual", "manual waiver (-none)")
    threads = _threads()
    try:
        inputs = prepare(cfg, db, int(lot), freeze=freeze)
        if isinstance(inputs, Unavailable):
            return unchanged("unavailable", inputs.why)
        if not inputs.paths or inputs.policy.required == 0:
            return unchanged(rd.NONE, LIGHT_WHY if inputs.paths else NO_PATH_WHY)
        excluded_names = {str(name or "").strip() for name in excluded or ()
                          if str(name or "").strip()}
        if excluded_names:
            inputs = dataclasses.replace(
                inputs,
                candidates=[candidate for candidate in inputs.candidates
                            if str(getattr(candidate, "name", "") or "").strip()
                            not in excluded_names])
        # #4491: the required opinions may already be rendered on this head by
        # other agents (the report: two families ok, only the blocked seat
        # left). The gate is the reference; when it is open the dead seat is
        # dropped, not replaced, and no request is mailed.
        gate_open = _gate_open(cfg, db, int(lot), freeze)
        with db.transaction() as tx:
            port = _ReseatPort(pg.PgPort(tx, after_commit=threads, at_freeze=False),
                               kept=kept, mark_id=mark_id,
                               dead=[name for name, _why in dead or ()],
                               excluded=sorted(excluded_names))
            if gate_open:
                out = {"state": "manual", "freeze": freeze,
                       "required": int(getattr(inputs.policy, "required", 0) or 0),
                       "reviewers": [], "missing": 0, "why": GATE_COVERED_WHY}
            else:
                out = _selection(inputs, port, kept=port.kept)
            if out.get("state") == "manual" and (port.kept or gate_open):
                out = port.commit(
                    int(lot), freeze=out.get("freeze") or freeze,
                    required=int(out.get("required") or 0), state=rd.DESIGNATED,
                    reviewers=(), missing=0,
                    why=GATE_COVERED_WHY if gate_open else COVERED_WHY,
                    authors=inputs.authors,
                    policy_ref=inputs.policy.ref, notify=inputs.notify, messages=(),
                    unknown_authors=inputs.unknown_authors)
            if out.get("state") == rd.DESIGNATED:
                _sync_reviewers(tx, item, freeze,
                                [r.get("name") for r in out.get("reviewers") or ()])
            elif out.get("state") == rd.WAITING:
                # #3369: a waiting outcome writes no designation mark (it would
                # retire the live seats from the column), so the journal note
                # is its only dedup mark. It is written **under the lot lock**
                # taken by the port's commit, before the pilot's alert: a
                # concurrent pass on another host then sees it and stays
                # silent instead of alerting and noting twice.
                if storage.of(tx).work.events_noted(
                        _reassign_prefix(lot, int(freeze.get("id") or 0), dead),
                        float(freeze.get("at_ts") or 0.0), ids=[int(lot)]):
                    raise _AlreadyNoted(
                        "the waiting alert of freeze #%s was already journaled"
                        " by a concurrent pass: nothing written"
                        % (freeze.get("id"),))
                tail = ("no replacement: %s; designation unchanged"
                        % (out.get("why") or "waiting"))
                storage.of(tx).work.note(
                    int(lot), str(item.get("state") or ""),
                    _reassign_note(int(lot), int(freeze.get("id") or 0), dead, tail,
                                   [seat["name"] for seat in port.kept]),
                    SENDER)
                _reseat_alert(tx, item, freeze, inputs.notify, port.kept, dead, out,
                              threads)
                out["noted"] = True
        _drain(db, threads)
        return out
    except (_AlreadyNoted, _SeatsMoved, rd.StaleFreeze) as exc:
        return unchanged("stale", str(exc))
    except rd.DesignationError as exc:
        return unchanged("unavailable", str(exc))


def reassign_unreachable(cfg, db, *, limit: int = RETRY_LIMIT) -> dict:
    """Replays the designation of a lot whose reviewer became unreachable.

    #3328 : the freeze hook now excludes a closed host, but a designation
    written before the host was lost keeps a dead seat: the request sits in a
    mailbox nobody will read, and the freeze waits for a verdict that cannot
    come. On the next runner pass, an open lot whose **effective automatic**
    mark of its latest freeze names an unreachable reviewer gets that seat
    replaced on the same freeze (`reseat`), with the usual selection, else the
    pilot's alert and the designation unchanged. The seat is journaled once per
    `(freeze, unreachable names)`: a replacement that later dies is a new loss
    and is handled again; a pass that cannot replace the same names does not
    loop.

    #3685 : a transient read failure (frozen head or policy unreadable) writes
    no consuming note. It is journaled under `REASSIGN_RETRY_PREFIX` and
    retried on the next pass, at most `REASSIGN_RETRY_MAX` times; only a
    replacement or an explicit "no candidate" consumes the `(freeze, names)`
    mark. The failure **after** the last retry consumes it with a note that
    leaves the seat to the pilot (so `REASSIGN_RETRY_MAX` retries then one
    final read), so an unreadable head cannot loop or disappear silently.

    #3367 : only the dead seats move. A live seat is never replaced, and an
    unreachable reviewer whose verdict is acquired on the exact frozen head
    keeps its seat (`_head_voters`): the 15:54Z pass of 24/10 had re-designated
    the whole set of 10 lots whose two families had already approved the head.

    #3369 : in a sensitive class a seat is kept only on a verdict that carries
    the exact #1465 receipt the gate requires; a waiting outcome writes its
    journal note under the lot lock, so two concurrent hosts alert once.

    #4491 : a long `blocked` reviewer (the runner's marked authentication,
    host, database or account pause) and a row whose `status_text` starts with
    `auth_blocked` are unreachable like a stopped agent: the seat is dropped
    when the kept seats cover the required reviews, otherwise replaced by an
    available agent. The freeze selection reads the same `is_blocked_pause`
    and `auth_blocked` prefix, so the two never disagree; the account's
    short-window pause keeps its seat on both sides (#3876).

    The pass reads only machine evidence (registry, host lifecycle, journal)
    and never raises: the runner logs the summary. It does not touch a manual
    designation, a name the pilot already removed from the reviewer column, a
    carried verdict mark, a waiting mark, or a lot where the mark belongs to an
    older freeze. A registry or journal read failure skips the pass instead of
    moving seats on a doubt.
    """
    out = {"examined": 0, "reassigned": 0, "waiting": 0, "retried": 0,
           "exhausted": 0, "skipped": 0, "errors": [], "summary": ""}
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = RETRY_LIMIT
    cursor = _read_cursor(cfg, REASSIGN_CURSOR)
    try:
        from .storage.postgres.work import has_reviewers
        if not _named_read("work_items.reviewers", lambda: has_reviewers(db)):
            out["summary"] = "no reviewer column: nothing to reassign"
            return out
        # #3631 : six colonnes suffisent (joignabilité), au lieu de la vue
        # d'observabilité complète que la base commune faisait expirer.
        listing = _named_read(
            "agents.reachability", lambda: storage.of(db).agents.reachability())
        if not listing:
            out["summary"] = "registry unreadable or empty: no reassignment"
            return out
        by_name = _reviewer_rows(listing)
        closed = _named_read(
            "alert_briefs.claims_closed_hosts",
            lambda: alert_briefs.claims_closed_hosts(db))
        hosts = [str(host).strip() for host in closed if str(host).strip()]
        # #3328 (relecture) : le filtre est dans la requête, AVANT la fenêtre
        # bornée. Les 200 lots anciens sains ne peuvent plus masquer le 201e
        # dont le siège est perdu ; le curseur fait avancer une passe dont les
        # lignes examinées ne demandent rien, puis se replie une fois au bout.
        rows = _named_read(
            "work_items.unreachable",
            lambda: _unreachable_lots(db, after=cursor, hosts=hosts))
        if not rows and cursor:
            cursor = 0
            rows = _named_read(
                "work_items.unreachable",
                lambda: _unreachable_lots(db, after=0, hosts=hosts))
        if not rows:
            if cursor:
                # The queue is empty: the next pass starts at the oldest lot
                # again instead of re-reading an exhausted window.
                _write_cursor(cfg, REASSIGN_CURSOR, 0)
            out["summary"] = "no unreachable evidence"
            return out
    except _UnreadableInputs as exc:
        # #3631 : la passe nomme la lecture qui a expiré (et la requête, quand
        # le pilote l'a portée) au lieu de perdre 639 passes en silence.
        out["errors"].append(str(exc))
        out["summary"] = "no reassignment: inputs unreadable"
        return out
    except Exception as exc:  # noqa: BLE001 - the pass never breaks the caller
        out["errors"].append("%s: %s" % (type(exc).__name__, str(exc)[:200]))
        out["summary"] = "no reassignment: inputs unreadable"
        return out
    attempted = 0
    last = cursor
    for row in rows or ():
        try:
            lot = int(row.get("id") or 0)
            state = str(row.get("state") or "")
        except (TypeError, ValueError):
            continue
        if not lot:
            continue
        if attempted >= limit:
            # budget spent: the cursor stops before this row, which the next
            # pass examines first instead of starving behind it.
            break
        # The cursor moves over every row a pass examines, including a row
        # that raises: a poison seat is retried after the wrap, never at the
        # cost of the rows behind it.
        last = max(last, lot)
        try:
            freeze = _latest_freeze(db, lot)
            if freeze is None:
                out["skipped"] += 1
                continue
            freeze_id = int(freeze.get("id") or 0)
            mark = _effective_mark(db, lot, freeze_id,
                                   since_ts=float(freeze.get("at_ts") or 0.0))
            if mark is None or mark[1] != "designation" \
                    or str((mark[2] or {}).get("state") or "") != rd.DESIGNATED:
                out["skipped"] += 1
                continue
            # #3367 : a live seat, or a verdict acquired on the frozen head,
            # is kept as it is; the pilot's removals are history.
            dead, kept = _dead_seats(cfg, db, lot, freeze, mark[2],
                                     row.get("reviewers") or (), by_name, closed)
            if not dead:
                out["skipped"] += 1
                continue
            # #3685 (B1) : a seat the mark records but the column no longer
            # names was removed by the pilot. It stays out of the replacement
            # selection even when another seat of the same mark is dead.
            removed = _removed_seats_from_mark(mark[2], row.get("reviewers") or ())
            prefix = _reassign_prefix(lot, freeze_id, dead)
            since = float(freeze.get("at_ts") or 0.0)
            if storage.of(db).work.events_noted(prefix, since, ids=[lot]):
                out["skipped"] += 1
                continue
            out["examined"] += 1
            attempted += 1
            result = reseat(cfg, db, lot, freeze=freeze, kept=kept, dead=dead,
                            mark_id=int(mark[0]), excluded=removed)
        except Exception as exc:  # noqa: BLE001 - one lot never stops the pass
            out["errors"].append("#%d: %s: %s"
                                 % (lot, type(exc).__name__, str(exc)[:160]))
            continue
        outcome = str((result or {}).get("state") or "")
        if outcome == "stale":
            # The freeze or its mark moved under the pass (another host's
            # pass, a new freeze): nothing was written, the next pass reads
            # the new designation.
            out["skipped"] += 1
            continue
        kept_names = [seat["name"] for seat in kept]
        if outcome == "unavailable":
            # #3685 : a head or policy that cannot be read is transient. The
            # failure is journaled under its own prefix and retried on the next
            # pass, so it never consumes the `(freeze, names)` mark; only a
            # replacement or an explicit "no candidate" does. The attempts are
            # capped: the last one consumes the mark with a note that leaves
            # the seat to the pilot.
            why = str((result or {}).get("why") or outcome)
            retry_prefix = "%s%d:%d:%s:" % (
                REASSIGN_RETRY_PREFIX, lot, freeze_id,
                ",".join(sorted(name for name, _why in dead)))
            try:
                attempts = len(storage.of(db).work.events_noted(
                    retry_prefix, since, ids=[lot]) or ())
            except Exception as exc:  # noqa: BLE001 - one lot never stops the pass
                out["errors"].append("#%d retry journal: %s: %s"
                                     % (lot, type(exc).__name__, str(exc)[:160]))
                continue
            try:
                if attempts >= REASSIGN_RETRY_MAX:
                    out["exhausted"] += 1
                    tail = ("%s; retry cap %d reached: the seat is left to the"
                            " pilot" % (why, REASSIGN_RETRY_MAX))
                    storage.of(db).work.note(
                        lot, state,
                        _reassign_note(lot, freeze_id, dead, tail, kept_names),
                        SENDER)
                else:
                    out["retried"] += 1
                    storage.of(db).work.note(
                        lot, state,
                        "%s attempt %d/%d: reviewer(s) unreachable: %s; %s"
                        % (retry_prefix, attempts + 1, REASSIGN_RETRY_MAX,
                           "; ".join("%s (%s)" % (name, _why)
                                     for name, _why in dead),
                           why),
                        SENDER)
            except Exception as exc:  # noqa: BLE001 - the next pass retries
                out["errors"].append("#%d retry note: %s: %s"
                                     % (lot, type(exc).__name__, str(exc)[:160]))
            continue
        added = [str((reviewer or {}).get("name") or "").strip()
                 for reviewer in (result or {}).get("reviewers") or ()
                 if str((reviewer or {}).get("name") or "").strip() not in kept_names]
        if outcome == rd.DESIGNATED:
            out["reassigned"] += 1
            tail = ("replaced by %s" % ", ".join(added) if added
                    else str(result.get("why") or COVERED_WHY))
        elif outcome == rd.WAITING:
            out["waiting"] += 1
            tail = ("no replacement: %s; designation unchanged"
                    % (result.get("why") or "waiting"))
        else:
            tail = "designation unchanged: %s" % (result.get("why") or outcome)
        if (result or {}).get("noted"):
            # #3369: the waiting note was written under the lot lock, with its
            # single alert; a concurrent pass never reaches this point twice.
            continue
        try:
            storage.of(db).work.note(
                lot, state, _reassign_note(lot, freeze_id, dead, tail, kept_names),
                SENDER)
        except Exception as exc:  # noqa: BLE001 - the mark is already written
            out["errors"].append("#%d note: %s: %s"
                                 % (lot, type(exc).__name__, str(exc)[:160]))
    # Move past what the pass examined even when nothing was attempted: the
    # next pass starts after it and wraps once at the end, so a backlog of
    # non-actionable seats cannot pin the window forever.
    if last > cursor or cursor == 0:
        _write_cursor(cfg, REASSIGN_CURSOR, last)
    out["summary"] = ("%d examined: %d reassigned, %d waiting, %d retried,"
                      " %d exhausted, %d skipped"
                      % (out["examined"], out["reassigned"], out["waiting"],
                         out["retried"], out["exhausted"], out["skipped"]))
    return out
