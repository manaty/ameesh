# SPDX-License-Identifier: AGPL-3.0-only
"""Feu du pilote au gel : la fusion part à la CI verte (canon §2, lot #1374).

Le pilote donne son feu **au gel**, sur la tête exacte du lot
(`ameesh work fire <lot> --sha <commit>`), avant les relectures : la fusion
part alors d'elle-même dès que les portes sont vertes, sans attendre le tour
d'un agent. Le feu est un jalon (`fire`), révocable par le pilote (`unfire`,
`ameesh work fire <lot> --withdraw`) ; il ne vaut que pour le gel qu'il cite,
jamais pour le lot.

À chaque passage (`fire.run_pass`, appelé par le relevé des branches de
l'exécuteur), pour chaque lot ouvert qui porte un feu, **dans cet ordre**
(#1374, P1 : les verdicts avant la PR, la CI avant la fusion) :

1. la PR est relue ; une PR fusionnée est réconciliée tout de suite
   (`work.close_merged`, et la relecture d'après fusion d'un lot léger) ;
2. un feu retiré ou dont la tête a bougé est invalidé (`unfire`, auto-fusion
   résiduelle désarmée, ligne de file retirée, assigné réveillé) : le feu est
   à redonner ;
3. **la porte des verdicts** est franchie (`merge_queue.review_gate`) : les
   avis requis par la classe, rendus sur la tête gelée, indépendants
   (familles distinctes, aucune celle de l'auteur) et sans blocage non levé.
   Tant qu'elle n'est pas franchie, aucune PR n'est ouverte, aucune ligne de
   file n'est écrite et aucune CI n'est déclenchée ;
4. la voie du lot (file ou fusion directe) est celle que **le projet déclare**,
   jamais celle de l'hôte qui a pris le bail (#2256) : un projet `land: local`
   ou `pr` entre en file ; `land: off` fusionne en direct ; un hôte qui ne
   déclare ni le projet ni sa file s'abstient (`no-land`) au lieu de fusionner
   en direct par défaut ;
5. la PR manquante est alors ouverte (mode GitHub) ou le lot entre dans la
   file de fusion (`merge_queue.enter`, Nexlink), une fois les portes vertes ;
6. en mode GitHub, la fusion est **faite par ce passage**, sous
   `gh pr merge --match-head-commit <sha>` : la tête autorisée est vérifiée au
   geste effectif, et aucune auto-fusion GitHub n'est laissée armée entre deux
   passages — GitHub fusionnerait une tête poussée après le feu (P1-4). Une
   auto-fusion trouvée armée est désarmée avant tout autre geste ;
7. en mode file, l'atterrissage re-vérifie le feu et les blocages au dernier
   geste (`fire.land_guard`, sous le bail de `land`).

Toute opération d'un lot — le passage, mais aussi `give` et `withdraw` — est
tenue par le même bail (`merge_locks`, clé `fire:<lot>`) : les jalons sont
relus sous le bail, renouvelé et vérifié juste avant chaque geste externe, et
feu, gel et verdicts sont revalidés juste avant la PR, la file ou la fusion.
Un détenteur périmé s'arrête (`busy`) au lieu d'agir sur un instantané
(P1-3, P2-6).

#3598 : un passage tient ce bail 15 à 20 s et le rend entre deux passages ;
`give` et `withdraw` l'attendent donc par tentatives courtes et espacées
jusqu'à `LOCK_WAIT_S` avant de refuser, en nommant le détenteur et son
échéance. Le feu ne se pose ni ne se retire sur un instantané : il attend la
fenêtre libre, sans jamais écrire hors bail.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from urllib.parse import quote

from . import land, lot_class, merge_queue, packet_guard, plan_git, storage, work

FIRE = "fire"
UNFIRE = "unfire"
FIRE_KINDS = (FIRE, UNFIRE)
#: lots examinés par passage (les feux sont rares : le plafond est large)
MAX_LOTS = 1000
#: durée du bail d'une opération de feu
LOCK_TTL_S = 120.0
#: #3598 : budget d'attente du bail par `give`/`withdraw` avant le refus. Un
#: passage d'auto-fusion tient le bail 15 à 20 s et le rend entre deux
#: passages : le retrait trouve une fenêtre libre bien avant la borne. À la
#: borne, le refus nomme le détenteur et son échéance.
LOCK_WAIT_S = LOCK_TTL_S
#: première attente entre deux tentatives, doublée jusqu'à LOCK_WAIT_MAX_S
LOCK_WAIT_FIRST_S = 0.25
LOCK_WAIT_MAX_S = 2.0
#: L165 (EP4) : marge du bail élu renouvelée juste avant `gh pr merge`
#: (timeout de 120 s + relecture de la PR). La course résiduelle est
#: documentée dans `docs/HOST-HANDOFFS.md`.
FIRE_CALL_MARGIN_S = 180.0
#: résultats qu'un passage n'a pas à journaliser
QUIET = ("idle", "armed", "queued", "busy", "withdrawn")
#: résultats qu'un passage de l'exécuteur dit dans son journal : état changé,
#: anomalie ou geste à faire. `not-ready` et les `would-*` se répètent à chaque
#: passage tant que la CI n'est pas verte : les dire serait du bruit.
LOUD = ("merged", "would-merge", "invalidated", "would-invalidate", "disarmed",
        "would-disarm", "error", "no-pr", "pr-closed", "ejected", "no-repository",
        "no-land")
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
    """Deux identifiants de commit qui nomment le même commit : égaux, résolus
    dans `repo` quand il peut nommer les deux, sinon par préfixe commun.

    Un gel, un feu et une tête de PR peuvent citer le même commit sur des
    longueurs différentes (`c4d85ac37` au gel, `c4d85ac37dce` à la tête) :
    l'égalité de chaînes seule invalidait un feu pourtant posé sur la même
    tête (#2349)."""
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
        # Un côté résolu compare par son SHA complet : une tête inconnue du
        # dépôt (poussée, pas encore lue) qui partage l'abréviation du feu
        # n'est pas le commit que le dépôt connaît sous cette abréviation.
        left, right = full_left or left, full_right or right
    # Pas de dépôt (ou un commit qu'il ne peut pas résoudre) : deux
    # abréviations du même commit sont préfixes l'une de l'autre (#1756).
    return plan_git.same_commit(left, right)


def state(db, lot: int) -> dict:
    """L'état du feu d'un lot, sans lecture GitHub : le feu actif, le gel
    qu'il couvre, et le dernier jalon écrit.

    La couverture compare les identifiants par préfixe (`_same_head`) : un
    gel peut citer `352ba651` quand le feu cite la tête complète."""
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
        "covered": bool(fire and frozen and _same_head(fire.get("sha"), frozen)),
        "withdrawn": bool(last and last.get("kind") == UNFIRE),
        "at_ts": (fire or last or {}).get("at_ts"),
    }


def give(db, lot: int, *, sha: str, actor: str, note: str = "", repo: str | None = None,
         exception: str = "", reason: str = "", wait: float | None = None) -> dict:
    """Donne le feu du pilote sur la tête gelée `sha` du lot (jalon `fire`).

    Refuse un lot terminé, un lot non gelé, un `sha` qui n'est pas un commit,
    ou un `sha` qui n'est pas le gel courant : le feu se donne sur la tête
    exacte, jamais sur un lot (« si la tête change, le feu est à redonner »).

    #2333 (canon §11 bis) : un feu sur une branche de travail (`lot/**`,
    `agent/**`, `review/**`) vers la cible d'intégration est refusé hors
    paquet, sauf exception `incident|unblock` motivée ; la déclaration est
    écrite au journal du lot sur cette tête (`packet_guard`) et le passage
    d'auto-fusion la relit.

    Le geste prend le bail du feu (`fire:<lot>`), celui du passage : un
    passage en cours d'armement ou de fusion ne peut pas ignorer un feu qui
    arrive, et le feu ne peut pas se poser pendant qu'un autre hôte agit
    (#1374, P1-3). Il l'attend jusqu'à `wait` (LOCK_WAIT_S par défaut) avant
    de refuser, en nommant alors le détenteur et son échéance (#3598)."""
    handle = _wait_lock(db, lot, wait=wait)
    try:
        # L165 (EP4) : le feu du pilote est un geste hors base (il arme la
        # fusion) ; l'époque du bail élu est vérifiée et journalisée avant.
        from . import pilot_gesture
        try:
            fence = pilot_gesture.gate(None, db, "merge", "fire #%d" % lot)
        except pilot_gesture.GestureRefused as exc:
            raise FireError(str(exc)) from exc
        return _give_locked(db, lot, sha=sha, actor=actor, note=note, repo=repo,
                            exception=exception, reason=reason, fence=fence)
    finally:
        _unlock(handle)


def _give_locked(db, lot: int, *, sha: str, actor: str, note: str = "",
                 repo: str | None = None, exception: str = "",
                 reason: str = "", fence=None) -> dict:
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
    # Le dépôt de l'assigné (registre) sert à résoudre les abréviations et à
    # enregistrer le SHA complet, comme le passage le fait déjà ; sans lui, la
    # comparaison par préfixe commun reste possible (#2349).
    repo = repo or _repo_of(db, item)
    if not same_commit(wanted, frozen, repo):
        raise FireError("the fire names %s, not the current freeze %s of lot #%d: the fire is "
                        "given on the exact head (freeze it again, then give the fire)"
                        % (wanted[:12], frozen[:12], lot))
    if exception:
        try:
            packet_guard.marker(exception, frozen, reason)
        except packet_guard.PacketGuardError as exc:
            raise FireError(str(exc)) from exc
    target = str(item.get("branch_target") or "").strip()
    if not target and repo:
        try:
            target = plan_git.default_target(repo) or ""
        except plan_git.MergeProbeError:
            target = ""
    full = merge_queue.pinned(repo, wanted) if repo else None

    def check(database):
        try:
            packet_guard.check_merge(database, lot, item.get("branch") or "", target,
                                     frozen, exception=exception, reason=reason,
                                     actor=actor)
        except packet_guard.PacketGuardError as exc:
            raise FireError(str(exc)) from exc

    if fence is None:
        check(db)
        return work.milestone(db, lot, FIRE, sha=full or wanted, actor=actor,
                              note=(note or "").strip())
    # L165 (EP4) : la déclaration `--exception` de `check_merge` et le jalon
    # sont des écritures du même geste ; hold, déclaration et jalon partagent
    # la transaction gardée après les lectures de préparation (revue cx2 B3).
    from . import pilot_gesture
    with db.transaction() as tx:
        try:
            fence.hold(tx)
        except pilot_gesture.GestureRefused as exc:
            raise FireError(str(exc)) from exc
        check(tx)
        return work.milestone(tx, lot, FIRE, sha=full or wanted, actor=actor,
                              note=(note or "").strip())


def withdraw(db, lot: int, *, actor: str, note: str = "", repo: str | None = None,
             wait: float | None = None) -> dict:
    """Retire le feu actif d'un lot (jalon `unfire`) : l'auto-fusion est
    désarmée et la ligne de file retirée au passage suivant, rien d'autre n'est
    annulé. Même bail que le passage (#1374, P1-3), attendu jusqu'à `wait`
    (LOCK_WAIT_S par défaut) : un passage le tient 15 à 20 s, le retrait doit
    trouver sa fenêtre ; à la borne, le refus nomme le détenteur et son
    échéance (#3598)."""
    handle = _wait_lock(db, lot, wait=wait)
    try:
        from . import pilot_gesture
        try:
            fence = pilot_gesture.gate(None, db, "merge", "unfire #%d" % lot)
        except pilot_gesture.GestureRefused as exc:
            raise FireError(str(exc)) from exc
        item = work.get(db, lot)
        if item is None:
            raise FireError("lot #%d not found" % lot)
        fire = active(work.milestones(db, lot))
        if fire is None:
            raise FireError("lot #%d carries no active fire: nothing to withdraw" % lot)
        if fence is None:
            return work.milestone(db, lot, UNFIRE, sha=_norm(fire.get("sha")), actor=actor,
                                  note=(note or "withdrawn by %s" % actor).strip())
        with db.transaction() as tx:
            try:
                fence.hold(tx)
            except pilot_gesture.GestureRefused as exc:
                raise FireError(str(exc)) from exc
            return work.milestone(tx, lot, UNFIRE, sha=_norm(fire.get("sha")),
                                  actor=actor,
                                  note=(note or "withdrawn by %s" % actor).strip())
    finally:
        _unlock(handle)


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

def _checkout(path: str | None) -> str | None:
    """La racine du checkout git `path`, ou None si `path` n'en est pas un.

    #2473 : un dossier peut exister sans être un dépôt. Le pilote Nexlink
    enregistre comme `cwd` de son agent le **parent** de ses clones
    (`/home/smichea/development/manaty`) : il n'a ni `origin` ni branches, et
    le lire comme un dépôt faisait répondre « branch … is neither local nor
    on origin » alors que la branche était poussée par le vrai clone. Le
    dossier d'un worktree se résout par `rev-parse` ; le dépôt du lot se
    choisit ensuite sur son `origin` (`_same_repository`)."""
    if not path:
        return None
    path = os.path.expanduser(str(path))
    if not path or not os.path.isdir(path):
        return None
    return plan_git._out(path, "rev-parse", "--show-toplevel") or None


def _repo_of(db, row: dict) -> str | None:
    """Le dossier de travail de l'assigné (registre), comme le relevé des
    branches ; None si l'assigné n'en a pas, si le dossier a disparu ou s'il
    n'est pas un checkout git (#2473)."""
    from . import registry
    assignee = str(row.get("assignee") or "").strip()
    if not assignee or ":" in assignee:
        return None
    try:
        reg = registry.get(db, assignee)
    except Exception:  # noqa: BLE001 - registre illisible : pas de dépôt
        return None
    return _checkout((reg or {}).get("cwd"))


def _same_repository(checkout: str, slug: str) -> bool:
    """Le checkout est-il celui du lot ? Un `origin` lisible qui nomme un
    autre dépôt l'écarte ; un checkout sans `origin` reste accepté (copie
    locale : `fetch_fresh` lit alors ses têtes locales)."""
    from . import migrations
    origin = migrations.origin_repo(checkout)
    return not origin or migrations.repo_key(origin) == migrations.repo_key(slug)


def _project_checkout(project) -> str | None:
    """Le clone que l'hôte déclare pour ce projet
    (`radar.projects.<p>.repo`), s'il est un checkout (#2473)."""
    return _checkout(getattr(project, "repo", "") or "") if project is not None else None


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


def _queue_declared(db, repo: str | None, slug: str | None) -> bool | None:
    """Le dépôt a déjà une file dans l'état partagé (#2256), ou None si illisible.

    Les lignes `merge_queue` portent la source du dépôt (`merge_queue.source_of`)
    et sont communes à tous les hôtes : peu importe celui qui les a écrites,
    elles déclarent que ce dépôt se fusionne par la file. Un hôte sans
    `radar.projects.<projet>` ne peut donc pas prétendre qu'il n'y a pas de
    file et retomber sur `_merge_direct` — c'est le défaut du paquet Nexlink 54.
    """
    from . import migrations
    sources = set()
    if repo:
        try:
            sources.add(merge_queue.normalize_source(merge_queue.source_of(repo)))
        except Exception:  # noqa: BLE001 - un dépôt illisible ne prouve rien
            pass
    key = migrations.repo_key(slug)
    if key.count("/") == 1:
        sources.add("github.com/%s" % key)
    if not sources:
        return False
    try:
        rows = storage.of(db).work.queue()
    except Exception:  # noqa: BLE001 - illisible ne signifie pas absent
        return None
    for row in rows or ():
        source = str(row.get("source") or "")
        if source and merge_queue.normalize_source(source) in sources:
            return True
    return False


