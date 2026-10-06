# SPDX-License-Identifier: AGPL-3.0-only
"""`agent_registry` : présence des agents et baux (R3, R5).

Un agent est épinglé à un hôte (v1 : les sessions des harnais sont des fichiers
locaux). Le bail est la seule autorisation de tourner :

* `claim()` est atomique : un `UPDATE ... WHERE` sous READ COMMITTED, donc deux
  exécuteurs concurrents ne peuvent pas gagner le même agent ;
* `renew()` et `end_turn()` sont *fencés* par `lease_epoch` : un exécuteur dont
  le bail a expiré ne peut ni le prolonger, ni écrire un résultat ;
* un bail expiré est reprenable par n'importe quel autre exécuteur de l'hôte
  (`lease_expires_at < now()`), donc la mort d'un exécuteur ne bloque rien.

Le SQL est dans le stockage (`storage.of(db).agents` / `.leases`, spec §10) ;
ce module garde les règles et les signatures publiques.
"""
from __future__ import annotations

from typing import Sequence

from . import storage
from .config import NAME_RE
from .db import Db
from .storage.postgres import registry as _pg

#: profondeur maximale d'une lignée d'éphémères suivie (au-delà : gouverné)
_LINEAGE_MAX = _pg.LINEAGE_MAX

#: colonnes rendues pour un agent (pilote Postgres ; alias de compatibilité)
AGENT_COLUMNS = _pg.AGENT_COLUMNS

#: condition de réclamation liée au canon (§4.1) et appartenance au canon :
#: expressions SQL du pilote Postgres, gardées ici sous leur nom historique
#: (`registry.canon_claim_predicate_sql`, cité par les migrations 0008, 0022
#: et 0024) — voir `storage.postgres.registry`
canon_governed_sql = _pg.canon_governed_sql
canon_claim_predicate_sql = _pg.canon_claim_predicate_sql


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("nom d'agent invalide : %r" % (name,))
    return name


def _responsible_required(db: Db) -> bool:
    cfg = getattr(db, "cfg", None)
    return bool(cfg is not None and getattr(cfg, "responsible_required", False))


def upsert(
    db: Db,
    name: str,
    *,
    chantier: str | None = None,
    harness: str | None = None,
    host: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    status_text: str | None = None,
    model: str | None = None,
    budget_usd: float | None = None,
) -> dict:
    """Crée l'agent ou met à jour ce qui est fourni (les autres champs restent)."""
    _check_name(name)
    return storage.of(db).agents.upsert(
        name, chantier=chantier, harness=harness, host=host, cwd=cwd,
        session_id=session_id, status=status, status_text=status_text, model=model,
        budget_usd=budget_usd)


def get(db: Db, name: str) -> dict | None:
    return storage.of(db).agents.get(name)


def overview(db: Db) -> list[dict]:
    """Vue d'observabilité (`mesh list --json`).

    Colonnes du canon : `canon_governed` (l'agent relève du canon),
    `canon_status` / `canon_diagnostic` / `canon_checked_ts` (dernier état du
    canon de son hôte, NULL si aucun), `canon_claim_ok` (la condition de
    réclamation liée au canon est remplie).
    """
    return storage.of(db).agents.overview()


def claim(db: Db, name: str, owner: str, ttl_seconds: float) -> dict | None:
    """Prend le bail s'il est libre ou expiré. Renvoie la ligne, sinon None.

    Le verrou de ligne est pris **d'abord** (`SELECT ... FOR UPDATE`), puis les
    conditions sont recontrôlées avec `clock_timestamp()` dans l'instruction qui
    écrit : un verrou attendu au-delà de l'échéance refuse la réclamation, même
    si le détenteur n'a fait qu'un `SELECT ... FOR UPDATE` sans modifier la ligne
    (EvalPlanQual ne réévalue pas un `WHERE` sans nouvelle version — grille
    codex3).

    `claim` applique **les mêmes règles que `claimable`** — éphémère échu,
    responsable résolu quand il est requis, prédicat du canon — dans la même
    instruction : la course `claimable` → `claim` est fermée.
    """
    _check_name(name)
    return storage.of(db).leases.claim(
        name, owner, ttl_seconds, require_responsible=_responsible_required(db))


