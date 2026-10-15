# SPDX-License-Identifier: AGPL-3.0-only
"""Merge queue per target (lot L160e1, study L160 § 3.3).

    ameesh work queue add <lot>       the lot is ready: into its target's queue
    ameesh work queue remove <lot>    out of the queue (to push again, or give up)
    ameesh work queue show [--target T] [--json]

A lot enters the queue of its target (`branch_target`, else the repository's
default target) when it is ready, which replaces the "merge light" given by
hand:

* frozen: a `frozen` milestone with its commit (`ameesh work milestone <lot>
  frozen --sha …`), and the branch still at that commit;
* review verdict `ok` on that very commit (`ameesh work milestone <lot>
  verdict --verdict ok --sha …`);
* CI green when the branch has an open PR (a team that merges locally has
  none: nothing to check).

A queued lot is frozen: its agent no longer pushes to its branch without
taking it out first. At each pass (`radar.run_pass`), for each queue:

1. a lot whose branch moved past its freeze is ejected; a lot that merged
   is `landed`;
2. the queue is ordered: priority first, then the lots without conflict with
   another queued lot, then those whose conflicts are prepared (L160d), then
   arrival. A lot whose conflict with a lot ahead of it is not prepared waits
   behind it, and the resolver of that pair is woken once: the conflict now
   blocks;
3. the queue is replayed stacked in the radar's private clone: the target,
   plus the lots ahead, plus this one, each merge with `rerere` primed by
   the resolutions of this lot with the lots ahead. A conflict that no
   resolution covers ejects the lot (its agent is woken) instead of blocking
   the lots behind it.

Nothing is merged here: `ameesh work land` (L160e2) merges the head of the
queue.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile

from . import plan_git, storage, work
from .canon import FETCH_TIMEOUT, GitError

STATES = ("queued", "landing", "landed", "ejected")
#: pair states where the conflict between two queued lots is not covered
UNPREPARED = ("conflict", "stale", "error")
#: lot branches fetched into the radar's clone for `readiness`
QUEUE_REF = "refs/ameesh/queue/"
#: a milestone names its commit by its id: a ref or a revision expression
#: (`HEAD`, the lot branch) moves, and would admit a commit nobody reviewed
_HEX = re.compile(r"[0-9a-f]{7,64}")


class QueueError(RuntimeError):
    """A lot that cannot enter the queue: actionable message."""


# --------------------------------------------------------------------------
# entering and leaving
# --------------------------------------------------------------------------

def _same(a: str | None, b: str | None) -> bool:
    """Two full commit ids, equal (never a prefix: `readiness` resolves them)."""
    return bool(a and b) and a == b


def pinned(repo: str, sha: str | None) -> str | None:
    """The full id of the commit `sha` names in `repo`, if `sha` is a
    hexadecimal commit id (full or abbreviated); None for a ref, a revision
    expression or an unknown commit."""
    sha = (sha or "").strip().lower()
    if not _HEX.fullmatch(sha):
        return None
    full = plan_git._commit(repo, sha)
    return full if full and full.startswith(sha) else None


def latest_verdict(rows: list[dict]) -> dict | None:
    verdicts = [r for r in rows if r.get("kind") == "verdict"]
    return max(verdicts, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0))) \
        if verdicts else None


# --------------------------------------------------------------------------
# la porte des avis (#1374, P1-1) : comptés, indépendants, sans blocage masqué
# --------------------------------------------------------------------------

#: la classe « sensible » demande un avis d'une autre famille ; **SQL, sécurité
#: et prod** en demandent deux, indépendants (canon §2, décision 18855a). Ces
#: trois domaines se lisent sur le diff classé (`klass["files"]` : le chemin et
#: la portée qui l'a classé), avec des motifs larges : un chemin critique non
#: reconnu coûterait un avis manquant, un chemin reconnu à tort ne coûte qu'un
#: avis de trop.
CRITICAL_SCOPE = (
    # SQL, migrations, stockage
    re.compile(r"(^|/)(migrations?|supabase|storage)(/|$)|(^|/)migrations\.py$|"
               r"(^|/)db\.py$|\.sql$"),
    # sécurité : droits, identité, clés, secrets, décisions
    re.compile(r"(^|/)(approve|auth|authority|identity|signing|cose|p256|jcs|decisions|"
               r"visibility|auth_failure|dsn|assignments)(/|\.py$|$)|"
               r"(^|/)(keys?|secrets?)(/|$)"),
    # prod, infra, déploiement, hôtes et relais — dont la porte de fusion
    re.compile(r"(^|/)(deploy|infra|lab|bin)(/|$)|(^|/)\.github/|"
               r"(^|/)scripts/(ci|deploy|compute|test)|(^|/)(Dockerfile|pyproject\.toml)$|"
               r"(^|/)host[-_]|(^|/)(hostcheck|exploitation|relay|serve|progress_serve|"
               r"relocation|placement|merge_queue|land|fire)\.py$"),
)
#: le nombre d'avis d'un lot sensible hors de ces domaines
SENSITIVE_REVIEWS = 1
#: le nombre d'avis d'un lot sensible qui touche SQL, la sécurité ou la prod
CRITICAL_REVIEWS = 2


def critical_scope(klass) -> bool:
    """Le diff gelé touche-t-il SQL, la sécurité ou la prod (deux avis) ?

    Un doute de classe (politique ou diff illisible, `files` vide) ne se
    suppose jamais rassurant : `required_verdicts` le compte alors comme un
    domaine critique, faute de pouvoir exclure SQL, la sécurité ou la prod."""
    for entry in (klass or {}).get("files") or ():
        if str(entry.get("class") or "") == "light":
            continue
        candidates = [entry.get("path")] + [str(p) for p in (entry.get("rule") or ())]
        for candidate in candidates:
            if any(rx.search(str(candidate or "")) for rx in CRITICAL_SCOPE):
                return True
    return False


def required_verdicts(klass) -> int:
    """Le nombre d'avis indépendants que la classe exige : 0 pour un lot léger,
    1 pour normal, 1 pour sensible (2 s'il touche SQL, la sécurité ou la prod).
    Un doute de classe (politique ou diff illisible, `files` vide) ne se
    suppose jamais rassurant : il garde l'exigence des domaines critiques
    (deux avis), faute de pouvoir exclure SQL, la sécurité ou la prod.
    Un lot release n'est jamais léger : il garde un avis (la procédure de
    release et la protection de cible restent au pilote ; le feu, lui, refuse
    la classe release avant tout compte)."""
    from . import lot_class
    klass = klass or {}
    cls = str(klass.get("class") or lot_class.SENSITIVE)
    if cls == lot_class.LIGHT:
        return 0
    if klass.get("doubt"):
        return CRITICAL_REVIEWS
    if cls in (lot_class.NORMAL, lot_class.RELEASE):
        return 1
    return CRITICAL_REVIEWS if critical_scope(klass) else SENSITIVE_REVIEWS


def _mark_order(mark: dict) -> tuple:
    return (float(mark.get("at_ts") or 0.0), int(mark.get("id") or 0))


def _same_sha(a, b, repo: str | None = None) -> bool:
    """Deux identifiants de commit qui nomment le même commit : égaux, ou
    résolus dans `repo` (un verdict peut citer une abréviation)."""
    left, right = str(a or "").strip().lower(), str(b or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    if repo:
        full_left, full_right = pinned(repo, left), pinned(repo, right)
        return bool(full_left and full_right and full_left == full_right)
    return False


def actor_family(db, name) -> str:
    """La famille de modèle d'un agent (`claude`, `codex`, `deepseek`), "" si
    elle n'est pas connue : un humain, un agent absent du registre, un harnais
    non reconnu. L'indépendance d'un avis ne se suppose jamais (#1374)."""
    from . import registry, review_lot
    name = str(name or "").strip()
    if not name or name.startswith(work.HUMAN_PREFIX) or ":" in name:
        return ""
    try:
        row = registry.get(db, name) or {}
    except Exception:  # noqa: BLE001 - registre illisible : famille inconnue
        return ""
    return review_lot.family_of(registry.live_harness(row))


def blocked_verdicts(marks, frozen, *, repo: str | None = None) -> list[dict]:
    """Les relecteurs dont le **dernier** verdict sur cette tête est `blocked`
    (#1374, P1-1). Un `ok` plus récent du même relecteur lève son blocage ;
    celui d'un autre relecteur ne le masque jamais."""
    latest: dict = {}
    for mark in (marks or ()):
        if mark.get("kind") != "verdict" or not _same_sha(mark.get("sha"), frozen, repo):
            continue
        key = str(mark.get("actor") or "").strip().lower() or "?"
        if key not in latest or _mark_order(mark) > _mark_order(latest[key]):
            latest[key] = mark
    return sorted((m for m in latest.values() if (m.get("verdict") or "") == "blocked"),
                  key=_mark_order)


def review_gate(db, marks, frozen, *, repo: str | None = None, klass=None,
                item=None) -> dict:
    """Les avis requis sur la tête gelée `frozen`, et eux seuls (#1374).

    Un verdict ne compte que s'il a été rendu **sur cette tête** (le `sha` du
    jalon est résolu, jamais une ref). Parmi ces verdicts, seul le **dernier de
    chaque relecteur** compte : un `ok` plus récent du même relecteur lève son
    blocage, celui d'un autre relecteur ne le masque pas. Un blocage encore en
    vigueur refuse la porte, quelle que soit la classe.

    Le nombre d'avis dépend de la classe (`required_verdicts`) : 1 pour un lot
    normal et pour un lot sensible hors SQL/sécurité/prod, 2 pour un lot
    sensible qui touche ces domaines ou dont la classe reste un doute. Les avis
    comptés sont **indépendants** : jamais le travail de l'auteur ; pour une
    classe sensible, une famille de modèle connue et autre que celle de
    l'auteur ; pour deux avis, deux familles distinctes. L'indépendance ne se
    suppose jamais : une famille d'auteur inconnue refuse la porte (canon §2,
    décision 18855a).

    Rend `{ok, why, class, required, accepted, blocked, author, author_family,
    on_freeze}` ; ne lève jamais.
    """
    from . import lot_class
    klass = klass or {}
    cls = str(klass.get("class") or lot_class.SENSITIVE)
    frozen = str(frozen or "").strip().lower()
    row = item or {}
    author = str(row.get("assignee") or "").strip()
    out = {"ok": False, "why": "", "class": cls, "required": 0, "accepted": [],
           "blocked": [], "author": author, "author_family": "", "on_freeze": 0}
    if not frozen:
        out["why"] = "no freeze to review: the fire names the exact frozen head"
        return out
    required = required_verdicts(klass)
    out["required"] = required
    on_freeze = [m for m in (marks or ()) if m.get("kind") == "verdict"
                 and _same_sha(m.get("sha"), frozen, repo)]
    out["on_freeze"] = len(on_freeze)
    latest: dict = {}
    for mark in on_freeze:
        key = str(mark.get("actor") or "").strip().lower() or "?"
        if key not in latest or _mark_order(mark) > _mark_order(latest[key]):
            latest[key] = mark
    blocked = sorted((m for m in latest.values() if (m.get("verdict") or "") == "blocked"),
                     key=_mark_order)
    out["blocked"] = [{"actor": m.get("actor") or "", "at_ts": m.get("at_ts"),
                       "note": m.get("note") or ""} for m in blocked]
    if blocked:
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): blocked verdict(s) "
                      "not lifted: %s"
                      % (frozen[:12], cls, ", ".join(sorted(
                          str(m.get("actor") or "relecteur sans nom") for m in blocked))))
        return out
    if required == 0:
        out["ok"] = True
        return out
    author_family = actor_family(db, author)
    out["author_family"] = author_family
    # L'indépendance ne se suppose jamais (canon §2, décision 18855a) : une
    # classe sensible — y compris un doute de classe, qui peut cacher SQL, la
    # sécurité ou la prod — n'accepte que des familles connues et autres que
    # celle de l'auteur. Famille d'auteur inconnue (agent absent du registre,
    # harnais non reconnu) : aucun avis ne peut être prouvé indépendant, la
    # porte refuse et le dit. Un auteur humain n'a pas de famille de modèle :
    # il n'y a rien à comparer, seules les familles des relecteurs comptent.
    strict = required >= 2 or cls == lot_class.SENSITIVE
    author_human = bool(author) and author.lower().startswith(work.HUMAN_PREFIX)
    if strict and not author_family and not author_human:
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): the author %s has "
                      "no known model family, so no verdict can be proven independent "
                      "(canon §2: independence is never assumed)"
                      % (frozen[:12], cls, author or "(none)"))
        return out
    accepted = []
    for mark in sorted((m for m in latest.values() if (m.get("verdict") or "") == "ok"),
                       key=_mark_order):
        actor = str(mark.get("actor") or "").strip()
        if author and actor and actor.lower() == author.lower():
            continue                      # on ne relit pas son propre travail
        family = actor_family(db, actor)
        if strict:
            if not family:
                continue                  # famille du relecteur inconnue : non prouvée
            if family == author_family:
                continue                  # la famille de l'auteur ne compte pas
        accepted.append({"actor": actor, "family": family, "id": mark.get("id"),
                         "at_ts": mark.get("at_ts")})
    if required >= 2:
        chosen, seen = [], set()
        for entry in accepted:
            if entry["family"] in seen:
                continue
            seen.add(entry["family"])
            chosen.append(entry)
            if len(chosen) >= required:
                break
        accepted = chosen
    out["accepted"] = accepted
    if len(accepted) >= required:
        out["ok"] = True
        return out
    got = ", ".join("%s (%s)" % (e["actor"] or "?", e["family"] or "famille inconnue")
                    for e in accepted) or "aucun"
    need = ("two independent verdicts from different model families, neither the author's"
            if required >= 2 else
            "one verdict from another family than the author's" if strict
            else "one verdict from another agent")
    out["why"] = ("no review verdict ok on the freeze %s (class: %s): %d independent review "
                  "verdict(s) required, %s; got %s"
                  % (frozen[:12], cls, required, need, got))
    return out