def landing_path(cfg, db, project, repo: str | None, slug: str | None) -> tuple[str, str]:
    """La voie du feu — `queue`, `direct` ou `unknown` — et sa raison (#2256).

    La décision vient de la **déclaration du projet**, jamais de la config
    locale de l'hôte qui a pris le bail. Le paquet Nexlink 54 a été fusionné
    en direct parce que l'hôte qui tenait `fire:<lot>` ne déclarait pas
    `radar.projects.nexlink` et retombait sur `_merge_direct`, alors qu'un
    autre hôte déclarait sa file.

    - `project` connu : `queue` pour `local`/`pr` ; `direct` exige `off`
      explicitement déclaré et l'absence vérifiée de file partagée. Le défaut
      `off` du parseur ne constitue pas une déclaration.
    - projet inconnu : `queue` si l'état partagé (`merge_queue`) porte déjà une
      ligne de ce dépôt ; `unknown` si l'hôte ne peut l'établir. Un hôte qui
      déclare d'autres projets radar mais aucun pour ce dépôt s'abstient : ses
      portes d'atterrissage ne sont pas celles du lot (même garde que
      `ameesh work queue add`, #1467/#2120).
    - `unknown` interdit la fusion directe : le feu s'abstient et un hôte qui
      connaît le projet agit (le bail est rendu).
    """
    if project is not None and getattr(project, "land", "off") != "off":
        name = getattr(project, "name", "") or "?"
        return "queue", "project %s declares land=%s" % (name, project.land)
    try:
        declared = merge_queue.declared_projects(cfg)
    except merge_queue.QueueError as exc:
        return "unknown", "cannot read repository landing conditions: %s" % exc
    if declared and project is None:
        return "unknown", (
            "the loaded configuration declares radar.projects (%s) but none matches %s: "
            "select the profile declaring radar.projects for this repository, including its "
            "land/protected conditions (#1467/#2120); no direct merge from this host (#2256)"
            % (", ".join(sorted(declared)), slug or repo or "this lot"))
    queued = _queue_declared(db, repo, slug)
    if queued:
        return "queue", (
            "the shared merge queue already holds %s: its landing path is the queue"
            % (slug or repo or "this repository"))
    if project is not None and queued is False:
        raw = (getattr(cfg, "radar", None) or {}).get("projects", {}).get(project.name, {})
        if raw.get("land") == "off":
            return "direct", ("project %s explicitly declares land=off without a shared queue"
                              % project.name)
    return "unknown", (
        "this host has no explicit landing mode for %s or cannot establish the absence of "
        "a shared queue: refusing a direct merge (#2256); declare radar.projects on the host "
        "that holds the repository, or queue the lot" % (slug or repo or "this lot"))


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
    from . import radar
    rollup = radar.merge_rollup(row.get("statusCheckRollup"), slug=slug,
                                head=_norm(row.get("headRefOid")), branch=branch, gh=gh)
    # `ci` garde son sens strict (« tous les checks verts »), que lisent la
    # file et l'affichage ; `ci_rule` porte la règle owner du 23/10 (#3085) —
    # un non concluant n'y bloque pas — pour la seule porte `ready_pr`.
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    ci_rule = radar.ci_state(rollup)
    commit = row.get("mergeCommit") or {}
    return {
        "number": row.get("number"), "state": str(row.get("state") or "").upper(),
        "head": _norm(row.get("headRefOid")), "base": str(row.get("baseRefName") or ""),
        "draft": bool(row.get("isDraft")), "ci": ci, "ci_rule": ci_rule,
        "merged": str(row.get("state") or "").upper() == "MERGED",
        "merge_commit": _norm(commit.get("oid") if isinstance(commit, dict) else ""),
        "armed": row.get("autoMergeRequest") is not None, "url": row.get("url") or "",
        # #1748 (a): what `ready_pr` needs to require the declared statuses;
        # #2271 : `ready_pr` lit les checks requis dans ce rollup, pas dans
        # `ci` seul (qui compte `SKIPPED` comme vert).
        "branch": branch, "rollup": rollup,
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


def _stacked_base_guard(slug: str, status: dict, target: str, default: str, *,
                        gh) -> str:
    """#2603 : refuse une fusion sur une base de travail déjà fusionnée dans la
    cible par défaut (home#112/#113 après canon/2452) : le contenu atterrirait
    sur une branche morte. La comparaison GitHub `base...default` rend `ahead`
    ou `identical` quand la base est un ancêtre de la cible par défaut. Une
    lecture impossible refuse : le doute ne fusionne pas sur une base morte."""
    base = merge_queue._remote_branch(status.get("base") or "")
    target = merge_queue._remote_branch(target or "")
    default = merge_queue._remote_branch(default or "")
    if not base or not target or base != target:
        return ""
    if not default:
        return ("PR #%s targets %s, and the repository's default target is unknown: "
                "nothing merged — read the default branch by hand"
                % (status.get("number"), base))
    if base == default:
        return ""
    proc = gh(["api", "repos/%s/compare/%s...%s" % (
        slug, quote(base, safe=""), quote(default, safe=""))])
    if proc.returncode != 0:
        return ("PR #%s targets %s, not the default target %s, and the comparison cannot be "
                "read (%s): nothing merged on a possibly dead base — check the PR by hand"
                % (status.get("number"), base, default,
                   (proc.stderr or proc.stdout or "").strip()[:200]))
    try:
        compared = land._json(proc, "gh api compare %s...%s" % (base, default), dict)
    except land.LandError as exc:
        return ("PR #%s targets %s, not the default target %s, and the comparison is "
                "unreadable (%s): nothing merged on a possibly dead base"
                % (status.get("number"), base, default, exc))
    comparison = str(compared.get("status") or "")
    if comparison not in ("ahead", "identical", "behind", "diverged"):
        return ("PR #%s: comparison of %s with %s unreadable: nothing merged"
                % (status.get("number"), base, default))
    if comparison in ("ahead", "identical"):
        return ("PR #%s targets %s, already merged into %s: merging it there would land its "
                "content on a dead branch — retarget it (`gh pr edit --base %s %s`), then "
                "freeze and re-review" % (status.get("number"), base, default, default,
                                          status.get("number")))
    return ""


def _merge_pr(slug: str, status: dict, sha: str, method: str, *, gh,
              target: str = "", default: str = "") -> tuple:
    """Fusionne la PR **maintenant**, sur la tête exacte `sha` (#1374, P1-4).

    C'est ce geste, et lui seul, qui est lié au feu : `--match-head-commit`
    est vérifié par GitHub au moment de la fusion, pas à l'armement. Laisser
    une auto-fusion armée entre deux passages ferait fusionner une tête
    poussée après le feu (GitHub ne la désarme pas pour un push d'un compte
    avec droit d'écriture). Rend `(ok, detail)`. #2603 : une base de travail
    déjà fusionnée dans la cible par défaut refuse avant l'appel."""
    refusal = _stacked_base_guard(slug, status, target, default, gh=gh)
    if refusal:
        return False, refusal
    proc = gh(["pr", "merge", str(status["number"]), "--repo", slug, "--%s" % method,
               "--match-head-commit", sha], timeout=120.0)
    if proc.returncode == 0:
        return True, "PR merged on %s" % sha[:12]
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

def ready_verdicts(cfg, db, row: dict, marks: list, *, repo: str | None,
                   project, target: str, base_sha: str | None = None) -> tuple:
    """#4804 : pré-lecture et porte partagent une seule échéance d'auteurs."""
    import contextlib
    from . import review_authors

    lot = int(row["id"])
    outer = review_authors.active_sweep()
    scope = (contextlib.nullcontext(outer) if outer is not None else
             review_authors.sweep_deadline(merge_queue.AUTHORS_SWEEP_BUDGET_S,
                                          "lot #%d" % lot))
    klass = {"class": "sensitive", "doubt": True}
    with scope as sweep:
        try:
            result = _ready_verdicts(cfg, db, row, marks, repo=repo,
                                     project=project, target=target, base_sha=base_sha)
            klass = result[2]
        except review_authors.SweepDeadline as exc:
            return False, "lot #%d: lecture d'auteurs non concluante: %s" % (lot, exc), klass
        if sweep.tripped or sweep.deadline.expired():
            return False, ("lot #%d: lecture d'auteurs non concluante: budget exhausted "
                           "at %s" % (lot, sweep.tripped or "attribution")), klass
    return result


def _ready_verdicts(cfg, db, row: dict, marks: list, *, repo: str | None,
                    project, target: str, base_sha: str | None = None) -> tuple:
    """La première porte, celle qui ne doit rien déclencher (#1374, B) : la
    classe du lot et les **avis requis** sur la tête gelée, comptés et
    indépendants (`merge_queue.review_gate`). Rend `(ok, raison, classe)`.

    Un doute de classe (dépôt inconnu, diff illisible) vaut sensible : le
    verdict reste exigé. Une cible `release/*` (classe release) n'est jamais
    une auto-fusion : c'est la procédure de release du pilote.

    #2505 : la cible de la classe est celle que la file va fusionner, donc
    `origin/<cible>` quand elle existe (#2207, `merge_queue._class_base`) ;
    la cible **locale** du checkout ne sert qu'à défaut. Un `main` local en
    retard y ajouterait des commits déjà fusionnés, dont des chemins
    sensibles, et surclasserait le lot au-dessus de ses vrais avis.

    #3091 : `base_sha` est la cible **déjà résolue** par l'appelant (la
    branche `baseRefName` de la PR, résolue en commit par `land.base_oid`,
    pour l'auto-fusion d'un paquet ; #3308). Elle sert de révision de classe
    et de preuve de la base de composition ; la ref locale peut être en
    retard, jamais cette lecture. Sans elle, la ref résolue par `_class_base`
    fait foi, comme avant.

    Cette porte s'évalue **avant** toute PR, toute entrée en file et toute CI :
    un feu peut être enregistré au gel, mais rien ne part avant les avis."""
    lot = int(row["id"])
    freeze = freeze_of(marks)
    frozen = _norm((freeze or {}).get("sha"))
    head = merge_queue.pinned(repo, frozen) if repo else None
    base = target or None
    if repo and target:
        base = merge_queue._class_base(repo, target) or target
    class_sha = str(base_sha or "").strip() or None
    if class_sha and repo:
        class_sha = merge_queue.pinned(repo, class_sha) or class_sha
    resolved = class_sha or (merge_queue.pinned(repo, base) if (repo and base) else None)
    # #4673 : la ref `origin/<cible>` du checkout n'est fraîche que de son
    # dernier fetch ; la branche **locale** peut même avoir divergé. La porte
    # (et `packet_reviews.need`, qui reçoit cette cible comme base) ne doit
    # jamais recevoir le nom nu : la pointe publiée est lue une fois ici et
    # passée en commit exact. Une forge muette retombe sur la ref locale.
    fresh_target = None
    if repo and target:
        try:
            from . import review_authors
            fresh_target = review_authors.refreshed_target(repo, target) or None
        except Exception:  # noqa: BLE001 - rafraîchissement au mieux
            fresh_target = None
    klass = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=repo or None,
                             base=target, base_sha=resolved or base, head=head)
    if str(klass.get("class") or "") == lot_class.RELEASE:
        return False, ("class: release (%s) — the release procedure belongs to the pilot: "
                       "a fire never arms an automatic merge on a release"
                       % (klass.get("why") or "release")), klass
    gate_authors = {}
    if frozen and merge_queue.required_verdicts(klass) > 0:
        # #2803/#2766 (B cx1) : sur un véhicule de paquet, la porte borne
        # elle-même la lecture au delta propre relu (`packet_reviews.need`).
        # Pré-lire toute la plage du paquet agrège les familles des membres
        # déjà relus et peut lever « famille ambiguë » avant que le périmètre
        # ne soit déterminé : la lecture est reportée à la porte, qui la borne.
        from . import packet_reviews
        if not packet_reviews.is_vehicle(row):
            # #1515 N1/B1 : la porte compte tous les auteurs du lot — détenteurs
            # du journal et auteurs du delta gelé (fusions comprises, petit
            # correctif de relecteur excepté, #2739) —, pas le seul assigné. Une
            # provenance illisible refuse la porte. On lit même si la ligne
            # courante porte une dispense : la porte relit la marque après les
            # jalons (#2281) et une dispense périmée ne doit pas laisser la porte
            # sur le seul assigné.
            try:
                authors, families = merge_queue._complete_authors(
                    db, row, frozen, repo, fresh_target or target, klass=klass,
                    marks=marks, cfg=cfg)
            except merge_queue._ProvenanceError as exc:
                return False, (
                    "no review verdict ok on the freeze %s (class: %s): the complete "
                    "authors of the lot cannot be established (%s), so no verdict can "
                    "be proven independent (canon §2: independence is never assumed)"
                    % (frozen[:12], klass.get("class") or "?", exc)), klass
            gate_authors = {"authors": authors, "families": families,
                            "authors_complete": True}
    # #3091 : la base de composition du véhicule est prouvée sur cette cible
    # résolue, jamais sur la ref du checkout. #4673 : sans base déjà résolue
    # par l'appelant, la porte reçoit la **pointe publiée** en commit exact
    # (pas le nom de branche, que `packet_reviews.need` résoudrait sur la
    # branche locale) ; un clone partagé non rafraîchi ne fait alors plus
    # passer des commits déjà dans la cible pour des tiers.
    gate_target = resolved if class_sha else (fresh_target or resolved or base)
    gate = merge_queue.review_gate(db, marks, frozen, repo=repo, klass=klass,
                                   item=row, cfg=cfg, target=gate_target,
                                   **gate_authors)
    if not gate["ok"]:
        return False, gate["why"], klass
    return True, "", klass


def _rollup_name(entry) -> str:
    """Le nom d'une entrée de `statusCheckRollup` : `name` d'un check run,
    `context` d'un statut de commit."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("name") or entry.get("context") or "")


def _rollup_state(entry) -> str:
    """L'état d'une entrée de `statusCheckRollup`, en majuscules : un statut
    de commit porte `state`, un check run `status` et `conclusion`."""
    if not isinstance(entry, dict):
        return ""
    if entry.get("__typename") == "StatusContext" or \
            ("state" in entry and "conclusion" not in entry):
        return str(entry.get("state") or "").upper()
    if str(entry.get("status") or "").upper() != "COMPLETED":
        return "PENDING"
    return str(entry.get("conclusion") or "").upper()


def _same_head(left, right) -> bool:
    """Deux identifiants du même commit : égaux, ou l'un abrégé de l'autre
    (un gel peut citer `48c6372e` quand la PR rend le SHA complet)."""
    left, right = _norm(left), _norm(right)
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = (left, right) if len(left) < len(right) else (right, left)
    return len(short) >= 7 and long.startswith(short)


def _required_pairs(required) -> tuple:
    """Les checks requis sous la forme `(contexte, app_id)` : un nom seul vaut
    `(nom, None)` (aucune application imposée). Un contexte est un nom exact :
    ses espaces significatifs sont conservés, `tests ` n'est pas `tests`."""
    values = (required,) if isinstance(required, str) else tuple(required or ())
    pairs = []
    for value in values:
        if isinstance(value, str) and value.strip():
            pairs.append((value, None))
            continue
        if isinstance(value, (tuple, list)) and len(value) == 2:
            name, app_id = value
            if isinstance(name, str) and name.strip() \
                    and (app_id is None or (isinstance(app_id, int)
                                            and not isinstance(app_id, bool)
                                            and app_id > 0)):
                pairs.append((name, app_id))
                continue
        raise land.LandError("invalid required check %r" % (value,))
    return tuple(pairs)