def lease_matches(db: Db, name: str, owner: str, epoch: int) -> tuple[bool, str]:
    """Le bail est-il vivant et détenu par ce runner à cet epoch ?

    C'est ce qui lie une identité agent-mail à un bail (et non à un dossier) :
    une session dont le bail a expiré ou a changé de main ne peut plus
    consommer le courrier de l'agent.
    """
    row = storage.of(db).leases.state(name)
    if row is None:
        return False, "agent %s inconnu du registre" % name
    if row.get("status") == "stopped":
        return False, "agent arrêté"
    if (row.get("lease_owner") or "") != owner:
        return False, "bail détenu par %s" % (row.get("lease_owner") or "personne")
    if int(row.get("lease_epoch") or 0) != int(epoch):
        return False, "epoch %s au registre, %s dans la session" % (row.get("lease_epoch"), epoch)
    if not row.get("live"):
        return False, "bail expiré"
    return True, ""


def renew(db: Db, name: str, owner: str, epoch: int, ttl_seconds: float) -> float | None:
    """Prolonge le bail. None si on ne le détient plus (fencing par epoch).

    Verrou pris d'abord, échéance recontrôlée avec `clock_timestamp()` dans
    l'écriture : un renouvellement qui a attendu le verrou au-delà de
    l'expiration est refusé (grille codex3).
    """
    return storage.of(db).leases.renew(name, owner, epoch, ttl_seconds)


