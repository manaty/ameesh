# SPDX-License-Identifier: AGPL-3.0-only
"""L101 — façade de l'espace de travail et du ménage (`policy.workspaces`).

Le runner ne connaît plus :mod:`ameesh.menage` ni :mod:`ameesh.housekeeping` :
il demande ici la politique de l'hôte, ouvre l'espace d'un tour, déclenche le
passage périodique, traduit une pression du tmpfs en consigne, et prépare
l'environnement d'une session `attach`. Les seuils, verrous et journaux
restent ceux de :mod:`ameesh.housekeeping` (L73, L162, L219) ; cette façade ne
porte aucune politique propre et ne change aucun comportement.

L101 T3 : le dossier de travail d'un lot et les worktrees de lot vivent ici
aussi. Le runner garde l'état de délai (backoff) et l'appel ; la façade porte
la résolution (:func:`resolve`, :func:`primary_workdir`), l'adoption bornée
d'un worktree déplacé (0018, :func:`adopt_moved_worktree`), le différé par lot
(#811, :func:`lot_deferral`, :func:`lot_wait`) et la règle partagée avec le
hook (:func:`mail_hold`).

L101 T4 : la façade est portée par le module intégré `dev` (capacité
`workspaces`) et le cœur l'obtient par le registre, jamais par un import de
l'implémentation (L97) : :func:`dev_module`. Au profil nu (`builtin=False`),
le module est absent : le ménage et les worktrees sont des no-ops documentés —
un tour éteint, aucune adoption ni résolution secondaire, aucun hold, rien
d'écrit dans le journal.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import NamedTuple

from . import canon, housekeeping, lot_workdir, projects, registry

#: Politique de ménage d'un hôte : même objet que `housekeeping.Policy`.
Policy = housekeeping.Policy


def dev_module():
    """Le module `dev` qui porte la capacité `workspaces` (L101 T4), ou None.

    Rendu par le registre (`registry.get("dev")`), jamais importé directement
    (L97). Au profil nu (`_builtin()` vide), il est absent : les fonctions de
    cette façade passent alors en no-op documenté."""
    from .modules import registry as modules_registry
    return modules_registry.get("dev")


def policy(canons, host: str) -> Policy:
    """Politique de `host` lue dans les canons (`workspaces.housekeeping`).

    Même calcul que `housekeeping.effective` : la première fiche qui déclare
    une clé l'emporte, les seuils gardent le plus strict ; l'alias
    `policy.housekeeping` a déjà été normalisé au parse par L101 T1."""
    return housekeeping.effective(canons, host)


def default_policy() -> Policy:
    """Politique avant qu'un canon soit lu (valeurs par défaut de L73)."""
    return housekeeping.Policy()


# -- dossier de travail d'un lot (L101 T3, L243-4, L35, #811) ---------------

class Workdir(NamedTuple):
    """Dossier de travail résolu pour un lot.

    `cwd` est le dossier utilisable, ou None ; `secondary` dit que l'échec ne
    concerne que ce lot (projet secondaire déclaré, #811 B1) et jamais le
    blocage global « dossier absent » (L35) ; `error` porte le diagnostic, et
    `path` le dossier calculé même absent — le dernier chemin connu dont le
    retour lève le différé sans attendre son échéance (#811)."""
    cwd: str | None = None
    secondary: bool = False
    error: str = ""
    path: str | None = None


class LotWait(NamedTuple):
    """État d'attente d'un lot secondaire sans dossier (#811 B1)."""
    waiting: bool          # le lot attend encore son dossier
    back: bool             # le dernier chemin connu est revenu


#: profondeur maximale de recherche d'un dossier de travail déplacé (0018) :
#: `~/development/manaty/ameesh/.claude/worktrees/<nom>` est à cinq niveaux.
WORKTREE_DEPTH = 6
#: dossiers cachés qu'on traverse quand même (worktrees imbriqués)
WORKTREE_HIDDEN = (".claude", ".worktrees")
#: répertoires jamais traversés (dépendances, caches, sorties de build)
WORKTREE_SKIP = ("node_modules", ".git", "venv", ".venv", "target", "build",
                 "dist", "__pycache__", ".cache", ".local")

#: #811 (B1) : dossier d'un lot secondaire absent — délai par lot (borné),
#: l'agent gardant ses autres tours. L'état (échéance, chemin connu) reste
#: dans l'exécuteur, qui garde l'appel.
LOT_WORKDIR_BACKOFF_MIN = 30.0
LOT_WORKDIR_BACKOFF_MAX = 600.0