def _remote_branch(name: str) -> str:
    """The branch of `origin` that a lot branch or a target names:
    `origin/main`, `refs/remotes/origin/main` and `main` all name `main`."""
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")
_URL = re.compile(r"^([a-z][a-z0-9+.-]*)://(?:[^@/]+@)?([^/:]+)(?::(\d+))?/(.+)$", re.I)
#: the port a scheme names when it names none: `ssh://host:22/x` is `host:x`
_DEFAULT_PORTS = {"ssh": 22, "git+ssh": 22, "ssh+git": 22, "git": 9418, "http": 80,
                  "https": 443}


def normalize_source(source: str) -> str:
    """Normalize a stored queue source, retaining server ports and fork identity."""
    if source.partition("/")[0].split(":")[0].lower() == "github.com":
        return source.lower()
    return source


def source_of(repo: str) -> str:
    """The repository a lot is queued from, as a radar pass compares it: its
    origin as `host/path` (`git@github.com:o/r.git` and
    `https://github.com/o/r` are one source; a checkout recreated keeps it;
    a port other than the scheme's own is kept: two servers on one host are
    two sources), else the real path of the origin or of `repo` itself. Two
    projects that share commits (a fork) are two sources."""
    url = (plan_git._out(repo, "remote", "get-url", "origin") or "").strip()
    if not url:
        return os.path.realpath(repo)
    host, port, path = None, None, ""
    found = _URL.match(url)
    if found and found.group(1).lower() != "file":
        scheme, host, port, path = found.groups()
        if port is not None and int(port) == _DEFAULT_PORTS.get(scheme.lower()):
            port = None
    elif not url.startswith(("/", ".", "file://")) and _SCP.match(url):
        host, path = _SCP.match(url).groups()
    if host is None:
        local = url[len("file://"):] if url.startswith("file://") else url
        return os.path.realpath(os.path.join(repo, local))
    path = path.strip("/")
    path = path[:-len(".git")] if path.endswith(".git") else path
    return normalize_source("%s%s/%s" % (host.lower(), ":%d" % int(port) if port else "", path))