def _run_stamp(run):
    """La date de début d'un check run de provenance, en secondes epoch, ou
    None (date absente ou illisible)."""
    from datetime import datetime

    try:
        stamp = datetime.fromisoformat(run.get("started_at").replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _latest_runs(runs) -> list:
    """#3008 : les derniers check runs par (application, suite, nom), comme
    `radar.latest_rollup` pour le rollup (#2524) : un re-run vert sur la même
    tête remplace l'ancien run rouge de sa suite.

    Seule une date strictement plus récente remplace une entrée. Suite
    inconnue, date absente, invalide ou ex aequo : toutes les entrées
    ambiguës restent, afin de ne jamais masquer un échec sur la foi de
    l'ordre reçu."""
    groups = {}
    for index, run in enumerate(runs or ()):
        suite = run.get("check_suite_id")
        key = (("suite", run.get("app_id"), suite, _rollup_name(run))
               if suite is not None else ("unknown", index))
        groups.setdefault(key, []).append((run, _run_stamp(run)))
    latest = []
    for entries in groups.values():
        if any(stamp is None for _, stamp in entries):
            latest.extend(run for run, _ in entries)
        else:
            newest = max(stamp for _, stamp in entries)
            latest.extend(run for run, stamp in entries if stamp == newest)
    return latest


def _suite_timeline(runs, provenance):
    """#4549 : `[(début, suite, run requis, tous ses check runs)]` pour les
    suites de `runs`, ou None si une suite ou une date manque.

    Le début d'une suite est la plus ancienne date de ses runs (le `plan`
    démarre en premier) : l'ordre des agrégats ne dit pas l'ordre des suites.
    Une date absente, une suite inconnue ou une suite absente de la
    provenance rendent la preuve impossible : l'appelant garde alors toutes
    les entrées."""
    by_suite: dict = {}
    for run in provenance or ():
        suite = run.get("check_suite_id")
        if suite is not None:
            by_suite.setdefault(suite, []).append(run)
    timeline = []
    for run in runs:
        suite = run.get("check_suite_id")
        if suite is None:
            return None
        checks = by_suite.get(suite) or []
        stamps = [_run_stamp(check) for check in checks]
        if not checks or any(stamp is None for stamp in stamps):
            return None
        timeline.append((min(stamps), suite, run, checks))
    return timeline


def _workflow_kind(checks):
    """#4549 : la famille du workflow d'une suite, lue sur ses seuls jobs.

    `paquet` publie le portier (couverture / suite complète) en plus de la
    suite appelée ; `tests` porte `plan` et l'agrégat `tests`. Une suite
    d'une autre forme ne permet pas de comparer deux workflows : None (le
    doute ne masque rien)."""
    from . import radar
    names = {_rollup_name(check) for check in checks or ()}
    if any(name == radar.PACKET_COVERAGE
           or name == radar.PACKET_SUITE
           or name.startswith(radar.PACKET_SUITE + " / ")
           for name in names):
        return "paquet"
    if radar.PLAN_CHECK in names and "tests" in names:
        return "tests"
    return None


def _plan_green(checks, app_id) -> bool:
    """#4549 : la suite a un `plan` conclu vert de la même application que le
    check requis — la composition a été validée et les parts sélectionnées."""
    from . import radar
    return any(_rollup_name(check) == radar.PLAN_CHECK
               and (app_id is None or check.get("app_id") == app_id)
               and radar.check_result(check) == radar.CI_GREEN_RESULT
               for check in checks or ())


def _suite_run(slug: str, run, *, gh, head: str = "", cache: dict | None = None):
    """#4549 : le run Actions d'une suite — identité du workflow, base
    attestée et tête — ou None si la preuve ne se lit pas.

    L'identité de workflow vient du run lui-même (`workflow_id`, `path`), pas
    des noms de checks : deux workflows homonymes restent distincts. La base
    attestée est celle que le run-name de la demande porte (`ci.pr_run`) :
    c'est la base que la porte du gel a composée, pas la tête du dépôt."""
    from . import ci, radar
    run_id = radar.actions_run_id(slug, run)
    if run_id is None or gh is None:
        return None
    if cache is not None and run_id in cache:
        return cache[run_id]
    try:
        proc = gh(["api", "repos/%s/actions/runs/%d" % (slug, run_id)])
        if getattr(proc, "returncode", 1) != 0:
            return None
        data = json.loads(proc.stdout or "{}")
    except (ValueError, TypeError, AttributeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("id") != run_id:
        return None
    if head and str(data.get("head_sha") or "").strip().lower() != head.strip().lower():
        return None
    identity = data.get("workflow_id")
    if isinstance(identity, bool) or not isinstance(identity, int):
        identity = None
    path = data.get("path")
    if not isinstance(path, str):
        path = None
    # #4549 (B2 de cx2) : la base est lue dans le run-name de la demande ;
    # un run sans demande lisible n'atteste aucune base (le doute refuse).
    attested = None
    try:
        request = ci.pr_run(data)
    except (AttributeError, TypeError, ValueError):
        request = None
    if request is not None:
        attested = request.get("base")
    proof = {"workflow_id": identity, "path": path, "attested_base": attested}
    if cache is not None:
        cache[run_id] = proof
    return proof


def _same_workflow(left, right) -> bool:
    """#4549 : deux runs sortent du même workflow (identité ou chemin)."""
    if not left or not right:
        return False
    if left.get("workflow_id") is not None and right.get("workflow_id") is not None:
        return left["workflow_id"] == right["workflow_id"]
    return bool(left.get("path") and left.get("path") == right.get("path"))


def _current_base_ok(attested, *, slug: str | None, gh, target: str) -> bool:
    """#4549 (B de cx2) : la suite verte atteste, par son run-name, la base
    sur laquelle elle a été demandée ; cette base doit être la tête courante
    de la cible, comparée par **SHA**.

    La date de committer de la tête de la cible ne prouve pas quand elle est
    devenue la tête : un commit ancien poussé en fast-forward après le run
    garde sa date et laisserait passer une composition plus ancienne. Une
    base attestée absente ou une lecture impossible refuse : le doute ne
    verdit jamais. Sans lecteur, l'appelant ne substitue rien
    (`_supersede_testless_runs` ferme par défaut)."""
    if not (slug and gh and target):
        return True
    if not isinstance(attested, str) or not attested.strip():
        return False
    try:
        current = land.base_oid(slug, merge_queue._remote_branch(target), gh=gh)
    except (land.LandError, ValueError, TypeError, AttributeError, OSError):
        return False
    return attested.strip().lower() == str(current or "").strip().lower()


def _supersede_testless_runs(name, app_id, runs, provenance, *, required=(),
                             slug: str | None = None, gh=None, head: str = "",
                             target: str = "") -> list:
    """#4549 : écarter les runs d'une suite ancienne échouée au plan sans
    exécuter de test lorsqu'une suite plus récente, sur la même tête, a conclu
    vert et a été composée sur la base courante de la cible.

    Le doute ne verdit jamais : suite inconnue ou en cours, dates absentes ou
    ex aequo, plan de la suite verte non vert, workflow d'une autre famille,
    ancien rouge non prouvé « sans test », ou base de la suite verte non
    courante laissent toutes les entrées. Un rouge de test, une part lancée
    ou un autre négatif ne sont donc pas masqués. Sans lecteur (`slug`/`gh`),
    aucune preuve de workflow ni de base n'existe : rien n'est substitué."""
    from . import radar
    timeline = _suite_timeline(runs, provenance)
    if not timeline or len(timeline) < 2:
        return runs
    if len({row[0] for row in timeline}) != len(timeline):
        return runs
    timeline.sort(key=lambda row: row[0])
    newest = timeline[-1]
    if radar.check_result(newest[2]) != radar.CI_GREEN_RESULT:
        return runs
    if not _plan_green(newest[3], app_id):
        return runs
    failures = [row for row in timeline[:-1]
                if radar.check_result(row[2]) == radar.CI_NEGATIVE_RESULT]
    if not failures:
        return runs
    if any(radar.check_result(row[2]) == radar.CI_WAITING_RESULT
           for row in timeline):
        return runs
    kind = _workflow_kind(newest[3])
    if kind is None:
        return runs
    required_names = {pair[0] for pair in _required_pairs(required)}
    for _start, _suite, _run, checks in failures:
        if _workflow_kind(checks) != kind:
            return runs
        if not radar.testless_failure(
                [check for check in checks if check.get("app_id") == app_id],
                name, required=required_names):
            return runs
    # #4549 (B3 de dev1, confirmé par cx2) : la preuve de workflow et de base
    # vient des runs Actions eux-mêmes. Sans lecteur, aucune preuve n'existe :
    # aucune substitution (le doute ne verdit jamais, y compris sur la porte
    # des paquets qui appelait cette fonction sans lecteur) ; avec un lecteur,
    # une lecture impossible refuse aussi.
    if not (slug and gh):
        return runs
    cache: dict = {}
    newest_run = _suite_run(slug, newest[2], gh=gh, head=head, cache=cache)
    if newest_run is None:
        return runs
    for _start, _suite, run, _checks in failures:
        proof = _suite_run(slug, run, gh=gh, head=head, cache=cache)
        if not _same_workflow(newest_run, proof):
            return runs
    if not _current_base_ok(newest_run.get("attested_base"), slug=slug,
                            gh=gh, target=target):
        return runs
    drop = {row[1] for row in failures}
    return [run for run in runs if run.get("check_suite_id") not in drop]


def _proven_superseded_suites(name, provenance, *, required=(), slug=None, gh=None,
                              head: str = "", target: str = "") -> set:
    """#4549 (B2 de cx2) : les suites dont le run de `name` est supplanté,
    prouvées application par application.

    `radar.latest_rollup` ne masque plus rien sur le seul nom : la décision
    se prend ici, avec la même preuve que la voie `app_id` (même workflow,
    plan vert, aucun test lancé, base attestée = cible courante). Un nom
    porté par deux applications, ou deux workflows homonymes, n'est donc
    jamais verdit par la suite de l'autre."""
    by_app: dict = {}
    for run in provenance or ():
        if not isinstance(run, dict) or _rollup_name(run) != name:
            continue
        app_id = run.get("app_id")
        if isinstance(app_id, bool) or not isinstance(app_id, int):
            continue
        by_app.setdefault(app_id, []).append(run)
    dropped: set = set()
    for app_id, runs in by_app.items():
        before = {run.get("check_suite_id") for run in runs}
        kept = _supersede_testless_runs(name, app_id, _latest_runs(runs),
                                        provenance, required=required,
                                        slug=slug, gh=gh, head=head,
                                        target=target)
        dropped |= before - {run.get("check_suite_id") for run in kept}
    return dropped


def _entry_suite(entry):
    """L'identifiant de suite d'une entrée de rollup (`checkSuiteId`) ou d'un
    check run de provenance (`check_suite_id`), ou None."""
    if not isinstance(entry, dict):
        return None
    return entry.get("checkSuiteId", entry.get("check_suite_id"))


def _required_gate(required, rollup, provenance=None, *, strict: bool = False,
                   unconcluded: list | None = None, slug: str | None = None,
                   gh=None, head: str = "", target: str = "") -> tuple:
    """Les checks requis que `rollup` et `provenance` ne prouvent pas.

    `provenance` porte les check runs de la tête exacte avec leur application
    source. Un couple `(contexte, app_id)` n'est vert que si un check run de
    ce nom **et de cette application** est `SUCCESS` ; une entrée sans source
    (statut de commit) ou d'une autre application ne prouve rien. Sans
    application imposée (`app_id` à `None`), tout check ou statut du nom exigé
    compte après sélection des derniers checks du rollup (#2524). Un même
    nom encore porté par plusieurs workflows ou sources n'est vert que si
    tous le sont : le doute ne verdit pas la porte.

    #3085 (règle owner du 23/10) : rend `(gaps, consigned)`.

    * `gaps` : ce qui bloque encore — un check **négatif** (`FAILURE`), absent,
      d'une autre source, ou encore en cours. Un check requis absent ne se
      suppose pas vert : la porte ne fusionne pas sur une preuve manquante.
    * `consigned` : les résultats **non concluants** (sauté, annulé, expiré,
      démarrage raté, statut en erreur) d'un check requis. Ils ne bloquent
      pas et sont journalisés ; la correction s'ouvre à part.

    #3186 (décision owner #128512 du 23/10) : un check requis qui n'a pas
    conclu **lui-même** (`CANCELLED`, `TIMED_OUT`, `STARTUP_FAILURE`,
    `STALE` : run annulé, runner perdu) n'est pas un test non concluant mais
    une preuve absente — l'agrégat `tests` (#876) n'a rien vérifié. Il reste
    dans `gaps` ; ses entrées sont ajoutées à `unconcluded`, si fourni, pour
    que l'appelant relance le run une fois (`radar.rerun_unconcluded`) — sauf
    si un autre check requis est un rouge prouvé : la relance ne lèverait pas
    la porte. Les non concluants d'un agrégat qui a conclu restent consignés
    (§3).

    `strict=True` garde l'ancienne preuve (tout ce qui n'est pas `SUCCESS`
    manque), pour les lecteurs qui n'ont pas reçu la règle du 23/10 (file de
    fusion, `merge-pr`).

    #4549 : une suite plus récente conclue verte remplace un run échoué au
    plan d'une suite plus ancienne **sans aucun test exécuté**
    (`_supersede_testless_runs`) ; un rouge de test, une suite concurrente ou
    une base de composition non courante gardent la porte fermée. La preuve
    vaut aussi pour un nom sans application imposée, application par
    application (`_proven_superseded_suites`, B2 de cx2) : le rollup n'est
    jamais filtré sur le seul nom. `slug`/`gh`/`target` portent la preuve de
    la base attestée."""
    from . import radar
    rollup = radar.latest_rollup(rollup)
    gaps, consigned = [], []
    mark = len(unconcluded) if unconcluded is not None else 0
    proven_red = []

    def classify(name: str, entries: list) -> None:
        """Trie les états d'un même nom : négatif, puis attente, puis jamais
        conclu (#3186), puis non concluant, puis vert ; `strict` remet tous
        les non-SUCCESS en gaps."""
        states = [_rollup_state(entry) for entry in entries]
        results = {state: radar.rollup_state_result(state) for state in states}
        negative = sorted({state for state, result in results.items()
                           if result == radar.CI_NEGATIVE_RESULT})
        waiting = sorted({state for state, result in results.items()
                          if result == radar.CI_WAITING_RESULT})
        inconclusive = sorted({state for state, result in results.items()
                               if result == radar.CI_INCONCLUSIVE_RESULT})
        never = [state for state in inconclusive if radar.never_concluded(state)]
        if strict:
            bad = sorted({state or "?" for state in states if state != "SUCCESS"})
            if bad:
                gaps.append("%s (%s)" % (name, ", ".join(bad)))
            return
        if negative:
            gaps.append("%s (%s)" % (name, ", ".join(negative)))
            proven_red.append(name)
        elif waiting:
            gaps.append("%s (%s)" % (name, ", ".join(waiting)))
        elif never:
            gaps.append("%s (%s: never concluded, no test proof)" % (name, ", ".join(never)))
            if unconcluded is not None:
                unconcluded.extend((name, entry) for entry in entries
                                   if radar.never_concluded(_rollup_state(entry)))
        elif inconclusive:
            consigned.append("%s (%s)" % (name, ", ".join(inconclusive)))

    for name, app_id in _required_pairs(required):
        if app_id is None:
            entries = [entry for entry in rollup or () if _rollup_name(entry) == name]
            if not entries:
                gaps.append("%s (absent)" % name)
                continue
            # #4549 (B2 de cx2) : le remplacement d'une suite échouée au plan
            # par une suite verte sans test se prouve ici, sur la provenance
            # de la tête et la base attestée ; le rollup n'est jamais filtré
            # sur le seul nom. Sans entrée restante, on garde tout : un vert
            # ne se déduit pas d'une absence.
            dropped = _proven_superseded_suites(
                name, provenance, required=required, slug=slug, gh=gh,
                head=head, target=target)
            if dropped:
                kept = [entry for entry in entries
                        if _entry_suite(entry) not in dropped]
                if kept:
                    entries = kept
            classify(name, entries)
            continue
        runs = _latest_runs([entry for entry in provenance or ()
                             if _rollup_name(entry) == name
                             and entry.get("app_id") == app_id])
        runs = _supersede_testless_runs(name, app_id, runs, provenance,
                                        required=required, slug=slug, gh=gh,
                                        head=head, target=target)
        if not runs:
            providers = sorted({str(entry.get("app_id")) for entry in provenance or ()
                                if _rollup_name(entry) == name
                                and entry.get("app_id") is not None})
            if providers:
                gaps.append("%s (app %s, expected %d)"
                            % (name, ", ".join(providers), app_id))
            elif any(_rollup_name(entry) == name for entry in provenance or ()) \
                    or any(_rollup_name(entry) == name for entry in rollup or ()):
                gaps.append("%s (source absent)" % name)
            else:
                gaps.append("%s (absent)" % name)
            continue
        classify(name, runs)
    if proven_red and unconcluded is not None:
        del unconcluded[mark:]
    return gaps, consigned


def relaunch_unconcluded(db, lot: int, slug: str, head: str, report: dict | None, *,
                         gh, dry_run: bool = False, guard=None,
                         actor: str = "ameesh") -> list:
    """#3186 (décision owner #128512 du 23/10) : la porte a trouvé un check
    requis jamais conclu (`report["unconcluded"]` de `ready_pr`) ; son run est
    relancé **une fois** (`radar.rerun_unconcluded`), la porte reste fermée
    jusqu'à une conclusion. `guard()` (bail du passage et garde du pilote
    élu) est relu juste avant chaque relance : un détenteur périmé ne relance
    rien. Chaque issue utile est notée au journal du lot, une seule fois.
    Rend les issues."""
    from . import radar
    pairs = [(name, radar.actions_run_id(slug, entry))
             for name, entry in (report or {}).get("unconcluded") or ()]
    if not pairs:
        return []
    reruns = radar.rerun_unconcluded(slug, head, pairs, gh=gh, dry_run=dry_run,
                                     guard=guard)
    if not dry_run:
        for row in reruns:
            if row["result"] in ("running", "busy"):
                continue
            try:
                work.note(db, int(lot),
                          "required check %s never concluded on %s — no test proof, "
                          "blocking (owner decision #128512): %s"
                          % (row["check"], head[:12], row["detail"]),
                          actor=actor, once=True)
            except Exception:  # noqa: BLE001 - la trace au mieux, la porte reste fermée
                pass
    return reruns


def _required_gap(required, rollup, provenance=None, *, slug: str | None = None,
                  gh=None, head: str = "", target: str = "") -> list:
    """L'ancienne preuve stricte (file de fusion, `merge-pr`) : les checks
    requis que `rollup` et `provenance` ne prouvent pas. #3085 : `ready_pr`
    passe par `_required_gate(strict=False)`, qui consigne un non concluant
    au lieu de le compter comme un manque. #4549 : la même règle de
    remplacement par une suite verte sans test s'applique."""
    return _required_gate(required, rollup, provenance, strict=True,
                          slug=slug, gh=gh, head=head, target=target)[0]


def _packet_proof_gate(rollup, replaced=()) -> tuple:
    """#2876/#3066 : la preuve du portier `paquet` reste **stricte** sur un
    défaut prouvé — une sentinelle `paquet (run proof unavailable)` ou un
    `FAILURE` du workflow `paquet` bloque, un check encore en cours attend.

    #3085 (règle owner du 23/10) : un check du portier **non concluant**
    (sauté, annulé pour quota, expiré, démarrage raté) n'est pas un défaut
    prouvé : il se consigne au lieu de bloquer, comme tout autre check.
    #3209 : la suite sautée parce qu'une PR porte la tête
    (`radar.packet_suite_covered`) n'a rien à consigner.
    #4812 : le portier annulé par `ci run --full` au profit du run de la PR
    (#2876), dont l'agrégat `tests` est vert, non plus
    (`radar.packet_portier_replaced`).
    #5298 : `replaced` porte les clés des seules entrées de flux qu'une preuve
    identifiée (`replaced_flux_checks`) remplace ; un homonyme courant reste
    bloquant. Rend `(gaps, consigned)`."""
    from . import radar
    gaps, consigned = [], []
    entries = radar.packet_proof_entries(rollup, replaced)
    for entry in entries:
        name = str(entry.get("name") or entry.get("context") or "paquet")
        state = _rollup_state(entry) or "?"
        result = radar.check_result(entry)
        if result == radar.CI_INCONCLUSIVE_RESULT:
            if radar.packet_suite_covered(entry, entries) \
                    or radar.packet_portier_replaced(entry, entries, rollup):
                continue
            consigned.append("%s (%s)" % (name, state))
        elif result != radar.CI_GREEN_RESULT:
            gaps.append("%s (%s)" % (name, state))
    return gaps, consigned


def _aggregate_detail_gate(rollup) -> tuple:
    """#3251 : le détail de l'agrégat requis `tests` (décision owner #128512).

    Le check requis `tests` conclut vert quand l'agrégat a conclu malgré une
    annulation interne (part annulée pour capacité, runner perdu) ; le check
    de détail `tests (bilan des parts)`, publié par la même étape, porte alors
    `neutral` et l'état de la part. Il n'est pas requis par la forge : sans
    cette lecture, la fusion passerait sans consigner l'annulation.

    Un `neutral` se consigne (non bloquant, canon §3) ; un `failure` bloque
    (l'agrégat est rouge de toute façon) ; un check en cours attend ; absent,
    il n'y a rien à consigner (projet sans cet agrégat). Rend
    `(gaps, consigned)`."""
    from . import radar
    gaps, consigned = [], []
    for entry in radar.aggregate_detail_entries(rollup):
        state = _rollup_state(entry) or "?"
        result = radar.check_result(entry)
        if result == radar.CI_INCONCLUSIVE_RESULT:
            consigned.append("%s (%s)" % (radar.AGGREGATE_DETAIL_CHECK, state))
        elif result == radar.CI_WAITING_RESULT:
            gaps.append("%s (%s)" % (radar.AGGREGATE_DETAIL_CHECK, state))
        elif result == radar.CI_NEGATIVE_RESULT:
            gaps.append("%s (%s)" % (radar.AGGREGATE_DETAIL_CHECK, state))
    return gaps, consigned


#: #2306 : les codes HTTP qui disent « réessaie » plutôt que « c'est prouvé ».
_TRANSIENT_CODE = re.compile(r"(?i)(?:\bhttp[ /]?|\(\s*)(?:408|429|5\d\d)\b")
_TRANSIENT_TEXT = (
    "timed out", "timeout", "deadline exceeded", "connection reset",
    "connection refused", "connection aborted", "broken pipe",
    "network is unreachable", "network is down", "network error",
    "no route to host", "dial tcp", "i/o timeout",
    "temporary failure in name resolution", "no such host",
    "name or service not known", "server misbehaving", "lookup ", "goaway",
    "tls handshake", "unexpected eof", "server error",
    "bad gateway", "service unavailable", "gateway timeout",
    "rate limit", "too many requests", "abuse detection",
)


def _transient_read(detail: str) -> bool:
    """#2306 : la lecture de la forge a-t-elle échoué pour une raison
    passagère ? Seuls ces échecs (HTTP 408/429/5xx, délai, coupure réseau,
    DNS, GOAWAY) gardent la ligne en file ; un 4xx, un refus de droits ou une
    réponse malformée restent des échecs prouvés, qui éjectent. #2344 : une
    sortie vide ne prouve rien non plus — elle est traitée en passagère."""
    low = str(detail or "").lower()
    if not low.strip():
        return True
    return bool(_TRANSIENT_CODE.search(low) or re.search(r"(?:^|:\s*)eof\s*$", low)
                or any(word in low for word in _TRANSIENT_TEXT))


#: #2344 : le seul refus que la déclaration du projet peut remplacer.
_FORBIDDEN_CODE = re.compile(r"(?i)(?:\bhttp[ /]?|\(\s*)403\b")


def _forbidden_read(detail: str) -> bool:
    """#2344 : la forge a-t-elle **prouvé** un refus de droits (HTTP 403) ?

    Un 403 dit que le jeton ne peut pas lire cette source : la déclaration du
    projet peut alors en tenir lieu. Aucun autre échec — DNS, GOAWAY, sortie
    vide, 404, réponse malformée — ne vaut cette preuve, sans quoi un gel
    atterrirait sur la seule déclaration alors que la forge n'a rien refusé."""
    return bool(_FORBIDDEN_CODE.search(str(detail or "").lower()))


def _read_error(detail: str):
    """#2344 : la classe d'erreur d'une lecture forge échouée.

    Un 403 prouvé lève `land.ForbiddenLandError` (seul cas où la déclaration
    peut remplacer la source) ; un échec passager (5xx, délai, DNS, GOAWAY,
    sortie vide) lève `land.TransientLandError` ; tout le reste reste un
    `land.LandError` ordinaire, fail-closed."""
    if _forbidden_read(detail):
        return land.ForbiddenLandError
    if _transient_read(detail):
        return land.TransientLandError
    return land.LandError


#: #3428 : refus de **plan** GitHub — la protection de branche et les rulesets
#: n'existent pas sur un dépôt privé hors GitHub Pro. C'est la preuve d'une
#: fonctionnalité indisponible, pas d'un jeton sans droits.
_PLAN_PROTECTION = re.compile(
    r"(?i)upgrade to github pro|make this repository public to enable this feature")


def _plan_unprotected(detail: str) -> bool:
    """#3428 : le 403 prouve-t-il que la protection est **indisponible** au
    plan du dépôt, et non que le jeton n'a pas les droits ?

    GitHub répond « Upgrade to GitHub Pro or make this repository public to
    enable this feature » quand un dépôt privé n'a pas accès à la protection
    de branche ni aux rulesets : il n'y a alors aucune protection à lire. Un
    « Resource not accessible by integration » reste un refus de droits, que
    la déclaration vide d'un projet ne couvre jamais."""
    return bool(_PLAN_PROTECTION.search(str(detail or "")))


def _json_pages(stdout: str, what: str) -> list:
    """Les pages d'une sortie `gh api --paginate` concaténée (`[…][…]`).

    `--slurp` (gh récent) rend une liste de pages ; le gh 2.45 de l'hôte ne
    le connaît pas (#2285) : sans lui, `--paginate` écrit les pages à la
    suite, sans séparateur, et chacune doit être décodée à son tour."""
    text = (stdout or "").strip()
    decoder = json.JSONDecoder()
    pages, index = [], 0
    while index < len(text):
        try:
            page, end = decoder.raw_decode(text, index)
        except ValueError as exc:
            raise land.LandError("%s: unreadable output (%s)" % (what, exc)) from exc
        pages.append(page)
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    return pages


def _api_pages(gh, path: str, what: str) -> list:
    """Les pages d'une lecture `gh api --paginate`, quel que soit le gh.

    `--slurp` est tenté d'abord (liste de pages, gh récent). Le gh 2.45 de
    l'hôte répond « unknown flag: --slurp » (#2285) : la lecture est alors
    rejouée sans lui et les pages concaténées sont décodées une à une. Un
    échec passager de la forge lève `land.TransientLandError` (#2306, la file
    réessaiera) ; toute autre lecture impossible lève `land.LandError` :
    l'absence de garde ne se suppose jamais."""
    proc = gh(["api", "--paginate", "--slurp", path])
    detail = (proc.stderr or proc.stdout or "").strip()[:300]
    if proc.returncode == 0:
        try:
            pages = json.loads(proc.stdout or "[]")
        except ValueError as exc:
            raise land.LandError("%s: unreadable output (%s)" % (what, exc)) from exc
        if not isinstance(pages, list):
            raise land.LandError("%s: unexpected output %s"
                                 % (what, (proc.stdout or "")[:100]))
        return pages
    if "unknown flag: --slurp" not in detail:
        raise _read_error(detail)("%s: %s" % (what, detail or "exit %d" % proc.returncode))
    proc = gh(["api", "--paginate", path])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise _read_error(detail)("%s: %s" % (what, detail or "exit %d" % proc.returncode))
    return _json_pages(proc.stdout, what)


def _head_check_runs(slug: str | None, sha: str, *, gh) -> list:
    """Les check runs de la tête **exacte** `sha`, avec l'application qui les a
    produits. `statusCheckRollup` ne porte pas cette source : elle se lit sur
    le commit (`commits/<sha>/check-runs`). Les noms sont rendus exactement,
    espaces significatifs compris : `tests ` n'est pas le contexte `tests`.
    Une lecture impossible ou malformée lève `land.LandError` : une source
    absente ne prouve rien. #2306 : un échec passager lève
    `land.TransientLandError`, que la file sait réessayer sans éjecter."""
    if not slug:
        raise land.LandError("no GitHub repository known: the source of the checks "
                             "of %s cannot be read" % (sha or "the head"))
    if gh is None:
        raise land.LandError("no GitHub reader: the source of the checks of %s "
                             "cannot be read" % (sha or "the head"))
    if not sha:
        raise land.LandError("no frozen head: the source of the required checks "
                             "cannot be read")
    path = "repos/%s/commits/%s/check-runs" % (slug, quote(sha, safe=""))
    pages = _api_pages(gh, path + "?per_page=100", "gh api %s" % path)
    runs = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise land.LandError("gh api %s: malformed response" % path)
        for run in page["check_runs"]:
            if not isinstance(run, dict) or not isinstance(run.get("name"), str) \
                    or not run["name"].strip():
                raise land.LandError("gh api %s: malformed check run" % path)
            head = _norm(run.get("head_sha"))
            if head and not _same_head(head, sha):
                raise land.LandError("gh api %s: a check run of %s is on %s"
                                     % (path, run["name"], head))
            app = run.get("app")
            app_id = app.get("id") if isinstance(app, dict) else None
            if app_id is not None and (isinstance(app_id, bool)
                                       or not isinstance(app_id, int)):
                raise land.LandError("gh api %s: malformed check run app" % path)
            # #3008 : la date et la suite départagent un re-run sur la même tête
            suite = run.get("check_suite")
            suite_id = suite.get("id") if isinstance(suite, dict) else None
            if isinstance(suite_id, bool) or not isinstance(suite_id, int):
                suite_id = None
            started = run.get("started_at")
            # #3186 : l'URL du job désigne le run à relancer d'un requis
            # jamais conclu (`radar.actions_run_id`).
            details = run.get("details_url")
            runs.append({"name": run["name"],
                         "status": str(run.get("status") or ""),
                         "conclusion": str(run.get("conclusion") or ""),
                         "app_id": app_id,
                         "started_at": started if isinstance(started, str) else None,
                         "check_suite_id": suite_id,
                         "details_url": details if isinstance(details, str) else None})
    return runs


#: #5298 : préfixe de l'`external_id` posé par `scripts/ci-flow.py` à la
#: création d'un check de flux (`ameesh-ci-flow/<exécution>/<tentative>`,
#: #1393). Recopié ici : le script reste autonome ; la forme est le protocole
#: du sweep et de la porte.
FLUX_EXTERNAL_PREFIX = "ameesh-ci-flow/"


def _flux_identity(check) -> tuple | None:
    """L'exécution et la tentative d'un check de flux, lues dans son
    `external_id`, ou None (absent, d'une autre forme, illisible)."""
    text = str((check or {}).get("external_id") or "")
    if not text.startswith(FLUX_EXTERNAL_PREFIX):
        return None
    parts = text[len(FLUX_EXTERNAL_PREFIX):].split("/")
    if len(parts) != 2:
        return None
    try:
        run, attempt = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    return (run, attempt) if run > 0 and attempt > 0 else None


def _check_id_in_url(url) -> int | None:
    """L'id du check run dans un `detailsUrl` de flux (`…/runs/<id>`, la forme
    par défaut d'un check publié par un jeton d'exécution), ou None.

    Une URL de job (`…/actions/runs/<exécution>/job/<job>`) ou d'exécution
    (`…/actions/runs/<exécution>`) ne nomme pas un check run : elle est
    refusée, sinon l'id de l'exécution pourrait être pris pour celui d'un
    check homonyme."""
    text = str(url or "").strip()
    if "/actions/runs/" in text:
        return None
    match = re.search(r"/runs/([1-9][0-9]*)/?$", text)
    return int(match.group(1)) if match else None


def _instant(value) -> float | None:
    """Une date ISO 8601 en secondes epoch, ou None (absente, illisible)."""
    from datetime import datetime

    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _check_run_of(slug: str, check: int, *, gh) -> dict | None:
    """Le check run `check` de `slug`, ou None (lecture impossible)."""
    path = "repos/%s/check-runs/%d" % (slug, int(check))
    proc = gh(["api", path])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _run_attempt_of(slug: str, run: int, *, gh) -> tuple | None:
    """`(tentative courante, tête)` de l'exécution `run`, ou None."""
    path = "repos/%s/actions/runs/%d" % (slug, int(run))
    proc = gh(["api", path])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    attempt = data.get("run_attempt")
    if type(attempt) is not int or attempt < 1:
        return None
    return attempt, str(data.get("head_sha") or "").strip().lower()


def _attempt_jobs(slug: str, run: int, attempt: int, *, gh) -> list | None:
    """Les jobs d'une tentative d'exécution, ou None si la lecture échoue."""
    path = ("repos/%s/actions/runs/%d/attempts/%d/jobs?per_page=100"
            % (slug, int(run), int(attempt)))
    try:
        pages = _api_pages(gh, path, "gh api %s" % path)
    except land.LandError:
        return None
    jobs = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
            return None
        jobs.extend(job for job in page["jobs"] if isinstance(job, dict))
    return jobs


def replaced_flux_checks(rollup, *, slug: str | None = None, head: str = "",
                         gh=None) -> set:
    """#5298 : les entrées de flux de `rollup` qu'une preuve **identifiée**
    remplace — le job de la même part, dans une tentative plus récente de la
    **même exécution**, `success` et conclu après le flux rouge.

    Le constat : après `gh run rerun --failed`, `statusCheckRollup` (et la
    lecture `filter=latest`) ne rend qu'un check par nom ; quand le
    `startedAt` de l'attempt précédent est plus grand (horloge du runner),
    l'ancien rouge reste seul visible. Ni un agrégat `tests` vert d'une autre
    exécution, ni un plan qui ne porte pas la part ne prouvent que *cette*
    part a été rejouée : la preuve est le job homonyme de la part
    (`psycopg 3/8` pour `tests (flux) psycopg 3/8`) relu dans la dernière
    tentative de l'exécution que nomme l'`external_id` du check
    (`ameesh-ci-flow/<exécution>/<tentative>`).

    Une entrée rouge n'est remplacée que si ce job est `success` et que sa
    date de fin est strictement postérieure à celle du flux. Une lecture
    impossible, un `external_id` ou un `detailsUrl` absent, une tentative
    courante, un job non vert ou des dates illisibles ne remplacent rien : la
    porte reste fermée.

    Rend les clés `radar.flux_entry_key` des seules entrées remplacées."""
    from . import ci as ci_mod, radar
    reds = [entry for entry in radar.latest_rollup(rollup) or ()
            if isinstance(entry, dict)
            and radar.check_result(entry) == radar.CI_NEGATIVE_RESULT
            and str(entry.get("name") or "").startswith(ci_mod.CHECK_PREFIX + " ")]
    if not reds or not slug or not head or gh is None:
        return set()
    wanted_head = str(head).strip().lower()
    replaced = set()
    runs: dict = {}
    for entry in reds:
        check_id = _check_id_in_url(entry.get("detailsUrl")
                                    or entry.get("details_url"))
        if check_id is None:
            continue
        check = _check_run_of(slug, check_id, gh=gh)
        if check is None or str(check.get("head_sha") or "").strip().lower() != wanted_head:
            continue
        name = str(check.get("name") or "")
        if not name.startswith(ci_mod.CHECK_PREFIX + " "):
            continue
        ident = _flux_identity(check)
        if ident is None:
            continue
        run, attempt = ident
        if run not in runs:
            runs[run] = _run_attempt_of(slug, run, gh=gh)
        latest = runs[run]
        if not latest or latest[0] <= attempt or latest[1] != wanted_head:
            continue
        flux_end = _instant(entry.get("completedAt")
                            or entry.get("completed_at"))
        if flux_end is None:
            continue
        part = name[len(ci_mod.CHECK_PREFIX) + 1:]
        jobs = _attempt_jobs(slug, run, latest[0], gh=gh)
        if jobs is None:
            continue
        for job in jobs:
            if str(job.get("name") or "") != part:
                continue
            if str(job.get("conclusion") or "").strip().lower() != "success":
                continue
            job_end = _instant(job.get("completed_at"))
            if job_end is not None and job_end > flux_end:
                replaced.add(radar.flux_entry_key(entry))
            break
    return replaced


def _app_id(value, path: str) -> int | None:
    """L'application GitHub imposée par une exigence, ou `None` pour « toute
    application ».

    `-1` est la valeur de l'API de protection pour « any app » (#2285) : la
    traiter comme une réponse malformée fermerait la porte au lieu de
    l'ouvrir à toute source. Un booléen, un non-entier, `0` ou un négatif
    autre que `-1` restent refusés."""
    if value is None or value == -1:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise land.LandError("gh api %s: invalid required check app_id" % path)
    return value


def _protection_required(slug: str | None, target: str, *, gh) -> tuple:
    """Les checks requis par la protection classique de `target`, lus à la
    forge, sous la forme `(contexte, app_id)` : `app_id` est l'application
    GitHub que la protection impose (`checks[].app_id`), `None` quand elle
    n'impose aucun fournisseur (ancienne liste `contexts`, ou `app_id` à
    `-1`, « toute application »). Perdre `app_id` accepterait un check
    homonyme produit par n'importe qui. Le contexte est rendu exactement,
    espaces significatifs compris : `tests ` n'est pas `tests`.

    Un 404 « Branch not protected » est une réponse (aucun check requis) ;
    toute autre lecture — droits, branche absente, réponse malformée — lève
    `land.LandError` : l'absence de garde ne se suppose jamais. #2344 : un
    refus de droits **prouvé** (403) lève `land.ForbiddenLandError`, seule
    erreur que `required_checks` peut remplacer par la déclaration."""
    if not slug:
        raise land.LandError("no GitHub repository known: the required checks of %s "
                             "cannot be read" % (target or "the target"))
    if gh is None:
        raise land.LandError("no GitHub reader: the required checks of %s cannot be read"
                             % target)
    path = "repos/%s/branches/%s/protection" % (slug, quote(target, safe=""))
    proc = gh(["api", path])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        if "Branch not protected" in detail:
            return ()
        raise _read_error(detail)("gh api %s: %s"
                                  % (path, detail or "exit %d" % proc.returncode))
    data = land._json(proc, "gh api %s" % path, dict)
    required = data.get("required_status_checks")
    if required is None:
        return ()
    if not isinstance(required, dict):
        raise land.LandError("gh api %s: required_status_checks is not an object" % path)
    contexts = required.get("contexts") or ()
    checks = required.get("checks") or ()
    if not isinstance(contexts, (list, tuple)) or not isinstance(checks, (list, tuple)):
        raise land.LandError("gh api %s: malformed required_status_checks" % path)
    pairs = []
    scoped = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("context"), str) \
                or not check["context"].strip():
            raise land.LandError("gh api %s: invalid required check" % path)
        app_id = _app_id(check.get("app_id"), path)
        context = check["context"]
        pairs.append((context, app_id))
        scoped.add(context)
    for context in contexts:
        if not isinstance(context, str) or not context.strip():
            raise land.LandError("gh api %s: invalid required context" % path)
        # `checks` porte la même liste avec l'application : ne pas la doubler
        # d'une exigence sans fournisseur qui n'ajouterait aucune garantie.
        if context not in scoped:
            pairs.append((context, None))
    return tuple(dict.fromkeys(pairs))


def _ruleset_required(slug: str | None, target: str, *, gh) -> tuple:
    """Les checks requis par les rulesets de branche appliqués à `target`,
    sous la forme `(contexte, app_id)`.

    `GET repos/<slug>/rules/branches/<target>` rend les règles actives qui
    s'appliquent à la branche, y compris celles d'une organisation ; une
    règle `required_status_checks` porte ses contextes et, quand elle en
    impose une, l'application (`integration_id`, `-1` valant « toute
    application »). Les autres règles ne concernent pas les checks. Un dépôt
    sans ruleset rend une liste vide ; une lecture impossible ou malformée
    lève `land.LandError` : l'absence de règle ne se suppose jamais (#2285).
    #2344 : un 403 prouvé lève `land.ForbiddenLandError`, que la déclaration
    peut remplacer ; les autres échecs restent fail-closed."""
    if not slug:
        raise land.LandError("no GitHub repository known: the rulesets of %s "
                             "cannot be read" % (target or "the target"))
    if gh is None:
        raise land.LandError("no GitHub reader: the rulesets of %s cannot be read"
                             % target)
    path = "repos/%s/rules/branches/%s" % (slug, quote(target, safe=""))
    what = "gh api %s" % path
    pages = _api_pages(gh, path + "?per_page=100", what)
    pairs = []
    for page in pages:
        if not isinstance(page, list):
            raise land.LandError("%s: malformed response" % what)
        for rule in page:
            if not isinstance(rule, dict):
                raise land.LandError("%s: malformed rule" % what)
            if rule.get("type") != "required_status_checks":
                continue
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                raise land.LandError("%s: malformed required_status_checks rule" % what)
            checks = parameters.get("required_status_checks")
            if not isinstance(checks, list):
                raise land.LandError("%s: malformed required_status_checks rule" % what)
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check.get("context"), str) \
                        or not check["context"].strip():
                    raise land.LandError("%s: invalid required check" % what)
                pairs.append((check["context"],
                              _app_id(check.get("integration_id"), what)))
    return tuple(dict.fromkeys(pairs))


