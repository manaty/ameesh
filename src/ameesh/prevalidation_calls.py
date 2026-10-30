# SPDX-License-Identifier: AGPL-3.0-only
"""#2644 — la prévalidation #1770 appelée par l'outillage.

Le geste existe (`ameesh work prevalidate`, #1770) mais aucun code ne
l'appelait : le paquet h-0500 a été regelé trois fois pour des rouges que la
sélection P0/P1 aurait vus avant la suite complète. Ce module porte les trois
accroches, avec les paramètres **compilés du canon** (`prevalidation` :
classes, contrats, hôtes, délai, lanceur de part) :

* ``gate_packet`` avant l'étiquette ``ci-full-<sha>`` d'un porteur
  ``integration/**`` : **seul un rouge** refuse nommément, avec les tests en
  cause, sans poser d'étiquette. Une prévalidation indisponible (carte
  absente ou trop vieille, VM absente, canon sans bloc, sélection non
  prouvée, run incomplet) ne bloque jamais : le motif est journalisé et la
  demande part (décision pilote 19/10) ;
* ``after_freeze`` au jalon ``frozen`` : le rapport est attaché à la tête
  gelée, le gel n'est jamais refusé ni perdu (il est déjà écrit quand ce
  module s'exécute) ; une indisponibilité est journalisée ; son échéance est
  plafonnée par celle du **gel entier** (#4073), qui couvre aussi les
  contrôles statiques passés avant lui ;
* ``gate_train`` dans le passage du train, avant publication : un rouge
  arrête la publication, une indisponibilité est journalisée et la CI reste
  la preuve finale ; son passage entier est borné sous ``land.LOCK_TTL_S``
  (#2665).

#4136 : un véhicule composé par ``scripts/paquet-auto.py`` porte à sa tête un
manifeste qui consigne ses tests locaux (unités, voisins, contrats) ; ni le
gel ni ``ci run --full`` ne le prévalident une seconde fois
(``composer_tested``). #4304 : paquet-auto pousse et gèle avant ses tests par
unité, qui tournent en parallèle de la CI complète ; son manifeste le dit et
dispense de même (la prévalidation rejouerait les mêmes tests ciblés). Un lot
ordinaire, ou un véhicule composé à la main sans ce manifeste, garde sa
prévalidation.

Les hôtes viennent du bloc canon ; un hôte **local déclaré au canon** est
admis (décision pilote 19/10 06:03, pour que les accroches tournent depuis
`ameesh-agents-1`), un poste local non déclaré reste refusé.

Le plan L161f n'est jamais deviné. Il est calculé par le
``scripts/test-plan.py`` du **commit candidat** (arbre détaché), avec la carte
mesurée fournie par l'appelant quand elle existe, sinon la dernière carte
publiée (artefact ``carte`` de GitHub, même lecture que la restauration
``tests.yml``). Sans carte fraîche, le plan retombe sur la suite complète :
``prevalidation.validate`` refuse ``selection-full`` / ``selection-unsafe``,
et ``contract_fallback`` (#3800) joue alors les contrats déclarés au canon
(``tests/parts/toujours.txt``) plus ``test_cluster`` et ``test_l161f_plan``,
en le disant dans le rapport. Aucun repli local, aucun test supposé. Le
rapport passe par ``prevalidation.record`` (0450), après le
run distant et hors de toute transaction ; une table absente n'annule jamais
un run réel.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace

from . import canon as canon_mod
from . import land, lot_class, mechanics, plan_git, prevalidation, registry, work
from . import transverse_contracts
from .deadline import Deadline

#: le calculateur du plan L161f, lu dans l'arbre du candidat (convention ameesh)
PLAN_SCRIPT = "scripts/test-plan.py"
#: nom du plan écrit dans l'arbre du candidat (relatif, embarqué par l'offload)
PLAN_NAME = "ameesh-prevalidation-plan.json"
#: borne de la génération du plan (lecture du dépôt, pas une suite de tests)
PLAN_TIMEOUT = 900
#: causes pour lesquelles un autre hôte déclaré au canon peut être essayé
RETRYABLE = ("no-vm", "gate-preempted", "transport-lost")
#: issues d'un run qui n'a pas exécuté la sélection : indisponible, pas rouge
TRANSPORT_OUTCOMES = ("no-vm", "gate-preempted", "transport-lost")
#: préfixe des notes du gel (lecture courte)
JOURNAL_PREFIX = "prevalidation"
#: artefact de la mesure carte (#161e) publié par `.github/workflows/carte.yml`
CARTE_WORKFLOW = "carte.yml"
CARTE_ARTIFACT = "carte"
#: #3352 : la carte se lit sur la **cible** (`main`), jamais sur un run de PR.
#: Depuis #3343, `carte.yml` tourne aussi sur `pull_request` ; sans ce filtre,
#: un run vert de PR fournissait un artefact à la tête de PR, non ancêtre de
#: la cible, que `scripts/test-plan.py` refusait ensuite (`card-stale`)
#: jusqu'au prochain run carte de `main`.
CARTE_BRANCH = "main"
#: #2644/N dev1 : la prévalidation du train tourne avant `_held()`, mais le
#: bail de la cible (land.LOCK_TTL_S, 1800 s par défaut) court déjà. Ces deux
#: plafonds bornent le plan et le run ; ils ne composent pas une borne du
#: passage (#2665/N cx1) : `Deadline` les ramène au temps qui reste.
TRAIN_PLAN_TIMEOUT = 300
TRAIN_RUN_TIMEOUT = 1200
#: #2665/N cx1 : marge gardée sous le bail de la cible pour relire et publier
#: après la prévalidation ; proportionnelle quand le bail est réglé plus court.
TRAIN_LOCK_MARGIN = 300.0
#: borne du nettoyage d'un arbre préparé (retrait du worktree git)
TRAIN_CLEANUP_TIMEOUT = 120
#: #4067 : budget entier d'une prévalidation appelée par un gel
#: (``work milestone frozen``) ou une demande de CI (``ci run``), file
#: d'attente de l'hôte comprise. Le compositeur borne ces commandes à 600 s
#: (``FREEZE_TIMEOUT`` de paquet-auto, ``COMMAND_TIMEOUT`` de paquet-horaire) ;
#: sans échéance, l'offload attendait jusqu'au ``timeout_seconds`` du canon
#: (3600 s) et la commande était tuée avant de rendre la main. L'appelant peut
#: la régler (``--prevalidation-budget``) ; épuisée, elle est ``run-incomplete``
#: (« timeout »), indisponible, jamais un rouge.
CALLER_BUDGET_S = 420.0
#: #4073 : budget entier du geste ``work milestone ... frozen`` — contrôles
#: statiques du gel ([`freeze_checks.TIMEOUT`], 300 s) puis prévalidation,
#: sous le même ``FREEZE_TIMEOUT`` de 600 s du compositeur. Les deux étapes
#: partagent cette échéance au lieu d'additionner leurs plafonds (300 + 420) ;
#: la marge de 30 s couvre le démarrage du processus et la sortie.
FREEZE_BUDGET_S = 570.0


def caller_deadline(budget: float | None = None, clock=time.monotonic,
                    limit: Deadline | None = None) -> Deadline:
    """L'échéance d'une prévalidation appelée par un gel ou un ``ci run``.

    ``limit`` (#4073) plafonne le budget par le temps qui reste d'une échéance
    déjà commencée — celle du gel entier, contrôles statiques compris.
    """
    seconds = CALLER_BUDGET_S if budget is None else float(budget)
    if limit is not None:
        seconds = min(seconds, limit.remaining())
    return Deadline(max(0.0, seconds), clock=clock)


def freeze_deadline(clock=time.monotonic) -> Deadline:
    """L'échéance du geste ``work milestone ... frozen`` entier (#4073).

    Elle commence avant les contrôles statiques et se partage entre eux et la
    prévalidation, pour que la commande rende la main sous les 600 s du
    compositeur (``FREEZE_TIMEOUT`` de paquet-auto, ``COMMAND_TIMEOUT`` de
    paquet-horaire).
    """
    return Deadline(FREEZE_BUDGET_S, clock=clock)


def _timeout_error(deadline: Deadline, where: str) -> prevalidation.PrevalidationError:
    """Une échéance de l'appelant épuisée : indisponible, motif ``timeout``."""
    return prevalidation.PrevalidationError("run-incomplete", _timed_out(deadline, where))


def _timed_out(deadline: Deadline | None, why: str) -> str:
    """``why`` préfixé de « timeout » quand l'échéance de l'appelant est épuisée."""
    if deadline is None or not deadline.expired() or str(why).startswith("timeout"):
        return why
    head = "timeout — budget de l'appelant (%d s) épuisé" % round(deadline.at - deadline.started)
    return "%s%s" % (head, (" " + why) if why else "")


def train_deadline(clock=time.monotonic) -> Deadline:
    """Le budget entier d'un passage de prévalidation du train (#2665).

    Le bail de la cible court déjà quand la prévalidation commence ; la marge
    de publication reste sous ``land.LOCK_TTL_S``, y compris quand ce bail est
    réglé plus court que la marge par défaut.
    """
    ttl = max(0.0, float(land.LOCK_TTL_S))
    margin = min(TRAIN_LOCK_MARGIN, ttl / 6.0)
    return Deadline(max(0.0, ttl - margin), clock=clock)


@dataclass(frozen=True)
class Parameters:
    """Les paramètres compilés d'un canon, et d'où ils viennent."""

    params: object
    repo: str
    commit: str
    project: str

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(getattr(self.params, "hosts", ()) or ())


@dataclass
class Prepared:
    """Un arbre détaché au commit candidat, avec le plan généré dedans."""

    repo: str
    root: str
    tree: str
    path: str
    rel: str
    plan: dict
    #: #3954 : la base de fusion (base → tête) que le plan a jugée ; le repli
    #: contractuel relit le même intervalle pour les contrats transverses.
    base: str = ""

    def close(self, timeout: float = TRAIN_CLEANUP_TIMEOUT) -> None:
        """Retire le worktree ; ``timeout`` est le temps qui reste au passage."""
        try:
            subprocess.run(["git", "-C", self.repo, "worktree", "remove", "--force",
                            self.tree], capture_output=True, text=True,
                           timeout=max(0.0, float(timeout)))
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(self.root, ignore_errors=True)


# --------------------------------------------------------------------------
# les paramètres : le canon relu, jamais une valeur par défaut
# --------------------------------------------------------------------------

def parameters(cfg, project: str, *, canon_repo: str = "",
               canon_commit: str = "",
               deadline: Deadline | None = None) -> Parameters:
    """Les paramètres de prévalidation du projet, au commit exact du canon.

    Un ``canon_repo``/``canon_commit`` explicite est lu tel quel (geste de
    reprise, banc) ; sinon le premier canon configuré qui porte un bloc
    ``prevalidation`` pour ce projet est retenu. Aucun fallback : un canon
    sans bloc, illisible ou sans commit exact refuse ``block-missing``.
    Avec ``deadline`` (#2665), la découverte des canons configurés
    (``canon.load_configured`` : résolution git, manifeste, fiches) et la
    lecture des paramètres ne prennent que le temps qui reste au passage ;
    une échéance épuisée est ``run-incomplete``, pas un ``block-missing``
    trompeur.
    """
    if canon_repo and canon_commit:
        _, params = prevalidation.load_parameters(canon_repo, canon_commit, project,
                                                  deadline=deadline)
        return Parameters(params=params, repo=canon_repo, commit=canon_commit,
                          project=project)
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        raise prevalidation.PrevalidationError(
            "block-missing", "aucun canon configuré : paramètres de prévalidation inconnus")
    try:
        canons = canon_mod.load_configured(cfg, deadline=deadline)
    except canon_mod.GitDeadline:
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé pendant la découverte des canons") from None
    except Exception as exc:  # noqa: BLE001 - un canon illisible ne prouve rien
        raise prevalidation.PrevalidationError(
            "block-missing", "canon configuré illisible: %s: %s"
            % (type(exc).__name__, str(exc)[:200])) from None
    if not canons:
        raise prevalidation.PrevalidationError(
            "block-missing", "aucun canon lisible configuré")
    errors: list[str] = []
    for canon in canons:
        for source in getattr(canon, "sources", ()) or ():
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete",
                    "budget de prévalidation épuisé pendant la lecture du canon")
            if getattr(source, "mode", "") != "git" or not source.repo or not source.commit:
                errors.append("%s: source de canon sans commit exact"
                              % (getattr(canon, "root", "") or "canon"))
                continue
            if getattr(source, "prefix", ""):
                errors.append("%s: canon sous un sous-répertoire" % source.repo)
                continue
            try:
                _, params = prevalidation.load_parameters(source.repo, source.commit,
                                                          project, deadline=deadline)
            except prevalidation.PrevalidationError as exc:
                if exc.cause == "run-incomplete":
                    raise
                errors.append("%s@%s: %s" % (source.repo, str(source.commit)[:12], exc))
                continue
            except mechanics.MechanicsError as exc:
                if deadline is not None and deadline.expired():
                    raise prevalidation.PrevalidationError(
                        "run-incomplete",
                        "budget de prévalidation épuisé pendant la lecture du canon") from None
                errors.append("%s@%s: %s" % (source.repo, str(source.commit)[:12],
                                             str(exc)[:200]))
                continue
            return Parameters(params=params, repo=source.repo,
                              commit=source.commit, project=project)
    raise prevalidation.PrevalidationError(
        "block-missing", "; ".join(errors[:4]) or "aucun canon ne porte de bloc prevalidation")