def worktree_marker(cwd: str | None) -> dict:
    """Identité git d'un dossier de travail (0018 : suivre un renommage).

    `git_common_dir` est le dépôt commun (le même pour tous les worktrees) ;
    la branche distingue les worktrees d'un même dépôt. Best-effort : renvoie
    `{}` si le dossier n'est pas un dépôt ou si git manque, et au profil nu
    (pas de module `dev`) : les worktrees y sont un no-op.
    """
    if dev_module() is None:
        return {}
    if not cwd or not os.path.isdir(cwd):
        return {}
    try:
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5)
        branche = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if common.returncode != 0 or not common.stdout.strip():
        return {}
    return {
        "cwd": os.path.abspath(cwd),
        "git_common_dir": os.path.abspath(os.path.join(cwd, common.stdout.strip())),
        "branch": branche.stdout.strip() if branche.returncode == 0 else "",
    }


def write_worktree_marker(cfg, name: str, cwd: str) -> dict:
    """Écrit le marqueur du dossier de travail dans l'état de l'agent."""
    if dev_module() is None:
        return {}
    marker = worktree_marker(cwd)
    if not marker:
        return {}
    directory = cfg.agent_dir(name)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(os.path.join(directory, "worktree.json"), "w", encoding="utf-8") as fh:
            json.dump(marker, fh, ensure_ascii=False)
    except OSError:
        return {}
    return marker


def _worktree_dirs(racine: str, ancien: str | None):
    """Dossiers candidats sous une racine, profondeur bornée (0018 B4).

    On ne descend pas dans les grands répertoires de dépendances ni dans
    `.git`, mais on traverse `.claude` / `.worktrees` : les worktrees
    imbriqués (ex. `~/development/manaty/ameesh/.claude/worktrees/<nom>`)
    doivent être trouvés, ou l'adoption refuse proprement.
    """
    base = racine.rstrip(os.sep).count(os.sep)
    for dossier, sous_dirs, _ in os.walk(racine):
        sous_dirs[:] = [d for d in sous_dirs
                        if d not in WORKTREE_SKIP
                        and (not d.startswith(".") or d in WORKTREE_HIDDEN)]
        if dossier.count(os.sep) - base >= WORKTREE_DEPTH:
            sous_dirs[:] = []
        if dossier != ancien and os.path.exists(os.path.join(dossier, ".git")):
            yield dossier


def adopt_moved_worktree(db, agent: dict, *, marker_path: str,
                         roots, report=None) -> str | None:
    """Retrouve un dossier de travail déplacé, borné aux racines (0018).

    Le marqueur git (common dir + branche) est écrit à l'inscription ; si le
    cwd a disparu, on cherche un candidat **unique** sous les racines
    configurées, en profondeur bornée, et on refuse un dossier déjà utilisé
    par un autre agent. `report` reçoit les diagnostics (l'appelant les
    journalise) ; le registre et `agent['cwd']` sont mis à jour sur succès.
    Au profil nu (pas de module `dev`), l'adoption est un no-op."""
    if dev_module() is None:
        return None
    try:
        with open(marker_path, encoding="utf-8") as fh:
            marqueur = json.load(fh) or {}
    except (OSError, ValueError):
        return None
    common = marqueur.get("git_common_dir")
    if not common:
        return None
    branche = marqueur.get("branch") or ""
    ancien = agent.get("cwd")
    candidats = []
    for racine in roots:
        if not os.path.isdir(racine):
            continue
        for chemin in _worktree_dirs(racine, ancien):
            ident = worktree_marker(chemin)
            if not ident or ident.get("git_common_dir") != common:
                continue
            if branche and ident.get("branch") != branche:
                continue
            candidats.append(chemin)
    if len(candidats) != 1:
        if len(candidats) > 1 and report is not None:
            report("moved directory ambiguous: %s" % ", ".join(candidats))
        return None
    candidat = candidats[0]
    if registry.cwd_used(db, candidat, agent["name"]):
        if report is not None:
            report("moved directory %s already used by another agent" % candidat)
        return None
    registry.upsert(db, agent["name"], cwd=candidat)
    agent["cwd"] = candidat
    if report is not None:
        report("moved working directory adopted: %s" % candidat)
    return candidat


def primary_workdir(agent: dict, *, mediated: bool = False,
                    mediated_workdir=None, adopt_worktree=None) -> str | None:
    """Le dossier primaire (cwd du registre, adoption bornée ; dépôt de
    l'exécuteur médié) — jamais un projet secondaire : la garde L35 ne se
    lève que sur le dossier qu'elle a vu manquer (#811 B1).

    `mediated_workdir` et `adopt_worktree` sont les fournisseurs de l'appelant
    quand ce dossier n'est pas un simple `cwd` : le dépôt d'un exécuteur médié
    (L113) et l'adoption d'un worktree déplacé (0018). Au profil nu, le
    dossier primaire est rendu tel quel et l'adoption est un no-op."""
    if mediated:
        return mediated_workdir() if mediated_workdir is not None else None
    cwd = agent.get("cwd")
    if cwd and os.path.isdir(cwd):
        return cwd
    if dev_module() is None:
        return None
    return adopt_worktree() if adopt_worktree is not None else None