def fetch_fresh(repo: str, names, clone=None) -> tuple[dict, str]:
    """({name: commit, or None if there is no such branch}, repository holding
    them) of the branches `names` (lot branches or targets), read fresh. With
    an `origin`, they are listed on it, then fetched from it now into the
    radar's clone (`clone`, a `resolutions.Replayer`: never into the agents'
    repository), under QUEUE_REF; neither a local branch nor the cached
    `origin/<branch>` counts, since another host may have pushed since. No
    clone, or a remote that does not answer, raises QueueError: no fetch, no
    tip. Without an `origin`, `repo` is the only copy and its local branches
    are the tips."""
    from . import resolutions
    names = sorted({n for n in names if n})
    remote = {n: _remote_branch(n) for n in names}
    url = plan_git._out(repo, "remote", "get-url", "origin")
    if not url:
        return {n: plan_git._commit(repo, "refs/heads/" + remote[n]) for n in names}, repo
    what = "branch%s %s" % ("es" if len(names) > 1 else "", ", ".join(names))
    if clone is None:
        raise QueueError("%s: origin cannot be fetched without the radar's clone "
                         "(ameesh work queue add finds it from the project)" % what)
    present: set = set()
    try:
        if not os.path.isdir(os.path.join(clone.path, ".git")):
            clone.ensure()  # an existing clone is not reset: the radar may use it
        wanted = sorted(set(remote.values()))
        listing = resolutions._run(clone.path, "ls-remote", url,
                                   *("refs/heads/" + b for b in wanted), timeout=FETCH_TIMEOUT)
        failed = resolutions._err(listing) if listing.returncode else None
        if failed is None:
            listed = {line.split("\t", 1)[1] for line in resolutions._text(listing).splitlines()
                      if "\t" in line}
            present = {b for b in wanted if "refs/heads/" + b in listed}
        if failed is None and present:
            proc = resolutions._run(clone.path, "fetch", "--quiet", url,
                                    *("+refs/heads/%s:%s%s" % (b, QUEUE_REF, b)
                                      for b in sorted(present)), timeout=FETCH_TIMEOUT)
            failed = resolutions._err(proc) if proc.returncode else None
    except (GitError, OSError) as exc:
        failed = str(exc)
    if failed is not None:
        raise QueueError("cannot fetch %s from origin (%s): try again once the remote answers"
                         % (what, failed[:300]))
    return {n: plan_git._commit(clone.path, QUEUE_REF + remote[n]) if remote[n] in present
            else None for n in names}, clone.path


def branch_tip(repo: str, branch: str, clone=None) -> tuple[str | None, str]:
    """(tip, repository holding it) of a lot branch, read fresh
    (`fetch_fresh`). A branch that origin no longer has raises QueueError:
    the commit the clone kept is not the tip."""
    tips, where = fetch_fresh(repo, [branch], clone)
    if tips[branch] is None and where != repo:
        raise QueueError("cannot fetch branch %s from origin (no such branch there): push "
                         "it again" % branch)
    return tips[branch], where