def repository(cfg, db, item, head: str, *, explicit: str = "") -> str:
    """Le dépôt qui porte la tête candidate, ou ``""``.

    L'arbre explicite d'abord, puis le répertoire courant (le composeur est
    dans l'arbre du paquet), le worktree de l'assigné (registre) et le dépôt du
    projet. Un doute ne devine pas : un chemin qui ne porte pas le commit n'est
    pas retenu.
    """
    wanted = (head or "").strip().lower()
    if not wanted:
        return ""
    candidates: list[str] = [explicit, os.getcwd()]
    assignee = str((item or {}).get("assignee") or "").strip()
    if db is not None and assignee:
        try:
            row = registry.get(db, assignee) or {}
        except Exception:  # noqa: BLE001 - registre illisible : indice suivant
            row = {}
        candidates.append(str(row.get("cwd") or ""))
    try:
        candidates.append(lot_class.repository(cfg, dict(item or {})) or "")
    except Exception:  # noqa: BLE001 - pas de radar : indice suivant
        candidates.append("")
    seen: set[str] = set()
    for path in candidates:
        path = os.path.expanduser(str(path or "").strip())
        if not path or not os.path.isdir(path):
            continue
        top = canon_mod.git_toplevel(path)
        if not top or top in seen:
            continue
        seen.add(top)
        if plan_git._commit(top, wanted):
            return top
    return ""


