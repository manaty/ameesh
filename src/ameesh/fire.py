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
4. la PR manquante est alors ouverte (mode GitHub) ou le lot entre dans la
   file de fusion (`merge_queue.enter`, Nexlink), une fois les portes vertes ;
5. en mode GitHub, la fusion est **faite par ce passage**, sous
   `gh pr merge --match-head-commit <sha>` : la tête autorisée est vérifiée au
   geste effectif, et aucune auto-fusion GitHub n'est laissée armée entre deux
   passages — GitHub fusionnerait une tête poussée après le feu (P1-4). Une
   auto-fusion trouvée armée est désarmée avant tout autre geste ;
6. en mode file, l'atterrissage re-vérifie le feu et les blocages au dernier
   geste (`fire.land_guard`, sous le bail de `land`).

Toute opération d'un lot — le passage, mais aussi `give` et `withdraw` — est
tenue par le même bail (`merge_locks`, clé `fire:<lot>`) : les jalons sont
relus sous le bail, renouvelé et vérifié juste avant chaque geste externe, et
feu, gel et verdicts sont revalidés juste avant la PR, la file ou la fusion.
Un détenteur périmé s'arrête (`busy`) au lieu d'agir sur un instantané
(P1-3, P2-6).
"""
from __future__ import annotations

import os
import re
import socket
from urllib.parse import quote

from . import land, lot_class, merge_queue, plan_git, storage, work

FIRE = "fire"
UNFIRE = "unfire"
FIRE_KINDS = (FIRE, UNFIRE)
#: lots examinés par passage (les feux sont rares : le plafond est large)
MAX_LOTS = 1000
#: durée du bail d'une opération de feu
LOCK_TTL_S = 120.0
#: résultats qu'un passage n'a pas à journaliser
QUIET = ("idle", "armed", "queued", "busy", "withdrawn")
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

    Le geste prend le bail du feu (`fire:<lot>`), celui du passage : un
    passage en cours d'armement ou de fusion ne peut pas ignorer un feu qui
    arrive, et le feu ne peut pas se poser pendant qu'un autre hôte agit
    (#1374, P1-3)."""
    handle = _lock(db, lot)
    if handle is None:
        raise FireError("another fire operation holds lot #%d: retry once it is done" % lot)
    try:
        return _give_locked(db, lot, sha=sha, actor=actor, note=note, repo=repo)
    finally:
        _unlock(handle)


def _give_locked(db, lot: int, *, sha: str, actor: str, note: str = "",
                 repo: str | None = None) -> dict:
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
    désarmée et la ligne de file retirée au passage suivant, rien d'autre n'est
    annulé. Même bail que le passage (#1374, P1-3)."""
    handle = _lock(db, lot)
    if handle is None:
        raise FireError("another fire operation holds lot #%d: retry once it is done" % lot)
    try:
        item = work.get(db, lot)
        if item is None:
            raise FireError("lot #%d not found" % lot)
        fire = active(work.milestones(db, lot))
        if fire is None:
            raise FireError("lot #%d carries no active fire: nothing to withdraw" % lot)
        return work.milestone(db, lot, UNFIRE, sha=_norm(fire.get("sha")), actor=actor,
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
        # #2271 : `ready_pr` lit les checks requis dans ce rollup, pas dans
        # `ci` seul (qui compte `SKIPPED` comme vert).
        "rollup": rollup,
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


def _merge_pr(slug: str, status: dict, sha: str, method: str, *, gh) -> tuple:
    """Fusionne la PR **maintenant**, sur la tête exacte `sha` (#1374, P1-4).

    C'est ce geste, et lui seul, qui est lié au feu : `--match-head-commit`
    est vérifié par GitHub au moment de la fusion, pas à l'armement. Laisser
    une auto-fusion armée entre deux passages ferait fusionner une tête
    poussée après le feu (GitHub ne la désarme pas pour un push d'un compte
    avec droit d'écriture). Rend `(ok, detail)`."""
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
                   project, target: str) -> tuple:
    """La première porte, celle qui ne doit rien déclencher (#1374, B) : la
    classe du lot et les **avis requis** sur la tête gelée, comptés et
    indépendants (`merge_queue.review_gate`). Rend `(ok, raison, classe)`.

    Un doute de classe (dépôt inconnu, diff illisible) vaut sensible : le
    verdict reste exigé. Une cible `release/*` (classe release) n'est jamais
    une auto-fusion : c'est la procédure de release du pilote.

    Cette porte s'évalue **avant** toute PR, toute entrée en file et toute CI :
    un feu peut être enregistré au gel, mais rien ne part avant les avis."""
    lot = int(row["id"])
    freeze = freeze_of(marks)
    frozen = _norm((freeze or {}).get("sha"))
    head = merge_queue.pinned(repo, frozen) if repo else None
    klass = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=repo or None,
                             base=target or None, head=head)
    if str(klass.get("class") or "") == lot_class.RELEASE:
        return False, ("class: release (%s) — the release procedure belongs to the pilot: "
                       "a fire never arms an automatic merge on a release"
                       % (klass.get("why") or "release")), klass
    gate = merge_queue.review_gate(db, marks, frozen, repo=repo, klass=klass, item=row)
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


