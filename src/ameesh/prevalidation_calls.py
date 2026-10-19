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
  module s'exécute) ; une indisponibilité est journalisée ;
* ``gate_train`` dans le passage du train, avant publication : un rouge
  arrête la publication, une indisponibilité est journalisée et la CI reste
  la preuve finale.

Les hôtes viennent du bloc canon ; un hôte **local déclaré au canon** est
admis (décision pilote 19/10 06:03, pour que les accroches tournent depuis
`ameesh-agents-1`), un poste local non déclaré reste refusé.

Le plan L161f n'est jamais deviné. Il est calculé par le
``scripts/test-plan.py`` du **commit candidat** (arbre détaché), avec la carte
mesurée fournie par l'appelant quand elle existe, sinon la dernière carte
publiée (artefact ``carte`` de GitHub, même lecture que la restauration
``tests.yml``). Sans carte fraîche, le plan retombe sur la suite complète et
``prevalidation.validate`` refuse ``selection-full`` / ``selection-unsafe`` :
aucun repli local, aucun test supposé ; l'appelant dit alors la prévalidation
indisponible. Le rapport passe par ``prevalidation.record`` (0450), après le
run distant et hors de toute transaction ; une table absente n'annule jamais
un run réel.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace

from . import canon as canon_mod
from . import lot_class, mechanics, plan_git, prevalidation, registry, work

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
#: #2644/N dev1 : la prévalidation du train tourne avant `_held()`, mais le
#: bail de la cible (land.LOCK_TTL_S, 1800 s par défaut) court déjà. Plan et
#: run restent sous 1500 s (300 + 1200) pour ne pas le laisser expirer.
TRAIN_PLAN_TIMEOUT = 300
TRAIN_RUN_TIMEOUT = 1200


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

    def close(self) -> None:
        try:
            subprocess.run(["git", "-C", self.repo, "worktree", "remove", "--force",
                            self.tree], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(self.root, ignore_errors=True)


# --------------------------------------------------------------------------
# les paramètres : le canon relu, jamais une valeur par défaut
# --------------------------------------------------------------------------

def parameters(cfg, project: str, *, canon_repo: str = "",
               canon_commit: str = "") -> Parameters:
    """Les paramètres de prévalidation du projet, au commit exact du canon.

    Un ``canon_repo``/``canon_commit`` explicite est lu tel quel (geste de
    reprise, banc) ; sinon le premier canon configuré qui porte un bloc
    ``prevalidation`` pour ce projet est retenu. Aucun fallback : un canon
    sans bloc, illisible ou sans commit exact refuse ``block-missing``.
    """
    if canon_repo and canon_commit:
        _, params = prevalidation.load_parameters(canon_repo, canon_commit, project)
        return Parameters(params=params, repo=canon_repo, commit=canon_commit,
                          project=project)
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        raise prevalidation.PrevalidationError(
            "block-missing", "aucun canon configuré : paramètres de prévalidation inconnus")
    try:
        canons = canon_mod.load_configured(cfg)
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
            if getattr(source, "mode", "") != "git" or not source.repo or not source.commit:
                errors.append("%s: source de canon sans commit exact"
                              % (getattr(canon, "root", "") or "canon"))
                continue
            if getattr(source, "prefix", ""):
                errors.append("%s: canon sous un sous-répertoire" % source.repo)
                continue
            try:
                _, params = prevalidation.load_parameters(source.repo, source.commit,
                                                          project)
            except prevalidation.PrevalidationError as exc:
                errors.append("%s@%s: %s" % (source.repo, str(source.commit)[:12], exc))
                continue
            except mechanics.MechanicsError as exc:
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
            runner=subprocess.run) -> tuple[str, str]:
    """La carte mesurée à passer au plan : explicite, locale, ou artefact carte.

    Rend ``(chemin, répertoire à nettoyer)`` ; ``("", "")`` quand aucune carte
    n'est disponible (le plan retombera sur la suite complète, et la
    prévalidation refusera). Un artefact de la dernière exécution carte verte
    est téléchargé dans un répertoire temporaire : la fraîcheur et l'ascendance
    de son commit restent vérifiées par ``scripts/test-plan.py``, jamais
    supposées ici.
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
    try:
        listed = runner(["gh", "run", "list", "--repo", slug, "--workflow",
                         CARTE_WORKFLOW, "--status", "success", "--limit", "1",
                         "--json", "databaseId"], capture_output=True, text=True,
                        timeout=30)
        if listed.returncode:
            return "", ""
        runs = json.loads(listed.stdout or "[]")
        ident = runs[0].get("databaseId") if runs else None
        if not ident:
            return "", ""
        tmp = tempfile.mkdtemp(prefix="ameesh-carte-")
        got = runner(["gh", "run", "download", str(ident), "--repo", slug, "-n",
                      CARTE_ARTIFACT, "-D", tmp], capture_output=True, text=True,
                     timeout=120)
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


def _free_name(repo: str, head: str) -> str:
    """Un nom de plan qui ne remplace aucun chemin du SHA candidat."""
    for index in range(0, 20):
        name = PLAN_NAME if index == 0 else "ameesh-prevalidation-plan-%d.json" % index
        proc = _run_git(repo, "cat-file", "-e", "%s:%s" % (head, name))
        if proc.returncode != 0:
            return name
    raise prevalidation.PrevalidationError(
        "selection-unsafe", "aucun nom libre pour le plan dans l'arbre du candidat")


def prepare(repo: str, head: str, base: str, *, map_path: str = "",
            timeout: int = PLAN_TIMEOUT) -> Prepared:
    """Construit un arbre détaché de ``head`` et y calcule le plan L161f.

    Le script ``scripts/test-plan.py`` lu est celui du commit candidat ; la
    carte mesurée est passée en lecture seule à ``--map`` quand l'appelant en
    fournit une. L'arbre reste vivant jusqu'à ``Prepared.close()`` : le plan
    doit rester dans l'arbre pour être embarqué dans la preuve.
    """
    full = str(head or "").strip().lower()
    if not plan_git._commit(repo, full):
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "la tête %s n'est pas un commit de %s"
            % ((full or "?")[:12], repo))
    base = str(base or "").strip().lower()
    if not base or not plan_git._commit(repo, base):
        raise prevalidation.PrevalidationError(
            "selection-unsafe", "base de comparaison inconnue de %s: %r"
            % (repo, (base or "—")[:12]))
    root = tempfile.mkdtemp(prefix="ameesh-prevalidation-")
    tree = os.path.join(root, "tree")
    try:
        proc = _run_git(repo, "worktree", "add", "--detach", tree, full)
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(root, ignore_errors=True)
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
            rel = _free_name(tree, full)
        except (OSError, subprocess.SubprocessError) as exc:
            raise prevalidation.PrevalidationError(
                "selection-unsafe", "nom du plan non vérifiable: %s: %s"
                % (type(exc).__name__, str(exc)[:200])) from None
        plan_path = os.path.join(tree, rel)
        argv = [sys.executable, os.path.join(tree, "scripts", "test-plan.py"), "plan",
                "--out", rel, "--event", "workflow_dispatch", "--ref", "",
                "--base", base, "--mode", "lot"]
        if map_path:
            argv += ["--map", os.path.abspath(map_path)]
        try:
            generated = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                                       timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
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
                        rel=rel, plan=plan)
    except BaseException:
        try:
            _run_git(repo, "worktree", "remove", "--force", tree)
        except Exception:  # noqa: BLE001 - nettoyage au mieux
            pass
        shutil.rmtree(root, ignore_errors=True)
        raise


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
        validate=None) -> dict:
    """Le geste complet : canon, plan du candidat, run sur les hôtes, rapport.

    Les hôtes du canon sont essayés dans l'ordre quand l'indisponibilité est
    de transport (`no-vm`, porte de l'hôte, connexion perdue), jamais après un
    rouge : un rouge est un résultat, pas une raison de changer de VM. Le
    rapport est persisté après le run, hors de toute transaction ; l'échec de
    cette écriture est signalé, jamais transformé en refus (le run réel reste
    la preuve).
    """
    params = parameters(cfg, project)
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
    carte, cleanup = map_for(repo_slug, repo, explicit=map_path)
    prepared = None
    try:
        prepared = prepare(repo, head, base, map_path=carte)
        report = _run_hosts(prepared, params, lot=lot, head=head, base=base,
                            branch=branch, profile=profile, say=say,
                            executable=executable, validate=validate)
    finally:
        if prepared is not None:
            prepared.close()
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
               validate) -> dict:
    """Les hôtes déclarés, dans l'ordre, sans repli local ni après un rouge."""
    check = validate or prevalidation.validate
    hosts = params.hosts
    if not hosts:
        raise prevalidation.PrevalidationError(
            "block-missing", "le bloc prevalidation du canon ne déclare aucun hôte")
    last: prevalidation.PrevalidationError | None = None
    for index, host in enumerate(hosts):
        last = None
        try:
            report = check(
                lot=lot, plan_path=prepared.path, head=head, host=host,
                params=params.params, base=base, repo=prepared.tree,
                published_ref=branch, profile=profile, executable=executable)
        except prevalidation.PrevalidationError as exc:
            if exc.cause in RETRYABLE and index + 1 < len(hosts):
                last = exc
                continue
            raise
        if not report.get("ok") and set(report.get("causes") or ()) & set(RETRYABLE) \
                and index + 1 < len(hosts):
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

def gate_packet(cfg, db, item, decision: dict, *, repo: str = "",
                map_path: str = "", say=None, warn=None) -> dict:
    """La prévalidation d'un porteur ``integration/**`` : rouge, ou état dit.

    Décision du pilote (19/10, #2644) : **seul un rouge bloque** ``ci run``.
    Une prévalidation indisponible — carte absente ou de plus de 48 h, hôte
    absent, canon sans bloc, sélection non prouvée, run incomplet — ne bloque
    jamais : l'appelant journalise le motif et pose l'étiquette. Rend donc
    toujours un état :

    * ``{"state": "green", "report": …}`` : la sélection a tourné et est verte ;
    * ``{"state": "red", "report": …}`` : un test a échoué, l'appelant refuse ;
    * ``{"state": "unavailable", "cause"/"why", "report"?}`` : rien de prouvé,
      l'appelant continue et cite le motif.

    L'exception déclarée au journal du paquet n'est pas lue ici : elle
    appartient à l'appelant (packet_guard), qui choisit de ne pas bloquer.
    """
    head = str(decision.get("sha") or "").strip().lower()
    item = item or {}
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
                     repo_slug=str(decision.get("repo") or ""), say=say, warn=warn)
    except prevalidation.PrevalidationError as exc:
        return {"state": "unavailable", "cause": exc.cause, "why": exc.why}
    except Exception as exc:  # noqa: BLE001 - une indisponibilité, jamais un vert
        return {"state": "unavailable", "cause": "selection-unsafe",
                "why": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
    state = report_state(report)
    if state == "green":
        return {"state": "green", "report": report}
    if state == "red":
        return {"state": "red", "report": report, "why": describe(report)}
    return {"state": "unavailable",
            "cause": (report.get("causes") or ("run-incomplete",))[0],
            "why": describe(report), "report": report}


# --------------------------------------------------------------------------
# (b) le gel : résultat attaché, jamais bloquant
# --------------------------------------------------------------------------

def _journal(db, lot: int, text: str) -> None:
    """Une note de journal, au mieux : le gel ne dépend jamais d'elle."""
    if db is None:
        return
    try:
        work.note(db, int(lot), text, actor="ameesh", once=True)
    except Exception:  # noqa: BLE001 - une note ne bloque pas un gel
        pass


def after_freeze(cfg, db, lot: int, sha: str, *, repo: str = "",
                 map_path: str = "", say=None, warn=None) -> dict:
    """La prévalidation du gel, après que le jalon ``frozen`` est écrit.

    Le résultat est attaché à la tête gelée dans ``prevalidation_reports`` et
    résumé au journal. Un rouge, une sélection non prouvée ou une
    indisponibilité ne défont pas le gel : il est déjà écrit, et ce geste ne
    lève pas. Rend ``{"state": "green"|"red"|"unavailable", ...}``.
    """
    try:
        return _after_freeze(cfg, db, lot, sha, repo=repo, map_path=map_path,
                             say=say, warn=warn)
    except Exception as exc:  # noqa: BLE001 - le gel est déjà écrit, rien ne le défait
        _journal(db, lot, "%s unavailable: %s: %s"
                 % (JOURNAL_PREFIX, type(exc).__name__, str(exc)[:300]))
        return {"state": "unavailable", "why": "%s: %s" % (type(exc).__name__, exc)}


def _repo_slug(cfg, item) -> str:
    """`owner/name` du lot pour l'artefact carte, ou ``""`` (pas de réseau)."""
    try:
        from . import ci
        return ci.repo_of(cfg, dict(item or {}), repo="") or ""
    except Exception:  # noqa: BLE001 - sans dépôt GitHub, pas d'artefact carte
        return ""


def _after_freeze(cfg, db, lot: int, sha: str, *, repo: str = "",
                  map_path: str = "", say=None, warn=None) -> dict:
    item = work.get(db, int(lot)) if db is not None else None
    if item is None:
        return {"state": "unavailable", "why": "lot inconnu"}
    head = str(sha or "").strip().lower()
    if not head:
        _journal(db, lot, "%s unavailable: gel sans --sha" % JOURNAL_PREFIX)
        return {"state": "unavailable", "why": "gel sans --sha"}
    where = repository(cfg, db, item, head, explicit=repo)
    if not where:
        _journal(db, lot, "%s unavailable: aucun arbre ne porte %s"
                 % (JOURNAL_PREFIX, head[:12]))
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
                     repo_slug=_repo_slug(cfg, item), say=say, warn=warn)
    except prevalidation.PrevalidationError as exc:
        text = "%s unavailable: %s%s" % (
            JOURNAL_PREFIX, exc.cause, (" — " + exc.why) if exc.why else "")
        _journal(db, lot, text)
        return {"state": "unavailable", "cause": exc.cause, "why": exc.why}
    state = report_state(report)
    text = "%s %s %s: %s" % (JOURNAL_PREFIX, state, head[:12], describe(report))
    _journal(db, lot, text)
    return {"state": state, "report": report}