def readiness(db, lot: int, repo: str, *, ci=None, clone=None, project=None,
              commit_ci=None, cfg=None) -> dict:
    """Checks that `lot` may enter its queue. Returns `{target, frozen_sha,
    pr, class, class_why}`; QueueError says what is missing. Commit ids are
    resolved to full ones before being compared; `ci` (`radar.github_ci(...,
    strict=True)`) must report the PR green on the frozen commit itself, and
    an error reading it refuses the lot. A project with `ci_required` (L160g)
    also needs GitHub's CI green on the frozen commit itself, PR or not
    (`commit_ci`, `radar.commit_ci` by default).

    L539 (§8): a **light** lot needs no review verdict — its class comes from
    `lot_class.of_lot` on the frozen diff — but it still needs a green CI on
    the frozen head, a non-draft PR, and a target that is not `release/*`.
    Any doubt about the class (unreadable canon, unreachable diff, `cfg`
    absent) leaves the verdict requirement in place; the class and its reason
    are traced in the queue note. The target resolved here (`branch_target`,
    then the project's, then the repository's default) carries through the
    classification and the admission (revue #544 r2).

    #1374 : les avis passent par la porte partagée `review_gate` — comptés par
    classe, indépendants (familles distinctes, aucune celle de l'auteur) et
    sans blocage non levé ; la même porte que le feu du pilote.

    The CI is required **explicitly**: for a project without `ci_required`,
    `ci=None` (no reader) and a reader that answers nothing are refused, like
    a red CI — an absent status is not a green one (revue #544 r1). A project
    with `ci_required` may instead certify admission with GitHub's CI on the
    frozen commit when no PR vouches for it (`ci=None`, or a reader that
    answers nothing because there is no open PR): a green commit CI is not
    blocked by the absence of a PR (revue #544 r2)."""
    from . import plan_github, radar
    row = work.get(db, lot)
    if row is None:
        raise QueueError("lot #%d not found" % lot)
    target = row.get("branch_target") or getattr(project, "target", "") or plan_git.default_target(repo)
    glob = radar.protected_target(project, target)
    if glob:
        # L160g (#473): main, release branches and tags stay an orchestrator's gesture
        raise QueueError("%s is a protected target of project %s (%s): the merge queue "
                         "never merges there; it is the orchestrator's gesture"
                         % (target, project.name, glob))
    if row.get("state") in work.MERGED_STATES or row.get("state") == "closed":
        raise QueueError("lot #%d is %s: nothing to merge" % (lot, row["state"]))
    if not row.get("branch"):
        raise QueueError("lot #%d has no branch (ameesh work assign %d <agent> --branch …)"
                         % (lot, lot))
    marks = work.milestones(db, lot)
    freeze = plan_git.latest_freeze(marks)
    if not freeze or not freeze.get("sha"):
        raise QueueError("lot #%d is not frozen: ameesh work milestone %d frozen --sha <commit>"
                         % (lot, lot))
    tip, where = branch_tip(repo, row["branch"], clone)
    if not tip:
        raise QueueError("branch %s of lot #%d is neither local nor on origin"
                         % (row["branch"], lot))
    frozen = pinned(where, freeze["sha"])
    if frozen is None and not _HEX.fullmatch(freeze["sha"].strip().lower()):
        raise QueueError("the freeze of lot #%d names %r, not a commit id: freeze it again "
                         "(ameesh work milestone %d frozen --sha %s)"
                         % (lot, freeze["sha"], lot, tip))
    if not _same(tip, frozen):
        raise QueueError("branch %s is at %s, not at its freeze %s%s: freeze it again "
                         "(ameesh work milestone %d frozen --sha %s), then have it reviewed"
                         % (row["branch"], tip[:12], freeze["sha"][:12],
                            "" if frozen else " (not a commit of this repository)", lot, tip))
    klass = _class_of(db, lot, row, repo, where, target, tip, cfg=cfg)
    # #1374 : la même porte que le feu — avis comptés par classe, indépendants
    # (familles), et aucun blocage non levé ; un `ok` récent ne masque plus le
    # `blocked` d'un autre relecteur (P1-1).
    gate = review_gate(db, marks, tip, repo=where, klass=klass, item=row)
    if not gate["ok"]:
        raise QueueError("lot #%d: %s" % (lot, gate["why"]))
    needs_commit_ci = project is not None and getattr(project, "ci_required", False)
    status = None
    if ci is None:
        if not needs_commit_ci:
            raise QueueError("lot #%d has no CI reader: a green CI on the frozen head %s is "
                             "required before entering the queue" % (lot, tip[:12]))
    else:
        try:
            status = ci(row["branch"])
        except plan_github.GithubError as exc:
            raise QueueError("cannot read the CI of branch %s on GitHub (%s): try again once "
                             "gh works" % (row["branch"], exc)) from exc
        if not status and not needs_commit_ci:
            raise QueueError("no CI status for branch %s (lot #%d): a green CI on the frozen "
                             "head %s is required" % (row["branch"], lot, tip[:12]))
    if status:
        if not _same(status.get("head"), tip):
            raise QueueError("PR #%s is at %s, not at the freeze %s: its CI does not "
                             "vouch for the frozen commit" % (status.get("number"),
                                                             (status.get("head") or "?")[:12],
                                                             tip[:12]))
        if not status.get("green"):
            raise QueueError("the CI of PR #%s is not green" % status.get("number"))
        if gate["required"] == 0 and status.get("draft"):
            raise QueueError("PR #%s is a draft: a light lot merges only from a "
                             "non-draft PR" % status.get("number"))
    if needs_commit_ci:
        _required_ci(project, tip, commit_ci)
    return {"target": target, "frozen_sha": tip, "frozen_id": freeze["id"],
            "pr": (status or {}).get("number"), "class": klass.get("class"),
            "class_why": klass.get("why") or ""}


def _class_of(db, lot: int, row: dict, repo: str, where: str | None, target: str | None,
              tip: str, *, cfg=None) -> dict:
    """La classe du lot pour la file (L539) : lue là où la tête gelée vit.

    Le clone de la file range les branches sous `QUEUE_REF` ; à défaut la
    référence de la cible y est tentée, puis le dépôt du projet. `cfg` absent
    ou dépôt illisible : `lot_class.of_lot` rend un doute (`sensitive`, drapeau
    `doubt`), et la file garde l'exigence de verdict.

    Les lectures sont **toutes** comparées : la plus haute l'emporte, une
    lecture calculée ne se rétrograde jamais devant une autre, et un doute ne
    se lit pas comme une classe sensible (le clone radar peut voir des
    changements sensibles que le checkout de l'auteur ne voit plus — revue
    #544 r1)."""
    from . import lot_class
    paths = []
    if target:
        base_repo = where or repo
        for candidate in (QUEUE_REF + target, target):
            if plan_git._commit(base_repo, candidate):
                paths.append((base_repo, candidate))
                break
        if where and where != repo:
            paths.append((repo, target))
    if not paths:
        paths.append((repo, target))
    best = None
    for path, base in paths:
        current = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=path, base=base,
                                   head=tip)
        if best is None or _class_worse(current, best):
            best = current
    return best


