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
(``composer_tested``). #4304/#7740 : le manifeste de paquet-auto déclare ses
tests locaux, joués sur le préfixe composé de chaque unité avant la poussée ;
il dispense de même (la prévalidation rejouerait les mêmes tests ciblés). Un
lot ordinaire, ou un véhicule composé à la main sans ce manifeste, garde sa
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
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace

from . import canon as canon_mod
from . import land, lot_class, mechanics, packet_guard, plan_git, prevalidation
from . import registry, work
from . import transverse_contracts
from .deadline import Deadline

#: le calculateur du plan L161f, lu dans l'arbre du candidat (convention
#: ameesh ; un projet le remplace par `prevalidation.plan_script`)
DEFAULT_PLAN_SCRIPT = "scripts/test-plan.py"
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
#: #6157 : runs verts essayés, du plus récent au plus ancien — une mesure
#: planifiée sautée par sa fenêtre finit verte sans artefact `carte`.
CARTE_RUNS = 5
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
#: #7161 : l'attente d'un créneau heavy est hors du budget de travail
#: (`Deadline.pause`), jamais au-delà de la borne murale de l'appelant : la
#: prévalidation rend « en file » avant que le compositeur ne tue la commande.
FREEZE_BUDGET_S = 570.0


def caller_deadline(budget: float | None = None, clock=time.monotonic,
                    limit: Deadline | None = None) -> Deadline:
    """L'échéance d'une prévalidation appelée par un gel ou un ``ci run``.

    ``limit`` (#4073) plafonne le budget par le temps qui reste d'une échéance
    déjà commencée — celle du gel entier, contrôles statiques compris.

    #7161 : la borne murale du geste appelant est transmise à part
    (``Deadline.wall``) ; l'attente d'un créneau heavy peut être suspendue du
    budget de travail, jamais du temps avant cette borne. Sans ``limit``, le
    geste est composé sous les 600 s du compositeur, comme le gel.
    """
    seconds = CALLER_BUDGET_S if budget is None else float(budget)
    if limit is not None:
        seconds = min(seconds, limit.remaining())
        wall = limit.at
    else:
        seconds = max(0.0, seconds)
        wall = clock() + max(FREEZE_BUDGET_S, seconds)
    return Deadline(seconds, clock=clock, wall=wall)


def freeze_deadline(clock=time.monotonic) -> Deadline:
    """L'échéance du geste ``work milestone ... frozen`` entier (#4073).

    Elle commence avant les contrôles statiques et se partage entre eux et la
    prévalidation, pour que la commande rende la main sous les 600 s du
    compositeur (``FREEZE_TIMEOUT`` de paquet-auto, ``COMMAND_TIMEOUT`` de
    paquet-horaire). Sa borne murale est le geste entier : la prévalidation
    n'en hérite que le temps qui reste.
    """
    return Deadline(FREEZE_BUDGET_S, clock=clock)


#: #7161 — la prévalidation passe par la file des suites lourdes de l'hôte
#: (`ameesh heavy`, L131) : au plus `max_heavy_suites` en parallèle, comme une
#: suite. Un gel qui attend son tour est « en file » : l'attente est suspendue
#: du budget de travail (`Deadline.pause`) et n'est jamais un `run-incomplete`.
#: Passé ce délai, la prévalidation est rendue « en file » sans run ; le gel,
#: lui, est déjà écrit et n'est pas défait.
PREVALIDATION_QUEUE_S = 900.0
#: le même délai, réglable pour les bancs et les témoins
PREVALIDATION_QUEUE_ENV = "AMEESH_PREVALIDATION_QUEUE_S"
#: #7161 : marge gardée avant la borne murale de l'appelant pour rendre
#: « en file » (écriture du journal, sortie du processus) ; même marge que
#: celle de ``FREEZE_BUDGET_S`` sous les 600 s du compositeur.
PREVALIDATION_QUEUE_MARGIN_S = 30.0


def queue_wait_seconds() -> float:
    """Le délai d'attente d'un créneau heavy : le défaut ou l'environnement."""
    raw = os.environ.get(PREVALIDATION_QUEUE_ENV)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return PREVALIDATION_QUEUE_S
    return value if value >= 0 else PREVALIDATION_QUEUE_S


def queue_bound_seconds(deadline: Deadline | None = None) -> float:
    """#7161 — l'attente maximale d'un créneau, jamais au-delà du mur appelant.

    Le budget de travail peut être rendu après l'attente (``Deadline.pause``),
    mais la commande extérieure a une borne murale (570 s de gel, 600 s du
    compositeur) : on rend « en file » avant, jamais après.
    """
    bound = queue_wait_seconds()
    if deadline is not None:
        bound = min(bound, deadline.wall_remaining() - PREVALIDATION_QUEUE_MARGIN_S)
    return max(0.0, bound)


def _queue_note(text: str) -> None:
    print("prévalidation en file : %s" % text, file=sys.stderr, flush=True)


@contextmanager
def heavy_queue(cfg, command: str, *, say=None,
                deadline: Deadline | None = None):
    """#7161 — tient un créneau de la file heavy de l'hôte pendant le passage.

    Rend le temps d'attente en secondes. L'attente est suspendue du budget de
    travail (`Deadline.pause`) : le run qui suit garde le budget qu'il avait,
    et l'attente n'est jamais comptée comme un `run-incomplete`. Elle reste
    bornée par le mur de l'appelant : sans créneau dans
    [`PREVALIDATION_QUEUE_S`] ou avant ce mur, lève `queued` : la prévalidation
    est « en file », le gel reste écrit."""
    from . import heavy

    if cfg is None or not str(getattr(cfg, "state_dir", "") or ""):
        yield 0.0
        return
    clock = deadline.clock if deadline is not None else time.monotonic
    started = clock()
    log = _queue_note if say is None else say
    held = heavy.acquire(cfg, command=command, log=log,
                         max_wait_s=queue_bound_seconds(deadline))
    waited = clock() - started
    if held is None:
        raise prevalidation.PrevalidationError(
            "queued",
            "aucun créneau heavy libre après %ds ; la prévalidation est en "
            "file, le gel reste écrit" % int(waited))
    try:
        if deadline is not None:
            deadline.pause(waited)
        yield waited
    finally:
        heavy.release(held[1])