def map_for(repo_slug: str, repo: str = "", *, explicit: str = "",
            runner=subprocess.run, deadline: Deadline | None = None) -> tuple[str, str]:
    """La carte mesurée à passer au plan : explicite, locale, ou artefact carte.

    Rend ``(chemin, répertoire à nettoyer)`` ; ``("", "")`` quand aucune carte
    n'est disponible (le plan retombera sur la suite complète, et la
    prévalidation refusera). Un artefact de la dernière exécution carte verte
    de ``CARTE_BRANCH`` est téléchargé dans un répertoire temporaire : la
    fraîcheur et l'ascendance de son commit restent vérifiées par
    ``scripts/test-plan.py``, jamais supposées ici. Le filtre ``--branch``
    exclut les runs de PR (#3352). Avec ``deadline``, les deux appels réseau
    ne prennent que le temps qui reste au passage (#2665).
    """
    text = (explicit or "").strip()
    if text:
        return text, ""
    if repo:
        local = os.path.join(repo, "carte", "carte.json.gz")
        if os.path.isfile(local):
            return local, ""
    slug = (repo_slug or "").strip()
    if not slug:
        return "", ""
    if deadline is not None and deadline.expired():
        return "", ""
    try:
        listed = runner(["gh", "run", "list", "--repo", slug, "--workflow",
                         CARTE_WORKFLOW, "--branch", CARTE_BRANCH,
                         "--status", "success", "--limit", "1",
                         "--json", "databaseId"], capture_output=True, text=True,
                        timeout=deadline.timeout(30) if deadline is not None else 30)
        if listed.returncode:
            return "", ""
        runs = json.loads(listed.stdout or "[]")
        ident = runs[0].get("databaseId") if runs else None
        if not ident:
            return "", ""
        if deadline is not None and deadline.expired():
            return "", ""
        tmp = tempfile.mkdtemp(prefix="ameesh-carte-")
        got = runner(["gh", "run", "download", str(ident), "--repo", slug, "-n",
                      CARTE_ARTIFACT, "-D", tmp], capture_output=True, text=True,
                     timeout=deadline.timeout(120) if deadline is not None else 120)
        path = os.path.join(tmp, "carte.json.gz")
        if got.returncode or not os.path.isfile(path):
            shutil.rmtree(tmp, ignore_errors=True)
            return "", ""
        return path, tmp
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return "", ""


def target_base(repo: str, item) -> str:
    """La base de comparaison du lot : la cible, le distant d'abord."""
    target = str((item or {}).get("branch_target") or "main").strip() or "main"
    for candidate in (plan_git._commit(repo, "origin/" + target),
                      plan_git._commit(repo, target)):
        if candidate:
            return candidate
    return ""


# --------------------------------------------------------------------------
# l'arbre candidat et son plan : le script du SHA, jamais celui du disque
# --------------------------------------------------------------------------

def _run_git(repo: str, *args: str, timeout: int = 120):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=timeout)


def tooling(repo: str, head: str, params: Parameters, *,
            deadline: Deadline | None = None) -> None:
    """#2794 : le plan et le lanceur de part existent au SHA candidat.

    Le bloc ``prevalidation`` global du canon vaut pour tous les projets, mais
    ``scripts/test-plan.py`` et le lanceur de part sont des outils du dépôt
    ameesh : un dépôt qui ne les porte pas (nexlink) n'est pas prévalidable.
    Le refus est nommé ``tooling-missing`` avant tout arbre, carte ou hôte,
    au lieu d'un ``can't open file`` de Python lu comme ``selection-unsafe``.
    Une tête qui n'est pas un commit est laissée au refus de ``prepare``.
    """
    full = str(head or "").strip().lower()
    launcher = str(getattr(params.params, "parts_script", None)
                   or prevalidation.DEFAULT_PARTS_SCRIPT)
    git = ["git", "-C", repo, *prevalidation.NO_REPLACE]
    absent = []
    limit = deadline.timeout if deadline is not None else (lambda cap: cap)
    try:
        if deadline is not None and deadline.expired():
            raise prevalidation.PrevalidationError(
                "run-incomplete", "budget de prévalidation épuisé avant l'outillage")
        if not full or subprocess.run(
                [*git, "rev-parse", "--verify", "--quiet", full + "^{commit}"],
                capture_output=True, text=True, timeout=limit(60)).returncode:
            return
        for path in (PLAN_SCRIPT, launcher):
            if subprocess.run([*git, "cat-file", "-e", "%s:%s" % (full, path)],
                              capture_output=True, text=True,
                              timeout=limit(60)).returncode:
                absent.append(path)
    except (OSError, subprocess.SubprocessError):
        return
    if absent:
        raise prevalidation.PrevalidationError(
            "tooling-missing", "%s absent de %s au SHA %s : le projet %s n'outille pas "
            "la prévalidation (plan L161f et lanceur de part d'ameesh)"
            % (", ".join(absent), repo, full[:12], params.project or "?"))


def _free_name(repo: str, head: str, *, deadline: Deadline | None = None) -> str:
    """Un nom de plan qui ne remplace aucun chemin du SHA candidat."""
    for index in range(0, 20):
        if deadline is not None and deadline.expired():
            raise prevalidation.PrevalidationError(
                "run-incomplete", "budget de prévalidation épuisé avant le plan")
        name = PLAN_NAME if index == 0 else "ameesh-prevalidation-plan-%d.json" % index
        wait = deadline.timeout(120) if deadline is not None else 120
        proc = _run_git(repo, "cat-file", "-e", "%s:%s" % (head, name), timeout=wait)
        if proc.returncode != 0:
            return name
    raise prevalidation.PrevalidationError(
        "selection-unsafe", "aucun nom libre pour le plan dans l'arbre du candidat")


def _merge_base(repo: str, base: str, head: str, *, timeout: int = 120) -> str:
    """Le merge-base de la base de comparaison et de la tête, ou ``""`` (#3800).

    Le plan L161f exige la base de fusion d'un lot, pas le tip de la cible :
    comparer le tip à la tête fait passer les changements de la cible depuis
    le point de branche pour des changements du lot (en sens inverse), et une
    carte en retard rend alors toute branche « non sûre » (fondation, données,
    scripts hors socle).
    """
    proc = _run_git(repo, "merge-base", base, head, timeout=timeout)
    if proc.returncode:
        return ""
    out = (proc.stdout or "").strip().split()
    return out[0] if out else ""


