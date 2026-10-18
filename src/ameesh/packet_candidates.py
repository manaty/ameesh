# SPDX-License-Identifier: AGPL-3.0-only
"""Lots prêts pour un paquet horaire, calculés et non demandés par courrier.

Constat #2404 (auditeur, 18/10) : des lots aux verdicts complets, gelés sur la
tête publiée de leur branche et sans conflit avec la cible ont attendu un
paquet de 1 h à 20 h, parce que le composeur partait des lots cités dans les
courriers (orchestrateur, pilote, auteurs) au lieu d'interroger l'état des
lots. Une admission perdue en courrier ne laissait alors aucune trace : le lot
n'était ni composé ni « écarté » au manifeste.

Ce module rend la liste **calculée** que la composition doit lire avant tout
courrier :

* état `qa` (aucun filtre d'assigné : un lot assigné est un candidat comme un
  autre) ;
* gel effectif (`plan_git.freeze_candidate`, #1756) sans refus, résolu en
  commit **égal à la tête publiée** de la branche (`origin`, lecture fraîche :
  une tête locale n'est pas la forge) ;
* verdicts requis acquis **sur cette tête**, par la même porte que la fusion
  (`merge_queue.review_gate`, #1374) : comptés par classe, indépendants, sans
  blocage courant ;
* `git merge-tree` propre contre la cible (le commit de base du paquet).

Chaque refus porte un **code** stable (`freeze-not-head`, `verdicts-missing`,
`conflict`, …) et un détail lisible : la composition peut donc écrire une
ligne « écarté » motivée, et un contrôle d'inclusion peut exiger que tout lot
prêt y figure. Aucune écriture : ce module lit la base, les jalons et Git.

Un lot dont la préparation n'a pas pu être lue (branche, cible, classe, porte)
n'est ni prêt ni écarté : il porte `error`, et l'appelant doit refuser de
composer sur une liste incomplète plutôt que de l'omettre en silence.
"""
from __future__ import annotations

import os
import re
import subprocess

from . import lot_class, merge_queue, packet_guard, plan_git, storage, work

SCHEMA = "ameesh-packet-candidates/1"

#: un identifiant de commit complet
_SHA = re.compile(r"[0-9a-f]{40}")


class CandidateError(RuntimeError):
    """La liste ne peut pas être calculée : cible, clone ou lecture Git."""


def _cause(code: str, detail: str = "", **extra) -> dict:
    """Un refus nommé, stable et lisible."""
    out = {"code": code, "detail": detail}
    out.update(extra)
    return out


def merge_tree(repo: str, base: str, tip: str) -> dict:
    """L'essai de fusion de `tip` dans `base` (`git merge-tree --write-tree`).

    Rend `{state: clean|conflict|error, files, detail}`. `conflict` n'est pas
    une erreur de lecture : c'est une exclusion nommée (résolution à préparer,
    ou paquet suivant). Un code de sortie autre que 0/1 est une erreur : on ne
    conclut pas.
    """
    proc = subprocess.run(
        ["git", "-C", repo, "merge-tree", "--write-tree", "--name-only", base, tip],
        capture_output=True,
    )
    text = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace").strip()
    lines = text.splitlines()
    tree = lines[0].strip() if lines else ""
    files: list[str] = []
    if proc.returncode == 1:
        # arbre écrit, puis un nom de fichier par ligne, puis un blanc et les
        # messages de conflit. On ne garde que la liste des fichiers.
        for line in lines[1:]:
            if not line.strip():
                break
            files.append(line)
        return {"state": "conflict", "files": files, "tree": tree, "detail": ""}
    if proc.returncode != 0:
        return {"state": "error", "files": [],
                "detail": (err or "git merge-tree rc=%d" % proc.returncode)[:300]}
    if not _SHA.fullmatch(tree):
        return {"state": "error", "files": [],
                "detail": "git merge-tree rendered no tree: %s" % (err or text[:300])}
    return {"state": "clean", "files": [], "tree": tree, "detail": ""}


def _ancestor(repo: str, tip: str, base: str) -> bool:
    """Le contenu du lot est-il déjà dans la cible (tête ancêtre) ?"""
    proc = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", tip, base],
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        raise CandidateError("git merge-base --is-ancestor rc=%d: %s"
                             % (proc.returncode,
                                proc.stderr.decode("utf-8", "replace").strip()[:300]))
    return proc.returncode == 0


def _row_target(row: dict, target: str) -> str:
    return merge_queue._remote_branch(row.get("branch_target") or target)


