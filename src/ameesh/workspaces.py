# SPDX-License-Identifier: AGPL-3.0-only
"""L101 T2 — façade de l'espace de travail et du ménage (`policy.workspaces`).

Le runner ne connaît plus :mod:`ameesh.menage` ni :mod:`ameesh.housekeeping` :
il demande ici la politique de l'hôte, ouvre l'espace d'un tour, déclenche le
passage périodique, traduit une pression du tmpfs en consigne, et prépare
l'environnement d'une session `attach`. Les seuils, verrous et journaux
restent ceux de :mod:`ameesh.housekeeping` (L73, L162, L219) ; cette façade ne
porte aucune politique propre et ne change aucun comportement.

Les dossiers de travail et les worktrees de lot rejoignent la façade en
L101 T3 ; le module `dev` (L101 T4) la lira par le registre.
"""
from __future__ import annotations

from . import housekeeping

#: Politique de ménage d'un hôte : même objet que `housekeeping.Policy`.
Policy = housekeeping.Policy


def policy(canons, host: str) -> Policy:
    """Politique de `host` lue dans les canons (`workspaces.housekeeping`).

    Même calcul que `housekeeping.effective` : la première fiche qui déclare
    une clé l'emporte, les seuils gardent le plus strict ; l'alias
    `policy.housekeeping` a déjà été normalisé au parse par L101 T1."""
    return housekeeping.effective(canons, host)


def default_policy() -> Policy:
    """Politique avant qu'un canon soit lu (valeurs par défaut de L73)."""
    return housekeeping.Policy()


def before_turn(cfg, policy: Policy, agent: str, cwd: str | None, *,
                new_session: bool, base_env: dict):
    """Ouvre l'espace de l'agent pour un tour (L73, L219).

    Rend le `Turn` de `housekeeping.Turn.begin` : son `env` est posé sur le
    tour, et il doit être conclu par :func:`after_turn`, même si le tour
    échoue. `new_session` est décidé par l'appelant — une suite de rotation,
    un travail de fond encore vivant ou un résident gardent le dossier
    (#2610, #2619)."""
    return housekeeping.Turn.begin(cfg, policy, agent, cwd,
                                   new_session=new_session, base_env=base_env)


def after_turn(turn, *, lot: str | None, turn_id: str, db, host: str,
               runtime=None) -> list:
    """Conclut l'espace du tour (L73, L162, L219).

    Quota du dossier temporaire, worktrees apparus, entrées de /tmp
    signalées, puis les conteneurs étiquetés par ce tour. Rend les entrées du
    journal, que l'appelant écrit (`storage.of(db).housekeeping.log`)."""
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
                  free_target: int = 0, artifact_scan: bool = True) -> dict:
    """Un passage d'hôte (L73), partagé par les exécuteurs et la CLI.

    Mêmes verrous (`host-pass`, intervalle du passage) et mêmes seuils que
    `housekeeping.run_pass` : la façade ne fait que nommer l'entrée."""
    return housekeeping.run_pass(
        cfg, db, policy, host=host, actor=actor, busy=busy,
        agents_in_turn=agents_in_turn, runtime=runtime,
        min_interval_s=min_interval_s, free_target=free_target,
        artifact_scan=artifact_scan)


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
    préparation ne bloque jamais la session."""
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