def _required_gap(required, rollup, provenance=None) -> list:
    """Les checks requis que `rollup` et `provenance` ne prouvent pas.

    `provenance` porte les check runs de la tête exacte avec leur application
    source. Un couple `(contexte, app_id)` n'est vert que si un check run de
    ce nom **et de cette application** est `SUCCESS` ; une entrée sans source
    (statut de commit) ou d'une autre application ne prouve rien. Sans
    application imposée (`app_id` à `None`), tout check ou statut du nom exigé
    compte, et un même nom porté par plusieurs entrées n'est vert que si
    toutes le sont : le doute ne verdit pas la porte."""
    gaps = []
    for name, app_id in _required_pairs(required):
        if app_id is None:
            states = [_rollup_state(entry) for entry in rollup or ()
                      if _rollup_name(entry) == name]
            if not states:
                gaps.append("%s (absent)" % name)
                continue
            bad = sorted({state or "?" for state in states if state != "SUCCESS"})
            if bad:
                gaps.append("%s (%s)" % (name, ", ".join(bad)))
            continue
        runs = [entry for entry in provenance or ()
                if _rollup_name(entry) == name and entry.get("app_id") == app_id]
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
        bad = sorted({_rollup_state(entry) or "?" for entry in runs
                      if _rollup_state(entry) != "SUCCESS"})
        if bad:
            gaps.append("%s (%s)" % (name, ", ".join(bad)))
    return gaps


#: #2306 : les codes HTTP qui disent « réessaie » plutôt que « c'est prouvé ».
_TRANSIENT_CODE = re.compile(r"(?i)(?:\bhttp[ /]?|\(\s*)(?:408|429|5\d\d)\b")
_TRANSIENT_TEXT = (
    "timed out", "timeout", "deadline exceeded", "connection reset",
    "connection refused", "connection aborted", "broken pipe",
    "network is unreachable", "temporary failure in name resolution",
    "no such host", "tls handshake", "unexpected eof", "server error",
    "bad gateway", "service unavailable", "gateway timeout",
    "rate limit", "too many requests", "abuse detection",
)