def _class_worse(candidate: dict, best: dict) -> bool:
    """La plus haute des deux lectures l'emporte ; à classe égale, une lecture
    calculée explique mieux qu'un doute."""
    from . import lot_class
    def rank(result):
        return lot_class.RANK.get(str(result.get("class") or ""),
                                  lot_class.RANK[lot_class.SENSITIVE])
    if rank(candidate) != rank(best):
        return rank(candidate) > rank(best)
    return bool(best.get("doubt")) and not candidate.get("doubt")


def _required_ci(project, sha: str, commit_ci=None) -> None:
    """L160g (#473): `ci_required` — GitHub's CI green on the frozen commit
    `sha` itself; QueueError otherwise (no run, running, red, unreadable)."""
    from . import plan_github, radar
    try:
        runs = (commit_ci or (lambda s: radar.commit_ci(project, s)))(sha)
    except plan_github.GithubError as exc:
        raise QueueError("project %s requires CI on the frozen commit (ci_required), "
                         "unreadable (%s): try again once gh works" % (project.name, exc)) from exc
    if not runs["runs"]:
        raise QueueError("project %s requires CI on the frozen commit (ci_required): no run "
                         "on %s yet (push it, let the workflows run)" % (project.name, sha[:12]))
    if runs["pending"]:
        raise QueueError("the CI on the frozen commit %s is still running (%s)"
                         % (sha[:12], ", ".join(runs["pending"])))
    if runs["failed"]:
        raise QueueError("the CI on the frozen commit %s is red (%s)"
                         % (sha[:12], ", ".join(runs["failed"])))
    if not runs.get("green"):
        raise QueueError("the CI on the frozen commit %s is not green" % sha[:12])


def enter(db, lot: int, *, repo: str, actor: str = "", ci="github", project=None,
          clone=None, commit_ci=None, cfg=None) -> dict:
    """Puts `lot` into the queue of its target, once `readiness` passes."""
    if ci == "github":
        from . import radar
        ci = radar.github_ci(project or radar.Project(name="", repo=repo), strict=True)
    ready = readiness(db, lot, repo, ci=ci, clone=clone, project=project,
                      commit_ci=commit_ci, cfg=cfg)
    row = storage.of(db).work.queue_add(
        lot, target=ready["target"], frozen_sha=ready["frozen_sha"], frozen_id=ready["frozen_id"],
        source=source_of(repo), note="merge queue of %s: queued at %s%s, class %s%s" % (
            ready["target"], ready["frozen_sha"][:12],
            ", PR #%s green" % ready["pr"] if ready["pr"] else "",
            ready.get("class") or "?", " (%s)" % ready["class_why"]
            if ready.get("class_why") else ""), actor=actor)
    if row is None:
        raise QueueError("lot #%d is no longer open" % lot)
    # L539 : la classe lue à l'entrée, rendue avec la ligne de file (lecture)
    row["class"] = ready.get("class")
    row["class_why"] = ready.get("class_why")
    return row


def leave(db, lot: int, *, actor: str = "", reason: str = "") -> bool:
    return storage.of(db).work.queue_remove(
        lot, note="merge queue: taken out%s" % (" (%s)" % reason if reason else ""),
        actor=actor)


# --------------------------------------------------------------------------
# order
# --------------------------------------------------------------------------

def _priority(row: dict) -> int:
    return int(row.get("priority") or 2)


def pair_states(report: dict) -> dict:
    """{frozenset(lots): pair} of the radar report."""
    return {frozenset(p["lots"]): p for p in report.get("pairs") or ()}


def order(entries: list[dict], pairs: dict) -> list[dict]:
    """The queued lots of one target in merge order: `[{work_item, rank,
    reason, waits: [(other, resolver)]}]`. `pairs`: `pair_states`."""
    ids = [int(e["work_item_id"]) for e in entries]

    def conflicts(lot: int) -> dict:
        out = {}
        for other in ids:
            pair = pairs.get(frozenset((lot, other))) if other != lot else None
            if pair and pair["state"] in UNPREPARED + ("prepared",):
                out[other] = pair
        return out

    def klass(lot: int) -> int:
        found = conflicts(lot)
        if not found:
            return 0
        return 2 if any(p["state"] in UNPREPARED for p in found.values()) else 1

    ranked = sorted(entries, key=lambda e: (_priority(e), klass(int(e["work_item_id"])),
                                            e.get("queued_ts") or 0, int(e["work_item_id"])))
    out = []
    ahead: list[int] = []
    for n, e in enumerate(ranked, 1):
        lot = int(e["work_item_id"])
        found = conflicts(lot)
        waits = [(other, (p.get("resolver") or {}).get("lot"))
                 for other, p in sorted(found.items())
                 if other in ahead and p["state"] in UNPREPARED]
        if waits:
            reason = "waits for the resolution of its conflict with %s" % ", ".join(
                "#%d" % o for o, _ in waits)
        elif found and any(o in ahead for o in found):
            reason = "conflicts with %s prepared" % ", ".join(
                "#%d" % o for o in sorted(found) if o in ahead)
        else:
            reason = "priority %d, %s" % (_priority(e), "no conflict in the queue"
                                          if not found else "conflicts with lots behind it")
        out.append({"work_item": lot, "rank": n, "reason": reason, "waits": waits})
        ahead.append(lot)
    return out


# --------------------------------------------------------------------------
# stacked replay in the radar's clone
# --------------------------------------------------------------------------