def _timeout_error(deadline: Deadline, where: str) -> prevalidation.PrevalidationError:
    """Une échéance de l'appelant épuisée : indisponible, motif ``timeout``."""
    return prevalidation.PrevalidationError("run-incomplete", _timed_out(deadline, where))


def _timed_out(deadline: Deadline | None, why: str) -> str:
    """``why`` préfixé de « timeout » quand l'échéance de l'appelant est épuisée."""
    if deadline is None or not deadline.expired() or str(why).startswith("timeout"):
        return why
    #: #7161 : `Deadline.pause` allonge `at` ; le motif nomme le budget reçu,
    #: pas le temps de mur (une attente de créneau heavy ne le gonfle pas).
    budget = getattr(deadline, "budget", deadline.at - deadline.started)
    head = "timeout — budget de l'appelant (%d s) épuisé" % round(budget)
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


def repository(cfg, db, item, head: str, *, explicit: str = "",
               deadline: Deadline | None = None) -> str:
    """Le dépôt qui porte la tête candidate, ou ``""``.

    L'arbre explicite d'abord, puis le répertoire courant (le composeur est
    dans l'arbre du paquet), le worktree de l'assigné (registre) et le dépôt du
    projet. Un doute ne devine pas : un chemin qui ne porte pas le commit n'est
    pas retenu.

    #8377 : un clone **déclaré** n'est pas fetché par un minuteur ; une tête
    gelée poussée après son dernier fetch y est absente et la prévalidation
    journalisait « aucun arbre ne porte la tête ». Après le balayage local, le
    SHA exact est donc demandé à l'origine des clones déclarés (borné par
    ``deadline`` et [`plan_git.FETCH_FROZEN_TIMEOUT_S`]), puis la sonde locale
    reprend. Seul le commit exact décide : un clone qui ne l'obtient pas ne
    change rien au doute.
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
    declared: list[str] = []
    try:
        # #6984 : le dépôt du projet et les clones déclarés par l'hôte
        # (`packet_watch`, `release`) ; un miroir bare est lu par SHA.
        declared = lot_class.declared_repo_paths(cfg, dict(item or {}))
        candidates.extend(declared)
    except Exception:  # noqa: BLE001 - pas de radar : indice suivant
        pass
    seen: set[str] = set()
    for path in candidates:
        path = os.path.expanduser(str(path or "").strip())
        if not path or not os.path.isdir(path):
            continue
        top = canon_mod.git_repository(path)
        if not top or top in seen:
            continue
        seen.add(top)
        # #8377/#2851 : la sonde d'abord locale (`local_commit`) ; un clone
        # partiel ne doit pas réveiller le promisor avant le fetch borné de
        # la tête exacte, tenté plus bas.
        if plan_git.local_commit(top, wanted):
            return top
    # #8377 : la tête peut avoir été poussée après le dernier fetch du clone
    # déclaré. Le SHA exact est demandé à l'origine de ce clone ; le commit
    # n'est retenu qu'une fois relu localement.
    fetched: set[str] = set()
    branch = str((item or {}).get("branch") or "")
    for path in declared:
        path = os.path.expanduser(str(path or "").strip())
        if not path or not os.path.isdir(path):
            continue
        top = canon_mod.git_repository(path)
        if not top or top in fetched:
            continue
        fetched.add(top)
        try:
            if plan_git.fetch_frozen(
                    top, wanted, branch,
                    timeout=plan_git.FETCH_FROZEN_TIMEOUT_S,
                    deadline=deadline):
                return top
        except (canon_mod.GitError, canon_mod.GitDeadline):
            continue
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
        # #6157 : un run vert n'a pas toujours publié de carte — la fenêtre
        # saute la mesure planifiée hors de son créneau (job `fenetre`, run
        # vert sans artefact) ; les runs suivants sont essayés dans l'ordre.
        listed = runner(["gh", "run", "list", "--repo", slug, "--workflow",
                         CARTE_WORKFLOW, "--branch", CARTE_BRANCH,
                         "--status", "success", "--limit", str(CARTE_RUNS),
                         "--json", "databaseId"], capture_output=True, text=True,
                        timeout=deadline.timeout(30) if deadline is not None else 30)
        if listed.returncode:
            return "", ""
        runs = json.loads(listed.stdout or "[]")
        for run in runs:
            ident = run.get("databaseId")
            if not ident:
                continue
            if deadline is not None and deadline.expired():
                return "", ""
            tmp = tempfile.mkdtemp(prefix="ameesh-carte-")
            got = runner(["gh", "run", "download", str(ident), "--repo", slug, "-n",
                          CARTE_ARTIFACT, "-D", tmp], capture_output=True, text=True,
                         timeout=deadline.timeout(120) if deadline is not None else 120)
            path = os.path.join(tmp, "carte.json.gz")
            if not got.returncode and os.path.isfile(path):
                return path, tmp
            shutil.rmtree(tmp, ignore_errors=True)
        return "", ""
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
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

    Le bloc ``prevalidation`` du canon du projet déclare ses outils : le script
    de plan (``plan_script``) et le lanceur de part (``parts_script``). Absents,
    la convention d'ameesh s'applique (``scripts/test-plan.py``,
    ``scripts/test-parts.py``) ; un dépôt qui ne les porte pas n'est pas
    prévalidable. Le refus est nommé ``tooling-missing`` avant tout arbre, carte
    ou hôte, au lieu d'un ``can't open file`` de Python lu comme
    ``selection-unsafe``. Une tête qui n'est pas un commit est laissée au refus
    de ``prepare``.
    """
    full = str(head or "").strip().lower()
    planner = str(getattr(params.params, "plan_script", None)
                  or DEFAULT_PLAN_SCRIPT)
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
        for path in (planner, launcher):
            if subprocess.run([*git, "cat-file", "-e", "%s:%s" % (full, path)],
                              capture_output=True, text=True,
                              timeout=limit(60)).returncode:
                absent.append(path)
    except (OSError, subprocess.SubprocessError):
        return
    if absent:
        raise prevalidation.PrevalidationError(
            "tooling-missing", "%s absent de %s au SHA %s : le projet %s n'outille pas "
            "la prévalidation (plan L161f et lanceur de part déclarés)"
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
            plan_script: str = DEFAULT_PLAN_SCRIPT, project: str = "",
            timeout: int = PLAN_TIMEOUT, deadline: Deadline | None = None) -> Prepared:
    """Construit un arbre détaché de ``head`` et y calcule le plan L161f.

    Le script lu est celui du commit candidat — ``plan_script`` déclaré par le
    bloc ``prevalidation`` du projet, sinon la convention d'ameesh
    ``scripts/test-plan.py``. ``AMEESH_PROJECT`` porte le projet au script pour
    qu'il lise le registre de son propre canon ; absent, le script garde sa
    convention. La carte mesurée est passée en lecture seule à ``--map`` quand
    l'appelant en fournit une. L'arbre reste vivant jusqu'à
    ``Prepared.close()`` : le plan doit rester dans l'arbre pour être embarqué
    dans la preuve. Avec ``deadline`` (#2665), aucun appel git ni le plan ne
    dépasse le temps qui reste au passage.
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
        planner = str(plan_script or DEFAULT_PLAN_SCRIPT)
        plan_path = os.path.join(tree, rel)
        argv = [sys.executable, os.path.join(tree, planner), "plan",
                "--out", rel, "--event", "workflow_dispatch", "--ref", "",
                "--base", comparison, "--mode", "lot"]
        if map_path:
            argv += ["--map", os.path.abspath(map_path)]
        env = None
        if project:
            # The project's adapter reads its own canon/registry; an N-1
            # adapter ignores the variable and keeps its convention.
            env = dict(os.environ, AMEESH_PROJECT=project)
        try:
            if deadline is not None and deadline.expired():
                raise prevalidation.PrevalidationError(
                    "run-incomplete", "budget de prévalidation épuisé avant le plan")
            generated = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                                       timeout=limit(timeout), env=env)
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
                "selection-unsafe", "%s a refusé le plan: %s"
                % (planner, tail or "exit %d" % generated.returncode))
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


#: #7823 : budget, en secondes mesurées (`tests/parts/durees.json`, pilote
#: psycopg), des modules qui importent les fichiers changés du lot. Le repli
#: tient dans la prévalidation d'un gel (`FREEZE_BUDGET_S`) : le lot y passe
#: avant les contrats, jamais toute la suite.
FALLBACK_IMPORTERS_BUDGET_S = 120.0
#: durée d'un module absent de la table (même forfait que paquet-attribution)
FALLBACK_UNKNOWN_S = 60.0
#: fichiers changés dont on cherche les importateurs (une racine de graphe)
FALLBACK_IMPORTED_FILES = 40
#: #8046 : profondeur du graphe d'imports lue depuis chaque fichier changé
#: (directs, puis transitifs). Au-delà, un test est trop loin du diff pour
#: valoir sa place dans le budget d'un gel.
FALLBACK_IMPORTER_MAX_DISTANCE = 3
#: #8046 : modules visités au plus par racine (borne le coût de la lecture)
FALLBACK_IMPORTER_MAX_NODES = 500
#: #8046 : modules retenus au plus par fichier changé. Le budget mesuré seul
#: laissait partir des dizaines de modules à 0 s, dont le démarrage n'est pas
#: dans la table ; neuf couvre les directs d'un petit module et garde le lot
#: jouable dans la prévalidation d'un gel.
FALLBACK_IMPORTERS_PER_ROOT = 9
#: #8046 : délai de la lecture du graphe d'imports (git + analyse)
FALLBACK_GRAPH_TIMEOUT = 120
DURATIONS_PATH = "tests/parts/durees.json"


def _own_tests(changes, tree_modules: set[str]) -> list[str]:
    """#7823 : les modules de test que le diff **propre** du lot ajoute ou change.

    Les classes P0 du plan ne suffisent plus : avec une carte de 46 h, le plan
    juge la dérive de `main` depuis la carte, et ses P0 portaient 247 modules
    que le lot ne touchait pas (#7813, 18/11)."""
    out = []
    for status, path in changes:
        name = os.path.basename(path)[:-3] if path.endswith(".py") else ""
        if status != "D" and path.startswith("tests/test") \
                and name in tree_modules and name not in out:
            out.append(name)
    return sorted(out)


def _dotted(path: str) -> str:
    """`src/ameesh/x/y.py` → `ameesh.x.y` ; `tests/support.py` → `tests.support`."""
    if not path.endswith(".py"):
        return ""
    if path.startswith("src/"):
        stem = path[len("src/"):-3]
    elif path.startswith("tests/") and not os.path.basename(path).startswith("test"):
        stem = path[:-3]
    else:
        return ""
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts):
        return ""
    return ".".join(parts)


