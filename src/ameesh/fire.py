# SPDX-License-Identifier: AGPL-3.0-only
"""Feu du pilote au gel : la fusion part à la CI verte (canon §2, lot #1374).

Le pilote donne son feu **au gel**, sur la tête exacte du lot
(`ameesh work fire <lot> --sha <commit>`), avant les relectures : la fusion
part alors d'elle-même dès que la CI requise est verte, sans attendre le tour
d'un agent. Le feu est un jalon (`fire`), révocable par le pilote (`unfire`,
`ameesh work fire <lot> --withdraw`) ; il ne vaut que pour le gel qu'il cite,
jamais pour le lot.

À chaque passage (`fire.run_pass`, appelé par le relevé des branches de
l'exécuteur), pour chaque lot ouvert qui porte un feu :

1. la PR est relue ; une PR fusionnée est réconciliée tout de suite
   (`work.close_merged`, et la relecture d'après fusion d'un lot léger) ;
2. un feu dont la tête a bougé est invalidé (`unfire`, auto-fusion désarmée,
   assigné réveillé) : le feu est à redonner ;
3. un feu actif et couvert par le gel courant arme la fusion automatique dès
   que les portes sont vertes (verdicts de la classe, CI verte sur cette tête,
   cible non-release) :
   * projet qui déclare `land` (`radar.projects`, Nexlink) : le lot entre dans
     la file de fusion (`merge_queue.enter`), l'exécutant l'atterrit ;
   * sinon (ameesh) : `gh pr merge --auto --match-head-commit <sha>` sur la PR
     ouverte — ouverte au besoin (`gh pr create`, règles de publication
     respectées) ;
4. tant que la fusion n'a pas eu lieu, la tête est revérifiée à chaque
   passage : `--match-head-commit` ne lie pas durablement le feu, c'est cette
   relecture qui garantit qu'aucun commit non couvert ne part.

Une opération par lot est tenue par un bail (`merge_locks`, clé `fire:<lot>`),
pour que deux hôtes ne désarment pas et ne réarment pas en même temps.
"""
from __future__ import annotations

import os
import socket

from . import land, lot_class, merge_queue, plan_git, storage, work

FIRE = "fire"
UNFIRE = "unfire"
FIRE_KINDS = (FIRE, UNFIRE)
#: lots examinés par passage (les feux sont rares : le plafond est large)
MAX_LOTS = 1000
#: durée du bail d'une opération de feu
LOCK_TTL_S = 120.0
#: résultats qu'un passage n'a pas à journaliser
QUIET = ("idle", "armed", "queued")
#: résultats qu'un passage de l'exécuteur dit dans son journal : état changé,
#: anomalie ou geste à faire. `not-ready` et les `would-*` se répètent à chaque
#: passage tant que la CI n'est pas verte : les dire serait du bruit.
LOUD = ("merged", "would-merge", "invalidated", "would-invalidate", "disarmed",
        "would-disarm", "error", "no-pr", "pr-closed", "ejected", "no-repository")
#: méthodes de fusion admises par `gh pr merge`
METHODS = ("merge", "squash", "rebase")


class FireError(RuntimeError):
    """A fire that cannot be given or withdrawn: actionable message."""


# --------------------------------------------------------------------------
# jalons
# --------------------------------------------------------------------------

def _order(mark: dict) -> tuple:
    return (float(mark.get("at_ts") or 0.0), int(mark.get("id") or 0))


def markers(marks) -> list[dict]:
    """Les jalons de feu d'un lot (`fire`, `unfire`), du plus ancien au plus
    récent."""
    return sorted((m for m in marks or () if m.get("kind") in FIRE_KINDS), key=_order)


def active(marks) -> dict | None:
    """Le feu en vigueur : le dernier jalon `fire` s'il n'est pas suivi d'un
    `unfire`, sinon None."""
    rows = markers(marks)
    return rows[-1] if rows and rows[-1].get("kind") == FIRE else None


def given(marks) -> dict | None:
    """Le dernier feu donné, même retiré (pour la trace)."""
    rows = [m for m in markers(marks) if m.get("kind") == FIRE]
    return rows[-1] if rows else None


def freeze_of(marks) -> dict | None:
    return plan_git.latest_freeze(marks or ())


def _norm(sha) -> str:
    return str(sha or "").strip().lower()