def _transient_read(detail: str) -> bool:
    """#2306 : la lecture de la forge a-t-elle échoué pour une raison
    passagère ? Seuls ces échecs (HTTP 408/429/5xx, délai, coupure réseau)
    gardent la ligne en file ; un 4xx, un refus de droits ou une réponse
    malformée restent des échecs prouvés, qui éjectent."""
    low = str(detail or "").lower()
    return bool(_TRANSIENT_CODE.search(low)) or any(word in low for word in _TRANSIENT_TEXT)


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
    proc = gh(["api", "--paginate", "--slurp", path + "?per_page=100"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        error = land.TransientLandError if _transient_read(detail) else land.LandError
        raise error("gh api %s: %s" % (path, detail or "exit %d" % proc.returncode))
    pages = land._json(proc, "gh api %s" % path, list)
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
            runs.append({"name": run["name"],
                         "status": str(run.get("status") or ""),
                         "conclusion": str(run.get("conclusion") or ""),
                         "app_id": app_id})
    return runs


def _protection_required(slug: str | None, target: str, *, gh) -> tuple:
    """Les checks requis par la protection classique de `target`, lus à la
    forge, sous la forme `(contexte, app_id)` : `app_id` est l'application
    GitHub que la protection impose (`checks[].app_id`), `None` quand elle
    n'impose aucun fournisseur (ancienne liste `contexts`). Perdre `app_id`
    accepterait un check homonyme produit par n'importe qui. Le contexte est
    rendu exactement, espaces significatifs compris : `tests ` n'est pas
    `tests`.

    Un 404 « Branch not protected » est une réponse (aucun check requis) ;
    toute autre lecture — droits, branche absente, réponse malformée — lève
    `land.LandError` : l'absence de garde ne se suppose jamais."""
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
        error = land.TransientLandError if _transient_read(detail) else land.LandError
        raise error("gh api %s: %s" % (path, detail or "exit %d" % proc.returncode))
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
        app_id = check.get("app_id")
        if app_id is not None and (isinstance(app_id, bool)
                                   or not isinstance(app_id, int) or app_id <= 0):
            raise land.LandError("gh api %s: invalid required check app_id" % path)
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


def required_checks(slug: str | None, target: str, project, *, gh) -> tuple:
    """Les checks requis de la branche cible, sous la forme `(contexte,
    app_id)` : la protection lue à la forge, complétée par la déclaration
    `radar.projects.<p>.required_checks` (des noms, sans application imposée).

    Quand la protection ne peut pas être lue (jeton sans droits, forge
    muette), la déclaration du projet la remplace ; sans déclaration, la
    lecture refuse. La déclaration ne peut jamais retirer un check que la
    forge exige : elle s'ajoute. Les noms sont exacts de bout en bout :
    la déclaration `tests ` ne vaut pas le contexte `tests`."""
    declared = tuple((str(name), None) for name in
                     (getattr(project, "required_checks", ()) or ())
                     if str(name).strip())
    if not target:
        if declared:
            return declared
        raise land.LandError("no target branch known: its required checks cannot be read")
    try:
        forged = _protection_required(slug, target, gh=gh)
    except land.LandError:
        if declared:
            return declared
        raise
    return tuple(dict.fromkeys(forged + declared))


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
    proc = gh(["api", "--paginate", "--slurp", path + "?per_page=100"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        error = land.TransientLandError if _transient_read(detail) else land.LandError
        raise error("gh api %s: %s" % (path, detail or "exit %d" % proc.returncode))
    pages = land._json(proc, "gh api %s" % path, list)
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
    rollup = ([{"__typename": "CheckRun", "name": run["name"], "status": run["status"],
                "conclusion": run["conclusion"]} for run in runs] + statuses)
    return _required_gap(required, rollup, runs)


def ready_pr(status: dict, *, target: str, project, frozen: str, required=None,
             slug: str | None = None, gh=None) -> tuple:
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
    check requis inconnu ne se suppose pas vert."""
    if status.get("draft"):
        return False, "PR #%s is a draft" % status.get("number")
    if target and status.get("base") and status["base"] != target:
        return False, "PR #%s targets %s, not %s" % (status.get("number"), status["base"],
                                                     target)
    head, frozen_head = _norm(status.get("head")), _norm(frozen)
    if head and frozen_head and not _same_head(head, frozen_head):
        return False, ("PR #%s is on %s, not the freeze %s"
                       % (status.get("number"), head[:12], frozen_head[:12]))
    if status.get("ci") != "green":
        return False, "the CI of PR #%s is %s on the freeze %s" % (
            status.get("number"), status.get("ci"), frozen[:12])
    if required is None:
        try:
            required = required_checks(slug, target, project, gh=gh)
        except land.LandError as exc:
            return False, ("the required checks of %s on the freeze %s cannot be proven: %s"
                           % (target or "the target", frozen[:12], exc))
    pairs = _required_pairs(required)
    provenance = None
    if any(app_id is not None for _, app_id in pairs):
        try:
            provenance = _head_check_runs(slug, head or frozen_head, gh=gh)
        except land.LandError as exc:
            return False, ("the source of the required checks of %s on the freeze %s "
                           "cannot be proven: %s"
                           % (target or "the target", frozen[:12], exc))
    gaps = _required_gap(pairs, status.get("rollup") or (), provenance)
    if gaps:
        return False, ("PR #%s does not prove the required checks of %s on the freeze %s: %s"
                       % (status.get("number"), target or "the target", frozen[:12],
                          ", ".join(gaps)))
    if project is not None and getattr(project, "ci_required", False):
        try:
            merge_queue._required_ci(project, frozen)
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
    if status is not None and status.get("head") \
            and not same_commit(status.get("head"), fire.get("sha"), work_repo):
        return _invalidate(db, row, marks, fire,
                           "the PR head moved to %s, the fire covers %s"
                           % (status["head"][:12], _norm(fire.get("sha"))[:12]),
                           status=status, gh=gh, slug=slug, dry_run=dry_run, handle=handle)

    info = _repo_info(slug, gh=gh, cache=cache)
    target = _target(row, project, work_repo, info)
    queue_mode = project is not None and getattr(project, "land", "off") != "off"

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

    # 5. file de fusion (projet qui déclare `land`) : entrée quand les avis et
    #    la CI sont là ; sinon PR ouverte, CI, puis fusion au geste effectif.
    if queue_mode:
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
    ok, why = ready_pr(status, target=target, project=project, frozen=frozen,
                       slug=slug, gh=gh)
    if not ok:
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
        ok, error = _merge_pr(slug, status, head, _method(project, info), gh=gh)
        if ok:
            out.update(result="merged", detail=error, sha=head)
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
    **déclaré** — `history=False`, comme `pin_authority` (#1374, P1-5) : avoir
    confié des lots récemment ne donne pas le feu."""
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