def stack(replayer, target_head: str, lots: list[dict], rows: list[dict], *, generated=()) -> dict:
    """Replays the queue stacked: from `target_head`, merges each lot of
    `lots` (`{work_item, tip}`, in order) with `rerere` primed by the
    resolutions (`rows`) of this lot with the lots already stacked. A lot
    that leaves a conflict is skipped. Returns `{lot: {state: clean|
    replayed|conflict|generated, files}}`. Generated conflicts are excluded
    before rerere; each trial tree is regenerated and checked."""
    from . import land, resolutions
    paths = {g.path for g in generated}
    latest: dict = {}
    for row in rows:
        latest.setdefault(frozenset((int(row["work_item_id"]), int(row["other_item_id"]))), row)
    ids = {int(one["work_item"]) for one in lots}
    wanted = [r["commit_sha"] for key, r in latest.items() if key <= ids]
    missing = set(replayer.prime(wanted)) if wanted else set()
    path = replayer.path
    run = resolutions._run
    resolutions._check(run(path, "checkout", "-q", "-f", "--detach", target_head), "checkout")
    stacked: list[int] = []
    out: dict = {}
    try:
        for one in lots:
            lot = int(one["work_item"])
            base = resolutions._check(run(path, "rev-parse", "HEAD"), "rev-parse")
            replayer._empty_cache()
            if run(path, "merge-base", "--is-ancestor", one["tip"], "HEAD").returncode == 0:
                # r6: already in the target (or a lot stacked): nothing to
                # merge, and no empty commit to stop the lots after it
                out[lot] = {"state": "contained", "files": []}
                stacked.append(lot)
                continue
            for other in stacked:
                row = latest.get(frozenset((lot, other)))
                if row and row["commit_sha"] not in missing \
                        and row["commit_sha"] in replayer.trained():
                    shutil.copytree(replayer._store(row["commit_sha"]), replayer._rr_cache(),
                                    dirs_exist_ok=True)
            merge = run(path, "-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false",
                        "merge", "--no-ff", "--no-commit", "--quiet", one["tip"])
            state, files = "clean", []
            if merge.returncode != 0:
                files = resolutions.unmerged(path)
                resolutions.exclude_generated(path, files, paths)
                resolutions._check(run(path, "-c", "rerere.enabled=true", "-c",
                                       "rerere.autoUpdate=false", "rerere"), "rerere")
                others = [f for f in files if f not in paths]
                remaining = [f for f in resolutions.rerere_remaining(path) if f not in paths] \
                    if others else []
                left = sorted(set(remaining) | set(resolutions.with_markers(path, others)))
                if not files or left:
                    run(path, "merge", "--abort")
                    run(path, "reset", "-q", "--hard", base)
                    out[lot] = {"state": "conflict", "files": left or files,
                                "error": "" if files else resolutions._err(merge)[:300]}
                    continue
                if others:
                    resolutions._check(run(path, "add", "--", *others), "add")
                    state = "replayed"
            if paths:
                made = land._regenerate(path, generated, paths)
                if not made["ok"]:
                    run(path, "merge", "--abort")
                    run(path, "reset", "-q", "--hard", base)
                    out[lot] = {"state": "generated", "files": [made["file"]],
                                "output": made["output"]}
                    continue
            resolutions._check(run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                                   "--no-verify", "-m", "queue: #%d" % lot), "commit")
            out[lot] = {"state": state, "files": [f for f in files if f not in paths]}
            stacked.append(lot)
    finally:
        run(path, "reset", "-q", "--hard")
        run(path, "clean", "-q", "-f", "-d")
        replayer._empty_cache()
    return out


# --------------------------------------------------------------------------
# a radar pass
# --------------------------------------------------------------------------

def _notified_path(cfg, project: str) -> str:
    from . import radar
    return os.path.join(radar.journal_dir(cfg), project + ".queue-notified")