# --------------------------------------------------------------------------
# (c) le train, avant sa publication
# --------------------------------------------------------------------------

def gate_train(cfg, *, repo: str, head: str, base: str, canon_repo: str = "",
               canon_commit: str = "", project: str = "ameesh",
               repo_slug: str = "", map_path: str = "",
               say=None, warn=None) -> dict:
    """La prévalidation de la composition, avant ``train_git.publish``.

    Un rouge lève ``PrevalidationError`` : rien n'est publié. Une
    indisponibilité (canon sans bloc, VM absente, plan non prouvé) est
    journalisée par l'appelant et ne bloque pas : la CI complète du paquet
    reste la preuve finale, et le train ne s'arrête pas sur une VM muette.
    Il n'y a pas de lot de paquet à ce stade : le rapport (lot 0) n'est pas
    persisté dans ``prevalidation_reports``.
    """
    if not repo:
        return {"state": "unavailable", "why": "dépôt du train inconnu"}
    try:
        params = parameters(cfg, project, canon_repo=canon_repo,
                            canon_commit=canon_commit)
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
        carte, cleanup = map_for(repo_slug, repo, explicit=map_path)
        prepared = None
        try:
            prepared = prepare(repo, head, base, map_path=carte,
                               timeout=TRAIN_PLAN_TIMEOUT)
            report = _run_hosts(prepared, params, lot=0, head=head, base=base,
                                branch="", profile="", say=say,
                                executable="ameesh", validate=None)
        finally:
            if prepared is not None:
                prepared.close()
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
    except prevalidation.PrevalidationError as exc:
        if exc.cause == "run-red":
            raise
        return {"state": "unavailable", "cause": exc.cause, "why": exc.why}
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
    return {"state": "green", "report": report}