def prepare(repo: str, head: str, base: str, *, map_path: str = "",
            timeout: int = PLAN_TIMEOUT, deadline: Deadline | None = None) -> Prepared:
    """Construit un arbre détaché de ``head`` et y calcule le plan L161f.

    Le script ``scripts/test-plan.py`` lu est celui du commit candidat ; la
    carte mesurée est passée en lecture seule à ``--map`` quand l'appelant en
    fournit une. L'arbre reste vivant jusqu'à ``Prepared.close()`` : le plan
    doit rester dans l'arbre pour être embarqué dans la preuve. Avec
    ``deadline`` (#2665), aucun appel git ni le plan ne dépasse le temps qui
    reste au passage.
    """
    limit = deadline.timeout if deadline is not None else (lambda cap: cap)

    def commit_probe(rev: str) -> str | None:
        """La sonde de présence d'un commit, bornée au temps qui reste (#2665).

        La sonde garde ``NO_LAZY_FETCH`` (c'est ``plan_git._commit`` qui la
        porte) et ne repart pas de ``GIT_TIMEOUT`` : une échéance épuisée est
        ``run-incomplete``, jamais un ``selection-unsafe`` trompeur.
        """
        if deadline is not None and deadline.expired():
            raise prevalidation.PrevalidationError(
                "run-incomplete",
                "budget de prévalidation épuisé avant la sonde du commit")
        try:
            return plan_git._commit(repo, rev, timeout=limit(canon_mod.GIT_TIMEOUT))
        except canon_mod.GitError:
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete",
                    "budget de prévalidation épuisé pendant la sonde du commit") from None
            raise

    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete", "budget de prévalidation épuisé avant l'arbre du SHA")
    full = str(head or "").strip().lower()
    if not commit_probe(full):
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "la tête %s n'est pas un commit de %s"
            % ((full or "?")[:12], repo))
    base = str(base or "").strip().lower()
    if not base or not commit_probe(base):
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "base de comparaison inconnue de %s: %r"
            % (repo, (base or "—")[:12]))
    #: #3800 : le plan juge le diff propre du lot, donc `--base` est la base de
    #: fusion avec la tête, jamais le tip de la cible. Le tip sert encore à
    #: nommer la cible dans le rapport ; il ne juge plus le diff.
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete", "budget de prévalidation épuisé avant le merge-base")
    try:
        comparison = _merge_base(repo, base, full,
                                 timeout=limit(canon_mod.GIT_TIMEOUT))
    except (OSError, subprocess.SubprocessError) as exc:
        if deadline is not None and deadline.expired():
            raise prevalidation.PrevalidationError(
                "run-incomplete",
                "budget de prévalidation épuisé pendant le merge-base") from None
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "merge-base illisible: %s: %s"
            % (type(exc).__name__, str(exc)[:200])) from None
    if not comparison:
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "base de fusion inconnue de %s entre %s et %s"
            % (repo, base[:12], full[:12]))
    root = tempfile.mkdtemp(prefix="ameesh-prevalidation-")
    tree = os.path.join(root, "tree")
    try:
        proc = _run_git(repo, "worktree", "add", "--detach", tree, full,
                        timeout=limit(120))
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        if deadline is not None and deadline.expired():
            raise prevalidation.PrevalidationError(
                "run-incomplete",
                "budget de prévalidation épuisé pendant l'arbre du SHA") from None
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "arbre du SHA non constructible: %s: %s"
            % (type(exc).__name__, str(exc)[:300])) from None
    if proc.returncode:
        shutil.rmtree(root, ignore_errors=True)
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "arbre du SHA non constructible: %s"
            % ((proc.stderr or proc.stdout or "").strip()[-300:] or "git worktree add"))
    try:
        try:
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete", "budget de prévalidation épuisé avant le plan")
            rel = _free_name(tree, full, deadline=deadline)
        except (OSError, subprocess.SubprocessError) as exc:
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete",
                    "budget de prévalidation épuisé pendant le nom du plan") from None
            raise prevalidation.PrevalidationError(
                "selection-unsafe", "nom du plan non vérifiable: %s: %s"
                % (type(exc).__name__, str(exc)[:200])) from None
        plan_path = os.path.join(tree, rel)
        argv = [sys.executable, os.path.join(tree, "scripts", "test-plan.py"), "plan",
                "--out", rel, "--event", "workflow_dispatch", "--ref", "",
                "--base", comparison, "--mode", "lot"]
        if map_path:
            argv += ["--map", os.path.abspath(map_path)]
        try:
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete", "budget de prévalidation épuisé avant le plan")
            generated = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                                       timeout=limit(timeout))
        except (OSError, subprocess.SubprocessError) as exc:
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete",
                    "budget de prévalidation épuisé pendant le plan L161f") from None
            raise prevalidation.PrevalidationError(
                "selection-unsafe", "plan L161f non calculable: %s: %s"
                % (type(exc).__name__, str(exc)[:300])) from None
        if generated.returncode:
            tail = ((generated.stderr or "") + (generated.stdout or "")).strip()[-500:]
            raise prevalidation.PrevalidationError(
                "selection-unsafe", "scripts/test-plan.py a refusé le plan: %s"
                % (tail or "exit %d" % generated.returncode))
        try:
            with open(plan_path, "r", encoding="utf-8") as fh:
                plan = json.load(fh)
        except (OSError, ValueError) as exc:
            raise prevalidation.PrevalidationError(
                "selection-unsafe", "plan L161f illisible: %s" % exc) from None
        if not isinstance(plan, dict):
            raise prevalidation.PrevalidationError(
                "selection-unsafe", "plan L161f: objet JSON attendu")
        return Prepared(repo=repo, root=root, tree=tree, path=plan_path,
                        rel=rel, plan=plan, base=comparison)
    except BaseException:
        try:
            _run_git(repo, "worktree", "remove", "--force", tree,
                     timeout=limit(120))
        except Exception:  # noqa: BLE001 - nettoyage au mieux
            pass
        shutil.rmtree(root, ignore_errors=True)
        raise


# --------------------------------------------------------------------------
# #3800 : le repli contractuel quand la sélection n'est pas prouvée
# --------------------------------------------------------------------------

#: contrats lus quand le canon n'en déclare aucun (convention ameesh)
FALLBACK_CONTRACTS = ("tests/parts/toujours.txt",)
#: modules ajoutés au repli même s'ils ne sont pas dans le fichier de contrat :
#: le lecteur des contrats de la porte de fusion et le rôle superviseur.
FALLBACK_MODULES = ("test_cluster", "test_l161f_plan")
#: refus de sélection qui déclenchent le repli (une tête déplacée, un plan
#: illisible ou une panne d'hôte n'en sont pas : ils ne se prouvent pas par des
#: contrats)
FALLBACK_CAUSES = ("selection-full", "selection-unsafe", "card-missing", "card-stale")


def _changed_paths(prepared: Prepared, base: str, head: str, *,
                   deadline: Deadline | None = None) -> list[tuple[str, str]] | None:
    """Les fichiers de l'intervalle base de fusion → tête, pour #3954.

    Le repli contractuel doit voir les mêmes changements que le plan : la
    base de fusion, pas le tip de la cible. Une lecture impossible rend
    ``None`` : le repli ne peut pas conclure qu'aucun contrat transverse
    n'est dû (B/P2 cx3 de #3954), il joue alors tous les contrats transverses
    déclarés, ou refuse plutôt que d'écrire un vert incomplet.
    """
    comparison = str(getattr(prepared, "base", "") or "").strip() \
        or str(base or "").strip()
    if not comparison:
        comparison = _merge_base(prepared.repo, base or "origin/main", head)
    if not comparison:
        return None
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé avant la lecture des changements")
    bounded = prevalidation._bounded_runner(subprocess.run, deadline)
    try:
        proc = bounded(["git", "-C", prepared.repo, *prevalidation.NO_REPLACE,
                        "diff", "--name-status", "--no-renames",
                        comparison, head], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1):
        return None
    out: list[tuple[str, str]] = []
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        status, _, path = line.partition("\t")
        if path:
            out.append((status[:1], path))
    return out


def _tree_modules(prepared: Prepared, head: str, *,
                  deadline: Deadline | None = None) -> set[str] | None:
    """Les modules de ``tests/`` présents à la tête, ou ``None`` si illisible.

    La déclaration des contrats transverses est vérifiée sur l'arbre du
    candidat, comme les contrats déclarés : un module disparu rend le repli
    incertain (B/P2 cx3 de #3954), jamais silencieusement sauté.
    """
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé avant la lecture des tests")
    bounded = prevalidation._bounded_runner(subprocess.run, deadline)
    try:
        proc = bounded(["git", "-C", prepared.repo, *prevalidation.NO_REPLACE,
                        "ls-tree", "--name-only", "-z", head, "tests/"],
                       capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1):
        return None
    out: set[str] = set()
    for name in (getattr(proc, "stdout", "") or "").split("\0"):
        if name.startswith("tests/test") and name.endswith(".py"):
            out.add(os.path.basename(name)[:-3])
    return out