def release(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Rend le bail (arrêt propre). L'epoch avance : tout ancien détenteur est caduc."""
    return storage.of(db).leases.release(name, owner, epoch)


def turn_in_progress(db: Db, name: str) -> bool:
    """Un tour (ou une session attach) est-il vivant ? (verdict L8 B1)

    `current_prompt` ne suffit pas : un tour de courrier, d'événement ou de
    reprise ne le remplit pas. Le marqueur fiable est `status = 'running'` avec
    un bail vivant — `begin_turn` le pose pour tout tour, `attach` pour sa
    session, `end_turn` / `release` le retirent.
    """
    return storage.of(db).leases.turn_in_progress(name)


def attach_claim(db: Db, name: str, owner: str, ttl_seconds: float) -> dict | None:
    """Prend le bail pour une session interactive (`ameesh attach`, C9).

    Contrairement à `claim`, un bail vivant mais **sans tour ni session en
    cours** peut être repris : c'est ce qui suspend la réclamation automatique
    (l'ancien exécuteur est fencé par l'epoch à son prochain renouvellement).
    Un tour vivant — courrier, événement, reprise ou attach déjà ouvert — n'est
    jamais interrompu (verdict L8 B1 : `status = 'running'` + bail vivant, et
    non `current_prompt`). Mêmes règles que `claimable` : éphémère échu,
    responsable résolu quand il est requis, prédicat du canon ; verrou pris
    d'abord et échéances recontrôlées avec `clock_timestamp()`.
    """
    _check_name(name)
    return storage.of(db).leases.attach_claim(
        name, owner, ttl_seconds, require_responsible=_responsible_required(db))


def mark_event_wake(db: Db, name: str) -> None:
    """Note l'instant du dernier réveil d'événements (regroupement C9).

    Cet état vit en base (et non dans l'exécuteur) : un redémarrage ne remet
    pas à zéro la fenêtre de regroupement.
    """
    storage.of(db).agents.mark_event_wake(name)


def claimable(db: Db, host: str, names: Sequence[str] | None = None, *,
              require_responsible: bool | None = None) -> list[dict]:
    """Agents de cet hôte sans bail vivant (libres, expirés, ou morts).

    R14 (C3) : un éphémère échu n'est jamais réclamable ; et quand le
    responsable est requis (`AMEESH_REQUIRE_RESPONSIBLE`, vrai par défaut dès
    qu'un canon est configuré), un agent sans humain responsable résolu non
    plus. `canon sync` n'écrit `responsible` que résolu ; un éphémère
    l'hérite de son créateur.

    §4.1 (toujours, quel que soit le réglage) : un agent gouverné par le canon
    n'est réclamable que si le dernier état du canon de son hôte est `ok`
    (`canon_claim_predicate_sql`).
    """
    if require_responsible is None:
        cfg = getattr(db, "cfg", None)
        require_responsible = bool(cfg is not None and cfg.responsible_required)
    return storage.of(db).agents.claimable(host, names,
                                           require_responsible=require_responsible)


def reap(db: Db, host: str | None = None) -> list[dict]:
    """Marque morts les agents dont le bail a expiré en plein tour."""
    return storage.of(db).leases.reap(host)


def set_session(db: Db, name: str, session_id: str) -> None:
    storage.of(db).agents.set_session(name, session_id)


def clear_session(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Oublie la session (rotation, 0018), **fencé par un bail valide**.

    Renvoie False si le bail n'est plus le nôtre ou s'il a expiré : un
    remplaçant a pu réclamer l'agent et ouvrir sa propre session, qu'il ne faut
    pas effacer (verdicts codex3 L11 B3). Le verrou est pris d'abord et
    l'échéance est recontrôlée avec `clock_timestamp()` dans l'écriture, comme
    les autres opérations de bail.
    """
    return storage.of(db).leases.clear_session(name, owner, epoch)


def pause(db: Db, name: str, owner: str, epoch: int, status_text: str) -> bool:
    """Met l'agent en pause sous un bail VIVANT détenu par ce worker (L31).

    Faux si le bail n'est plus le nôtre, s'il a expiré (un remplaçant a pu
    réclamer la ligne et lancer son tour), ou si un tour est en cours : une
    pause ne coupe jamais un tour (0028). Verrou pris d'abord, conditions
    recontrôlées dans l'écriture."""
    return storage.of(db).leases.pause(name, owner, epoch, status_text)


def pending_spend_put(db: Db, name: str, start_index: int, turn: str | None = None,
                      model: str | None = None) -> bool:
    """Pose (atomiquement) le marqueur comptable d'un tour : une seule ligne."""
    return storage.of(db).pending_spend.put(name, start_index, turn, model)


def pending_spend_set_model(db: Db, name: str, model: str | None) -> bool:
    """Remplace le modèle du marqueur par celui **annoncé par le flux** (L13 B4).

    L'annonce doit vivre en base dès qu'elle arrive : si l'écriture comptable
    échoue et qu'un **autre** worker répare, il doit facturer le modèle
    réellement utilisé, pas celui du lancement. Faux si aucun marqueur n'existe
    (rien n'a été mis à jour) : l'appelant le traite comme un échec.
    """
    return storage.of(db).pending_spend.set_model(name, model)


def pending_spend_get(db: Db, name: str) -> dict | None:
    """Le marqueur comptable en attente, ou None (aucun travail suspendu)."""
    row = storage.of(db).pending_spend.get(name)
    if row is None:
        return None
    return {
        "agent": row["agent"],
        "start": int(row["start_index"]),
        "turn": row.get("turn") or "",
        "model": row.get("model"),
        "created_ts": row.get("created_ts"),
    }


def pending_spend_clear(db: Db, name: str) -> bool:
    return storage.of(db).pending_spend.clear(name)


def cwd_used(db: Db, cwd: str, exclude: str) -> bool:
    """Ce dossier de travail est-il déjà celui d'un autre agent ? (0018)"""
    return storage.of(db).agents.cwd_used(cwd, exclude)


def set_status(
    db: Db, name: str, status: str, status_text: str | None = None, error: str | None = None
) -> None:
    storage.of(db).agents.set_status(name, status, status_text, error)


def set_pending_prompt(db: Db, name: str, prompt: str | None) -> None:
    storage.of(db).agents.set_pending_prompt(name, prompt)


def take_pending_prompt(db: Db, name: str, owner: str, epoch: int) -> str | None:
    """Consomme la consigne en attente, atomiquement, et passe l'agent en `running`.

    Verrou pris d'abord, échéance recontrôlée avec `clock_timestamp()` dans
    l'écriture (grille codex3) : une consigne n'est pas consommée sous un bail
    expiré pendant l'attente du verrou.
    """
    return storage.of(db).leases.take_pending_prompt(name, owner, epoch)


def begin_turn(db: Db, name: str, owner: str, epoch: int, status_text: str) -> bool:
    return storage.of(db).leases.begin_turn(name, owner, epoch, status_text)


def restore_prompt(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Remet en attente la consigne d'un tour qui n'a pas abouti.

    Si une consigne était déjà en attente, les deux sont conservées (l'ancienne
    d'abord) : un `coalesce` perderait la consigne du tour interrompu.
    """
    return storage.of(db).leases.restore_prompt(name, owner, epoch)


def end_turn(
    db: Db,
    name: str,
    owner: str,
    epoch: int,
    *,
    status: str = "idle",
    status_text: str | None = None,
    error: str | None = None,
    cost_usd: float | None = None,
) -> bool:
    """Clôt un tour *seulement si le bail est encore détenu* (fencing)."""
    return storage.of(db).leases.end_turn(name, owner, epoch, status=status,
                                          status_text=status_text, error=error,
                                          cost_usd=cost_usd)