def same_commit(a, b, repo=None) -> bool:
    """Deux identifiants de commit qui nomment le même commit : égaux, ou
    résolus dans `repo` (un gel et un feu peuvent citer des abréviations
    différentes)."""
    left, right = _norm(a), _norm(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if repo:
        full_left = merge_queue.pinned(repo, left)
        full_right = merge_queue.pinned(repo, right)
        if full_left and full_right:
            return full_left == full_right
    return False


def state(db, lot: int) -> dict:
    """L'état du feu d'un lot, sans lecture GitHub : le feu actif, le gel
    qu'il couvre, et le dernier jalon écrit."""
    item = work.get(db, lot)
    if item is None:
        raise FireError("lot #%d not found" % lot)
    marks = work.milestones(db, lot)
    fire, last = active(marks), (markers(marks) or [None])[-1]
    freeze = freeze_of(marks)
    frozen = _norm((freeze or {}).get("sha"))
    return {
        "lot": int(lot), "title": item.get("title") or "", "item_state": item.get("state"),
        "branch": item.get("branch") or "", "freeze_sha": frozen,
        "active": bool(fire), "sha": _norm((fire or last or {}).get("sha")),
        "actor": (fire or last or {}).get("actor") or "",
        "covered": bool(fire and frozen and same_commit(fire.get("sha"), frozen)),
        "withdrawn": bool(last and last.get("kind") == UNFIRE),
        "at_ts": (fire or last or {}).get("at_ts"),
    }


def give(db, lot: int, *, sha: str, actor: str, note: str = "", repo: str | None = None) -> dict:
    """Donne le feu du pilote sur la tête gelée `sha` du lot (jalon `fire`).

    Refuse un lot terminé, un lot non gelé, un `sha` qui n'est pas un commit,
    ou un `sha` qui n'est pas le gel courant : le feu se donne sur la tête
    exacte, jamais sur un lot (« si la tête change, le feu est à redonner »).
    """
    item = work.get(db, lot)
    if item is None:
        raise FireError("lot #%d not found" % lot)
    if item["state"] in tuple(work.MERGED_STATES) + ("closed",):
        raise FireError("lot #%d is %s: no fire to give" % (lot, item["state"]))
    marks = work.milestones(db, lot)
    freeze = freeze_of(marks)
    frozen = _norm((freeze or {}).get("sha"))
    if not freeze or not frozen:
        raise FireError("lot #%d is not frozen: freeze it first (`ameesh work milestone %d frozen "
                        "--sha <commit>`); the fire is given on the exact head" % (lot, lot))
    wanted = _norm(sha)
    if not wanted:
        raise FireError("the fire names the frozen head: `ameesh work fire %d --sha %s`"
                        % (lot, frozen[:12]))
    if not merge_queue._HEX.fullmatch(wanted):
        raise FireError("--sha: a commit id (7 to 64 hex characters), not %r" % sha)
    if not same_commit(wanted, frozen, repo):
        raise FireError("the fire names %s, not the current freeze %s of lot #%d: the fire is "
                        "given on the exact head (freeze it again, then give the fire)"
                        % (wanted[:12], frozen[:12], lot))
    full = merge_queue.pinned(repo, wanted) if repo else None
    return work.milestone(db, lot, FIRE, sha=full or wanted, actor=actor,
                          note=(note or "").strip())


def withdraw(db, lot: int, *, actor: str, note: str = "", repo: str | None = None) -> dict:
    """Retire le feu actif d'un lot (jalon `unfire`) : l'auto-fusion est
    désarmée au passage suivant, rien d'autre n'est annulé."""
    item = work.get(db, lot)
    if item is None:
        raise FireError("lot #%d not found" % lot)
    fire = active(work.milestones(db, lot))
    if fire is None:
        raise FireError("lot #%d carries no active fire: nothing to withdraw" % lot)
    return work.milestone(db, lot, UNFIRE, sha=_norm(fire.get("sha")), actor=actor,
                          note=(note or "withdrawn by %s" % actor).strip())


# --------------------------------------------------------------------------
# courrier
# --------------------------------------------------------------------------

def _passive(db, to, lot: int, text: str, data: dict) -> None:
    """Le mot au fil du lot, sans réveiller personne (clé `radar`)."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return
    try:
        mail.send(db, work.SYSTEM_SENDER, to, text, kind="event", work_item_id=str(lot),
                  payload={"radar": dict(data, kind="fire")})
    except Exception:  # noqa: BLE001 - un mot n'est jamais fatal
        pass


def _wake(db, to, lot: int, text: str, data: dict) -> int | None:
    """Un courrier ordinaire (il réveille, L125) : il y a une geste à faire."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot), payload={"fire": dict(data, kind="fire")})
    except Exception:  # noqa: BLE001 - retenté au passage suivant
        return None


# --------------------------------------------------------------------------
# le dépôt, la cible, GitHub
# --------------------------------------------------------------------------

def _repo_of(db, row: dict) -> str | None:
    """Le dossier de travail de l'assigné (registre), comme le relevé des
    branches ; None si l'assigné n'en a pas ou si le dossier a disparu."""
    from . import registry
    assignee = str(row.get("assignee") or "").strip()
    if not assignee or ":" in assignee:
        return None
    try:
        reg = registry.get(db, assignee)
    except Exception:  # noqa: BLE001 - registre illisible : pas de dépôt
        return None
    path = os.path.expanduser(str((reg or {}).get("cwd") or ""))
    return path if path and os.path.isdir(path) else None


def _slug(cfg, item: dict, repo: str | None) -> str | None:
    """`owner/repo` du lot : son issue, le projet déclaré, sinon l'`origin` du
    dossier de travail."""
    from . import migrations
    try:
        return work.lot_repository(item, cfg)
    except work.WorkError:
        pass
    origin = migrations.origin_repo(repo) if repo else None
    return migrations.repo_key(origin) if origin else None


def _project(cfg, repo: str | None, slug: str | None):
    """Le projet `radar.projects` qui porte ce dépôt, ou None."""
    from . import migrations, radar
    try:
        projects = list(radar.settings_of(cfg).projects.values())
    except radar.SettingsError as exc:
        raise FireError("cannot read repository landing conditions: %s" % exc) from exc
    if repo:
        project = merge_queue.radar_project(cfg, repo)
        if project is not None:
            return project
    if slug:
        key = migrations.repo_key(slug)
        for project in projects:
            if migrations.repo_key(project.github) == key:
                return project
    return None


def _repo_info(slug: str, *, gh, cache: dict) -> dict:
    """`gh api repos/<slug>` : `allow_auto_merge`, branche par défaut, visibilité."""
    if slug in cache:
        return cache[slug]
    proc = gh(["api", "repos/%s" % slug])
    if proc.returncode != 0:
        raise land.LandError("gh api repos/%s: %s"
                             % (slug, (proc.stderr or proc.stdout or "").strip()[:300]))
    data = land._json(proc, "gh api repos/%s" % slug, dict)
    info = {"slug": slug, "allow_auto_merge": bool(data.get("allow_auto_merge")),
            "default_branch": str(data.get("default_branch") or ""),
            "private": bool(data.get("private")),
            "merge": bool(data.get("allow_merge_commit", True)),
            "squash": bool(data.get("allow_squash_merge", True)),
            "rebase": bool(data.get("allow_rebase_merge", True))}
    cache[slug] = info
    return info


def _pr_status(slug: str, branch: str, *, gh) -> dict | None:
    """La PR de la branche : la plus récente ouverte, sinon la plus récente
    fusionnée ; None s'il n'y en a aucune. Une erreur de `gh` est levée."""
    fields = ("number,state,headRefOid,baseRefName,isDraft,autoMergeRequest,"
              "statusCheckRollup,mergeCommit,url,isCrossRepository")
    proc = gh(["pr", "list", "--repo", slug, "--head", branch, "--state", "all",
               "--limit", "10", "--json", fields])
    if proc.returncode != 0:
        raise land.LandError("gh pr list --head %s: %s"
                             % (branch, (proc.stderr or proc.stdout or "").strip()[:300]))
    rows = land._json(proc, "gh pr list --head %s" % branch, list)
    rows = [r for r in rows if not r.get("isCrossRepository")]
    opened = [r for r in rows if str(r.get("state") or "").upper() == "OPEN"]
    merged = [r for r in rows if str(r.get("state") or "").upper() == "MERGED"]
    row = (opened or merged or rows or [None])[0]
    if row is None:
        return None
    rollup = row.get("statusCheckRollup") or []
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    from . import radar
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    commit = row.get("mergeCommit") or {}
    return {
        "number": row.get("number"), "state": str(row.get("state") or "").upper(),
        "head": _norm(row.get("headRefOid")), "base": str(row.get("baseRefName") or ""),
        "draft": bool(row.get("isDraft")), "ci": ci,
        "merged": str(row.get("state") or "").upper() == "MERGED",
        "merge_commit": _norm(commit.get("oid") if isinstance(commit, dict) else ""),
        "armed": row.get("autoMergeRequest") is not None, "url": row.get("url") or "",
    }


def _target(row: dict, project, repo: str | None, info: dict | None) -> str:
    """La cible d'intégration du lot : celle du lot, du projet, du dépôt, puis
    la branche par défaut GitHub. Rendue sans `origin/`."""
    target = str(row.get("branch_target") or "").strip()
    if not target and project is not None:
        target = str(getattr(project, "target", "") or "").strip()
    if not target and repo:
        try:
            target = plan_git.default_target(repo) or ""
        except plan_git.MergeProbeError:
            target = ""
    if not target and info:
        target = info.get("default_branch") or ""
    return land._branch_of(target) if target else ""


# --------------------------------------------------------------------------
# ouvrir la PR
# --------------------------------------------------------------------------

def _guard(cfg, db):
    """Le contrôle de publication (L126) : secrets, termes exclus, noms d'hôtes."""
    from . import plan_github
    try:
        settings = plan_github.settings_of(cfg)
    except plan_github.SettingsError:
        settings = plan_github.Settings()
    names = {socket.gethostname()}
    try:
        names |= {row.get("host") for row in storage.of(db).hosts.current(None)}
    except Exception:  # noqa: BLE001 - sans hôtes connus, le contrôle reste
        pass
    return plan_github.make_guard(settings, hosts=sorted(n for n in names if n),
                                  forge_hosts=getattr(cfg, "forge_hosts", None) or ())


def _pr_body(item: dict, *, slug: str, public: bool) -> str:
    """Le corps de la PR ouverte par ameesh : les deux lignes de liaison
    (`ameesh-work`, `Closes`), puis ce que la visibilité du dépôt autorise."""
    from . import plan_github
    lot = int(item["id"])
    lines = ["ameesh-work: %d" % lot]
    ref = plan_github._ref_parts(item.get("issue_ref"))
    if ref and ref[0].casefold() == slug.casefold():
        lines.append("Closes #%d" % ref[1])
    if public:
        summary = plan_github.public_summary(item.get("body"))
        if summary:
            lines.append("")
            lines.append(plan_github._quiet(summary))
    else:
        text = plan_github._clean(item.get("body"))
        if text:
            lines.append("")
            lines.append(plan_github._quiet(plan_github._cut(text, plan_github.BODY_MAX)))
    return "\n".join(lines)


def _open_pr(cfg, db, item: dict, *, slug: str, target: str, info: dict, gh) -> tuple:
    """Ouvre la PR de la branche gelée si elle n'existe pas (idempotent : la
    PR n'est créée que si la lecture n'en a trouvé aucune). Rend `(statut ou
    None, raison)`. Les règles de publication (L126) sont respectées : un
    titre ou un corps refusé n'est jamais publié."""
    title = str(item.get("title") or "").strip()
    if not title:
        return None, "lot without title: PR not opened"
    public = not info.get("private")
    guard = _guard(cfg, db)
    reasons = guard.problems(title, public=public)
    if reasons:
        return None, "title not publishable (%s): PR not opened" % ", ".join(reasons)
    body = _pr_body(item, slug=slug, public=public)
    reasons = guard.problems(body, public=public)
    if reasons:
        return None, "body not publishable (%s): PR not opened" % ", ".join(reasons)
    proc = gh(["pr", "create", "--repo", slug, "--head", str(item.get("branch") or ""),
               "--base", target or info.get("default_branch") or "",
               "--title", title, "--body", body], timeout=120.0)
    if proc.returncode != 0:
        return None, "gh pr create: %s" % (proc.stderr or proc.stdout or "").strip()[:300]
    return _pr_status(slug, str(item.get("branch") or ""), gh=gh), "opened"


# --------------------------------------------------------------------------
# armer, désarmer
# --------------------------------------------------------------------------

def _method(project, info: dict) -> str:
    """La méthode de fusion : celle du projet si le dépôt l'autorise, sinon la
    première autorisée, sinon `merge`."""
    wanted = str(getattr(project, "merge_method", "") or "merge")
    allowed = [m for m in METHODS if info.get(m, True)]
    if wanted in allowed:
        return wanted
    return allowed[0] if allowed else "merge"


def _arm_auto(slug: str, status: dict, sha: str, method: str, *, gh) -> tuple:
    """Arme l'auto-fusion GitHub sur le commit `sha` (idempotent : relu si
    l'appel échoue). Rend `(ok, detail)`."""
    proc = gh(["pr", "merge", str(status["number"]), "--repo", slug, "--%s" % method,
               "--auto", "--match-head-commit", sha], timeout=120.0)
    if proc.returncode == 0:
        return True, "auto-merge armed on %s" % sha[:12]
    error = (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode
    return False, error


def _disarm_auto(slug: str, status: dict, *, gh) -> tuple:
    """Désarme l'auto-fusion GitHub (idempotent de même). Rend `(ok, detail)`."""
    proc = gh(["pr", "merge", str(status["number"]), "--repo", slug, "--disable-auto"],
              timeout=120.0)
    if proc.returncode == 0:
        return True, "auto-merge disarmed"
    error = (proc.stderr or proc.stdout or "").strip()[:300] or "exit %d" % proc.returncode
    return False, error


# --------------------------------------------------------------------------
# les portes de l'auto-fusion (miroir de `merge_queue.readiness`, sans clone)
# --------------------------------------------------------------------------

def ready(cfg, db, row: dict, marks: list, status: dict, *, repo: str | None,
          project, target: str) -> tuple:
    """Les portes de l'auto-fusion : gel couvert, verdicts de la classe, CI
    verte sur la tête exacte, cible non-release. Rend `(ok, raison, classe)`.

    Un doute de classe (dépôt inconnu, diff illisible) vaut sensible : le
    verdict reste exigé. Une cible `release/*` (classe release) n'est jamais
    une auto-fusion : c'est la procédure de release du pilote."""
    lot = int(row["id"])
    freeze = freeze_of(marks)
    frozen = _norm((freeze or {}).get("sha"))
    head = merge_queue.pinned(repo, frozen) if repo else None
    klass = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=repo or None,
                             base=target or None, head=head)
    light = klass.get("class") == lot_class.LIGHT
    if not light:
        if klass.get("class") == lot_class.RELEASE:
            return False, "class release (%s): the release procedure is the pilot's, not the fire" \
                % (klass.get("why") or "release"), klass
        verdict = merge_queue.latest_verdict(marks)
        if not verdict or verdict.get("verdict") != "ok" \
                or not same_commit((verdict or {}).get("sha"), frozen, repo):
            return False, ("no review verdict ok on the freeze %s (class: %s)"
                           % (frozen[:12], klass.get("class") or "?")), klass
    if status.get("draft"):
        return False, "PR #%s is a draft" % status.get("number"), klass
    if target and status.get("base") and status["base"] != target:
        return False, "PR #%s targets %s, not %s" % (status.get("number"), status["base"],
                                                     target), klass
    if status.get("ci") != "green":
        return False, "the CI of PR #%s is %s on the freeze %s" % (
            status.get("number"), status.get("ci"), frozen[:12]), klass
    if project is not None and getattr(project, "ci_required", False):
        try:
            merge_queue._required_ci(project, frozen)
        except merge_queue.QueueError as exc:
            return False, str(exc), klass
    return True, "", klass


# --------------------------------------------------------------------------
# une opération sous bail
# --------------------------------------------------------------------------

def _lock(db, lot: int):
    """Le bail d'une opération de feu, ou None si un autre hôte la tient."""
    ops = storage.of(db).work
    owner = "fire:%s:%d" % (socket.gethostname(), os.getpid())
    key = "fire:%d" % lot
    epoch = ops.land_lock(key, owner, LOCK_TTL_S)
    return (ops, owner, epoch, key) if epoch is not None else None


def _unlock(handle) -> None:
    ops, owner, epoch, key = handle
    try:
        ops.land_unlock(key, owner, epoch)
    except Exception:  # noqa: BLE001 - le bail expire seul
        pass


def _reconcile(db, row: dict, marks: list, status: dict, *, slug: str, repo: str | None,
               target: str, dry_run: bool) -> dict:
    """La PR est fusionnée : le lot passe `merged` tout de suite."""
    lot = int(row["id"])
    freeze = freeze_of(marks)
    fire = given(marks)
    covered = bool(fire) and same_commit(status.get("head"), (fire or {}).get("sha"))
    covered = covered or (bool(freeze) and same_commit(status.get("head"),
                                                       (freeze or {}).get("sha")))
    out = {"lot": lot, "result": "merged", "pr": status.get("number"),
           "sha": status.get("merge_commit") or "", "covered": covered}
    if dry_run:
        out["result"] = "would-merge"
        return out
    done = work.close_merged(
        db, lot, sha=status.get("merge_commit") or "", actor=work.SYSTEM_SENDER,
        source="PR #%s" % status.get("number"),
        pr_ref="%s#%s" % (slug, status.get("number")),
        frozen_id=int(freeze["id"]) if freeze else None,
        extra=("" if covered else "the PR merged %s, not the head the fire covered (%s): "
               "the fire did not cover this merge"
               % ((status.get("head") or "?")[:12], _norm((fire or {}).get("sha"))[:12])))
    out.update(result=done["result"], detail=done["detail"])
    if done["result"] == "merged":
        _passive(db, row.get("assignee"), lot, (
            "Fire on lot #%d « %s »: PR #%s merged (%s)%s. The branch sweep closes the lot."
            % (lot, row.get("title") or "", status.get("number"),
               (status.get("merge_commit") or "?")[:12],
               "" if covered else ". WARNING: the merged head is not the one the fire covered; "
               "the fire is to be given again on a new freeze")),
            {"project": row.get("app") or "", "pr": status.get("number"), "merged": True})
        from . import review_lot
        try:
            out["review_lot"] = review_lot.open_after_merge(
                db, lot=lot, item=row, merged_sha=status.get("merge_commit") or "",
                pr=status.get("number"), target=target,
                klass=lot_class.of_lot(db, lot, item=row, repo=repo, base=target or None,
                                       head=merge_queue.pinned(repo, _norm((freeze or {}).get(
                                           "sha"))) if repo else None))
        except Exception as exc:  # noqa: BLE001 - la fusion est un fait
            out["review_lot"] = {"id": None, "created": False,
                                 "why": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    if not covered:
        _wake(db, row.get("assignee"), lot, (
            "Fire on lot #%d « %s »: PR #%s was merged at %s while the fire covered %s. "
            "The merge is recorded; check what landed and give the fire again on a new freeze."
            % (lot, row.get("title") or "", status.get("number"),
               (status.get("head") or "?")[:12], _norm((fire or {}).get("sha"))[:12] or "—")),
            {"project": row.get("app") or "", "pr": status.get("number"), "anomaly": True})
    return out


def _invalidate(db, row: dict, marks: list, fire: dict, reason: str, *, status, gh, slug: str,
                dry_run: bool) -> dict:
    """Le feu ne couvre plus la tête : `unfire`, désarmement, assigné réveillé."""
    lot = int(row["id"])
    out = {"lot": lot, "result": "invalidated", "detail": reason,
           "sha": _norm(fire.get("sha"))}
    if dry_run:
        out["result"] = "would-invalidate"
        return out
    work.milestone(db, lot, UNFIRE, sha=_norm(fire.get("sha")), actor=work.SYSTEM_SENDER,
                   note="invalidated: %s" % reason)
    if status and status.get("armed"):
        ok, detail = _disarm_auto(slug, status, gh=gh)
        out["disarmed"] = ok
        if not ok:
            out["detail"] = "%s; disarm failed (%s)" % (reason, detail)
    _wake(db, row.get("assignee"), lot, (
        "Fire on lot #%d « %s »: %s. The automatic merge is disarmed; freeze again and have the "
        "pilot give the fire on the new head." % (lot, row.get("title") or "", reason)),
        {"project": row.get("app") or "", "reason": reason})
    _passive(db, fire.get("actor"), lot, (
        "Fire on lot #%d « %s » (%s) is invalidated: %s. It is to be given again on the new head."
        % (lot, row.get("title") or "", _norm(fire.get("sha"))[:12], reason)),
        {"project": row.get("app") or "", "reason": reason})
    return out


def _disarm(db, row: dict, status: dict, reason: str, *, gh, slug: str, dry_run: bool) -> dict:
    lot = int(row["id"])
    out = {"lot": lot, "result": "disarmed", "detail": reason}
    if dry_run:
        out["result"] = "would-disarm"
        return out
    ok, detail = _disarm_auto(slug, status, gh=gh)
    out["disarmed"] = ok
    if not ok:
        # relu : un autre hôte a pu désarmer entre-temps
        try:
            after = _pr_status(slug, str(row.get("branch") or ""), gh=gh)
        except land.LandError:
            after = None
        if after is not None and not after.get("armed"):
            out["disarmed"] = True
        else:
            out.update(result="error", error=detail)
    return out


# --------------------------------------------------------------------------
# le passage
# --------------------------------------------------------------------------

def run_pass(cfg, db, *, gh=None, dry_run: bool = False, only_lot: int | None = None,
             repo: str | None = None, limit: int = MAX_LOTS, rows=None,
             lock: bool = True) -> list[dict]:
    """Un passage du feu sur les lots ouverts qui en portent un.

    `only_lot` restreint à un lot (la CLI) ; `repo` force le dépôt (idem) ;
    `rows` permet au relevé des branches de passer sa propre sélection.
    Chaque opération d'un lot est tenue par un bail : un autre hôte qui la
    tient rend `busy`. Ne lève jamais pour un lot : l'erreur est rendue."""
    st = storage.of(db)
    if rows is None and only_lot is not None:
        single = work.get(db, only_lot)
        rows = [single] if single is not None and single.get("state") not in \
            tuple(work.MERGED_STATES) + ("closed",) else []
    if rows is None:
        rows = st.work.open_for_sweep(limit)
    wanted = [r for r in rows if only_lot is None or int(r["id"]) == int(only_lot)]
    if not wanted:
        return []
    marks: dict = {}
    for mark in st.progress.lot_milestones([int(r["id"]) for r in wanted]):
        marks.setdefault(int(mark["work_item_id"]), []).append(mark)
    gh = gh or land._gh
    cache: dict = {}
    out = []
    for row in wanted:
        lot = int(row["id"])
        if not markers(marks.get(lot, [])):
            continue
        try:
            out.append(_one(cfg, db, row, marks.get(lot, []), gh=gh, dry_run=dry_run,
                            repo=repo, cache=cache, lock=lock))
        except Exception as exc:  # noqa: BLE001 - jamais fatal pour un lot
            out.append({"lot": lot, "result": "error",
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
    return out


def _one(cfg, db, row: dict, marks: list, *, gh, dry_run: bool, repo: str | None,
         cache: dict, lock: bool) -> dict:
    lot = int(row["id"])
    handle = _lock(db, lot) if (lock and not dry_run) else None
    if lock and not dry_run and handle is None:
        return {"lot": lot, "result": "busy"}
    try:
        return _act(cfg, db, row, marks, gh=gh, dry_run=dry_run, repo=repo, cache=cache)
    finally:
        if handle is not None:
            _unlock(handle)


def _act(cfg, db, row: dict, marks: list, *, gh, dry_run: bool, repo: str | None,
         cache: dict) -> dict:
    lot = int(row["id"])
    fire, freeze = active(marks), freeze_of(marks)
    branch = str(row.get("branch") or "")
    out: dict = {"lot": lot, "branch": branch, "fire": bool(fire),
                 "fire_sha": _norm((fire or {}).get("sha")),
                 "freeze_sha": _norm((freeze or {}).get("sha")), "result": "idle"}
    if not branch:
        out.update(result="no-branch", detail="lot without branch: no PR to arm")
        return out
    work_repo = repo or _repo_of(db, row)
    slug = _slug(cfg, row, work_repo)
    if not slug:
        out.update(result="no-repository", detail="no GitHub repository known for this lot")
        return out
    project = _project(cfg, work_repo, slug)
    status = _pr_status(slug, branch, gh=gh)
    out.update(slug=slug, repo=work_repo,
               project=project.name if project is not None else "",
               pr=(status or {}).get("number"))

    # 1. une PR fusionnée est un fait : réconciliée tout de suite, quel que
    #    soit le feu (retiré, invalide, absent).
    if status is not None and status.get("merged"):
        return _reconcile(db, row, marks, status, slug=slug, repo=work_repo,
                          target=str(row.get("branch_target") or ""), dry_run=dry_run)

    # 2. sans feu actif : désarmer ce qui aurait pu l'être, et rien d'autre.
    if fire is None:
        if status is not None and status.get("armed"):
            return _disarm(db, row, status,
                           "no active fire on lot #%d (withdrawn or invalidated)" % lot,
                           gh=gh, slug=slug, dry_run=dry_run)
        out.update(result="withdrawn" if markers(marks) else "idle")
        return out

    # 3. le feu ne couvre pas le gel courant, ou la tête a bougé : invalidation.
    if not freeze or not same_commit(fire.get("sha"), (freeze or {}).get("sha"), work_repo):
        return _invalidate(db, row, marks, fire,
                           "the freeze is %s, not the head the fire covers (%s)"
                           % (_norm((freeze or {}).get("sha"))[:12] or "—",
                              _norm(fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run)
    if status is not None and status.get("head") \
            and not same_commit(status.get("head"), fire.get("sha"), work_repo):
        return _invalidate(db, row, marks, fire,
                           "the PR head moved to %s, the fire covers %s"
                           % (status["head"][:12], _norm(fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run)

    info = _repo_info(slug, gh=gh, cache=cache)
    target = _target(row, project, work_repo, info)
    queue_mode = project is not None and getattr(project, "land", "off") != "off"
    if not queue_mode and not info.get("allow_auto_merge"):
        queue_mode = True   # repli : dépôt sans auto-fusion → file de fusion

    # 4. la PR manquante est ouverte (mode GitHub), ou le lot entre en file.
    if status is not None and status.get("state") == "CLOSED" and not status.get("merged"):
        out.update(result="pr-closed", detail="PR #%s is closed without merge: reopen it"
                   % status.get("number"))
        return out
    detail = ""
    if queue_mode:
        return _queue(db, row, marks, fire, project=project, repo=work_repo, target=target,
                      cfg=cfg, dry_run=dry_run, out=out)
    if status is None:
        if dry_run:
            out.update(result="would-open-pr", detail="no PR for %s" % branch)
            return out
        status, detail = _open_pr(cfg, db, row, slug=slug, target=target, info=info, gh=gh)
        if status is None:
            out.update(result="no-pr", detail=detail)
            return out
        out.update(pr=status.get("number"), detail=detail)
        _passive(db, row.get("assignee"), lot,
                 "Fire on lot #%d « %s »: PR #%s opened by ameesh for %s."
                 % (lot, row.get("title") or "", status.get("number"), branch),
                 {"project": row.get("app") or "", "pr": status.get("number")})
    if status.get("head") and not same_commit(status.get("head"), fire.get("sha"), work_repo):
        return _invalidate(db, row, marks, fire,
                           "the PR head moved to %s, the fire covers %s"
                           % (status["head"][:12], _norm(fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run)

    # 5. les portes du feu (verdicts, CI, cible), puis l'armement.
    ok, why, klass = ready(cfg, db, row, marks, status, repo=work_repo, project=project,
                           target=target)
    out["class"] = klass.get("class")
    if not ok:
        if status.get("armed"):
            return _disarm(db, row, status, "fire gates no longer hold: %s" % why,
                           gh=gh, slug=slug, dry_run=dry_run)
        out.update(result="not-ready", detail=why)
        return out
    if status.get("armed"):
        out.update(result="armed", detail="auto-merge already armed on %s"
                   % _norm(fire.get("sha"))[:12])
        return out
    if dry_run:
        out.update(result="would-arm", detail="PR #%s, class %s, CI green"
                   % (status.get("number"), klass.get("class")))
        return out
    ok, error = _arm_auto(slug, status, _norm(fire.get("sha")), _method(project, info), gh=gh)
    if not ok:
        # relu : un autre hôte a pu armer entre-temps (idempotence)
        try:
            after = _pr_status(slug, branch, gh=gh)
        except land.LandError:
            after = None
        if after is not None and after.get("armed"):
            ok, error = True, "already armed by another pass"
        else:
            out.update(result="error", error=error)
            return out
    out.update(result="armed", detail=error)
    _passive(db, row.get("assignee"), lot, (
        "Fire on lot #%d « %s »: automatic merge armed on %s (PR #%s, class %s). It departs "
        "as soon as the CI is green; a commit pushed after the freeze invalidates the fire."
        % (lot, row.get("title") or "", _norm(fire.get("sha"))[:12], status.get("number"),
           klass.get("class") or "?")),
        {"project": row.get("app") or "", "pr": status.get("number"), "sha": fire.get("sha")})
    _passive(db, fire.get("actor"), lot,
             "Fire on lot #%d « %s » is armed on %s (PR #%s)."
             % (lot, row.get("title") or "", _norm(fire.get("sha"))[:12], status.get("number")),
             {"project": row.get("app") or "", "pr": status.get("number")})
    return out


def _queue(db, row: dict, marks: list, fire: dict, *, project, repo: str | None, target: str,
           cfg, dry_run: bool, out: dict) -> dict:
    """Le repli file de fusion : le lot entre dans la file de sa cible ; c'est
    l'exécutant qui l'atterrit (`land`). Un lot déjà en file ou en atterrissage
    n'est pas ré-entré (une ré-entrée remettrait `landing` à `queued`)."""
    lot = int(row["id"])
    if not repo:
        out.update(result="no-repository", detail="no repository path known for this lot")
        return out
    rows = {int(r["work_item_id"]): r for r in storage.of(db).work.queue()}
    entry = rows.get(lot)
    if entry is not None:
        state = entry.get("state") or "queued"
        if state in ("queued", "landing", "landed"):
            out.update(result=state, detail="merge queue of %s: %s" % (entry.get("target"), state))
            return out
        out.update(result="ejected", detail="merge queue of %s: ejected (%s)"
                   % (entry.get("target"), entry.get("reason") or "?"))
        return out
    if dry_run:
        out.update(result="would-queue", detail="enter the merge queue of %s" % (target or "?"))
        return out
    from . import radar, resolutions
    name = project.name if project is not None else os.path.basename((repo or "project").rstrip("/"))
    clone = resolutions.Replayer(repo, radar.clone_dir(cfg, name)) if repo else None
    try:
        entered = merge_queue.enter(db, lot, repo=repo, actor=work.SYSTEM_SENDER,
                                    project=project, clone=clone, cfg=cfg)
    except merge_queue.QueueError as exc:
        out.update(result="not-ready", detail=str(exc))
        return out
    out.update(result="queued", target=entered.get("target"))
    _passive(db, row.get("assignee"), lot, (
        "Fire on lot #%d « %s »: entered the merge queue of %s (frozen %s%s); the executor "
        "lands it as soon as the CI is green."
        % (lot, row.get("title") or "", entered.get("target"),
           _norm(fire.get("sha"))[:12],
           ", PR #%s" % entered.get("pr") if entered.get("pr") else "")),
        {"project": row.get("app") or "", "target": entered.get("target"),
         "sha": fire.get("sha")})
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "fire", help="the pilot's go at freeze (#1374): ameesh arms the automatic merge, "
                     "which departs at green CI")
    parser.add_argument("id", type=int)
    parser.add_argument("--sha", default="",
                        help="the frozen head the fire covers (to give the fire)")
    parser.add_argument("--withdraw", action="store_true",
                        help="withdraw the fire: the automatic merge is disarmed")
    parser.add_argument("--repo", default=None,
                        help="the lot's repository (default: the current git repository)")
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="",
                        help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def run(cfg, db, args) -> int:
    """`ameesh work fire <lot> --sha S | --withdraw | (rien : l'état)`. Le feu
    est un geste de pilote : humain, orchestrateur ou agent de conception
    (`work.corrector`)."""
    import json
    import sys

    from . import resolutions
    if args.sha and args.withdraw:
        print("--sha and --withdraw are exclusive", file=sys.stderr)
        return 2
    try:
        repo = resolutions.repo_root(args.repo) if args.repo else None
    except (resolutions.ResolveError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        # l'état du feu se lit par tous ; donner ou retirer le feu est le geste
        # du pilote (humain, orchestrateur ou agent de conception).
        if args.sha or args.withdraw:
            try:
                actor = work.corrector(cfg, db, args.actor)
            except work.WorkError as exc:
                print("the fire is the pilot's gesture (a human, an orchestrator or a design "
                      "agent): %s" % exc, file=sys.stderr)
                return 1
        if args.sha:
            row = give(db, args.id, sha=args.sha, actor=actor, note=args.note, repo=repo)
            print("fire given to lot #%d on %s (by %s)" % (args.id, row["sha"][:12], actor))
            results = run_pass(cfg, db, only_lot=args.id, repo=repo)
        elif args.withdraw:
            row = withdraw(db, args.id, actor=actor, note=args.note, repo=repo)
            print("fire withdrawn from lot #%d (%s)" % (args.id, row["sha"][:12] or "—"))
            results = run_pass(cfg, db, only_lot=args.id, repo=repo)
        else:
            now = state(db, args.id)
            print("lot #%d « %s »: %s" % (
                now["lot"], now["title"],
                "fire %s by %s%s" % (now["sha"][:12] or "?", now["actor"] or "?",
                                     "" if now["covered"] else " (NOT the current freeze %s)"
                                     % now["freeze_sha"][:12])
                if now["active"] else
                ("no active fire (withdrawn)" if now["withdrawn"]
                 else "no active fire%s" % (" (freeze %s)" % now["freeze_sha"][:12]
                                            if now["freeze_sha"] else " (not frozen)"))))
            if args.json:
                print(json.dumps(now, ensure_ascii=False, indent=2, default=str))
            return 0
    except FireError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for entry in results:
        if args.json:
            print(json.dumps(entry, ensure_ascii=False, default=str))
        else:
            print("lot #%d: %s%s" % (entry["lot"], entry["result"],
                                     " — %s" % (entry.get("detail") or entry.get("error"))
                                     if entry.get("detail") or entry.get("error") else ""))
    return 0