def contract_fallback(prepared: Prepared, params, *, head: str, base: str,
                      deadline: Deadline | None = None) -> dict | None:
    """#3800 : sur sélection incertaine, la prévalidation joue les contrats.

    Le plan d'un lot peut retomber sur la suite complète (carte en retard,
    fichier de la cible hors socle, sélection non sûre). Refuser alors sans
    rien exécuter laissait passer des rouges de contrat déterministes jusqu'à
    la CI du paquet. Ce repli remplace le refus par un **plan contractuel**
    explicite : les modules de ``params.contracts`` (``tests/parts/toujours.txt``
    par convention), plus ``test_cluster`` et ``test_l161f_plan``, en une part
    psycopg. #3954 y ajoute les **contrats transverses** que les fichiers du
    lot rendent dus (``transverse_contracts``) et les tests du lot lui-même
    (P0 du plan) : un test d'identité changé et un contrat de l'exécuteur ne
    dépendent pas de la mesure par blocs pour être dus. Le plan écrit dans
    l'arbre porte ``fallback`` : le rapport dit la cause d'origine et les
    modules joués, il ne se lit pas comme un vert de sélection. `validate`
    retire ensuite de ce plan les tests que le guest ne peut pas exécuter
    (#4071, `GUEST_EXCLUSIONS`) ; la suite complète les garde.

    Rend le bloc ``fallback`` quand le repli a été écrit, ``None`` sinon (le
    refus d'origine reste alors la réponse). Un contrat déclaré illisible ne
    se suppose pas : pas de repli, le refus est nommé par l'appelant.
    """
    plan = getattr(prepared, "plan", None)
    path = str(getattr(prepared, "path", "") or "")
    tree = str(getattr(prepared, "tree", "") or "")
    if not isinstance(plan, dict) or not path or not tree:
        return None
    chosen = prevalidation.selection(plan, head=head, base=base)
    #: une tête déplacée n'est pas une sélection incertaine : le plan ne parle
    #: pas de la tête candidate, aucun contrat ne la prouve.
    if chosen.ok or "head-moved" in chosen.causes \
            or not (set(chosen.causes) & set(FALLBACK_CAUSES)):
        return None
    block = getattr(params, "params", params)
    declared = tuple(getattr(block, "contracts", ()) or ())
    #: #3800/N cx2 B1 : `gate_train` lit les contrats sous le bail de la cible.
    #: La lecture git doit être bornée par la même échéance que le reste du
    #: passage (`validate` borne déjà `contract_modules` ainsi) ; un
    #: `git cat-file` suspendu ne doit pas dépasser le budget global.
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé avant la lecture des contrats")
    bounded = prevalidation._bounded_runner(subprocess.run, deadline)
    required, problem = prevalidation.contract_modules(
        declared or FALLBACK_CONTRACTS, repo=prepared.tree, head=head,
        runner=bounded)
    if problem:
        return None
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé pendant la lecture des contrats")
    #: #3954 : la déclaration des contrats transverses est relue dans l'arbre
    #: du candidat ; un module déclaré disparu rend le repli incertain (B/P2
    #: cx3), jamais silencieusement sauté.
    tree_modules = _tree_modules(prepared, head, deadline=deadline)
    if tree_modules is None:
        return None
    if transverse_contracts.problem(tree_modules):
        return None
    #: #3954 : les contrats transverses dus par les fichiers du lot, et les
    #: tests que le lot change lui-même (P0 du plan) — ils sont dus par le
    #: diff, pas par la carte, et un repli « contrats seuls » les sautait.
    #: Le plan du candidat peut déjà les nommer (`transverse`) : ils comptent
    #: même si le diff relu ci-dessous est illisible.
    known = plan.get("transverse") if isinstance(plan.get("transverse"), dict) else {}
    changes = _changed_paths(prepared, base, head, deadline=deadline)
    if changes is None:
        #: diff illisible : on ne peut pas conclure qu'aucun contrat
        #: transverse n'est dû. Les jouer tous est le seul repli conservateur
        #: (le contraire sauterait test_l107 sur un lot d'interface).
        transverse = {str(name): "diff illisible : contrat transverse joué"
                      for name in transverse_contracts.CONTRACTS}
        diff_read = False
    else:
        transverse = dict(transverse_contracts.hits(changes))
        diff_read = True
    for name, why in known.items():
        transverse.setdefault(str(name), str(why) or "plan L161f")
    #: Le plan peut déjà classer un contrat transverse en P0/P1 sans porter le
    #: champ `transverse` (sélecteur antérieur) : sa classe est une preuve
    #: suffisante, et le repli ne doit pas la perdre.
    for name, entry in (plan.get("classes") or {}).items():
        if str(name) not in transverse_contracts.CONTRACTS:
            continue
        if isinstance(entry, (list, tuple)) and entry \
                and str(entry[0]) in ("P0", "P1"):
            transverse.setdefault(str(name), "classe %s du plan" % entry[0])
    changed_tests = [str(name) for name, entry in (plan.get("classes") or {}).items()
                     if isinstance(entry, (list, tuple)) and entry
                     and str(entry[0]) == "P0"]
    modules: list[str] = []
    for name in (*required, *FALLBACK_MODULES, *sorted(transverse),
                 *sorted(changed_tests)):
        if name not in modules:
            modules.append(name)
    if not modules:
        return None
    classes = tuple(getattr(block, "classes", ()) or ())
    rank = classes[0] if classes else "P1"
    extra = bool(transverse or changed_tests)
    fallback = {
        "cause": chosen.causes[0],
        "causes": list(chosen.causes),
        "why": chosen.why,
        "contracts": list(required),
        "transverse": sorted(transverse),
        "changed_tests": sorted(changed_tests),
        "transverse_all": not diff_read,
        "diff": "read" if diff_read else "unreadable",
        "modules": list(modules),
    }
    plan = json.loads(json.dumps(plan))
    plan["head"] = head
    plan["fallback"] = fallback
    plan["selection"] = {
        "mode": "contract",
        "why": (("sélection incertaine (%s): contrats et tests du lot" if extra
                 else "sélection incertaine (%s): contrats seuls")
                % chosen.causes[0]),
        "modules": len(modules), "impacted": len(modules),
        "contract": len(required), "selected": len(modules), "skipped": 0,
        "impacted_estimate_s": 0.0, "total_estimate_s": 0.0,
    }
    #: `meets_canon` exige que les contrats déclarés soient dans la sélection
    #: et que les classes retenues soient exactement les modules joués.
    plan["contract"] = list(required)
    plan["classes"] = {name: [rank, "contrat (#3800)"] for name in modules}
    plan["parts"] = {
        "psycopg": [{"part": 1, "of": 1, "label": "psycopg contrats",
                     "modules": list(modules), "estimate_s": 0.0}],
        "psql": [],
    }
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé avant l'écriture du plan de contrats")
    try:
        with open(prepared.path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
    except OSError:
        return None
    prepared.plan = plan
    return fallback


# --------------------------------------------------------------------------
# le run et son rapport
# --------------------------------------------------------------------------

def report_state(report: dict) -> str:
    """`green`, `red` ou `unavailable` : seul `run-red` est un rouge.

    Un run incomplet (timeout, part sans fin, module muet), une préemption de
    la porte ou un transport perdu ne disent pas qu'un test a échoué : selon
    la décision pilote du 19/10, ils sont indisponibles et ne bloquent pas.
    """
    if report.get("ok"):
        return "green"
    causes = set(report.get("causes") or ())
    if "run-red" in causes or report.get("outcome") == "run-red":
        return "red"
    return "unavailable"


def describe(report: dict) -> str:
    """Le refus nommé d'un rapport : cause, tests en échec, modules muets."""
    bits: list[str] = []
    causes = [str(c) for c in (report.get("causes") or ())]
    if causes:
        bits.append(", ".join(causes))
    failed = [str(t) for t in (report.get("failed") or ())]
    if failed:
        bits.append("tests en échec: " + ", ".join(failed[:6])
                    + (" (+%d)" % (len(failed) - 6) if len(failed) > 6 else ""))
    missing = [str(m) for m in (report.get("missing") or ())]
    if missing:
        bits.append("modules non exécutés: " + ", ".join(missing[:4])
                    + (" (+%d)" % (len(missing) - 4) if len(missing) > 4 else ""))
    detail = [str(d) for d in (report.get("detail") or ())]
    if detail:
        bits.append("; ".join(detail[:3]))
    return " — ".join(bit for bit in bits if bit) or str(report.get("outcome") or "?")


def run(cfg, db, lot: int, *, repo: str, head: str, base: str, project: str,
        branch: str = "", map_path: str = "", repo_slug: str = "", profile: str = "",
        record: bool = True, say=None, warn=None, executable: str = "ameesh",
        validate=None, deadline: Deadline | None = None) -> dict:
    """Le geste complet : canon, plan du candidat, run sur les hôtes, rapport.

    Les hôtes du canon sont essayés dans l'ordre quand l'indisponibilité est
    de transport (`no-vm`, porte de l'hôte, connexion perdue), jamais après un
    rouge : un rouge est un résultat, pas une raison de changer de VM. Le
    rapport est persisté après le run, hors de toute transaction ; l'échec de
    cette écriture est signalé, jamais transformé en refus (le run réel reste
    la preuve).

    `profile` nomme le profil d'offload du guest ; sans lui, le profil du
    **projet** s'applique (convention L159b : `ameesh` porte le venv et la
    base jetable). Un automatisme qui ne le nommait pas jouait le profil
    `default`, sans base : les contrats tombaient en `setUpClass` et la
    prévalidation rendait un faux rouge (#4008).

    Avec ``deadline`` (#4067), le passage entier — canon, carte, plan, run
    sur l'hôte (attente de la file comprise), nettoyage — lit la même
    échéance : le job distant est annulé à l'échéance (``--wait-timeout``) et
    le geste rend ``run-incomplete`` (« timeout ») dans le délai de l'appelant.
    """
    profile = (profile or project or "").strip()
    params = parameters(cfg, project, deadline=deadline)
    if deadline is not None:
        cap = int(getattr(params.params, "timeout_seconds", 0) or 0)
        governed = int(max(1.0, min(cap or deadline.remaining(), deadline.remaining())))
        params = Parameters(params=replace(params.params, timeout_seconds=governed),
                            repo=params.repo, commit=params.commit,
                            project=params.project)
    if not repo:
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "aucun arbre ne porte la tête %s" % (head or "?")[:12])
    #: décision pilote 19/10 06:03 : un hôte local **déclaré au canon** est
    #: admis (agents-1), un poste non déclaré reste refusé. Refuser tôt évite
    #: de calculer un plan et de télécharger une carte pour un run qui ne peut
    #: pas partir.
    declared = params.hosts
    if not any(prevalidation.host_allowed(host, declared=declared)[0]
               for host in declared):
        raise prevalidation.PrevalidationError(
            "no-vm", "aucun hôte de prévalidation autorisé (%s)"
            % ", ".join(declared))
    tooling(repo, head, params, deadline=deadline)
    carte, cleanup = map_for(repo_slug, repo, explicit=map_path, deadline=deadline)
    prepared = None
    try:
        if deadline is not None and deadline.expired():
            raise _timeout_error(deadline, "avant l'arbre du SHA")
        prepared = prepare(repo, head, base, map_path=carte, deadline=deadline)
        fallback = contract_fallback(prepared, params, head=head, base=base,
                                     deadline=deadline)
        if deadline is not None and deadline.expired():
            raise _timeout_error(deadline, "avant l'hôte")
        report = _run_hosts(prepared, params, lot=lot, head=head, base=base,
                            branch=branch, profile=profile, say=say,
                            executable=executable, validate=validate,
                            deadline=deadline)
        if fallback:
            #: #4071 : le plan de repli est filtré par `validate` avant le run ;
            #: quand le rapport ne porte pas déjà son bloc (validate simulé),
            #: il dit au moins les modules réellement planifiés et les exclus.
            played = dict(fallback)
            if isinstance(report.get("planned"), list):
                played["modules"] = list(report["planned"])
            if "excluded" in report:
                played["excluded"] = list(report.get("excluded") or ())
            report.setdefault("fallback", played)
            report.setdefault("selection", "contract")
    finally:
        if prepared is not None:
            prepared.close(timeout=TRAIN_CLEANUP_TIMEOUT if deadline is None
                           else max(1.0, deadline.timeout(TRAIN_CLEANUP_TIMEOUT)))
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    if record and db is not None \
            and str(report.get("outcome") or "") not in TRANSPORT_OUTCOMES:
        try:
            prevalidation.record(db, report)
        except Exception as exc:  # noqa: BLE001 - 0450 absente ou base en défaut
            if warn is not None:
                warn("prévalidation : rapport non persisté — %s" % exc)
    return report