def default_app(repo: str) -> str:
    """Le projet d'un dépôt sans `radar.projects` : le nom du dépôt d'origine."""
    try:
        from . import migrations
        origin = migrations.origin_repo(repo)
        if origin:
            return origin.rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 - repli sur le nom du dossier
        pass
    return os.path.basename(os.path.realpath(repo))


def _items(db, app: str) -> list[dict]:
    """Tous les lots `qa` du projet, sans filtre d'assigné ni borne de page.

    Un filtre `assignee` ou une limite d'interface cacherait précisément les
    lots assignés que #2404 a vus omis (le composeur doit voir tous les
    candidats, pas ceux qui restent à prendre).
    """
    rows = storage.of(db).work.items(state="qa", assignee=None, limit=None)
    return [row for row in rows if (row.get("app") or "") == app]


def _marks(db, ids) -> dict:
    """Les jalons de tous les lots en une lecture (`progress.lot_milestones`)."""
    out: dict = {}
    for mark in storage.of(db).progress.lot_milestones(list(ids)):
        out.setdefault(int(mark["work_item_id"]), []).append(mark)
    return out


def inventory(db, project, *, repo: str, clone, target: str | None = None,
              app: str | None = None, base: str | None = None,
              cfg=None) -> dict:
    """La liste calculée des candidats d'un paquet pour `project`.

    `project` porte le nom et la cible déclarée (`radar.Project`) ; `clone` est
    un `resolutions.Replayer` (clone privé du radar ou clone jetable) où les
    branches et la cible sont lues **fraîches** de `origin`. `target` et `app`
    remplacent ceux du projet ; `base` force le commit de base de l'essai de
    fusion (reproduction d'un manifeste) au lieu de la tête fraîche de la
    cible.

    Rend le schéma `ameesh-packet-candidates/1` : `candidates` (tous les lots
    `qa` du projet, prêts ou refusés avec leurs codes), `ready` (ids, ordre de
    priorité) et `errors` (lectures impossibles, à corriger avant de
    composer). Aucune écriture, aucun effet hors du clone `clone`.
    """
    app = (app or getattr(project, "name", "") or default_app(repo)).strip()
    target_branch = merge_queue._remote_branch(
        (target or getattr(project, "target", "") or plan_git.default_target(repo) or "").strip())
    if not target_branch:
        raise CandidateError("no target branch: pass --target or declare the project's target")
    rows = _items(db, app)
    ids = [int(row["id"]) for row in rows]
    marks = _marks(db, ids)
    names = [target_branch]
    names += [str(row.get("branch") or "") for row in rows if row.get("branch")]
    try:
        tips, where = merge_queue.fetch_fresh(repo, names, clone)
    except merge_queue.QueueError as exc:
        raise CandidateError(str(exc)) from exc
    fetched_target = tips.get(target_branch)
    if not fetched_target:
        raise CandidateError("target %s is not on origin: the package base cannot be read"
                             % target_branch)
    if base:
        resolved = plan_git._commit(where, base)
        if not resolved:
            raise CandidateError("base %s is not a commit of %s" % (base, where))
        package_base, base_source = resolved, "option"
    else:
        package_base, base_source = fetched_target, "target"
    canons = work.configured_canons(cfg)
    candidates: list[dict] = []
    errors: list[str] = []
    for row in rows:
        candidates.append(_candidate(
            db, row, target_branch=target_branch, tips=tips, where=where,
            package_base=package_base, marks=marks.get(int(row["id"]), []),
            cfg=cfg, canons=canons))
    for entry in candidates:
        if entry["error"]:
            errors.append("#%d : %s" % (entry["work_item"], entry["error"]))
    by_id = {entry["work_item"]: entry for entry in candidates}
    ready = sorted((entry["work_item"] for entry in candidates if entry["ready"]),
                   key=lambda lot: _ready_order(by_id[lot]))
    return {
        "schema": SCHEMA,
        "project": app,
        "repo": repo,
        "clone": where,
        "target": target_branch,
        "target_head": fetched_target,
        "base": package_base,
        "base_source": base_source,
        "candidates": candidates,
        "ready": ready,
        "waiting": sorted(entry["work_item"] for entry in candidates
                          if not entry["ready"] and not entry["error"]),
        "errors": errors,
    }


def _ready_order(entry: dict) -> tuple:
    """Priorité, puis âge de création, puis id : un ordre stable, jamais un tirage."""
    return (entry.get("priority") if entry.get("priority") is not None else 2,
            entry.get("created_ts") or 0.0, entry["work_item"])