def _read_notified(cfg, project: str) -> set:
    try:
        with open(_notified_path(cfg, project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _write_notified(cfg, project: str, keys: set) -> None:
    from . import radar
    directory = radar.journal_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s.queue-notified." % project)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(sorted(keys), fh)
    os.replace(tmp, _notified_path(cfg, project))


def _wake(db, to: str | None, lot: int, text: str, data: dict) -> int | None:
    """An ordinary mail (it wakes the agent, L125 batching): the queue is
    blocked on it."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot), payload={"merge_queue": data})
    except Exception:  # noqa: BLE001 - retried at the next pass
        return None


def _eject(db, row: dict, reason: str, text: str, data: dict) -> dict:
    lot = int(row["work_item_id"])
    storage.of(db).work.queue_update(lot, state="ejected", reason=reason,
                                     note="merge queue of %s: ejected, %s"
                                     % (row["target"], reason), actor=work.SYSTEM_SENDER)
    sent = _wake(db, row.get("assignee"), lot, text, dict(data, lot=lot, reason=reason))
    return {"lot": lot, "to": row.get("assignee"), "kind": "ejected", "message_id": sent}


def run_pass(cfg, db, project, report: dict, *, replayer=None, cache=None) -> dict:
    """One pass of the queues of `project`, after the radar scan and the
    replay of the resolutions. Writes ranks and ejections; returns
    `{targets: {target: [{lot, rank, state, reason, stack}]}, mail: […]}`,
    kept in the radar report. Never raises for one queue: the error is
    noted."""
    from . import resolutions
    out: dict = {"targets": {}, "mail": []}
    try:
        rows = storage.of(db).work.queue()
    except Exception as exc:  # noqa: BLE001 - e.g. database not yet migrated (0058)
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    branches = {b["work_item"]: b for b in report.get("branches") or ()}
    source = source_of(project.repo)

    def ours(row: dict) -> bool:
        """A lot of this project: queued from its source (the queue holds
        every project's lots). Neither the commits a repository holds (two
        projects may share one; a checkout recreated may lack it) nor the
        scan decide it; only a row with no source, if any, falls back on the
        scan."""
        if row.get("source"):
            return normalize_source(row["source"]) == source
        return int(row["work_item_id"]) in branches

    mine = [r for r in rows if r["state"] in ("queued", "landing") and ours(r)]
    if not mine:
        return out
    notified = _read_notified(cfg, project.name)
    keys = set()
    pairs = pair_states(report)
    against = {int(a["work_item"]): a for a in report.get("against_target") or ()
               if a.get("state") == "conflict"}
    ejected: set = set()
    by_target: dict = {}
    live = []
    landing: dict = {}
    for row in mine:
        if row.get("item_state") in work.MERGED_STATES:
            storage.of(db).work.queue_update(int(row["work_item_id"]), state="landed",
                                             reason="merged",
                                             note="merge queue of %s: landed" % row["target"],
                                             actor=work.SYSTEM_SENDER)
        else:
            live.append(row)
    if live and replayer is None:
        from . import radar
        replayer = resolutions.Replayer(project.repo, radar.clone_dir(cfg, project.name))
    # the radar reads the agents' refs, which may be stale either way, or gone
    # (a checkout recreated): each pass fetches the branch of every queued
    # lot and its target, scanned or not, and those tips decide
    failed = None
    tips: dict = {}
    where = project.repo
    if live:
        try:
            tips, where = fetch_fresh(project.repo, [r.get("branch") for r in live]
                                      + [r["target"] for r in live], replayer)
        except QueueError as exc:
            failed = exc
            where = replayer.path

    def defer(row: dict, why: str) -> None:
        # neither ejected nor ranked on a guess: out of the order until the
        # next pass, its rank cleared so that no other lot shares it
        reason = "deferred: %s" % why
        if row.get("rank") is not None or row.get("reason") != reason:
            storage.of(db).work.queue_update(int(row["work_item_id"]), clear_rank=True,
                                             reason=reason)
        out.setdefault("deferred", []).append({"lot": int(row["work_item_id"]),
                                               "reason": why})

    for row in live:
        lot = int(row["work_item_id"])
        tip, head = tips.get(row.get("branch")), tips.get(row["target"])
        if failed is not None:
            defer(row, str(failed))
            continue
        place = "origin" if where != project.repo else "the repository"
        if tip is None:
            defer(row, "branch %s not found on %s" % (row.get("branch") or "—", place))
            continue
        if head is None:
            defer(row, "target %s not found on %s" % (row["target"], place))
            continue
        if row["state"] == "landing" and (_same(tip, row["frozen_sha"])
                                          or _same(tip, row.get("landing_sha"))):
            # being merged by ameesh (L160e2): its branch may hold the merge commit
            landing.setdefault(row["target"], []).append(row)
            continue
        if not _same(tip, row["frozen_sha"]):
            reason = "branch moved past its freeze (%s, frozen %s)" % (
                tip[:12], row["frozen_sha"][:12])
            out["mail"].append(_eject(db, row, reason, (
                "Merge queue of %s, lot #%d « %s »: out of the queue, its branch %s moved "
                "past the freeze (%s, frozen %s). A queued lot is frozen: freeze it again, "
                "have it reviewed, then ameesh work queue add %d."
                % (row["target"], lot, row.get("title") or "", row.get("branch") or "?",
                   tip[:12], row["frozen_sha"][:12], lot)),
                {"project": project.name, "target": row["target"], "kind": "ejected"}))
            continue
        clash = against.get(lot)
        # the radar tried the target its checkout had: only a conflict with
        # the target fetched now ejects; otherwise the stacked trial below,
        # on that target, decides
        ordinary = [f for f in (clash or {}).get("files", ())
                    if f not in {g.path for g in getattr(project, "generated", ())}]
        if clash and ordinary and clash.get("target") == row["target"] \
                and _same(clash.get("tip"), row["frozen_sha"]) \
                and _same(clash.get("target_head"), head):
            # no resolution is prepared against the target: the agent integrates it
            reason = "conflict with %s on %s" % (row["target"], ", ".join(
                ordinary[:6]))
            out["mail"].append(_eject(db, row, reason, (
                "Merge queue of %s, lot #%d « %s »: out of the queue, its frozen commit "
                "conflicts with %s on %s. Integrate %s into %s, freeze again, have it "
                "reviewed, then ameesh work queue add %d."
                % (row["target"], lot, row.get("title") or "", row["target"],
                   ", ".join(ordinary[:6]), row["target"], row.get("branch") or "?",
                   lot)),
                {"project": project.name, "target": row["target"], "kind": "ejected",
                 "files": ordinary}))
            ejected.add(lot)
            continue
        # stacked on the target as origin has it now, not the agents' cached one
        by_target.setdefault(row["target"], []).append(dict(row, tip=tip, target_head=head))
    try:
        resolution_rows = storage.of(db).work.resolutions(
            [int(r["work_item_id"]) for rs in list(by_target.values()) + list(landing.values())
             for r in rs]) \
            if by_target else []
    except Exception:  # noqa: BLE001 - database without 0057: no replay
        resolution_rows = []
    stacks = getattr(cache, "stacks", None) if cache is not None else None
    stacks = {} if stacks is None else stacks
    for target in sorted(set(landing) - set(by_target)):
        out["targets"][target] = [{"lot": int(r["work_item_id"]), "rank": 1, "state": "landing",
                                   "reason": r.get("reason") or "", "stack": None}
                                  for r in landing[target]]
    for target, entries in sorted(by_target.items()):
        ranked = order(entries, pairs)
        first = len(landing.get(target) or ())
        for one in ranked:
            one["rank"] += first
        rows_by_id = {int(e["work_item_id"]): e for e in entries}
        for one in ranked:
            row = rows_by_id[one["work_item"]]
            if row.get("rank") != one["rank"] or row.get("reason") != one["reason"]:
                storage.of(db).work.queue_update(one["work_item"], rank=one["rank"],
                                                 reason=one["reason"])
        heads = {e["target_head"] for e in entries}
        result: dict = {}
        if len(heads) == 1:
            commits = tuple(sorted(r["commit_sha"] for r in resolution_rows))
            # a lot being landed goes first: the lots behind it merge after it
            stacked = [{"work_item": int(r["work_item_id"]), "tip": r["frozen_sha"]}
                       for r in landing.get(target) or ()] + \
                [{"work_item": o["work_item"], "tip": rows_by_id[o["work_item"]]["tip"]}
                 for o in ranked]
            generated = tuple(getattr(project, "generated", ()))
            key = (heads.pop(), tuple((o["work_item"], o["tip"]) for o in stacked), commits,
                   generated)
            if key not in stacks:
                try:
                    replayer.ensure()
                    stacks[key] = stack(replayer, key[0], stacked, resolution_rows,
                                        generated=generated)
                except (GitError, OSError) as exc:
                    stacks[key] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            result = stacks[key]
        listed = [{"lot": int(r["work_item_id"]), "rank": 1, "state": "landing",
                   "reason": r.get("reason") or "", "stack": None}
                  for r in landing.get(target) or ()]
        for one in ranked:
            row = rows_by_id[one["work_item"]]
            trial = result.get(one["work_item"]) if isinstance(result, dict) else None
            state = "queued"
            if trial and trial["state"] == "generated":
                lot = one["work_item"]
                reason = "stacked trial: generated file %s: regeneration or check failed" % trial["files"][0]
                out["mail"].append(_eject(db, row, reason, (
                    "Merge queue of %s, lot #%d: out of the queue. %s.\n%s\n"
                    "Fix the generator or its inputs, freeze again, then ameesh work queue add %d."
                    % (target, lot, reason, trial["output"], lot)),
                    {"project": project.name, "target": target, "kind": "ejected",
                     "files": trial["files"]}))
                state = "ejected"
                ejected.add(lot)
            if trial and trial["state"] == "conflict":
                lot = one["work_item"]
                reason = "stacked trial: conflict not covered on %s" % ", ".join(
                    trial["files"][:6] or ["?"])
                out["mail"].append(_eject(db, row, reason, (
                    "Merge queue of %s, lot #%d « %s »: out of the queue. Merged after the "
                    "lots ahead of it, it conflicts on %s and no prepared resolution covers "
                    "it. Prepare it (ameesh work resolve %d --with <lot>) or integrate %s, "
                    "freeze again, then ameesh work queue add %d."
                    % (target, lot, row.get("title") or "", ", ".join(trial["files"][:6]),
                       lot, target, lot)),
                    {"project": project.name, "target": target, "kind": "ejected",
                     "files": trial["files"]}))
                state = "ejected"
                ejected.add(lot)
            listed.append({"lot": one["work_item"], "rank": one["rank"], "state": state,
                           "reason": one["reason"], "stack": trial})
        for one in ranked:
            for other, resolver in one["waits"]:
                who = resolver or one["work_item"]
                if ejected & {one["work_item"], other, who}:
                    # out of the queue in this pass: nobody waits on it, and an
                    # ejected resolver already has its ejection mail
                    continue
                key = "wait:%d-%d" % (other, one["work_item"])
                keys.add(key)
                if key in notified:
                    continue
                holder = rows_by_id.get(who) or {}
                to = holder.get("assignee") or (branches.get(who) or {}).get("assignee")
                sent = _wake(db, to, who, (
                    "Merge queue of %s: lot #%d waits behind lot #%d, and their conflict "
                    "is not prepared. It now blocks the queue: ameesh work resolve %d "
                    "--with %d." % (target, one["work_item"], other, who,
                                    other if who == one["work_item"] else one["work_item"])),
                    {"project": project.name, "target": target, "kind": "wait",
                     "lots": [other, one["work_item"]]})
                out["mail"].append({"lot": who, "to": to, "kind": "wait",
                                    "message_id": sent})
        out["targets"][target] = listed
        if isinstance(result, dict) and result.get("error"):
            out["error"] = result["error"]
    if keys != notified:
        _write_notified(cfg, project.name, keys)
    return out


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "queue", help="merge queue per target (L160e): a ready lot enters it, ameesh orders "
                      "it and replays it stacked")
    sub = parser.add_subparsers(dest="queue_command", required=True)
    add = sub.add_parser("add", help="put a frozen, reviewed lot into its target's queue")
    add.add_argument("id", type=int)
    add.add_argument("--repo", default=None,
                     help="the project repository (default: the current git repository)")
    rm = sub.add_parser("remove", help="take a lot out of the queue")
    rm.add_argument("id", type=int)
    rm.add_argument("-m", "--message", default="", help="why")
    show = sub.add_parser("show", help="the queues, in merge order")
    show.add_argument("--target", default=None)
    for p in (add, rm, show):
        p.add_argument("--json", action="store_true")
        p.add_argument("--actor", default="",
                       help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def show_lines(rows: list[dict], classes: dict | None = None) -> list[str]:
    """Les lignes de `ameesh work queue` ; `classes` (L539) ajoute la classe de
    revue de chaque lot quand elle est connue ({lot: « light »…})."""
    out = []
    target = None
    for r in rows:
        if r["target"] != target:
            target = r["target"]
            out.append("queue of %s:" % target)
        klass = (classes or {}).get(int(r["work_item_id"]))
        out.append("  %-3s %-8s #%d « %s » (%s, frozen %s%s)%s" % (
            r["rank"] if r.get("rank") else "—", r["state"], int(r["work_item_id"]),
            r.get("title") or "", r.get("assignee") or "unassigned", r["frozen_sha"][:12],
            ", class %s" % klass if klass else "",
            " : %s" % r["reason"] if r.get("reason") else ""))
    return out


def radar_project(cfg, repo: str):
    """The radar project (`radar.projects`) of the repository `repo`, if any:
    its private clone receives the fetches of `readiness`."""
    from . import radar, migrations
    try:
        projects = list(radar.settings_of(cfg).projects.values())
    except radar.SettingsError as exc:
        raise QueueError("cannot read repository landing conditions: %s" % exc) from exc
    real = os.path.realpath(repo)
    identities = {p.name: {migrations.repo_key(p.github),
                           migrations.repo_key(migrations.origin_repo(p.repo))} - {""}
                  for p in projects}
    slugs = {migrations.repo_key(migrations.origin_repo(repo))} - {""}
    # A checkout without origin still has its configured repository identity.
    # An exact path contributes matches; it never suppresses another project's.
    for p in projects:
        if os.path.realpath(p.repo) == real:
            slugs.update(identities[p.name])
    matching = [p for p in projects if os.path.realpath(p.repo) == real or
                slugs & identities[p.name]]
    if len(matching) > 1:
        raise QueueError("multiple radar projects match this repository; landing conditions are ambiguous")
    return matching[0] if matching else None


def run(cfg, db, args) -> int:
    import sys
    from . import radar, resolutions
    try:
        if args.queue_command == "add":
            repo = resolutions.repo_root(args.repo)
            project = radar_project(cfg, repo)
            clone = resolutions.Replayer(repo, radar.clone_dir(cfg, project.name)) \
                if project else None
            row = enter(db, args.id, repo=repo, actor=args.actor, project=project,
                        clone=clone, cfg=cfg)
            print(json.dumps(row, ensure_ascii=False, indent=2, default=str) if args.json else
                  "lot #%d queued for %s at %s, class %s; the radar orders the queue at its "
                  "next pass" % (args.id, row["target"], row["frozen_sha"][:12],
                                 row.get("class") or "?"))
            return 0
        if args.queue_command == "remove":
            done = leave(db, args.id, actor=args.actor, reason=args.message)
            print(json.dumps({"lot": args.id, "removed": done}) if args.json else
                  "lot #%d %s" % (args.id, "taken out of the queue" if done
                                  else "was not queued"))
            return 0
        rows = storage.of(db).work.queue(args.target)
        # L539 : la classe de chaque lot de la file (lecture seule, best effort)
        from . import lot_class
        classes = {}
        for r in rows:
            info = lot_class.of_lot(db, int(r["work_item_id"]), cfg=cfg)
            classes[int(r["work_item_id"])] = info.get("class")
            r["class"] = info.get("class")
            r["class_why"] = info.get("why")
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(show_lines(rows, classes)) if rows else "no lot queued")
        return 0
    except (QueueError, GitError, plan_git.MergeProbeError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