def _run_hosts(prepared: Prepared, params: Parameters, *, lot: int, head: str,
               base: str, branch: str, profile: str, say, executable: str,
               validate, deadline: Deadline | None = None) -> dict:
    """Les hôtes déclarés, dans l'ordre, sans repli local ni après un rouge.

    Avec ``deadline`` (#2665), chaque tentative ne reçoit que le temps qui
    reste au passage ; une échéance épuisée arrête les replis multi-hôtes et
    se dit ``run-incomplete``, jamais un rouge.
    """
    check = validate or prevalidation.validate
    hosts = params.hosts
    if not hosts:
        raise prevalidation.PrevalidationError(
            "block-missing", "le bloc prevalidation du canon ne déclare aucun hôte")
    last: prevalidation.PrevalidationError | None = None
    for index, host in enumerate(hosts):
        last = None
        attempt = params
        if deadline is not None:
            if deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete",
                    "budget de prévalidation épuisé avant l'hôte %s" % host)
            cap = float(getattr(params.params, "timeout_seconds", 0) or 0)
            remaining = deadline.remaining()
            governed = int(max(1.0, min(cap or remaining, remaining)))
            attempt = Parameters(
                params=replace(params.params, timeout_seconds=governed),
                repo=params.repo, commit=params.commit, project=params.project)
        try:
            report = check(
                lot=lot, plan_path=prepared.path, head=head, host=host,
                params=attempt.params, base=base, repo=prepared.tree,
                published_ref=branch, profile=profile, executable=executable,
                deadline=deadline)
        except prevalidation.PrevalidationError as exc:
            if exc.cause in RETRYABLE and index + 1 < len(hosts):
                if deadline is not None and deadline.expired():
                    raise prevalidation.PrevalidationError(
                        "run-incomplete",
                        "budget de prévalidation épuisé après l'hôte %s: %s"
                        % (host, exc.why or exc.cause)) from exc
                last = exc
                continue
            raise
        if not report.get("ok") and set(report.get("causes") or ()) & set(RETRYABLE) \
                and index + 1 < len(hosts):
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete",
                    "budget de prévalidation épuisé après l'hôte %s" % host)
            last = prevalidation.PrevalidationError(
                (report.get("causes") or ("no-vm",))[0], describe(report))
            continue
        return report
    if last is not None:
        raise last
    raise prevalidation.PrevalidationError("no-vm", "aucun hôte de prévalidation disponible")


# --------------------------------------------------------------------------
# (a) la porte du paquet, avant l'étiquette ci-full-<sha>
# --------------------------------------------------------------------------

#: #4136 : le manifeste d'un véhicule composé par paquet-auto (sans agent)
COMPOSER_MARK = "Composeur : `scripts/paquet-auto.py`"
#: #4136 : la ligne de ses tests locaux (`_tests_summary` de paquet-auto)
COMPOSER_TESTS_RE = re.compile(
    r"^- Tests locaux : .*?(\d+) tests exécutés, (\d+) échec\(s\)", re.MULTILINE)
#: #4304 : la ligne d'un manifeste poussé avant les tests par unité
#: (`TESTS_PARALLEL_LINE` de paquet-auto) ; ses tests tournent pendant la CI.
COMPOSER_PARALLEL_RE = re.compile(
    r"^- Tests locaux : joués par le composeur après la poussée, en parallèle "
    r"de la CI complète \(#4304\)", re.MULTILINE)