def required_checks(slug: str | None, target: str, project, *, gh) -> tuple:
    """Les checks requis de la branche cible, sous la forme `(contexte,
    app_id)` : la protection classique et les rulesets lus à la forge,
    complétés par la déclaration `radar.projects.<p>.required_checks` (des
    noms, sans application imposée).

    Quand la forge **refuse** une lecture (403 prouvé : jeton sans droits), la
    déclaration du projet la remplace **sans retirer l'autre lecture** ; sans
    déclaration, la lecture refuse. Tout autre échec (404, DNS, GOAWAY, sortie
    vide, réponse malformée) ne se suppose jamais un refus de droits : il
    refuse ou lève `land.TransientLandError` quand il est passager — la
    déclaration ne tient pas lieu de preuve (#2344). La déclaration ne peut
    jamais retirer un check que la forge exige : elle s'ajoute. Les noms sont
    exacts de bout en bout : la déclaration `tests ` ne vaut pas le contexte
    `tests`.

    #3428 : une déclaration **vide** (`"required_checks": []`) ne couvre que
    le 403 de **plan** GitHub, qui prouve que la protection est indisponible
    sur ce dépôt (privé hors Pro) — il n'y a alors aucune protection à lire.
    Un 403 de droits, une protection lisible ou tout autre échec gardent le
    refus : une déclaration vide n'affaiblit jamais une source qui existe."""
    declared = tuple((str(name), None) for name in
                     (getattr(project, "required_checks", ()) or ())
                     if str(name).strip())
    # #3428 : `[]` déclaré est une déclaration, pas une absence ; elle ne vaut
    # que pour le 403 de plan, prouvé source par source ci-dessous.
    empty_declared = (not declared
                      and bool(getattr(project, "required_checks_declared", False)))
    if not target:
        if declared:
            return declared
        raise land.LandError("no target branch known: its required checks cannot be read")
    forged = []
    for read in (lambda: _protection_required(slug, target, gh=gh),
                 lambda: _ruleset_required(slug, target, gh=gh)):
        try:
            forged.extend(read())
        except land.ForbiddenLandError as exc:
            # #2344 : seul un 403 prouvé autorise la déclaration à tenir lieu
            # de cette source ; sans déclaration, la lecture refuse.
            # #3428 : une déclaration vide couvre le seul 403 de plan, qui
            # prouve l'absence de protection ; un 403 de droits refuse.
            if not declared and not (empty_declared and _plan_unprotected(str(exc))):
                raise
        except land.TransientLandError:
            # #2306 : un échec passager n'est pas une source absente ; la
            # file réessaiera au passage suivant, jamais un atterrissage avec
            # la seule déclaration.
            raise
        except land.LandError:
            # #2344 : 404, DNS, GOAWAY, sortie vide, réponse malformée : la
            # forge n'a pas prouvé de refus, la déclaration ne remplace rien ;
            # la lecture partielle est écartée (fail-closed).
            raise
    return tuple(dict.fromkeys(tuple(forged) + declared))