def _durations(prepared: Prepared, head: str, run) -> dict[str, float]:
    """Les secondes par module de la table du candidat (psycopg), ou `{}`."""
    try:
        proc = run(["git", "-C", prepared.repo, *prevalidation.NO_REPLACE,
                    "show", "%s:%s" % (head, DURATIONS_PATH)],
                   capture_output=True, text=True)
        if getattr(proc, "returncode", 1):
            return {}
        table = json.loads(getattr(proc, "stdout", "") or "{}").get("psycopg")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return {}
    if not isinstance(table, dict):
        return {}
    out = {}
    for name, seconds in table.items():
        try:
            out[str(name)] = float(seconds)
        except (TypeError, ValueError):
            continue
    return out


def _module_name(path: str) -> str:
    """Le nom importable d'un fichier Python de `src/` ou `tests/`, ou ``""``."""
    if not path.endswith(".py"):
        return ""
    if path.startswith("src/"):
        stem = path[len("src/"):-3]
    elif path.startswith("tests/"):
        stem = path[:-3]
    else:
        return ""
    if stem.endswith("/__init__"):
        stem = stem[:-len("/__init__")]
    parts = [part for part in stem.split("/") if part]
    if not parts or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
                            for part in parts):
        return ""
    return ".".join(parts)


def _import_targets(text: str, importer: str, known: set[str]) -> set[str]:
    """Les modules du SHA qu'une ligne ``import``/``from`` importe (#8046).

    Résout les imports relatifs depuis le paquet de ``importer``. Un nom
    importé n'est retenu que s'il est un module du SHA : ``from a import b``
    lie ``a.b`` quand c'est un module, sinon le module ``a`` (import de
    symbole). Il ne lie jamais ``a`` au seul prétexte que ``b`` est un module :
    le paquet lui-même n'est pas la dépendance utile, et le lier rendrait tous
    les tests importateurs d'un nom quelconque de ``a`` des importateurs
    directs du fichier changé.
    """
    line = str(text or "").strip()
    if line.startswith("from "):
        match = re.match(r"from\s+(\S+)\s+import\s+(.+)$", line)
        if not match:
            return set()
        base, names = match.group(1), match.group(2)
        names = (names.split("#", 1)[0].replace("(", "").replace(")", "")
                 .replace("\\", ""))
        if base.startswith("."):
            dots = len(base) - len(base.lstrip("."))
            rest = base[dots:]
            package = importer.rpartition(".")[0]
            for _ in range(dots - 1):
                package = package.rpartition(".")[0]
            if not package:
                return set()
            base = (package + "." + rest).strip(".") if rest else package
        targets: set[str] = set()
        submodule = False
        for name in names.split(","):
            name = name.strip().split(" as ")[0].strip()
            if not name or name == "*" \
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            target = (base + "." + name).strip(".")
            if target in known:
                targets.add(target)
                submodule = True
        if not submodule and base in known:
            targets.add(base)
        return targets
    if line.startswith("import "):
        names = line[len("import "):].split("#", 1)[0]
        targets = set()
        for name in names.split(","):
            name = name.strip().split(" as ")[0].strip()
            if name in known and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", name):
                targets.add(name)
        return targets
    return set()