def composer_tested(cfg, db, item, head: str, *, repo: str = "") -> dict | None:
    """#4136 : le véhicule a-t-il été testé en local par son composeur ?

    Rend ``{"manifest": chemin, "sha": tête, "tests": n}`` quand le lot porte
    une branche ``integration/**`` et que sa tête publie le manifeste de
    paquet-auto avec des tests locaux exécutés, sans échec — ou joués en
    parallèle de la CI (#4304, ``"parallel": True``, ``tests`` 0) —, et que la
    tête est le commit de ce manifeste ; sinon ``None``.
    Un doute (arbre absent, manifeste illisible, autre composeur, aucun test,
    un échec) ne dispense de rien : la prévalidation reste due.
    """
    from . import packet_red
    head = str(head or "").strip().lower()
    if not head or not packet_red.is_carrier(item):
        return None
    where = repository(cfg, db, item, head, explicit=repo)
    if not where:
        return None
    path = packet_red.manifest_name(str((item or {}).get("branch") or ""))
    try:
        text = plan_git._out(where, "show", "%s:%s" % (head, path)) or ""
        # paquet-auto gèle le commit de son manifeste : un commit posé
        # ensuite (correctif à la main) n'a pas été testé par le composeur.
        last = plan_git._out(where, "log", "-1", "--format=%H", head, "--", path) or ""
    except Exception:  # noqa: BLE001 - arbre illisible : rien n'est dispensé
        return None
    if last.strip().lower() != head:
        return None
    if COMPOSER_MARK not in text:
        return None
    if COMPOSER_PARALLEL_RE.search(text):
        return {"manifest": path, "sha": head, "tests": 0, "parallel": True}
    found = COMPOSER_TESTS_RE.search(text)
    if not found or int(found.group(1)) <= 0 or int(found.group(2)) != 0:
        return None
    return {"manifest": path, "sha": head, "tests": int(found.group(1))}


def skipped_text(tested: dict) -> str:
    """#4136 : la ligne de journal d'un véhicule dispensé (ni rouge ni indisponible)."""
    how = ("tested by its composer in parallel with the full CI"
           if tested.get("parallel") else "tested by the composer")
    return "%s skipped: vehicle %s (manifest %s)" % (
        JOURNAL_PREFIX, how, str(tested.get("sha") or "")[:12])


def gate_packet(cfg, db, item, decision: dict, *, repo: str = "",
                map_path: str = "", say=None, warn=None,
                deadline: Deadline | None = None) -> dict:
    """La prévalidation d'un porteur ``integration/**`` : rouge, ou état dit.

    Décision du pilote (19/10, #2644) : **seul un rouge bloque** ``ci run``.
    Une prévalidation indisponible — carte absente ou de plus de 48 h, hôte
    absent, canon sans bloc, sélection non prouvée, run incomplet — ne bloque
    jamais : l'appelant journalise le motif et pose l'étiquette. Rend donc
    toujours un état :

    * ``{"state": "green", "report": …}`` : la sélection a tourné et est verte ;
    * ``{"state": "red", "report": …}`` : un test a échoué, l'appelant refuse ;
    * ``{"state": "unavailable", "cause"/"why", "report"?}`` : rien de prouvé,
      l'appelant continue et cite le motif ;
    * ``{"state": "skipped", "manifest", "sha", "why"}`` (#4136) : véhicule
      testé en local par paquet-auto, aucun offload lancé.

    L'exception déclarée au journal du paquet n'est pas lue ici : elle
    appartient à l'appelant (packet_guard), qui choisit de ne pas bloquer.

    #4067 : le passage est borné par ``deadline`` (par défaut
    ``CALLER_BUDGET_S``) pour que ``ci run`` rende la main sous le délai du
    compositeur ; une échéance épuisée est ``unavailable`` (« timeout »).
    """
    head = str(decision.get("sha") or "").strip().lower()
    item = item or {}
    tested = composer_tested(cfg, db, item, head, repo=repo)
    if tested:
        return {"state": "skipped", **tested, "why": skipped_text(tested)}
    if deadline is None:
        deadline = caller_deadline()
    lot = int(item.get("id") or decision.get("work_item") or 0)
    where = repository(cfg, db, item, head, explicit=repo)
    if not where:
        return {"state": "unavailable", "cause": "selection-unsafe",
                "why": "aucun arbre ne porte la tête %s (--prevalidation-repo)"
                       % (head[:12] or "?")}
    try:
        report = run(cfg, db, lot, repo=where, head=head,
                     base=decision.get("base") or "",
                     project=str(item.get("app") or "ameesh"),
                     branch=str(item.get("branch") or ""), map_path=map_path,
                     repo_slug=str(decision.get("repo") or ""), say=say, warn=warn,
                     deadline=deadline)
    except prevalidation.PrevalidationError as exc:
        return {"state": "unavailable", "cause": exc.cause,
                "why": _timed_out(deadline, exc.why)}
    except Exception as exc:  # noqa: BLE001 - une indisponibilité, jamais un vert
        why = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        if deadline.expired():
            return {"state": "unavailable", "cause": "run-incomplete",
                    "why": _timed_out(deadline, why)}
        return {"state": "unavailable", "cause": "selection-unsafe", "why": why}
    state = report_state(report)
    if state == "green":
        return {"state": "green", "report": report}
    if state == "red":
        return {"state": "red", "report": report, "why": describe(report)}
    return {"state": "unavailable",
            "cause": (report.get("causes") or ("run-incomplete",))[0],
            "why": _timed_out(deadline, describe(report)), "report": report}


# --------------------------------------------------------------------------
# (b) le gel : résultat attaché, jamais bloquant
# --------------------------------------------------------------------------

def _modules_fingerprint(missing) -> str:
    """Jeton stable de la liste des modules non exécutés (#4769).

    Le journal déduplique une indisponibilité par (lot, cause). Pour
    ``run-incomplete``, cette clé masquait aussi la liste des modules d'une
    tête ultérieure. Le jeton, ajouté à la note, permet de comparer la liste
    au dernier constat de la cause sans citer la tête : une nouvelle liste
    écrit sa ligne, la même liste (même ordre ou non) n'ajoute rien.
    """
    modules = sorted({str(m) for m in (missing or ())})
    digest = hashlib.sha256("\n".join(modules).encode("utf-8")).hexdigest()[:12]
    return "[modules %d:%s]" % (len(modules), digest)


def _journal(db, lot: int, text: str, *, key: str = "",
             fingerprint: str = "") -> None:
    """Une note de journal, au mieux : le gel ne dépend jamais d'elle.

    #4758 : ``once=True`` ne déduplique que le texte exact. Une note
    d'indisponibilité qui cite la tête ou le chemin du dépôt change à chaque
    gel et revient donc à chaque fois. ``key`` est la clé stable du couple
    (lot, cause), sans SHA ni chemin : quand une note du lot commence déjà par
    elle, rien n'est réécrit. Une lecture impossible n'empêche jamais la note.

    #4769 : ``fingerprint`` (la liste des modules non exécutés) rend la cause
    ``run-incomplete`` de nouveau visible quand cette liste change. La
    déduplication reste celle de la cause tant que le dernier constat porte le
    même jeton ; une liste différente écrit une ligne à sa tête.
    """
    if db is None:
        return
    try:
        if key:
            try:
                noted = work.events_noted(db, int(lot), key)
            except Exception:  # noqa: BLE001 - clé illisible : la note passe
                noted = []
            if noted:
                if not fingerprint:
                    return
                last = noted[-1]
                last = (str(last.get("note") or "")
                        if isinstance(last, dict) else "")
                if fingerprint in last:
                    return
        work.note(db, int(lot), text, actor="ameesh", once=True)
    except Exception:  # noqa: BLE001 - une note ne bloque pas un gel
        pass