def resolve(cfg, db, agent: dict, lot, *, mediated: bool = False,
            adopt_worktree=None, mediated_workdir=None, canons=None) -> Workdir:
    """Le dossier de travail utilisable pour `lot`, ou la raison de son absence.

    Le primaire garde le `cwd` du registre relu par `pick()` et l'adoption
    bornée d'un worktree déplacé (0018). Un lot SECONDaire DÉCLARÉ (projet de
    l'agent autre que le primaire) utilise le dossier de son projet au Host,
    sans modifier le registre ; quand ce dossier manque, l'échec ne concerne
    que ce lot (`secondary`), jamais le blocage global « dossier absent » (L35)
    réservé au cwd primaire. Un lot d'un projet NON déclaré par l'agent, ou un
    lot inconnu, tourne dans le cwd primaire comme avant L243-4 (#811 B1).
    Au profil nu (pas de module `dev`), seul le primaire est résolu : aucun
    dossier de projet secondaire, aucune adoption de worktree.
    """
    if dev_module() is None:
        return Workdir(primary_workdir(
            agent, mediated=mediated, mediated_workdir=mediated_workdir))
    names = projects.agent_projects(agent)
    if len(names) > 1:  # N3 : aucun accès base pour un agent mono-projet
        try:
            project = lot_workdir.project(db, lot)
        except (lot_workdir.WorkdirError, canon.CanonError):
            project = ""  # lot inconnu : cwd primaire, comme avant L243-4
        if project and project != names[0] and project in names[1:]:
            if mediated:
                return Workdir(None, True,
                               "mediated executor has no per-project depot")
            try:
                cwd = lot_workdir.destination(cfg, agent, project, canons=canons)
            except (lot_workdir.WorkdirError, canon.CanonError) as exc:
                return Workdir(None, True, "project %s: %s" % (project, exc))
            error = "project %s: %r" % (project, cwd)
            return Workdir(cwd if cwd and os.path.isdir(cwd) else None,
                           True, error, cwd)
    return Workdir(primary_workdir(
        agent, mediated=mediated, mediated_workdir=mediated_workdir,
        adopt_worktree=adopt_worktree))


def lot_deferral(current: float | None, *, now: float | None = None
                 ) -> tuple[float, float]:
    """Le prochain délai d'un lot sans dossier et son échéance (#811 B1).

    `current` est le délai en cours (None au premier échec) : 30 s, puis 60 s,
    120 s… borné à 10 min. L'état du délai reste dans l'exécuteur."""
    delay = (LOT_WORKDIR_BACKOFF_MIN if current is None
             else min(LOT_WORKDIR_BACKOFF_MAX, current * 2))
    if now is None:
        now = time.monotonic()
    return delay, now + delay


def lot_wait(path: str | None, until: float, *,
             now: float | None = None) -> LotWait:
    """Ce lot attend-il encore son dossier ? (#811 B1)

    Le dernier chemin connu est revérifié à chaque appel : un dossier recréé
    lève le différé tout de suite (pas d'attente de l'échéance)."""
    if path and os.path.isdir(path):
        return LotWait(False, True)
    if now is None:
        now = time.monotonic()
    return LotWait(until > now, False)


def mail_hold(cfg, db, agent: dict, lot, *, canons=None) -> bool:
    """Le courrier de ce lot attend-il son dossier ? (#811 B1)

    Règle PARTAGÉE entre l'exécuteur et le hook de harnais : un message dont
    l'unique lot vise un projet DÉCLARÉ autre que le primaire, dont le dossier
    n'est pas utilisable, ne se remet pas dans la session d'un autre lot — le
    hook le marquerait livré et l'exécuteur ne le verrait jamais pour ce lot.
    Faux pour le projet primaire, un projet non déclaré, un lot inconnu ou un
    dossier prêt : ces messages suivent le flux normal. Un canon illisible ne
    retient rien (flux normal), comme `problem` le fait pour son propre repli.
    Au profil nu (pas de module `dev`), le courrier suit toujours le flux."""
    if dev_module() is None:
        return False
    names = projects.agent_projects(agent)
    if len(names) < 2:                # mono-projet : pas de dossier secondaire
        return False
    try:
        lot_project = lot_workdir.project(db, lot)
    except (lot_workdir.WorkdirError, canon.CanonError):
        return False                  # lot inconnu : flux normal
    if not lot_project or lot_project == names[0]:
        return False                  # projet primaire : flux normal
    try:
        canons = canon.load_configured(cfg) if canons is None else canons
    except canon.CanonError:
        return False
    return bool(lot_workdir.problem(cfg, agent, lot_project, canons=canons))