def _import_graph(prepared: Prepared, head: str, *,
                  deadline: Deadline | None = None
                  ) -> tuple[dict[str, set[str]], set[str]]:
    """#8046 : le graphe inverse des imports au SHA candidat.

    Une seule lecture de l'arbre (``git ls-tree``) et une seule lecture des
    lignes d'import (``git grep``) : ``rev[module]`` est l'ensemble des modules
    qui l'importent, ``known`` l'ensemble des modules du SHA. Un import sur
    plusieurs lignes, dynamique ou conditionnel n'est pas vu (même limite que
    la sélection #7823) ; la ci-full du paquet reste la preuve finale. Une
    lecture impossible rend ``({}, set())`` : le repli garde alors les seuls
    tests du diff, jamais un élargissement supposé.
    """
    if not str(getattr(prepared, "repo", "") or ""):
        return {}, set()
    bounded = prevalidation._bounded_runner(subprocess.run, deadline)
    timeout = FALLBACK_GRAPH_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline.remaining()))
    if timeout <= 0:
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé avant le graphe d'imports")
    tree = ["git", "-C", prepared.repo, *prevalidation.NO_REPLACE,
            "ls-tree", "-r", "--name-only", "-z", head]
    try:
        proc = bounded(tree, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return {}, set()
    if getattr(proc, "returncode", 1):
        return {}, set()
    modules = {}
    for path in (getattr(proc, "stdout", "") or "").split("\0"):
        if not path.endswith(".py"):
            continue
        module = _module_name(path)
        if module:
            modules[path] = module
    known = set(modules.values())
    grep = ["git", "-C", prepared.repo, *prevalidation.NO_REPLACE,
            "grep", "-n", "-E", r"^[[:space:]]*(import|from)[[:space:]]",
            head, "--", "*.py"]
    try:
        proc = bounded(grep, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return {}, known
    if getattr(proc, "returncode", 1):
        return {}, known
    rev: dict[str, set[str]] = {}
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        fields = line.split(":", 3)
        if len(fields) != 4:
            continue
        importer = modules.get(fields[1])
        if not importer:
            continue
        for target in _import_targets(fields[3], importer, known):
            rev.setdefault(target, set()).add(importer)
    return rev, known


def _test_module(module: str, tree_modules: set[str]) -> str:
    """`tests.test_2820_events_rotation` → `test_2820_events_rotation`."""
    if not module.startswith("tests.test_"):
        return ""
    name = module[len("tests."):]
    return name if name in tree_modules else ""


def _importer_candidates(rev: dict[str, set[str]], roots,
                         tree_modules: set[str]) -> dict[str, dict[str, int]]:
    """#8046 : pour chaque fichier changé, ses tests importateurs et leur
    distance d'import (1 = direct, 2 et 3 = transitifs). Le parcours est borné
    en profondeur et en modules visités par racine."""
    out: dict[str, dict[str, int]] = {}
    for root in roots:
        found: dict[str, int] = {}
        seen = {root: 0}
        queue = deque([root])
        while queue and len(seen) <= FALLBACK_IMPORTER_MAX_NODES:
            node = queue.popleft()
            distance = seen[node]
            if distance >= FALLBACK_IMPORTER_MAX_DISTANCE:
                continue
            for importer in rev.get(node, ()):
                if importer in seen:
                    continue
                seen[importer] = distance + 1
                name = _test_module(importer, tree_modules)
                if name and name not in found:
                    found[name] = distance + 1
                queue.append(importer)
        out[root] = found
    return out


def _budget_importers(queues: dict[str, dict[str, int]],
                      durations: dict[str, float], taken: set[str],
                      budget: float,
                      per_root_max: int = FALLBACK_IMPORTERS_PER_ROOT
                      ) -> tuple[list[str], list[str]]:
    """#8046 : un quota par fichier changé, puis la coupe au budget global.

    Chaque racine (fichier changé) retient d'abord ses `per_root_max`
    importateurs les plus pertinents : par distance d'import (directs, puis
    transitifs), puis par durée mesurée. Une racine très importée ne peut donc
    plus consommer seule le budget avant qu'une autre, modérément importée,
    n'ait joué les siens : c'est ce qui arrivait à `test_2820_events_rotation`
    (6 s, neuvième de `housekeeping`) face aux importateurs bon marché d'un
    autre fichier dans la coupe globale de #7823.

    Le budget `budget` (secondes mesurées) reste une borne **globale** : si la
    somme des modules retenus la dépasse, les moins pertinents sont retirés
    d'abord — plus grande distance d'import, puis plus longue durée. Un
    importateur direct coûteux passe donc avant un transitif bon marché, et
    les directs courts avant les directs longs. Le reliquat n'est pas
    redistribué : il reste la marge entre la table mesurée et le run réel, qui
    l'a déjà dépassée (#7823). Rend `(ordre de jeu, noms laissés de côté)`.
    """
    live = []
    for root, found in queues.items():
        queue = []
        for name, distance in found.items():
            if name in taken:
                continue
            queue.append((distance, durations.get(name, FALLBACK_UNKNOWN_S),
                          name))
        queue.sort()
        if queue:
            live.append((root, queue))
    if not live:
        return [], []
    picked: dict[str, list[str]] = {root: [] for root, _ in live}
    owner: dict[str, str] = {}
    for root, queue in live:
        for _distance, _seconds, name in queue:
            if len(picked[root]) >= per_root_max:
                break
            if name in owner:
                continue
            owner[name] = root
            picked[root].append(name)
    #: la distance la plus courte parmi les racines qui voient le module : un
    #: test qui importe directement un fichier changé reste direct même si une
    #: autre racine ne l'atteint que par un intermédiaire.
    distance = {}
    for _root, queue in live:
        for d, _seconds, name in queue:
            if name in owner and d < distance.get(name, 99):
                distance[name] = d
    seconds = {name: durations.get(name, FALLBACK_UNKNOWN_S) for name in owner}
    spent = sum(seconds.values())
    if spent > budget + 1e-9:
        for name in sorted(owner, key=lambda n: (-distance[n], -seconds[n], n)):
            if spent <= budget + 1e-9:
                break
            picked[owner[name]].remove(name)
            del owner[name]
            spent -= seconds[name]
    # Ordre de jeu : un module de chaque racine à tour de rôle, du plus
    # pertinent au moins pertinent : un rouge du lot est vu tôt, avant que
    # les contrats ne prennent le reste du budget.
    order: list[str] = []
    index = {root: 0 for root, _ in live}
    while True:
        moved = False
        for root, _queue in live:
            if index[root] < len(picked[root]):
                order.append(picked[root][index[root]])
                index[root] += 1
                moved = True
        if not moved:
            break
    all_names = {name for _root, queue in live for _d, _s, name in queue}
    return order, sorted(all_names - set(owner))


def _importers(prepared: Prepared, head: str, changes, tree_modules: set[str],
               taken, *, deadline: Deadline | None = None
               ) -> tuple[list[str], list[str]]:
    """#7823/#8046 : le repli statique borné — les tests qui importent les
    fichiers changés du lot.

    Le graphe inverse des imports est lu au SHA candidat, puis parcouru depuis
    chaque fichier **source** changé : directs (distance 1), puis transitifs
    (2, 3). Un changement de l'infrastructure de test (`tests/support.py`)
    n'est pas une racine : ses importateurs sont presque toute la suite, que
    la CI du paquet couvre et que le budget du gel ne peut pas jouer ; les
    tests du lot restent pris par `_own_tests`. Chaque fichier changé retient
    au plus `FALLBACK_IMPORTERS_PER_ROOT` modules, par distance puis durée
    mesurée (`tests/parts/durees.json`) ; la coupe au budget global
    `FALLBACK_IMPORTERS_BUDGET_S` retire ensuite les plus éloignés, puis les
    plus longs (`_budget_importers`).
    Rend `(modules retenus dans l'ordre de jeu, noms laissés de côté)`. Un
    import sur plusieurs lignes ou dynamique n'est pas vu (la ci-full du paquet
    reste la preuve finale).
    """
    if deadline is not None and deadline.expired():
        raise prevalidation.PrevalidationError(
            "run-incomplete",
            "budget de prévalidation épuisé avant les importateurs du lot")
    if not str(getattr(prepared, "repo", "") or ""):
        return [], []
    roots = []
    for status, path in changes:
        dotted = _dotted(path) if status != "D" else ""
        if not dotted or dotted.startswith("tests.") or dotted in roots:
            continue
        roots.append(dotted)
    roots = roots[:FALLBACK_IMPORTED_FILES]
    if not roots:
        return [], []
    bounded = prevalidation._bounded_runner(subprocess.run, deadline)
    durations = _durations(prepared, head, bounded)
    graph, _known = _import_graph(prepared, head, deadline=deadline)
    queues = _importer_candidates(graph, roots, tree_modules)
    order, dropped = _budget_importers(
        queues, durations, set(taken), FALLBACK_IMPORTERS_BUDGET_S)
    return order, dropped


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

    #7823 : les « tests du lot » sont ceux de son diff propre, plus les
    importateurs statiques de ses fichiers sous `FALLBACK_IMPORTERS_BUDGET_S`
    (`_importers`), jamais les P0 d'un plan jugé depuis une carte ancienne. Le
    lot passe dans une première part, les contrats dans une seconde.

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
    shared = plan.get("contract_plan")
    if isinstance(shared, dict) and shared.get("active"):
        from . import contract_plan
        try:
            contract_plan.consume(shared)
        except ValueError:
            return None
        required = tuple(sorted(set(required) | set(shared["contracts"])))
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
    #: #7823 : diff lu, seuls les tests que le lot change lui-même comptent.
    #: Les P0 d'un plan jugé depuis le commit de la carte portent toute la
    #: dérive de `main` (247 modules le 18/11 pour #7813) : le repli jouait
    #: alors la suite presque entière et n'aboutissait jamais dans sa borne.
    #: Un P0 garde sa place s'il nomme un fichier du diff (projet sans
    #: `tests/test*.py`) ; diff illisible, les P0 restent (élargir, jamais
    #: rétrécir en silence).
    importers: list[str] = []
    importers_skipped: list[str] = []
    if changes is not None:
        changes = [(str(item[0]), str(item[1]))
                   if isinstance(item, (list, tuple)) and len(item) >= 2
                   else ("M", str(item)) for item in changes]
        stems = {os.path.splitext(path)[0] for status, path in changes
                 if status != "D"}
        own = set(_own_tests(changes, tree_modules))
        own.update(name for name in changed_tests
                   if name in stems or any(stem.endswith("/" + name)
                                           for stem in stems))
        changed_tests = sorted(own)
        importers, importers_skipped = _importers(
            prepared, head, changes, tree_modules,
            set(changed_tests) | set(required) | set(transverse),
            deadline=deadline)
    #: #6186 : les modules de la porte et du superviseur sont un filet propre à
    #: ameesh ; l'inventaire du plan dit si le projet les porte. Sans inventaire
    #: (plan N-1), le filet historique reste appliqué.
    historical = FALLBACK_MODULES
    if isinstance(shared, dict) and shared.get("active"):
        inventory = set(shared.get("inventory") or ())
        if inventory:
            historical = tuple(name for name in FALLBACK_MODULES
                               if name in inventory)
    #: #7823 : deux parts jouées dans l'ordre — le lot (ses tests, puis les
    #: importateurs de ses fichiers), puis les contrats. Chaque part rend son
    #: flux en finissant : un budget épuisé pendant les contrats garde la
    #: preuve du lot, et un rouge du lot est vu avant l'échéance.
    own_part: list[str] = []
    for name in (*sorted(changed_tests), *importers):
        if name not in own_part:
            own_part.append(name)
    contract_part: list[str] = []
    for name in (*required, *historical, *sorted(transverse)):
        if name not in contract_part and name not in own_part:
            contract_part.append(name)
    modules = own_part + contract_part
    if not modules:
        return None
    classes = tuple(getattr(block, "classes", ()) or ())
    rank = classes[0] if classes else "P1"
    extra = bool(transverse or changed_tests or importers)
    fallback = {
        "cause": chosen.causes[0],
        "causes": list(chosen.causes),
        "why": chosen.why,
        "contracts": list(required),
        "transverse": sorted(transverse),
        "changed_tests": sorted(changed_tests),
        "importers": list(importers),
        "importers_dropped": len(importers_skipped),
        "importers_skipped": list(importers_skipped),
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
    listed = [(label, names) for label, names in
              (("psycopg lot", own_part), ("psycopg contrats", contract_part))
              if names]
    plan["parts"] = {
        "psycopg": [{"part": index, "of": len(listed), "label": label,
                     "modules": list(names), "estimate_s": 0.0}
                    for index, (label, names) in enumerate(listed, 1)],
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

    Avec ``deadline`` (#4067), le passage — canon, carte, plan, run sur
    l'hôte, nettoyage — lit la même échéance : le job distant est annulé à
    l'échéance (``--wait-timeout``) et le geste rend ``run-incomplete``
    (« timeout ») dans le délai de l'appelant. #7161 : la prévalidation prend
    un créneau de la file heavy de l'hôte avant son travail lourd (plan,
    snapshot, run) ; le temps d'attente de ce créneau est suspendu du budget
    de travail, il n'est jamais un ``run-incomplete``, mais la commande rend
    « en file » avant la borne murale de l'appelant (``Deadline.wall``).
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
    recurrent = seen_elsewhere(db, lot, cfg=cfg, warn=warn)
    carte, cleanup = map_for(repo_slug, repo, explicit=map_path, deadline=deadline)
    prepared = None
    try:
        if deadline is not None and deadline.expired():
            raise _timeout_error(deadline, "avant la file heavy")
        #: #7161 : le plan, le snapshot et le run invité passent par la file
        #: des suites lourdes de l'hôte (`ameesh heavy`, L131). L'attente d'un
        #: créneau est suspendue de l'échéance : elle n'est jamais un
        #: `run-incomplete`, et le run garde le budget qu'il avait.
        with heavy_queue(cfg, "prévalidation #%d %s"
                         % (lot, (head or "?")[:12]),
                         say=say, deadline=deadline) as waited:
            if deadline is not None and deadline.expired():
                raise _timeout_error(deadline, "avant l'arbre du SHA")
            prepared = prepare(repo, head, base, map_path=carte,
                               plan_script=getattr(params.params, "plan_script", None)
                               or DEFAULT_PLAN_SCRIPT,
                               project=params.project, deadline=deadline)
            fallback = contract_fallback(prepared, params, head=head, base=base,
                                         deadline=deadline)
            if deadline is not None and deadline.expired():
                raise _timeout_error(deadline, "avant l'hôte")
            report = _run_hosts(prepared, params, lot=lot, head=head, base=base,
                                branch=branch, profile=profile, say=say,
                                executable=executable, validate=validate,
                                deadline=deadline, recurrent=recurrent)
            if waited and isinstance(report, dict):
                #: #7161 : la note du gel dit que la prévalidation a attendu son
                #: créneau ; la preuve ne le cache pas.
                report.setdefault("queue_wait_s", round(waited, 1))
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
    if db is not None and report.get("environment_recurrent"):
        cause_lots(cfg, db, report, project=project, warn=warn)
    return report


# --------------------------------------------------------------------------
# #7988 : le même test rouge sur plusieurs lots distincts
# --------------------------------------------------------------------------

#: source du lot de cause, un par (projet, module de test) : `work.add_or_note`
#: note les occurrences suivantes sur le lot ouvert au lieu d'en ouvrir un autre
CAUSE_SOURCE = "prevalidation-environment:%s:%s"


def _scope(cfg, item: dict | None) -> tuple[str, str] | None:
    """#7988 (B cx2/dev3) : le projet d'un lot et son dépôt **effectif**.

    Le dépôt se résout comme `ci.repo_of` : celui du ticket (`issue_ref`),
    sinon celui configuré pour le projet. `None` pour un lot illisible, sans
    projet, sans dépôt prouvé, ou porteur de paquet (il contient ses unités,
    leur rouge n'est pas indépendant du sien)."""
    if not item or packet_guard.is_packet_branch(str(item.get("branch") or "")):
        return None
    app = str(item.get("app") or "").strip()
    if not app:
        return None
    from . import plan_github
    parts = plan_github._ref_parts(item.get("issue_ref"))
    repo = parts[0] if parts and parts[0] else None
    if not repo and cfg is not None:
        try:
            repo = plan_github.settings_of(cfg).repo_for(app)
        except Exception:  # noqa: BLE001 - réglages illisibles : dépôt non prouvé
            repo = None
    if not repo:
        return None
    return app.casefold(), str(repo).strip().casefold()


def seen_elsewhere(db, lot: int, *, cfg=None, warn=None) -> dict:
    """#7988 : `{test: [lots]}` des tests rouges en prévalidation sur d'autres
    lots **du même projet et du même dépôt effectif** (`_scope`), hors
    porteurs de paquet, dans `prevalidation.RECURRENT_WINDOW_S`.

    `prevalidation_reports` est partagée par tous les projets : un test
    homonyme d'un autre projet ou dépôt ne dit rien de celui-ci. Un lot dont
    le dépôt n'est pas prouvé ne compte pas ; un porteur de paquet ou un lot
    courant sans dépôt prouvé est jugé seul. Une lecture impossible rend `{}` :
    chaque gel est alors jugé seul, comme avant, et rien n'est supposé vert."""
    if db is None or int(lot or 0) <= 0:
        return {}
    from . import storage

    def scope(number: int) -> tuple[str, str] | None:
        try:
            return _scope(cfg, work.get(db, number))
        except Exception:  # noqa: BLE001 - lot illisible : ne compte pas
            return None

    mine = scope(int(lot))
    if mine is None:
        return {}
    try:
        rows = storage.of(db).prevalidation.failures_since(
            int(lot), prevalidation.RECURRENT_WINDOW_S)
    except Exception as exc:  # noqa: BLE001 - lecture impossible : jugé seul
        if warn is not None:
            warn("prévalidation : rouges des autres lots illisibles — %s" % exc)
        return {}
    ignored = {other for other in {int(r.get("work_item_id") or 0)
                                   for r in rows or ()}
               if scope(other) != mine}
    return prevalidation.recurrent_failures(rows, int(lot), ignored=ignored)


def cause_lots(cfg, db, report: dict, *, project: str, warn=None) -> list[dict]:
    """#7988 : un lot de cause par module de test rouge sur plusieurs lots.

    Le canon §3 dit le test dépendant de l'environnement défaut du test,
    corrigé à la source : le lot le demande, et un lot déjà ouvert pour ce
    module reçoit l'occurrence en note. Une création impossible est signalée,
    jamais transformée en refus du gel."""
    lot = int(report.get("lot") or 0)
    head = str(report.get("sha") or "")[:12]
    by_module: dict[str, list[dict]] = {}
    for entry in report.get("environment_recurrent") or ():
        name = str(entry.get("test") or "")
        module = prevalidation.test_module(name) or name
        by_module.setdefault(module, []).append(entry)
    made: list[dict] = []
    for module, entries in sorted(by_module.items()):
        lots = sorted({lot} | {int(n) for e in entries for n in e.get("lots") or ()})
        cited = ", ".join("#%d" % n for n in lots)
        tests = "\n".join("- %s" % e.get("test") for e in entries)
        try:
            made.append(work.add_or_note(
                db, source=CAUSE_SOURCE % (project, module),
                occurrence="prévalidation invitée : %s encore rouge sur #%d (tête %s) ; "
                           "lots vus : %s" % (module, lot, head, cited),
                actor="ameesh", cfg=cfg, app=project, type="bug", priority=2,
                title="Prévalidation invitée : %s rouge sur %d lots distincts "
                      "(%s) — test dépendant de l'environnement"
                      % (module, len(lots), cited),
                body="Constat (#7988) : les mêmes tests sont rouges en "
                     "prévalidation invitée sur %d lots distincts (%s) ; le "
                     "rouge est classé environnement, non concluant (canon §3), "
                     "et ne retient plus ces gels.\n%s\n"
                     "Attendu : reproduire dans l'invité (#4044), nommer la "
                     "dépendance (HOME, UID, droits, binaire, minuterie…) et "
                     "corriger le test à la source ; l'exclure de l'invité "
                     "(`GUEST_EXCLUSIONS`) seulement si la dépendance est "
                     "légitime, raison écrite. Preuve : une prévalidation "
                     "invitée verte sur ces tests." % (len(lots), cited, tests),
                footprint=["tests/%s.py" % module] if module.startswith("test")
                else []))
        except Exception as exc:  # noqa: BLE001 - le lot de cause ne bloque rien
            if warn is not None:
                warn("prévalidation : lot de cause %s non ouvert — %s" % (module, exc))
    return made


def _run_hosts(prepared: Prepared, params: Parameters, *, lot: int, head: str,
               base: str, branch: str, profile: str, say, executable: str,
               validate, deadline: Deadline | None = None,
               recurrent: dict | None = None) -> dict:
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
                project=attempt.project, canon_repo=attempt.repo,
                canon_commit=attempt.commit, deadline=deadline,
                **({"recurrent": recurrent} if recurrent else {}))
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
#: #4304/#7740 : la ligne d'un manifeste de paquet-auto déclarant ses tests
#: locaux (`TESTS_PARALLEL_LINE`). #7740 : ils sont joués avant la poussée,
#: sur le préfixe composé de chaque unité ; l'ancienne formulation (#4304,
#: après la poussée, en parallèle de la CI) reste reconnue pour les
#: manifestes déjà poussés.
COMPOSER_PARALLEL_RE = re.compile(
    r"^- Tests locaux : (?:joués par le composeur sur le préfixe composé de "
    r"chaque unité, avant la poussée \(#7740\)|joués par le composeur après "
    r"la poussée, en parallèle de la CI complète \(#4304\))", re.MULTILINE)


def composer_tested(cfg, db, item, head: str, *, repo: str = "") -> dict | None:
    """#4136 : le véhicule a-t-il été testé en local par son composeur ?

    Rend ``{"manifest": chemin, "sha": tête, "tests": n}`` quand le lot porte
    une branche ``integration/**`` et que sa tête publie le manifeste de
    paquet-auto avec des tests locaux exécutés, sans échec — ou déclarés joués
    avant la poussée sur le préfixe de chaque unité (#7740,
    ``"parallel": True``, ``tests`` 0, clé gardée pour compatibilité) —, et
    que la tête est le commit de ce manifeste ; sinon ``None``.
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
    from . import packet_snapshot
    from .db import DbError
    try:
        receipt = packet_snapshot.lookup(db=db, checkout=where, head=head,
            branch=str(item.get("branch") or ""), package_id=int(item.get("id") or 0),
            target=item.get("branch_target") or None)
    except (packet_snapshot.SnapshotError, DbError):
        # An unreadable receipt proves nothing: the prevalidation remains due,
        # but a database outage must not break the freeze or the packet gate.
        return None
    if receipt:
        tests = receipt["payload"].get("tests") or {}
        if tests.get("status") == "parallel":
            return {"manifest": "packet-snapshot:%d" % receipt["id"], "sha": head,
                    "tests": 0, "parallel": True}
        if tests.get("status") != "success" or int(tests.get("count") or 0) <= 0:
            return None
        return {"manifest": "packet-snapshot:%d" % receipt["id"], "sha": head,
                "tests": int(tests["count"])}
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
    how = ("tested by its composer before the push"
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
    where = repository(cfg, db, item, head, explicit=repo, deadline=deadline)
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
        if exc.cause == "queued":
            #: #7161 : aucun créneau heavy dans le délai. Ce n'est pas un
            #: run-incomplete : la porte du paquet cite « en file » et
            #: laisse la CI complète rester la preuve finale.
            return {"state": "unavailable", "cause": "queued",
                    "why": "prévalidation en file : %s" % exc.why}
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
    résumé au journal. Un rouge, une sélection non prouvée, une indisponibilité
    ou un dépôt sans outillage (#8321) ne défont pas le gel : il est déjà
    écrit, et ce geste ne lève pas. Rend
    ``{"state": "green"|"red"|"unavailable"|"skipped", ...}``.

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


def freeze_line(outcome: dict | None) -> str:
    """#7823 : ce que la commande de gel affiche de sa prévalidation.

    Une prévalidation non concluante ne doit pas passer pour une absence de
    tests : le gel dit combien de modules prévus ont réellement tourné, et
    que les tests impactés du lot restent à jouer avant le paquet."""
    if not isinstance(outcome, dict):
        return ""
    state = str(outcome.get("state") or "")
    report = outcome.get("report") if isinstance(outcome.get("report"), dict) else {}
    ran = len(report.get("modules") or ())
    planned = len(report.get("planned") or ())
    played = (" (%d/%d module(s) joué(s))" % (ran, planned)) if planned else ""
    if state == "green":
        return "prevalidation: verte%s" % played
    if state == "red":
        return "prevalidation: ROUGE%s — %s" % (played, describe(report))
    if state == "unavailable":
        why = describe(report) if report else str(outcome.get("why") or "")
        return ("prevalidation: NON CONCLUANTE%s — les tests impactés du lot "
                "n'ont pas été prouvés avant le paquet ; à jouer localement "
                "(`ameesh work prevalidate`) : %s" % (played, why[:400]))
    if state in ("queued", "skipped"):
        return "prevalidation: %s — %s" % (
            "en file" if state == "queued" else "sautée",
            str(outcome.get("why") or "")[:300])
    return ""


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
    where = repository(cfg, db, item, head, explicit=repo, deadline=deadline)
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
        if exc.cause == "queued":
            #: #7161 : le gel attend un créneau heavy ; il est déjà écrit et
            #: n'est pas défait. « en file » n'est pas un run-incomplete.
            key = "%s en file" % JOURNAL_PREFIX
            _journal(db, lot, "%s: %s" % (key, exc.why), key=key)
            return {"state": "queued", "cause": "queued", "why": exc.why}
        if exc.cause == "tooling-missing":
            #: #8321 : un dépôt qui ne porte ni plan ni lanceur n'a rien à
            #: jouer : le gel le dit « sautée », jamais « non concluante », et
            #: ne renvoie pas vers `ameesh work prevalidate` pour un test qui
            #: n'existe pas.
            key = "%s skipped: %s" % (JOURNAL_PREFIX, exc.cause)
            _journal(db, lot, "%s — project not tooled: %s" % (key, exc.why),
                     key=key)
            return {"state": "skipped", "cause": exc.cause,
                    "why": "projet non outillé : %s" % exc.why}
        why = _timed_out(deadline, exc.why)
        key = "%s unavailable: %s" % (JOURNAL_PREFIX, exc.cause)
        text = "%s%s" % (key, (" — " + why) if why else "")
        _journal(db, lot, text, key=key)
        return {"state": "unavailable", "cause": exc.cause, "why": why}
    state = report_state(report)
    why = describe(report)
    wait = report.get("queue_wait_s")
    suffix = (" (en file %ss)" % wait) if wait else ""
    if state == "unavailable":
        why = _timed_out(deadline, why)
        cause = (report.get("causes") or ("run-incomplete",))[0]
        key = "%s unavailable: %s" % (JOURNAL_PREFIX, cause)
        fingerprint = ""
        if cause == "run-incomplete" and report.get("missing"):
            fingerprint = _modules_fingerprint(report.get("missing"))
            _journal(db, lot, "%s %s — tête %s: %s%s"
                     % (key, fingerprint, head[:12], why, suffix),
                     key=key, fingerprint=fingerprint)
        else:
            _journal(db, lot, "%s — tête %s: %s%s" % (key, head[:12], why, suffix),
                     key=key)
    else:
        _journal(db, lot, "%s %s %s: %s%s"
                 % (JOURNAL_PREFIX, state, head[:12], why, suffix))
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
                               plan_script=getattr(params.params, "plan_script", None)
                               or DEFAULT_PLAN_SCRIPT,
                               project=params.project,
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