def after_freeze(cfg, db, lot: int, sha: str, *, repo: str = "",
                 map_path: str = "", say=None, warn=None,
                 deadline: Deadline | None = None) -> dict:
    """La prévalidation du gel, après que le jalon ``frozen`` est écrit.

    Le résultat est attaché à la tête gelée dans ``prevalidation_reports`` et
    résumé au journal. Un rouge, une sélection non prouvée ou une
    indisponibilité ne défont pas le gel : il est déjà écrit, et ce geste ne
    lève pas. Rend ``{"state": "green"|"red"|"unavailable", ...}``.

    #4067 : borné par ``deadline`` (par défaut ``CALLER_BUDGET_S``), pour que
    ``work milestone frozen`` rende la main sous le délai du compositeur.
    """
    if deadline is None:
        deadline = caller_deadline()
    try:
        return _after_freeze(cfg, db, lot, sha, repo=repo, map_path=map_path,
                             say=say, warn=warn, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - le gel est déjà écrit, rien ne le défait
        why = _timed_out(deadline, "%s: %s" % (type(exc).__name__, str(exc)[:300]))
        _journal(db, lot, "%s unavailable: %s" % (JOURNAL_PREFIX, why))
        return {"state": "unavailable", "why": why}


def _repo_slug(cfg, item) -> str:
    """`owner/name` du lot pour l'artefact carte, ou ``""`` (pas de réseau)."""
    try:
        from . import ci
        return ci.repo_of(cfg, dict(item or {}), repo="") or ""
    except Exception:  # noqa: BLE001 - sans dépôt GitHub, pas d'artefact carte
        return ""


def _after_freeze(cfg, db, lot: int, sha: str, *, repo: str = "",
                  map_path: str = "", say=None, warn=None,
                  deadline: Deadline | None = None) -> dict:
    item = work.get(db, int(lot)) if db is not None else None
    if item is None:
        return {"state": "unavailable", "why": "lot inconnu"}
    head = str(sha or "").strip().lower()
    if not head:
        _journal(db, lot, "%s unavailable: gel sans --sha" % JOURNAL_PREFIX)
        return {"state": "unavailable", "why": "gel sans --sha"}
    tested = composer_tested(cfg, db, item, head, repo=repo)
    if tested:
        _journal(db, lot, skipped_text(tested))
        return {"state": "skipped", **tested, "why": skipped_text(tested)}
    where = repository(cfg, db, item, head, explicit=repo)
    if not where:
        key = "%s unavailable: aucun arbre ne porte" % JOURNAL_PREFIX
        _journal(db, lot, "%s %s" % (key, head[:12]), key=key)
        return {"state": "unavailable", "why": "aucun arbre ne porte la tête"}
    base = target_base(where, item)
    if not base:
        _journal(db, lot, "%s unavailable: cible du lot illisible (%s)"
                 % (JOURNAL_PREFIX, item.get("branch_target") or "main"))
        return {"state": "unavailable", "why": "cible illisible"}
    try:
        report = run(cfg, db, lot, repo=where, head=head, base=base,
                     project=str(item.get("app") or "ameesh"),
                     branch=str(item.get("branch") or ""), map_path=map_path,
                     repo_slug=_repo_slug(cfg, item), say=say, warn=warn,
                     deadline=deadline)
    except prevalidation.PrevalidationError as exc:
        why = _timed_out(deadline, exc.why)
        key = "%s unavailable: %s" % (JOURNAL_PREFIX, exc.cause)
        text = "%s%s" % (key, (" — " + why) if why else "")
        _journal(db, lot, text, key=key)
        return {"state": "unavailable", "cause": exc.cause, "why": why}
    state = report_state(report)
    why = describe(report)
    if state == "unavailable":
        why = _timed_out(deadline, why)
        cause = (report.get("causes") or ("run-incomplete",))[0]
        key = "%s unavailable: %s" % (JOURNAL_PREFIX, cause)
        fingerprint = ""
        if cause == "run-incomplete" and report.get("missing"):
            fingerprint = _modules_fingerprint(report.get("missing"))
            _journal(db, lot, "%s %s — tête %s: %s"
                     % (key, fingerprint, head[:12], why),
                     key=key, fingerprint=fingerprint)
        else:
            _journal(db, lot, "%s — tête %s: %s" % (key, head[:12], why), key=key)
    else:
        _journal(db, lot, "%s %s %s: %s" % (JOURNAL_PREFIX, state, head[:12], why))
    return {"state": state, "report": report}


# --------------------------------------------------------------------------
# (c) le train, avant sa publication
# --------------------------------------------------------------------------

def gate_train(cfg, *, repo: str, head: str, base: str, canon_repo: str = "",
               canon_commit: str = "", project: str = "ameesh",
               repo_slug: str = "", map_path: str = "",
               say=None, warn=None, clock=None) -> dict:
    """La prévalidation de la composition, avant ``train_git.publish``.

    Un rouge lève ``PrevalidationError`` : rien n'est publié. Une
    indisponibilité (canon sans bloc, VM absente, plan non prouvé) est
    journalisée par l'appelant et ne bloque pas : la CI complète du paquet
    reste la preuve finale, et le train ne s'arrête pas sur une VM muette.
    Il n'y a pas de lot de paquet à ce stade : le rapport (lot 0) n'est pas
    persisté dans ``prevalidation_reports``.

    Le passage entier est borné sous ``land.LOCK_TTL_S`` (#2665) : carte,
    préparation, replis multi-hôtes et nettoyage lisent une même échéance
    monotone, avec la marge gardée pour relire et publier. Une échéance
    épuisée est une indisponibilité ``run-incomplete``, jamais un rouge.
    """
    if not repo:
        return {"state": "unavailable", "why": "dépôt du train inconnu"}
    deadline = train_deadline(clock or time.monotonic)
    try:
        params = parameters(cfg, project, canon_repo=canon_repo,
                            canon_commit=canon_commit, deadline=deadline)
        declared = params.hosts
        if not any(prevalidation.host_allowed(host, declared=declared)[0]
                   for host in declared):
            return {"state": "unavailable", "cause": "no-vm",
                    "why": "aucun hôte de prévalidation autorisé (%s)"
                           % ", ".join(declared)}
        governed = min(int(getattr(params.params, "timeout_seconds", 0) or 0),
                       TRAIN_RUN_TIMEOUT)
        params = Parameters(params=replace(params.params, timeout_seconds=governed),
                            repo=params.repo, commit=params.commit,
                            project=params.project)
        tooling(repo, head, params, deadline=deadline)
        carte, cleanup = map_for(repo_slug, repo, explicit=map_path,
                                 deadline=deadline)
        if deadline.expired():
            raise prevalidation.PrevalidationError(
                "run-incomplete", "budget de prévalidation épuisé avant l'arbre du SHA")
        prepared = None
        try:
            prepared = prepare(repo, head, base, map_path=carte,
                               timeout=TRAIN_PLAN_TIMEOUT, deadline=deadline)
            fallback = contract_fallback(prepared, params, head=head, base=base,
                                         deadline=deadline)
            if deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete", "budget de prévalidation épuisé avant l'hôte")
            report = _run_hosts(prepared, params, lot=0, head=head, base=base,
                                branch="", profile=project, say=say,
                                executable="ameesh", validate=None,
                                deadline=deadline)
            if fallback:
                report.setdefault("fallback", fallback)
                report.setdefault("selection", "contract")
        finally:
            if prepared is not None:
                prepared.close(timeout=deadline.timeout(TRAIN_CLEANUP_TIMEOUT))
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
    except prevalidation.PrevalidationError as exc:
        # #2644/#4073 : un rouge levé (rapport, hôte ou contrat) n'est pas une
        # indisponibilité ; le train ne publie pas.
        if exc.cause == "run-red":
            raise
        return {"state": "unavailable", "cause": exc.cause,
                "why": _timed_out(deadline, exc.why)}
    except Exception as exc:  # noqa: BLE001 - panne d'outillage : indisponible
        return {"state": "unavailable", "cause": "internal",
                "why": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
    state = report_state(report)
    if state == "red":
        raise prevalidation.PrevalidationError("run-red", describe(report))
    if state == "unavailable":
        return {"state": "unavailable",
                "cause": (report.get("causes") or ("run-incomplete",))[0],
                "why": describe(report), "report": report}
    if deadline.expired():
        return {"state": "unavailable", "cause": "run-incomplete",
                "why": "budget de prévalidation épuisé avant publication",
                "report": report}
    return {"state": "green", "report": report}