def before_turn(cfg, policy: Policy, agent: str, cwd: str | None, *,
                new_session: bool, base_env: dict):
    """Ouvre l'espace de l'agent pour un tour (L73, L219).

    Rend le `Turn` de `housekeeping.Turn.begin` : son `env` est posé sur le
    tour, et il doit être conclu par :func:`after_turn`, même si le tour
    échoue. `new_session` est décidé par l'appelant — une suite de rotation,
    un travail de fond encore vivant ou un résident gardent le dossier
    (#2610, #2619). Au profil nu (pas de module `dev`), le ménage est un
    no-op : un tour éteint (`off=True`), sans dossier temporaire ni cache
    touchés ; le tour part avec l'environnement d'origine."""
    if dev_module() is None:
        return housekeeping.Turn(cfg=cfg, policy=policy, agent=agent, cwd=cwd,
                                 off=True)
    return housekeeping.Turn.begin(cfg, policy, agent, cwd,
                                   new_session=new_session, base_env=base_env)


def after_turn(turn, *, lot: str | None, turn_id: str, db, host: str,
               runtime=None) -> list:
    """Conclut l'espace du tour (L73, L162, L219).

    Quota du dossier temporaire, worktrees apparus, entrées de /tmp
    signalées, puis les conteneurs étiquetés par ce tour. Rend les entrées du
    journal, que l'appelant écrit (`storage.of(db).housekeeping.log`). Au
    profil nu (pas de module `dev`), rien n'est conclu : aucune entrée."""
    if dev_module() is None or turn is None:
        return []
    entries = turn.end(lot=lot, turn_id=turn_id, db=db, host=host)
    if not turn.off:
        # conteneurs étiquetés par CE tour, sans lot : supprimés tout de
        # suite (ceux d'un lot attendent sa fin, au passage périodique)
        entries += housekeeping.reap_containers(
            runtime, db, host, only_turn=turn_id)["entries"]
    return entries


def periodic_pass(cfg, db, policy: Policy, *, host: str, actor: str,
                  busy: bool = False, agents_in_turn: set | None = None,
                  runtime=None, min_interval_s: float = 0.0,
                  free_target: int = 0, artifact_scan: bool = True,
                  units_dir: str | None = None) -> dict:
    """Un passage d'hôte (L73), partagé par les exécuteurs et la CLI.

    Mêmes verrous (`host-pass`, intervalle du passage) et mêmes seuils que
    `housekeeping.run_pass` : la façade ne fait que nommer l'entrée. Au profil
    nu (pas de module `dev`), le passage est un no-op : aucune entrée."""
    if dev_module() is None:
        return {"status": "no-dev-module", "entries": []}
    # #4754 : le dossier des unités n'est passé que s'il est nommé
    extra = {"units_dir": units_dir} if units_dir else {}
    return housekeeping.run_pass(
        cfg, db, policy, host=host, actor=actor, busy=busy,
        agents_in_turn=agents_in_turn, runtime=runtime,
        min_interval_s=min_interval_s, free_target=free_target,
        artifact_scan=artifact_scan, **extra)


def host_units_dir() -> str | None:
    """#4754 : le dossier des unités de veille de l'hôte, que le passage
    périodique re-épingle (None hors systemd)."""
    return housekeeping.host_units_dir()


def tmpfs_pressure(top, policy: Policy) -> str:
    """Consigne jointe à une alerte de tmpfs (L162) : plus grosses entrées de
    /tmp et action attendue."""
    return housekeeping.tmpfs_hint(top, policy)


def attach_env(cfg, name: str, base_env: dict, holds: list) -> dict:
    """Environnement d'une session `ameesh attach` (L162, L219).

    Lit la politique de l'hôte sur place (canons), verrouille le dossier
    temporaire de l'agent jusqu'à la fin de la session (le verrou est ajouté
    à `holds`) et rend l'environnement à poser. Une erreur est rendue à
    l'appelant, qui journalise et garde l'environnement d'origine : la
    préparation ne bloque jamais la session. Au profil nu (pas de module
    `dev`), aucune préparation : l'environnement d'origine est rendu."""
    if dev_module() is None:
        return {}
    from . import canon as canon_mod
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:
        canons = []
    host_policy = policy(canons, cfg.host or "")
    if not host_policy.redirect or housekeeping.disabled(base_env):
        return {}
    hold = housekeeping.hold_tmp(cfg, host_policy, name, new_session=False)
    holds.append(hold)
    return housekeeping.turn_env(cfg, host_policy, name, base_env, hold=hold)