def _candidate(db, row: dict, *, target_branch: str, tips: dict, where: str,
               package_base: str, marks: list[dict], cfg=None,
               canons=None) -> dict:
    """Un lot : tête publiée, gel, classe, porte des avis, essai de fusion."""
    lot = int(row["id"])
    branch = str(row.get("branch") or "").strip()
    target = _row_target(row, target_branch)
    entry: dict = {
        "work_item": lot,
        "title": row.get("title") or "",
        "assignee": row.get("assignee") or "",
        "priority": row.get("priority"),
        "created_ts": row.get("created_ts"),
        "branch": branch,
        "branch_target": row.get("branch_target") or "",
        "target": target,
        "published_head": "",
        "freeze": None,
        "frozen_sha": "",
        "review_class": None,
        "gate": None,
        "merge_tree": None,
        "ready": False,
        "causes": [],
        "error": "",
    }
    if not branch:
        entry["causes"].append(_cause("missing-branch",
                                      "no branch: the lot has nothing to compose"))
        return entry
    if packet_guard.is_packet_branch(branch):
        # Le lot porteur d'un paquet (`integration/**`) est le véhicule, pas
        # un lot à composer dans un autre paquet.
        entry["causes"].append(_cause(
            "packet-vehicle", "branch %s is itself a package, never a unit" % branch))
        return entry
    if target != target_branch:
        entry["causes"].append(_cause(
            "target-mismatch",
            "lot targets %s, package targets %s" % (target, target_branch)))
    tip = tips.get(branch)
    if not tip:
        entry["causes"].append(_cause(
            "branch-not-published",
            "branch %s is not on origin: a local head is not the forge" % branch))
        return entry
    entry["published_head"] = tip
    try:
        freeze = plan_git.freeze_candidate(marks, repo=where, target=package_base)
    except Exception as exc:  # noqa: BLE001 - lecture du gel impossible
        entry["error"] = "cannot read the effective freeze: %s" % exc
        return entry
    entry["freeze"] = freeze
    frozen = merge_queue.pinned(where, freeze.get("sha"))
    entry["frozen_sha"] = frozen or ""
    for cause in freeze.get("causes") or ():
        entry["causes"].append(_cause(cause, "effective freeze refused: %s" % cause))
    if freeze.get("sha") and not frozen:
        entry["causes"].append(_cause(
            "freeze-unresolved",
            "freeze %r is not a commit of %s" % (freeze.get("sha"), where)))
    if frozen and frozen != tip:
        entry["causes"].append(_cause(
            "freeze-not-head",
            "freeze %s is not the published head %s: freeze again, then review"
            % (frozen[:12], tip[:12])))
    try:
        klass = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=where,
                                 base=package_base, head=tip, milestones=marks,
                                 canons=canons)
    except Exception as exc:  # noqa: BLE001 - classification impossible
        entry["error"] = "cannot read the review class: %s" % exc
        return entry
    entry["review_class"] = klass
    if klass.get("class") == lot_class.RELEASE:
        entry["causes"].append(_cause(
            "release",
            "class release: the release procedure, not a package, carries it"))
    try:
        if frozen:
            gate = merge_queue.review_gate(db, marks, frozen, repo=where,
                                           klass=klass, item=row)
            entry["gate"] = gate
            if not gate.get("ok"):
                entry["causes"].append(_cause(
                    "verdicts-missing", gate.get("why") or "required verdicts missing"))
    except Exception as exc:  # noqa: BLE001 - porte illisible
        entry["error"] = "cannot read the review gate: %s" % exc
        return entry
    try:
        merged = merge_tree(where, package_base, tip)
    except Exception as exc:  # noqa: BLE001 - essai de fusion impossible
        entry["error"] = "cannot probe the merge with the target: %s" % exc
        return entry
    entry["merge_tree"] = merged
    if merged["state"] == "error":
        entry["error"] = merged["detail"] or "the merge with the target is unreadable"
        return entry
    if merged["state"] == "conflict":
        entry["causes"].append(_cause(
            "conflict", "conflict with %s: %s"
            % (target_branch, ", ".join(merged["files"]) or "files unreadable"),
            files=merged["files"]))
    try:
        if _ancestor(where, tip, package_base):
            entry["causes"].append(_cause(
                "already-in-target",
                "the frozen head is an ancestor of %s: sync-merges should close it"
                % target_branch))
    except CandidateError as exc:
        entry["error"] = str(exc)
        return entry
    entry["ready"] = not entry["causes"] and not entry["error"]
    return entry