def _head_statuses(slug: str | None, sha: str, *, gh) -> list:
    """Les statuts de commit de la tête **exacte** `sha`, sous la forme d'une
    entrée de `statusCheckRollup` (`context`, `state`).

    L'endpoint `status` rend le dernier statut de chaque contexte ; un nom
    exact traverse tel quel. Une lecture impossible ou malformée lève
    `land.LandError` : une source absente ne prouve rien. #2306 : les pages
    sont lues jusqu'au `total_count` annoncé — un total de 100 n'est plus pris
    pour une troncature — et un échec passager lève `land.TransientLandError`."""
    if not slug:
        raise land.LandError("no GitHub repository known: the statuses of %s "
                             "cannot be read" % (sha or "the head"))
    if gh is None:
        raise land.LandError("no GitHub reader: the statuses of %s cannot be read"
                             % (sha or "the head"))
    if not sha:
        raise land.LandError("no frozen head: the statuses of the required checks "
                             "cannot be read")
    path = "repos/%s/commits/%s/status" % (slug, quote(sha, safe=""))
    pages = _api_pages(gh, path + "?per_page=100", "gh api %s" % path)
    total = None
    statuses = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("statuses"), list):
            raise land.LandError("gh api %s: malformed response" % path)
        announced = page.get("total_count")
        if type(announced) is not int or announced < 0 \
                or (total is not None and announced != total):
            raise land.LandError("gh api %s: malformed or truncated statuses" % path)
        total = announced
        statuses.extend(page["statuses"])
    if total is None or len(statuses) != total:
        raise land.LandError("gh api %s: malformed or truncated statuses" % path)
    out = []
    for status in statuses:
        if not isinstance(status, dict) or not isinstance(status.get("context"), str) \
                or not status["context"].strip() \
                or status.get("state") not in ("success", "pending", "failure", "error"):
            raise land.LandError("gh api %s: invalid commit status" % path)
        out.append({"__typename": "StatusContext", "context": status["context"],
                    "state": status["state"]})
    return out


def required_on_head(project, target: str, sha: str, *, gh) -> list:
    """#2286 : les checks requis de la cible que la tête **exacte** `sha` ne
    prouve pas, pour la voie file de fusion (`land`).

    La source est la déclaration `radar.projects.<p>.required_checks` du
    projet, complétée par la protection de la forge quand elle se lit (mêmes
    règles que `required_checks`) : un projet qui ne déclare aucun check n'a
    rien à prouver ici, et la protection seule reste portée par la forge au
    moment de la fusion d'une PR. Sans déclaration, cette garde ne retire
    jamais rien au comportement existant ; avec une déclaration, elle
    s'applique à **tout** atterrissage de la file, `ci_required` ou non —
    c'est le trou de la voie file (Nexlink n'a pas de protection de branche).

    Rend la liste des manques (`tests (absent)`, `coverage (FAILURE)`…) ;
    lève `land.LandError` quand les checks ou leur source ne peuvent pas être
    lus : un vert ne se suppose jamais. #2306 : une lecture passagère lève
    `land.TransientLandError` (la file réessaiera) ; une réponse malformée ou
    un manque prouvé restent des `LandError`."""
    from . import radar
    declared = tuple(getattr(project, "required_checks", ()) or ())
    if not declared:
        return []
    slug = radar.github_slug(project)
    required = required_checks(slug, target, project, gh=gh)
    if not required:
        return []
    runs = _head_check_runs(slug, sha, gh=gh)
    statuses = _head_statuses(slug, sha, gh=gh)
    # #3008 : la date ne sert que si la suite est connue — sans elle,
    # `latest_rollup` regrouperait deux workflows homonymes par le seul nom
    rollup = ([dict({"__typename": "CheckRun", "name": run["name"],
                     "status": run["status"], "conclusion": run["conclusion"]},
                    **({"startedAt": run["started_at"], "checkSuiteId": run["check_suite_id"]}
                       if run.get("check_suite_id") is not None else {}))
               for run in runs] + statuses)
    return _required_gap(required, rollup, runs, slug=slug, gh=gh, head=sha,
                         target=target)


def _required_status_gate(entries, required) -> tuple:
    """#1748 (a) + #3085 : les préfixes de statuts de commit exigés par la
    branche cible, classés comme les checks requis.

    Chaque préfixe doit être porté par au moins un statut de commit
    (`StatusContext`) : un `FAILURE` bloque, un statut en attente attend, un
    statut `ERROR` (le check n'a pas conclu, il n'a pas échoué) se consigne et
    ne bloque pas. Un préfixe absent bloque : un vert ne se suppose jamais.
    Un statut hors des préfixes requis ne gate pas.
    Rend `(gaps, consigned)`."""
    from . import radar
    gaps, consigned = [], []
    for prefix in required:
        matches = [entry for entry in entries
                   if str(entry.get("context") or "").startswith(prefix)]
        if not matches:
            gaps.append("%s* (absent)" % prefix)
            continue
        states = sorted({str(entry.get("state") or "?").upper() for entry in matches})
        results = {radar.state_result(entry.get("state")) for entry in matches}
        if radar.CI_NEGATIVE_RESULT in results:
            gaps.append("%s* (%s)" % (prefix, ", ".join(states)))
        elif radar.CI_WAITING_RESULT in results:
            gaps.append("%s* (%s)" % (prefix, ", ".join(states)))
        elif radar.CI_INCONCLUSIVE_RESULT in results:
            consigned.append("%s* (%s)" % (prefix, ", ".join(states)))
    return gaps, consigned


def ready_pr(status: dict, *, target: str, project, frozen: str, required=None,
             slug: str | None = None, gh=None, report: dict | None = None) -> tuple:
    """Les portes qui dépendent de la PR : brouillon, cible, CI verte sur la
    tête gelée, **checks requis de la branche cible** présents et `SUCCESS`
    sur cette tête (protection de la forge ou déclaration du projet), CI du
    commit gelé quand le projet l'exige (L160g).

    La protection peut imposer une application GitHub pour un contexte
    (`checks[].app_id`) : la provenance se lit alors sur la tête exacte, et un
    check homonyme produit par une autre application — ou un statut de commit,
    sans source — ne prouve rien. La tête de la PR doit être le gel.

    `required` remplace la lecture (tests, appelant qui l'a déjà faite) ;
    `slug`/`gh` la permettent. Sans l'un ou l'autre, la porte refuse : un
    check requis inconnu ne se suppose pas vert.

    #3085 (règle owner du 23/10) : la porte lit les **checks requis** et
    classe chaque résultat. Un `FAILURE`, un check absent ou encore en cours
    bloque ; un résultat non concluant (sauté, annulé pour quota, expiré,
    démarrage raté, statut en erreur) est **consigné** et ne bloque pas. Un
    check facultatif ne gate jamais (il n'est pas dans `required`), y compris
    par la lecture globale `ci_required` quand les checks requis sont déjà
    prouvés. Sans check requis connu, l'ancienne preuve `ci == green` reste
    la seule porte. `report`, s'il est fourni, reçoit `{"consigned": [...]}`
    pour la trace.
    #3251 (décision owner #128512) : le check de détail de l'agrégat `tests`
    est lu en plus des checks requis — une part interne annulée laisse
    `tests` vert et se consigne par ce canal.

    #3186 (décision owner #128512) : un check requis qui n'a pas conclu
    lui-même (annulé, expiré, démarrage raté) bloque — preuve absente — et
    `report["unconcluded"]` reçoit ses entrées `(nom, entrée)`, pour que
    l'appelant relance le run une fois (`relaunch_unconcluded`)."""
    if status.get("draft"):
        return False, "PR #%s is a draft" % status.get("number")
    if target and status.get("base") and status["base"] != target:
        return False, "PR #%s targets %s, not %s" % (status.get("number"), status["base"],
                                                     target)
    head, frozen_head = _norm(status.get("head")), _norm(frozen)
    if head and frozen_head and not _same_head(head, frozen_head):
        return False, ("PR #%s is on %s, not the freeze %s"
                       % (status.get("number"), head[:12], frozen_head[:12]))
    if required is None:
        try:
            required = required_checks(slug, target, project, gh=gh)
        except land.LandError as exc:
            return False, ("the required checks of %s on the freeze %s cannot be proven: %s"
                           % (target or "the target", frozen[:12], exc))
    pairs = _required_pairs(required)
    rollup = status.get("rollup") or ()
    # #5298 : le flux d'un attempt antérieur n'est remplacé que sur une preuve
    # identifiée (même exécution, même part, tentative plus récente verte). La
    # lecture peut échouer : sans preuve, la porte reste fermée.
    replaced = ()
    try:
        replaced = replaced_flux_checks(rollup, slug=slug,
                                        head=head or frozen_head, gh=gh)
    except Exception:  # noqa: BLE001 - une preuve illisible ne verdit jamais
        replaced = ()
    if pairs:
        provenance = None
        if any(app_id is not None for _, app_id in pairs):
            try:
                provenance = _head_check_runs(slug, head or frozen_head, gh=gh)
            except land.LandError as exc:
                return False, ("the source of the required checks of %s on the freeze %s "
                               "cannot be proven: %s"
                               % (target or "the target", frozen[:12], exc))
        unconcluded: list = []
        gaps, consigned = _required_gate(pairs, rollup, provenance, strict=False,
                                         unconcluded=unconcluded, slug=slug,
                                         gh=gh, head=head or frozen_head,
                                         target=target)
        # #2876/#3066 : la preuve du portier `paquet` n'est jamais un check
        # facultatif — un rouge ou une preuve indisponible bloque ; un non
        # concluant du portier se consigne.
        proof_gaps, proof_consigned = _packet_proof_gate(rollup, replaced)
        gaps += proof_gaps
        consigned += proof_consigned
        # #3251 : le détail de l'agrégat `tests` (décision owner #128512) —
        # une part interne annulée se consigne, elle ne retient pas la fusion.
        detail_gaps, detail_consigned = _aggregate_detail_gate(rollup)
        gaps += detail_gaps
        consigned += detail_consigned
        from . import radar
        if any(radar.check_result(entry) == radar.CI_NEGATIVE_RESULT
               for entry in radar.packet_proof_entries(rollup, replaced)):
            # #3186 : un portier rouge retient la porte quoi qu'il arrive ;
            # relancer l'agrégat jamais conclu ne coûterait qu'une suite.
            unconcluded.clear()
        if report is not None:
            report.update({"consigned": consigned,
                           "required": [name for name, _app in pairs],
                           "unconcluded": unconcluded})
        if gaps:
            return False, ("PR #%s does not prove the required checks of %s on the freeze %s: %s"
                           % (status.get("number"), target or "the target", frozen[:12],
                              ", ".join(gaps)))
    else:
        # aucun check requis connu : l'ancienne preuve « CI verte » reste la
        # seule porte ; `ci_rule` applique la règle du 23/10 (#3085), `ci` le
        # sens strict des lecteurs qui ne la portent pas.
        state = status.get("ci_rule") or status.get("ci")
        if state != "green":
            return False, "the CI of PR #%s is %s on the freeze %s" % (
                status.get("number"), state, frozen[:12])
    from . import radar
    # #1748 (a): a head of a declared branch is green only with a `success`
    # status of each required prefix; an unknown branch gets them all.
    # #3085 : seuls les statuts de commit portent ces préfixes ; un statut en
    # erreur se consigne, un check facultatif ne retient pas la porte.
    statuses = radar.required_statuses(project, status.get("branch"))
    required_proven = bool(pairs)
    if statuses:
        entries = [c for c in rollup
                   if isinstance(c, dict) and (c.get("__typename") == "StatusContext"
                                               or "state" in c and "conclusion" not in c)]
        status_gaps, status_consigned = _required_status_gate(entries, statuses)
        if report is not None:
            report["consigned"] = (list(report.get("consigned") or ())
                                   + status_consigned)
        if status_gaps:
            return False, "PR #%s carries no successful %s status on the freeze %s " \
                "(required_statuses: %s)" % (status.get("number"),
                                             " / ".join("%s*" % p for p in statuses),
                                             frozen[:12], ", ".join(status_gaps))
        required_proven = True
    # #3085 (B1) : la lecture globale `ci_required` ne reprend pas la main
    # quand les checks requis (ou les statuts exigés) sont déjà prouvés — son
    # rollup mêle les checks facultatifs (macOS), dont un `pending` ou un
    # `FAILURE` ne gate pas. Elle reste la seule preuve d'un projet qui
    # n'expose aucun check requis sur cette branche.
    if project is not None and getattr(project, "ci_required", False) \
            and not required_proven:
        try:
            merge_queue._required_ci(project, frozen, branch=status.get("branch"),
                                     allow_inconclusive=True)
        except merge_queue.QueueError as exc:
            return False, str(exc)
    return True, ""


def ready(cfg, db, row: dict, marks: list, status: dict, *, repo: str | None,
          project, target: str, gh=None) -> tuple:
    """Les deux portes réunies : verdicts puis PR/CI. Rend `(ok, raison,
    classe)` ; `status` peut être None (aucune PR lue : seule la porte des
    verdicts est évaluée)."""
    ok, why, klass = ready_verdicts(cfg, db, row, marks, repo=repo, project=project,
                                    target=target)
    if not ok or status is None:
        return ok, why, klass
    frozen = _norm((freeze_of(marks) or {}).get("sha"))
    ok, why = ready_pr(status, target=target, project=project, frozen=frozen,
                       slug=_slug(cfg, row, repo), gh=gh or land._gh)
    return ok, why, klass


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


def _renew(handle, ttl: float = LOCK_TTL_S) -> bool:
    """Le bail est-il encore le nôtre ? Le renouvelle au passage (#1374, P2-6).

    Appelé juste avant chaque geste externe : plusieurs appels `gh` peuvent
    dépasser la durée du bail, et un second hôte le reprendrait pendant que le
    premier continue. Un détenteur périmé rend `busy` et n'agit pas.

    `ttl` est celui du bail : `LOCK_TTL_S` pour `fire:<lot>`, le TTL du train
    (`land.LOCK_TTL_S`) pour le bail de cible partagé avec lui."""
    if handle is None:          # dry-run, ou test sans bail
        return True
    ops, owner, epoch, key = handle
    try:
        return ops.land_lock(key, owner, ttl, epoch=epoch) is not None
    except Exception:  # noqa: BLE001 - bail illisible : on ne mute pas
        return False


def _land_holder(db, lot: int) -> dict | None:
    """Le détenteur du bail `fire:<lot>`, pour le refus (#3598).

    Lecture de diagnostic, jamais une garde : un backend qui ne l'expose pas,
    ou une lecture en échec, laisse le message générique."""
    try:
        reader = getattr(storage.of(db).work, "land_holder", None)
        return reader("fire:%d" % lot) if reader is not None else None
    except Exception:  # noqa: BLE001 - un message ne casse pas le refus
        return None


def _held_error(lot: int, holder: dict | None, waited: float) -> FireError:
    """Le refus du bail, avec son détenteur et son échéance (#3598)."""
    prefix = "another fire operation holds lot #%d" % lot
    if not holder:
        return FireError("%s: retry once it is done (waited %ds)" % (prefix, int(waited)))
    owner = str(holder.get("owner") or "?")
    until = str(holder.get("expires_at") or "?")
    left = holder.get("left_s")
    left_s = ""
    if left is not None:
        try:
            left_s = " (%.0fs left)" % float(left)
        except (TypeError, ValueError):
            left_s = ""
    return FireError("%s: held by %s until %s%s; retry once it is done (waited %ds)"
                     % (prefix, owner, until, left_s, int(waited)))


def _wait_lock(db, lot: int, *, wait: float | None = None,
               sleep=time.sleep, clock=time.monotonic):
    """Le bail `fire:<lot>`, en attendant qu'un passage le rende (#3598).

    `give`/`withdraw` ne doivent plus échouer au premier refus : les passages
    d'auto-fusion tiennent le bail 15 à 20 s, et le retrait doit passer dans
    une fenêtre libre. Tentatives courtes et espacées jusqu'à `wait`
    (`LOCK_WAIT_S` par défaut), puis refus nommant le détenteur et son
    échéance. `sleep`/`clock` sont injectables pour les témoins."""
    budget = LOCK_WAIT_S if wait is None else max(0.0, float(wait))
    deadline = clock() + budget
    interval = LOCK_WAIT_FIRST_S
    while True:
        handle = _lock(db, lot)
        if handle is not None:
            return handle
        remaining = deadline - clock()
        if remaining <= 0:
            raise _held_error(lot, _land_holder(db, lot), budget)
        sleep(min(interval, remaining))
        interval = min(interval * 2.0, LOCK_WAIT_MAX_S)


def _reconcile(db, row: dict, marks: list, status: dict, *, slug: str, repo: str | None,
               target: str, dry_run: bool) -> dict:
    """La PR est fusionnée : le lot passe `merged` tout de suite."""
    lot = int(row["id"])
    freeze = freeze_of(marks)
    fire = given(marks)
    covered = bool(fire) and same_commit(status.get("head"), (fire or {}).get("sha"), repo)
    covered = covered or (bool(freeze) and same_commit(status.get("head"),
                                                       (freeze or {}).get("sha"), repo))
    out = {"lot": lot, "result": "merged", "pr": status.get("number"),
           "sha": status.get("merge_commit") or "", "covered": covered}
    if dry_run:
        out["result"] = "would-merge"
        return out
    done = work.deliver(
        db, lot, connector=work.merge_connector(),
        evidence={"seen": "PR #%s" % status.get("number"),
                  "sha": status.get("merge_commit") or "",
                  "pr": "%s#%s" % (slug, status.get("number"))},
        actor=work.SYSTEM_SENDER, frozen_id=int(freeze["id"]) if freeze else None, repo=repo,
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
        # #2374 : la relecture est ouverte par `close_merged` (une seule fois,
        # sur le diff que la fusion a fait entrer), pas recalculée ici sur
        # `cible..gel` — vide après la fusion, donc faussement « light ».
        out["review_lot"] = done.get("review_lot") or {}
    if not covered:
        _wake(db, row.get("assignee"), lot, (
            "Fire on lot #%d « %s »: PR #%s was merged at %s while the fire covered %s. "
            "The merge is recorded; check what landed and give the fire again on a new freeze."
            % (lot, row.get("title") or "", status.get("number"),
               (status.get("head") or "?")[:12], _norm((fire or {}).get("sha"))[:12] or "—")),
            {"project": row.get("app") or "", "pr": status.get("number"), "anomaly": True})
    return out


def _fresh(db, lot: int) -> tuple:
    """Le lot et ses jalons, relus à l'instant (#1374, P1-3) : un instantané
    pris avant le bail ne vaut pas pour un geste externe."""
    item = work.get(db, lot)
    if item is None:
        return None, []
    return item, work.milestones(db, lot)


def _fire_covers(fire, freeze, repo: str | None = None) -> bool:
    return bool(fire) and bool(freeze) and same_commit(fire.get("sha"), freeze.get("sha"), repo)


def _leave_queue(db, lot: int, *, dry_run: bool = False, handle=None, reason: str = "") -> bool:
    """Retire la ligne de file d'un lot que le feu n'autorise plus (#1374,
    P1-2) : sans cela, `land` — qui ne connaît pas le feu — pourrait encore
    l'atterrir. Rend True si une ligne a été retirée (ou le serait)."""
    try:
        entries = {int(r["work_item_id"]) for r in storage.of(db).work.queue()}
    except Exception:  # noqa: BLE001 - file illisible : le garde de land couvre
        return False
    if lot not in entries:
        return False
    if dry_run:
        return True
    if not _renew(handle):
        return False
    try:
        return bool(merge_queue.leave(db, lot, actor=work.SYSTEM_SENDER,
                                      reason=reason or "fire withdrawn or invalidated"))
    except Exception:  # noqa: BLE001 - la ligne sera retirée au passage suivant
        return False


def _invalidate(db, row: dict, marks: list, fire: dict, reason: str, *, status, gh, slug: str,
                dry_run: bool, handle=None) -> dict:
    """Le feu ne couvre plus la tête : `unfire`, auto-fusion résiduelle
    désarmée, ligne de file retirée, assigné réveillé."""
    lot = int(row["id"])
    out = {"lot": lot, "result": "invalidated", "detail": reason,
           "sha": _norm(fire.get("sha"))}
    if not _renew(handle):
        return {"lot": lot, "result": "busy"}
    if dry_run:
        out["result"] = "would-invalidate"
        return out
    work.milestone(db, lot, UNFIRE, sha=_norm(fire.get("sha")), actor=work.SYSTEM_SENDER,
                   note="invalidated: %s" % reason)
    out["queue_left"] = _leave_queue(db, lot, handle=handle, reason=reason)
    if status and status.get("armed"):
        if _renew(handle):
            ok, detail = _disarm_auto(slug, status, gh=gh)
            out["disarmed"] = ok
            if not ok:
                out["detail"] = "%s; disarm failed (%s)" % (reason, detail)
        else:
            out["detail"] = "%s; disarm deferred (lease lost, retried at the next pass)" % reason
    _wake(db, row.get("assignee"), lot, (
        "Fire on lot #%d « %s »: %s. The automatic merge is disarmed and the merge queue entry "
        "is removed; freeze again and have the pilot give the fire on the new head."
        % (lot, row.get("title") or "", reason)),
        {"project": row.get("app") or "", "reason": reason})
    _passive(db, fire.get("actor"), lot, (
        "Fire on lot #%d « %s » (%s) is invalidated: %s. It is to be given again on the new head."
        % (lot, row.get("title") or "", _norm(fire.get("sha"))[:12], reason)),
        {"project": row.get("app") or "", "reason": reason})
    return out


def _disarm(db, row: dict, status: dict, reason: str, *, gh, slug: str, dry_run: bool,
            handle=None) -> dict:
    lot = int(row["id"])
    out = {"lot": lot, "result": "disarmed", "detail": reason}
    if dry_run:
        out["result"] = "would-disarm"
        return out
    if not _renew(handle):
        return {"lot": lot, "result": "busy"}
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
        # #1374, P1-3 : les jalons sont relus SOUS le bail. Ceux lus avant (par
        # le relevé) peuvent dater d'un autre feu, d'un autre gel ou d'un
        # verdict rendu entre-temps ; un passage ne réarme ni ne fusionne sur
        # un instantané périmé.
        fresh_row, fresh_marks = _fresh(db, lot)
        if fresh_row is None or fresh_row.get("state") in \
                tuple(work.MERGED_STATES) + ("closed",):
            return {"lot": lot, "result": "idle", "detail": "lot no longer open"}
        if not markers(fresh_marks):
            return {"lot": lot, "result": "idle"}
        return _act(cfg, db, fresh_row, fresh_marks, gh=gh, dry_run=dry_run, repo=repo,
                    cache=cache, handle=handle)
    finally:
        if handle is not None:
            _unlock(handle)


def _not_ready(db, row: dict, status, out: dict, why: str, *, gh, slug: str,
               dry_run: bool, handle=None) -> dict:
    """La porte n'est pas franchie : désarmer une auto-fusion résiduelle (elle
    pourrait emporter une tête non couverte, P1-4), ne rien déclencher
    d'autre."""
    if status is not None and status.get("armed"):
        if dry_run:
            out.update(result="would-disarm", detail="fire gates not met: %s" % why)
            return out
        disarmed = _disarm(db, row, status, "fire gates not met: %s" % why, gh=gh, slug=slug,
                           dry_run=dry_run, handle=handle)
        if disarmed.get("result") in ("busy", "error"):
            return disarmed
        out.update(result="disarmed", detail=disarmed.get("detail") or why)
        return out
    out.update(result="not-ready", detail=why)
    return out


def _carrier_gap(db, row: dict, fire: dict, status, *, slug: str, target: str, gh,
                 repo: str | None) -> str:
    """#3611 : pourquoi le feu seul ne fusionnera pas ce porteur, ou "".

    h-23 v4 (#3599, 25/10) : le porteur poussé, son workflow `paquet.yml` lance
    la suite complète sur push ; le feu répond « the CI on the frozen commit
    is still running » alors que rien ne pouvait s'armer — ni PR, ni
    étiquette `ci-full`, et le check requis de `main` (`tests`) n'existe que
    sur le run `pull_request` de `ci-full`. La file attend ce run push, puis
    `land_guard` éjecte faute du check requis : fusion admin forcée.

    Le passage **dit** la cause et nomme le geste ; il ne le fait pas : la
    publication (PR ouverte, puis `ameesh ci run <porteur> --full`) porte la prévalidation du
    paquet, l'épinglage de la composition et l'armement (#2813), et lance une
    suite complète — un geste de pilote, pas un effet de bord d'un passage
    périodique.

    Rien n'est dit — la voie reste celle d'avant — quand la branche n'est pas
    un porteur, quand la fusion automatique est déjà armée sur la tête, quand
    la forge n'impose aucun check (le feu est alors la voie du porteur, #2813)
    ou que cela ne peut se lire, et quand la tête porte déjà tous les checks
    requis (la file sait alors l'atterrir). Un diagnostic illisible ne change
    jamais la voie."""
    from . import packet_automerge
    branch = str(row.get("branch") or "")
    if not packet_automerge.carrier(branch):
        return ""
    lot = int(row["id"])
    sha = merge_queue.pinned(repo, _norm(fire.get("sha"))) if repo else None
    sha = sha or _norm(fire.get("sha"))
    try:
        published = packet_automerge.publication(db, lot, sha)
    except Exception:  # noqa: BLE001 - journal illisible : la voie ne change pas
        return ""
    if published["armed"]:
        return ""
    try:
        names = [name for name, _app in _required_pairs(
            required_checks(slug, target, None, gh=gh))]
    except Exception:  # noqa: BLE001 - 403 (aucune garde de la forge) ou illisible
        return ""
    if not names:
        return ""
    try:
        present = {r["name"] for r in _head_check_runs(slug, sha, gh=gh)} \
            | {s["context"] for s in _head_statuses(slug, sha, gh=gh)}
        missing = [name for name in names if name not in present]
    except Exception:  # noqa: BLE001 - tête illisible : seules les causes locales
        missing = None
    causes = []
    if status is None:
        causes.append("no PR for %s" % branch)
    if not published["requested"]:
        causes.append("no ci-full-%s label requested" % sha[:12])
    if missing:
        causes.append("the required check(s) %s of %s are absent from the head %s (a push "
                      "run of the packet workflow does not produce them)"
                      % (", ".join(missing), target, sha[:12]))
    if not causes or missing == []:
        return ""
    gesture = "`ameesh ci run %d --full --reason \"packet\"`" % lot
    if status is None:
        # `ci run` exige la PR ouverte (#1343) ; son ouverture ne lance rien
        gesture = "`gh pr create --repo %s --base %s --head %s`, then %s" % (
            slug, target, branch, gesture)
    return ("packet carrier: the fire alone never merges it — %s; the packet auto-merge is "
            "not armed on %s. Publish it: %s (label ci-full-<head>, arms the auto-merge, "
            "#2813)" % ("; ".join(causes), sha[:12], gesture))


def _queue_landing(cfg, db, row: dict, fire: dict, freeze: dict, status: dict, *,
                   repo: str | None, project, target: str, path: str) -> bool:
    """#3080: only the queue's clean two-parent landing preserves the fire.

    Recompute its tree without checking out any branch. Remote objects and
    the target are fetched into the radar clone; an unreadable proof raises
    QueueError, so a transient read never withdraws an otherwise valid fire.
    Conflict resolutions or extra edits require a new freeze and review.
    """
    if path != "queue" or not repo or getattr(project, "land", "pr") != "pr" \
            or status.get("base") != land._branch_of(target):
        return False
    entry = next((r for r in storage.of(db).work.queue()
                  if int(r["work_item_id"]) == int(row["id"])), None)
    if not entry or entry.get("state") != "landing" \
            or entry.get("target") != target \
            or entry.get("source") != merge_queue.source_of(repo) \
            or entry.get("frozen_id") != freeze.get("id") \
            or not same_commit(entry.get("frozen_sha"), fire.get("sha"), repo) \
            or not same_commit(entry.get("landing_sha"), status.get("head"), repo):
        return False
    from . import radar, resolutions
    name = project.name if project is not None else os.path.basename(repo.rstrip("/"))
    clone = resolutions.Replayer(repo, radar.clone_dir(cfg, name), target=target, db=db)
    tips, where = merge_queue.fetch_fresh(repo, [row["branch"], target], clone, base=target)
    head = merge_queue.pinned(where, status.get("head"))
    frozen = merge_queue.pinned(where, fire.get("sha"))
    if not head or not frozen or tips.get(row["branch"]) != head or not tips.get(target):
        return False
    parents = plan_git._out(where, "show", "-s", "--format=%P", head)
    if parents is None:
        raise merge_queue.QueueError("cannot read the queue landing's parents")
    if parents.split() != [tips[target], frozen]:
        return False
    merged = resolutions._run(where, "merge-tree", "--write-tree", tips[target], frozen)
    if merged.returncode not in (0, 1):
        raise merge_queue.QueueError("cannot prove the queue landing's clean merge tree")
    tree = plan_git._out(where, "show", "-s", "--format=%T", head)
    if tree is None:
        raise merge_queue.QueueError("cannot read the queue landing's tree")
    return merged.returncode == 0 and resolutions._text(merged).strip() == tree


def _act(cfg, db, row: dict, marks: list, *, gh, dry_run: bool, repo: str | None,
         cache: dict, handle=None) -> dict:
    lot = int(row["id"])
    fire, freeze = active(marks), freeze_of(marks)
    branch = str(row.get("branch") or "")
    out: dict = {"lot": lot, "branch": branch, "fire": bool(fire),
                 "fire_sha": _norm((fire or {}).get("sha")),
                 "freeze_sha": _norm((freeze or {}).get("sha")), "result": "idle"}
    if not branch:
        out.update(result="no-branch", detail="lot without branch: no PR to arm")
        return out
    work_repo = _repo_of(db, row) if repo is None else _checkout(repo)
    # #2535 : un `--repo` explicite qui n'est pas un checkout est un refus,
    # jamais un repli silencieux sur le clone que le projet déclare
    # (`radar.projects.<p>.repo`). Le pilote qui se trompe de dépôt verrait
    # sinon le feu lire une autre copie que celle demandée et rendre un
    # verdict de préparation trompeur.
    if repo is not None and work_repo is None:
        out.update(result="no-repository", detail=(
            "--repo %s is not a git checkout: pass a path inside the lot's repository "
            "(#2535: refusing to read the project's declared clone instead)" % repo))
        return out
    slug = _slug(cfg, row, work_repo)
    if not slug:
        out.update(result="no-repository", detail="no GitHub repository known for this lot")
        return out
    # #2473 : le dossier de l'assigné n'est pas toujours un checkout du lot
    # (le pilote Nexlink enregistre le parent de ses clones). Un `origin` qui
    # nomme un autre dépôt l'écarte ; on retombe alors sur le clone que l'hôte
    # déclare pour le projet (`radar.projects.<p>.repo`), jamais sur un
    # dossier qui ne porte ni origin ni branches. Un `--repo` explicite est
    # un geste du pilote : il est gardé tel quel s'il est un checkout.
    if repo is None and work_repo is not None and not _same_repository(work_repo, slug):
        work_repo = None
    project = _project(cfg, work_repo, slug)
    if work_repo is None:
        work_repo = _project_checkout(project)
    status = _pr_status(slug, branch, gh=gh)
    out.update(slug=slug, repo=work_repo,
               project=project.name if project is not None else "",
               pr=(status or {}).get("number"))

    # 1. une PR fusionnée est un fait : réconciliée tout de suite, quel que
    #    soit le feu (retiré, invalide, absent).
    if status is not None and status.get("merged"):
        return _reconcile(db, row, marks, status, slug=slug, repo=work_repo,
                          target=str(row.get("branch_target") or ""), dry_run=dry_run)

    # 2. sans feu actif : la ligne de file est retirée, l'auto-fusion résiduelle
    #    désarmée, et rien d'autre (P1-2).
    if fire is None:
        out["queue_left"] = _leave_queue(db, lot, dry_run=dry_run, handle=handle,
                                         reason="no active fire on lot #%d" % lot)
        if status is not None and status.get("armed"):
            if dry_run:
                out.update(result="would-disarm",
                           detail="no active fire on lot #%d: auto-merge armed" % lot)
                return out
            return _disarm(db, row, status,
                           "no active fire on lot #%d (withdrawn or invalidated)" % lot,
                           gh=gh, slug=slug, dry_run=dry_run, handle=handle)
        out.update(result="withdrawn" if markers(marks) else "idle")
        return out

    # 3. le feu ne couvre pas le gel courant, ou la tête a bougé : invalidation.
    if not _fire_covers(fire, freeze, work_repo):
        return _invalidate(db, row, marks, fire,
                           "the freeze is %s, not the head the fire covers (%s)"
                           % (_norm((freeze or {}).get("sha"))[:12] or "—",
                              _norm(fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run, handle=handle)
    info = _repo_info(slug, gh=gh, cache=cache)
    target = _target(row, project, work_repo, info)
    path, landing_why = landing_path(cfg, db, project, work_repo, slug)
    out["landing"] = path
    if status is not None and status.get("head") \
            and not same_commit(status.get("head"), fire.get("sha"), work_repo):
        try:
            landing = _queue_landing(cfg, db, row, fire, freeze, status, repo=work_repo,
                                     project=project, target=target, path=path)
        except merge_queue.QueueError as exc:
            return _not_ready(db, row, status, out, str(exc), gh=gh, slug=slug,
                              dry_run=dry_run, handle=handle)
        if not landing:
            return _invalidate(db, row, marks, fire,
                               "the PR head moved to %s, the fire covers %s"
                               % (status["head"][:12], _norm(fire.get("sha"))[:12]),
                               status=status, gh=gh, slug=slug, dry_run=dry_run, handle=handle)

    # #2333 (canon §11 bis) : aucune fusion de lot directe hors paquet, même
    # pour un feu donné avant la garde ou avec une auto-fusion déjà armée. La
    # déclaration d'exception est relue au journal du lot ; un refus désarme
    # une auto-fusion résiduelle (`_not_ready`) avant toute PR ou file.
    try:
        packet_guard.check_merge(db, lot, branch, target, _norm(fire.get("sha")))
    except packet_guard.PacketGuardError as exc:
        return _not_ready(db, row, status, out, str(exc), gh=gh, slug=slug,
                          dry_run=dry_run, handle=handle)

    # 4. LA PORTE DES VERDICTS, AVANT TOUTE PR, TOUTE FILE ET TOUTE CI (#1374,
    #    B et P1-1). Le feu peut être enregistré au gel ; rien ne part avant
    #    les avis requis, indépendants et sans blocage non levé.
    ok, why, klass = ready_verdicts(cfg, db, row, marks, repo=work_repo, project=project,
                                    target=target)
    out["class"] = klass.get("class")
    if not ok:
        return _not_ready(db, row, status, out, why, gh=gh, slug=slug, dry_run=dry_run,
                          handle=handle)

    if status is not None and status.get("state") == "CLOSED" and not status.get("merged"):
        out.update(result="pr-closed", detail="PR #%s is closed without merge: reopen it"
                   % status.get("number"))
        return out

    # #3611 : un porteur `integration/**` sur une cible dont la forge impose
    # des checks ne fusionne que par sa publication (`ameesh ci run --full`,
    # #2813) ; la file attendrait une CI qui ne produit pas ces checks.
    gap = _carrier_gap(db, row, fire, status, slug=slug, target=target, gh=gh,
                       repo=work_repo)
    if gap:
        return _not_ready(db, row, status, out, gap, gh=gh, slug=slug, dry_run=dry_run,
                          handle=handle)

    # 5. la voie du lot (#2256) : décidée par la déclaration du projet ou
    #    l'état partagé de sa file, jamais par la config de cet hôte. Sans
    #    déclaration ni file connue, le feu s'abstient — il ne fusionne jamais
    #    en direct par défaut.
    if path != "direct" and status is not None and status.get("armed"):
        return _not_ready(db, row, status, out,
                          "landing path %s forbids GitHub auto-merge" % path,
                          gh=gh, slug=slug, dry_run=dry_run, handle=handle)
    if path == "unknown":
        out.update(result="no-land", detail=landing_why)
        return out

    # 6. file de fusion (projet qui déclare `land`) : entrée quand les avis et
    #    la CI sont là ; sinon PR ouverte, CI, puis fusion au geste effectif.
    if path == "queue":
        if not dry_run and not _renew(handle):
            return {"lot": lot, "result": "busy"}
        return _queue(db, row, marks, fire, project=project, repo=work_repo, target=target,
                      cfg=cfg, dry_run=dry_run, out=out)
    if status is None:
        if dry_run:
            out.update(result="would-open-pr", detail="no PR for %s" % branch)
            return out
        return _open_then_merge(cfg, db, row, marks, fire, out=out, slug=slug, branch=branch,
                                target=target, info=info, repo=work_repo, project=project,
                                gh=gh, dry_run=dry_run, handle=handle)
    return _merge_direct(cfg, db, row, marks, fire, status, out=out, slug=slug, branch=branch,
                         target=target, info=info, repo=work_repo, project=project, gh=gh,
                         dry_run=dry_run, handle=handle)


def _open_then_merge(cfg, db, row: dict, marks: list, fire: dict, *, out: dict, slug: str,
                     branch: str, target: str, info: dict, repo: str | None, project,
                     gh, dry_run: bool, handle=None) -> dict:
    """Ouvre la PR (elle déclenche la CI) seulement après une revalidation du
    feu, du gel et des verdicts, puis poursuit vers la fusion (#1374, B/P1-3)."""
    lot = int(row["id"])
    fresh_row, fresh_marks = _fresh(db, lot)
    fresh_fire, fresh_freeze = active(fresh_marks), freeze_of(fresh_marks)
    if fresh_row is None or fresh_fire is None:
        out.update(result="withdrawn", detail="the fire was withdrawn before the PR was opened")
        return out
    if not _fire_covers(fresh_fire, fresh_freeze, repo):
        return _invalidate(db, fresh_row, fresh_marks, fresh_fire,
                           "the freeze is %s, not the head the fire covers (%s)"
                           % (_norm((fresh_freeze or {}).get("sha"))[:12] or "—",
                              _norm(fresh_fire.get("sha"))[:12]),
                           status=None, gh=gh, slug=slug, dry_run=dry_run, handle=handle)
    ok, why, _klass = ready_verdicts(cfg, db, fresh_row, fresh_marks, repo=repo,
                                     project=project, target=target)
    if not ok:
        out.update(result="not-ready", detail=why)
        return out
    if not _renew(handle):
        return {"lot": lot, "result": "busy"}
    status, detail = _open_pr(cfg, db, fresh_row, slug=slug, target=target, info=info, gh=gh)
    if status is None:
        out.update(result="no-pr", detail=detail)
        return out
    out.update(pr=status.get("number"), detail=detail)
    _passive(db, fresh_row.get("assignee"), lot,
             "Fire on lot #%d « %s »: PR #%s opened by ameesh for %s."
             % (lot, fresh_row.get("title") or "", status.get("number"), branch),
             {"project": fresh_row.get("app") or "", "pr": status.get("number")})
    if status.get("head") and not same_commit(status.get("head"), fresh_fire.get("sha"), repo):
        return _invalidate(db, fresh_row, fresh_marks, fresh_fire,
                           "the PR head moved to %s, the fire covers %s"
                           % (status["head"][:12], _norm(fresh_fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run, handle=handle)
    return _merge_direct(cfg, db, fresh_row, fresh_marks, fresh_fire, status, out=out, slug=slug,
                         branch=branch, target=target, info=info, repo=repo, project=project,
                         gh=gh, dry_run=dry_run, handle=handle)


def _target_lock(db, project, target: str):
    """Le bail de la cible, commun au train de fusion (#1467) et à la fusion
    directe du feu.

    `(handle, False)` : le bail est pris, `handle` le rend ensuite.
    `(None, True)` : un autre hôte tient `land.lock_key(project, target)` :
    aucun geste. `(None, False)` sans projet ni cible déclarés : il n'y a pas de
    train à croiser (#1508). Le bail est pris après `fire:<lot>` et jamais avant, le
    train ne prenant jamais `fire:<lot>` : pas de cycle.

    Le TTL est celui du train (`land.LOCK_TTL_S`, réglable par
    `AMEESH_LAND_LOCK_TTL`), pas celui du bail de feu : c'est le même bail que
    le train renouvelle, et un détenteur direct qui expirerait plus tôt que le
    TTL annoncé rendrait la cible au train en cours de geste."""
    if project is None or not target:
        return None, False
    ops = storage.of(db).work
    owner = "fire:%s:%d" % (socket.gethostname(), os.getpid())
    key = land.lock_key(project, target)
    epoch = ops.land_lock(key, owner, land.LOCK_TTL_S)
    return ((ops, owner, epoch, key), False) if epoch is not None else (None, True)


def _merge_direct(cfg, db, row: dict, marks: list, fire: dict, status: dict, *, out: dict,
                  slug: str, branch: str, target: str, info: dict, repo: str | None, project,
                  gh, dry_run: bool, handle=None) -> dict:
    """Les portes de la PR, puis la fusion **maintenant**, sur la tête exacte
    (#1374, P1-4) : une auto-fusion résiduelle est désarmée d'abord, et le feu,
    le gel et les verdicts sont relus juste avant le geste (P1-3)."""
    lot = int(row["id"])
    frozen = _norm((freeze_of(marks) or {}).get("sha"))
    report: dict = {}
    ok, why = ready_pr(status, target=target, project=project, frozen=frozen,
                       slug=slug, gh=gh, report=report)
    if not ok:
        # #3186 : un requis jamais conclu bloque ; son run est relancé une fois,
        # sous le bail du feu et la garde du pilote élu, comme la fusion.
        def guard() -> bool:
            if not _renew(handle):
                return False
            from . import pilot_gesture
            try:
                fence = pilot_gesture.gate(cfg, db, "merge", "fire rerun #%d" % lot,
                                           project=(row.get("app") or None))
                if fence is not None:
                    fence.hold(db, margin_s=FIRE_CALL_MARGIN_S)
            except pilot_gesture.GestureRefused:
                return False
            return True
        reruns = relaunch_unconcluded(db, lot, slug, status.get("head") or frozen, report,
                                      gh=gh, dry_run=dry_run, guard=guard)
        if reruns:
            from . import radar
            out["reruns"] = reruns
            why = "%s — %s" % (why, radar.describe_reruns(reruns))
        return _not_ready(db, row, status, out, why, gh=gh, slug=slug, dry_run=dry_run,
                          handle=handle)
    if status.get("armed"):
        if dry_run:
            out.update(result="would-disarm",
                       detail="auto-merge armed: disarmed before an exact-head merge")
            return out
        disarmed = _disarm(db, row, status,
                           "auto-merge armed outside the fire: disarmed before an exact-head merge",
                           gh=gh, slug=slug, dry_run=dry_run, handle=handle)
        if disarmed.get("result") in ("busy", "error"):
            return disarmed
        try:
            status = _pr_status(slug, branch, gh=gh) or status
        except land.LandError:
            pass
        if status.get("merged"):
            return _reconcile(db, row, marks, status, slug=slug, repo=repo, target=target,
                              dry_run=dry_run)
    if dry_run:
        out.update(result="would-merge", detail="PR #%s, class %s, CI green"
                   % (status.get("number"), out.get("class") or "?"))
        return out
    # revalidation à l'instant du geste : le feu peut avoir été retiré, le gel
    # rejoué ou un verdict bloqué rendu depuis la lecture initiale.
    fresh_row, fresh_marks = _fresh(db, lot)
    fresh_fire, fresh_freeze = active(fresh_marks), freeze_of(fresh_marks)
    if fresh_row is None or fresh_fire is None:
        out.update(result="withdrawn", detail="the fire was withdrawn before the merge")
        return out
    if not _fire_covers(fresh_fire, fresh_freeze, repo):
        return _invalidate(db, fresh_row, fresh_marks, fresh_fire,
                           "the freeze is %s, not the head the fire covers (%s)"
                           % (_norm((fresh_freeze or {}).get("sha"))[:12] or "—",
                              _norm(fresh_fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run, handle=handle)
    ok, why, klass = ready_verdicts(cfg, db, fresh_row, fresh_marks, repo=repo, project=project,
                                    target=target)
    if not ok:
        return _not_ready(db, fresh_row, status, out, why, gh=gh, slug=slug, dry_run=dry_run,
                          handle=handle)
    head = _norm(fresh_fire.get("sha"))
    cible, blocked = _target_lock(db, project, target)
    if blocked:
        # le train de fusion (ou un autre feu) tient déjà la cible sous le même
        # bail : aucun geste tant qu'il ne l'a pas rendue (#1467)
        return {"lot": lot, "result": "busy"}
    try:
        if not _renew(handle) or not _renew(cible, land.LOCK_TTL_S):
            return {"lot": lot, "result": "busy"}
        # L165 (EP4) : le passage peut être mené par une minuterie de rôle
        # (`AMEESH_ROLE`) ; le bail élu est renouvelé juste avant l'appel
        # GitHub, avec la marge de `gh pr merge` et de sa relecture.
        from . import pilot_gesture
        try:
            fence = pilot_gesture.gate(cfg, db, "merge", "fire merge #%d" % lot,
                                       project=(fresh_row.get("app") or None))
            if fence is not None:
                fence.hold(db, margin_s=FIRE_CALL_MARGIN_S)
        except pilot_gesture.GestureRefused as exc:
            return {"lot": lot, "result": "error", "error": str(exc)}
        ok, error = _merge_pr(slug, status, head, _method(project, info), gh=gh,
                              target=target,
                              default=str(info.get("default_branch") or ""))
        if ok:
            out.update(result="merged", detail=error, sha=head)
            # #3085 : la trace du non concluant qui n'a pas retenu la fusion.
            consigned = list(report.get("consigned") or ())
            if consigned:
                out["consigned"] = consigned
                try:
                    work.note(db, lot,
                              "merged with non-conclusive required checks (owner rule "
                              "23/10, consigned): %s" % ", ".join(consigned),
                              actor="ameesh", once=True)
                except Exception:  # noqa: BLE001 - la fusion est faite, la trace au mieux
                    pass
            try:
                after = _pr_status(slug, branch, gh=gh)
            except land.LandError:
                after = None
            if after is not None and after.get("merged"):
                return _reconcile(db, fresh_row, fresh_marks, after, slug=slug, repo=repo,
                                  target=target, dry_run=dry_run)
            _passive(db, fresh_row.get("assignee"), lot, (
                "Fire on lot #%d « %s »: PR #%s merged on %s (class %s)."
                % (lot, fresh_row.get("title") or "", status.get("number"), head[:12],
                   klass.get("class") or "?")),
                {"project": fresh_row.get("app") or "", "pr": status.get("number"), "sha": head})
            return out
        # la fusion a échoué : relire la PR avant de conclure
        try:
            after = _pr_status(slug, branch, gh=gh)
        except land.LandError:
            after = None
        if after is not None and after.get("merged"):
            return _reconcile(db, fresh_row, fresh_marks, after, slug=slug, repo=repo,
                              target=target, dry_run=dry_run)
        if after is not None and after.get("head") \
                and not same_commit(after.get("head"), head, repo):
            return _invalidate(db, fresh_row, fresh_marks, fresh_fire,
                               "the PR head moved to %s before the merge, the fire covers %s"
                               % (after["head"][:12], head[:12]),
                               status=after, gh=gh, slug=slug, dry_run=dry_run, handle=handle)
        out.update(result="error", error=error)
        return out
    finally:
        if cible is not None:
            _unlock(cible)


def _queue(db, row: dict, marks: list, fire: dict, *, project, repo: str | None, target: str,
           cfg, dry_run: bool, out: dict) -> dict:
    """Le repli file de fusion : le lot entre dans la file de sa cible ; c'est
    l'exécutant qui l'atterrit (`land`). Un lot déjà en file ou en atterrissage
    n'est pas ré-entré (une ré-entrée remettrait `landing` à `queued`).

    La porte `ready_pr` n'est pas lue ici : la file est le mode des dépôts
    dont la protection de branche GitHub n'est pas exploitable (Nexlink, plan
    gratuit : la lecture répond 403). Le contrôle de la cible y est celui de
    `merge_queue.enter` — la CI du commit gelé quand le projet déclare
    `ci_required` (#2285 N3, hors périmètre de ce lot).

    #3532 : une sortie de file (`queue remove`, reçu typé `removed`) écrite
    après ce feu est définitive pour lui : le repli ne ré-entre pas le lot
    (`taken-out`). Sans cela, chaque passage défaisait la sortie, et le lot
    atterrissait dès que la cible se libérait. Un nouveau feu ou un
    `ameesh work queue add` explicite le remet en file.

    #3550 : cette sortie ne vaut que pour l'entrée qu'elle a retirée — sa
    cible et son gel. Une sortie d'une autre cible, ou d'un gel plus ancien,
    ne retient pas le repli du feu courant (lecture `queue_withdrawn_since`
    portée par `target` et `frozen_id`)."""
    lot = int(row["id"])
    if not repo:
        out.update(result="no-repository", detail=(
            "no git checkout of %s known for lot #%d: the assignee's registered directory is "
            "not a checkout and no radar.projects clone is declared for the project; pass "
            "--repo <path> (a directory that is not a repository carries no branch, #2473)"
            % (out.get("slug") or "the lot's repository", lot)))
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
    granted = float(fire.get("at_ts") or 0.0)
    freeze = freeze_of(marks)
    frozen_id = freeze.get("id") if freeze else None
    try:
        removed = storage.of(db).work.queue_withdrawn_since(
            lot, granted, target=target or None, frozen_id=frozen_id)
    except Exception as exc:  # noqa: BLE001 - reçus illisibles : on n'entre pas
        out.update(result="not-ready", detail=(
            "cannot read the queue take-outs of lot #%d (%s: %s): not queued"
            % (lot, type(exc).__name__, str(exc)[:120])))
        return out
    if removed is not None:
        return _taken_out(out, lot, removed, granted)
    if dry_run:
        out.update(result="would-queue", detail="enter the merge queue of %s" % (target or "?"))
        return out
    from . import radar, resolutions
    name = project.name if project is not None else os.path.basename((repo or "project").rstrip("/"))
    clone = resolutions.Replayer(
        repo, radar.clone_dir(cfg, name),
        target=getattr(project, "target", "") or "", db=db) if repo else None
    try:
        entered = merge_queue.enter(db, lot, repo=repo, actor=work.SYSTEM_SENDER,
                                    project=project, clone=clone, cfg=cfg,
                                    withdrawn_after=granted)
    except merge_queue.QueueWithdrawn:
        # #3550: a failed re-read here is a transient refusal (not-ready), not
        # an error that escapes the fire pass. The read is scoped like the
        # first one: the take-out must name this target and this freeze.
        try:
            removed = storage.of(db).work.queue_withdrawn_since(
                lot, granted, target=target or None, frozen_id=frozen_id)
        except Exception as exc:  # noqa: BLE001 - reçus illisibles : on n'entre pas
            out.update(result="not-ready", detail=(
                "cannot read the queue take-outs of lot #%d (%s: %s): not queued"
                % (lot, type(exc).__name__, str(exc)[:120])))
            return out
        return _taken_out(out, lot, removed or granted, granted)
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


def _taken_out(out: dict, lot: int, removed: float, granted: float) -> dict:
    out.update(result="taken-out", detail=(
        "lot #%d was taken out of the merge queue at %s, after its fire (%s): the fire's "
        "fallback does not queue it again; fire again or `ameesh work queue add %d`"
        % (lot, merge_queue._utc(removed), merge_queue._utc(granted), lot)))
    return out


# --------------------------------------------------------------------------
# le dernier geste de l'atterrissage
# --------------------------------------------------------------------------

def land_guard(db, lot: int, frozen_sha: str, *, repo: str | None = None,
               project=None, target: str = "", gh=None) -> tuple:
    """Le dernier contrôle avant le geste d'atterrissage (#1374, P1-2) : un lot
    qui a porté un feu ne s'atterrit que sous un feu actif couvrant la tête
    gelée de sa file, et jamais sous un blocage non levé.

    #2286 : pour un atterrissage **local** (`project.land == "local"`, la voie
    file de Nexlink), les checks requis **déclarés** du projet
    (`radar.projects.<p>.required_checks`) sont aussi exigés sur le gel : la
    poussée directe ne passe par aucune protection de branche, et cette
    déclaration est la seule source de la cible (Nexlink n'en a pas). La garde
    vaut pour tout atterrissage, `ci_required` ou non ; un projet qui ne
    déclare aucun check garde le comportement d'avant, et la voie `pr` reste
    tenue par la forge (et par `ready_pr` au feu) : ses checks de PR vivent
    sur la ref de fusion, pas sur le commit gelé.

    Rend `(ok, raison)`. La garde est appelée par `land` juste avant la poussée
    ou la fusion de la PR, sous son propre bail : la ligne de file peut avoir
    été retirée par le passage du feu, mais la course entre les deux reste
    fermée ici. Sans aucun jalon de feu, la file d'équipe garde son cours : la
    garde du feu ne concerne que les lots que le feu a autorisés (les checks
    requis, eux, valent pour tout atterrissage local).

    #2306 : une cible vide est refusée (elle sautait la garde) ; une lecture
    passagère des checks lève `land.TransientLandError`, que `land` traduit en
    nouvelle tentative au passage suivant, sans éjection.
    """
    lot = int(lot)
    try:
        marks = work.milestones(db, lot)
    except Exception as exc:  # noqa: BLE001 - lecteur illisible : on refuse
        return False, ("cannot read the fire of lot #%d (%s: %s): landing refused"
                       % (lot, type(exc).__name__, str(exc)[:120]))
    if markers(marks):
        fire = active(marks)
        if fire is None:
            return False, ("the fire of lot #%d was withdrawn or invalidated: no automatic "
                           "landing" % lot)
        if not same_commit(fire.get("sha"), frozen_sha, repo):
            return False, ("the fire of lot #%d covers %s, not the queued freeze %s: freeze and "
                           "fire again" % (lot, _norm(fire.get("sha"))[:12],
                                           str(frozen_sha or "")[:12] or "—"))
        blocked = merge_queue.blocked_verdicts(marks, frozen_sha, repo=repo)
        if blocked:
            return False, ("blocked verdict(s) on the freeze %s not lifted: %s: no automatic "
                           "landing" % (str(frozen_sha or "")[:12],
                                        ", ".join(sorted(str(m.get("actor") or "?")
                                                         for m in blocked))))
    if project is not None and getattr(project, "land", "") == "local":
        sha = _norm(frozen_sha)
        if not target:
            # #2306 : sans cible, la garde des checks requis était sautée
            # (fail-open). Un atterrissage local sans cible est impossible
            # (`fresh_target` la réclame) ; on refuse au lieu de laisser passer.
            return False, ("no target branch known: the required checks cannot be proven: "
                           "landing refused")
        try:
            gaps = required_on_head(project, target, sha, gh=gh)
        except land.TransientLandError:
            # #2306 : lecture passagère — l'appelant garde la ligne en file et
            # réessaie au passage suivant ; le refus (fail-closed) tient.
            raise
        except land.LandError as exc:
            return False, ("the required checks of %s on %s cannot be proven: %s: landing "
                           "refused" % (target, sha[:12] or "—", exc))
        if gaps:
            return False, ("the required checks of %s are not proven on %s: %s: landing "
                           "refused" % (target, sha[:12] or "—", ", ".join(gaps)))
    return True, ""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "fire", help="the pilot's go at freeze (#1374): ameesh arms the automatic merge, "
                     "which departs at green CI")
    parser.add_argument("id", type=int)
    parser.add_argument("--sha", default="",
                        help="the frozen head the fire covers (to give the fire); waits for an "
                             "in-flight pass's lease (up to ~2 min) before refusing")
    parser.add_argument("--withdraw", action="store_true",
                        help="withdraw the fire: the automatic merge is disarmed; waits for an "
                             "in-flight pass's lease (up to ~2 min) before refusing (#3598)")
    parser.add_argument("--repo", default=None,
                        help="the lot's repository: a checkout path, or the current "
                             "checkout's owner/name (default: the current git repository)")
    parser.add_argument("--exception", choices=packet_guard.EXCEPTIONS, default="",
                        help="#2333/canon §11 bis: incident|unblock, for a lot branch merged "
                             "outside the packet (the declaration is journaled in the lot's "
                             "note); requires --sha and --reason")
    parser.add_argument("--reason", default="",
                        help="why the packet exception is taken (incident: target red; "
                             "unblock: CI stuck); written to the lot's journal")
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="",
                        help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def run(cfg, db, args) -> int:
    """`ameesh work fire <lot> --sha S | --withdraw | (rien : l'état)`. Le feu
    est un geste de pilote : humain, orchestrateur ou agent de conception
    **déclaré** — `history=False`, comme `pin_authority` (#1374, P1-5) : avoir
    confié des lots récemment ne donne pas le feu."""
    import json
    import sys

    from . import resolutions
    if args.sha and args.withdraw:
        print("--sha and --withdraw are exclusive", file=sys.stderr)
        return 2
    if args.exception and not args.sha:
        print("--exception applies when the fire is given: pass --sha", file=sys.stderr)
        return 2
    try:
        repo = resolutions.repo_root(args.repo) if args.repo else None
    except (resolutions.ResolveError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        # l'état du feu se lit par tous ; donner ou retirer le feu est le geste
        # du pilote (humain, orchestrateur ou agent de conception), sur un rôle
        # DÉCLARÉ : un exécutant qui a délégué un lot n'est pas pilote.
        if args.sha or args.withdraw:
            try:
                actor = work.corrector(cfg, db, args.actor, history=False)
            except work.WorkError as exc:
                print("the fire is the pilot's gesture (a human, an orchestrator or a design "
                      "agent with a declared role): %s" % exc, file=sys.stderr)
                return 1
        if args.sha:
            row = give(db, args.id, sha=args.sha, actor=actor, note=args.note, repo=repo,
                       exception=args.exception or "", reason=args.reason or "")
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
