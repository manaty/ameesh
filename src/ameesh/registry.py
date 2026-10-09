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

import sys

from typing import Sequence

from . import persona_sessions, storage
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

#: modes d'agent (L37, décision 0030) : `execute` = mené par un exécuteur,
#: sous bail ; `externe` = session humaine, boîte seulement, non réveillable
MODES = ("execute", "externe")
#: raisons d'arrêt structurées (`stop_reason`, L37) ; NULL tant que l'agent tourne
STOP_REASONS = ("manuel", "bail_expire", "retire_du_canon", "externe", "erreur", "fin_de_lot")


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
    mode: str | None = None,
) -> dict:
    """Crée l'agent ou met à jour ce qui est fourni (les autres champs restent).

    `mode` (L37, 0030) ne s'applique qu'à la CRÉATION : un hook sans bail crée
    un agent `externe`, mais n'écrase jamais le mode d'un agent existant
    (`set_mode` / `ameesh set <agent> mode=…` pour le changer)."""
    _check_name(name)
    if mode is not None and mode not in MODES:
        raise ValueError("mode d'agent inconnu : %r (%s)" % (mode, " | ".join(MODES)))
    return storage.of(db).agents.upsert(
        name, chantier=chantier, harness=harness, host=host, cwd=cwd,
        session_id=session_id, status=status, status_text=status_text, model=model,
        budget_usd=budget_usd, mode=mode)


def upsert_unleased(db: Db, name: str, *, chantier: str | None = None,
                    harness: str | None = None, host: str | None = None,
                    cwd: str | None = None, session_id: str | None = None) -> dict:
    """L46 : inscription par un hook SANS bail (session externe, shell qui a
    hérité de AGENT_MAIL_NAME).

    * agent inconnu : il naît `externe` (L37, 0030) avec ces champs ;
    * agent `execute` existant : SEUL `last_seen` avance — jamais l'hôte, le
      harnais, la session, le dossier ni le chantier (une session étrangère ne
      doit pas se faire reprendre par l'exécuteur, ni le déplacer d'hôte) ;
    * agent `externe` existant : hôte, harnais et chantier suivent la session
      humaine ; session et dossier ne sont écrits que s'il n'y en avait pas
      encore (L36).

    Une seule instruction : pas de course entre la lecture du mode et
    l'écriture."""
    _check_name(name)
    return storage.of(db).agents.upsert_unleased(
        name, chantier=chantier, harness=harness, host=host, cwd=cwd,
        session_id=session_id)


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

    L46 : un agent `externe` n'est jamais réclamé (ni par `claimable`, ni par
    `claim`), même avec une session et du courrier en attente.

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

    L46 : seule différence voulue avec `claimable` / `claim`, un agent
    `externe` PEUT être attaché — `attach` ouvre une session humaine
    interactive, ce qu'est précisément un agent externe. Le bail d'attach ne
    change pas son mode : rendu, l'agent reste hors de portée de l'exécuteur.
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


def set_session(db: Db, name: str, session_id: str, account: str | None = None) -> None:
    """Enregistre la session et, L39 (0030), le compte sous lequel elle tourne
    (`session_account` ; None : aucun compte déclaré pour le harnais)."""
    storage.of(db).agents.set_session(name, session_id, account)
    _historique(lambda: persona_sessions.record_start(db, name, session_id, account=account))


def set_session_account(db: Db, name: str, account: str | None) -> bool:
    """Le compte de la session courante change sans changer de session : reprise
    portable sous un autre compte (L39). Faux sans session enregistrée."""
    return storage.of(db).agents.set_session_account(name, account)


def clear_session(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Oublie la session (rotation, 0018), **fencé par un bail valide**.

    Renvoie False si le bail n'est plus le nôtre ou s'il a expiré : un
    remplaçant a pu réclamer l'agent et ouvrir sa propre session, qu'il ne faut
    pas effacer (verdicts codex3 L11 B3). Le verrou est pris d'abord et
    l'échéance est recontrôlée avec `clock_timestamp()` dans l'écriture, comme
    les autres opérations de bail.
    """
    rows = db.query("SELECT session_id FROM agent_registry WHERE name = %s", (name,))
    ok = storage.of(db).leases.clear_session(name, owner, epoch)
    if ok and rows and rows[0].get("session_id"):
        _historique(lambda: persona_sessions.record_end(db, name, rows[0]["session_id"],
                                                        "rotation"))
    return ok


def _historique(action) -> None:
    """L52 : l'historique des sessions ne bloque jamais l'agent (base en retard
    de migration comprise)."""
    try:
        action()
    except Exception as exc:  # noqa: BLE001
        print("ameesh : historique des sessions non tenu : %s" % exc, file=sys.stderr)


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
    db: Db, name: str, status: str, status_text: str | None = None, error: str | None = None,
    *, stop_reason: str | None = None,
) -> None:
    """Pose le statut. `stop_reason` (L37, 0030) dit pourquoi un agent passe
    `stopped`/`dead` ; il est effacé pour tout autre statut (déclencheur de la
    migration 0030), donc dès que l'agent repart."""
    if stop_reason is not None and stop_reason not in STOP_REASONS:
        raise ValueError("raison d'arrêt inconnue : %r (%s)"
                         % (stop_reason, " | ".join(STOP_REASONS)))
    storage.of(db).agents.set_status(name, status, status_text, error, stop_reason)


def set_mode(db: Db, name: str, mode: str) -> bool:
    """Change le mode d'un agent (`ameesh set <agent> mode=…`, L37)."""
    if mode not in MODES:
        raise ValueError("mode d'agent inconnu : %r (%s)" % (mode, " | ".join(MODES)))
    return storage.of(db).agents.set_mode(name, mode)


def wake_status(db: Db, cfg, name: str) -> tuple[bool, str, str]:
    """`wakeable`, plus une remarque non bloquante (L46) : `(ok, raison,
    remarque)`. La remarque dit qu'un agent réveillable n'est pas réclamable
    À L'INSTANT parce que le canon de son hôte n'est pas `ok`."""
    row = storage.of(db).agents.wake_check(name)
    if row is None:
        return False, "%s n'est pas dans le registre (agent inconnu)" % name, ""
    if (row.get("mode") or "execute") == "externe":
        return False, ("%s est un agent externe (session humaine, non réveillable)%s"
                       % (name, "" if row.get("responsible")
                          else " et n'a pas de responsable humain")), ""
    if not row.get("alive"):
        return False, "%s est un éphémère échu" % name, ""
    if cfg is None:
        cfg = getattr(db, "cfg", None)
    if cfg is not None and getattr(cfg, "responsible_required", False) \
            and not (row.get("responsible") or "").strip():
        return False, "%s n'a pas de responsable humain (requis avec un canon)" % name, ""
    if not row.get("placement_admitted"):
        if row.get("placement_ok") is True:
            why = "profil divergé depuis l'évaluation (prochaine canon sync)"
        else:
            why = row.get("placement_diagnostic") or (
                "placement non évalué" if row.get("placement_ok") is None
                else "placement refusé")
        return False, ("%s n'est pas admis sur son hôte %s (%s)"
                       % (name, row.get("host") or "?", why)), ""
    note = ""
    if not row.get("canon_claim_ok"):
        note = ("canon de l'hôte %s momentanément %s : %s ne sera réclamé qu'à la "
                "prochaine synchronisation valide" % (
                    row.get("host") or "?", row.get("canon_status") or "sans état", name))
    return True, "", note


def wakeable(db: Db, cfg, name: str) -> tuple[bool, str]:
    """L'agent est-il réveillable par ameesh ? (règle 2 de 0030, L37)

    Rend `(vrai, "")` ou `(faux, raison)`. Réveillable : connu du registre, en
    mode `execute`, éphémère non échu, et ADMIS sur son hôte (placement admis
    pour son profil actuel, `placement_admitted_sql`). Quand le responsable
    est requis (`cfg.responsible_required`, vrai dès qu'un canon est
    configuré), il faut aussi un humain responsable. Un agent arrêté
    (`stopped`) reste réveillable au sens de cette règle : l'arrêt est un
    geste humain réversible, que l'appelant peut signaler.

    L46 : l'ÉTAT du canon de l'hôte n'en fait plus partie — un canon
    momentanément invalide ne doit pas bloquer une attribution ; la
    réclamation, elle, reste fermée tant qu'il n'est pas `ok` (`claim`).
    `wake_status` rend en plus la remarque correspondante.
    """
    ok, why, _note = wake_status(db, cfg, name)
    return ok, why


def set_pending_prompt(db: Db, name: str, prompt: str | None) -> None:
    storage.of(db).agents.set_pending_prompt(name, prompt)


def set_marked_block(db: Db, name: str, owner: str, epoch: int, status_text: str,
                     error: str, error_prefix: str) -> str:
    """Blocage marqué fencé par le bail (L35) : "done" | "kept" | "lease"."""
    return storage.of(db).leases.set_marked_block(name, owner, epoch, status_text,
                                                  error, error_prefix)


def clear_marked_block(db: Db, name: str, owner: str, epoch: int, status_text: str,
                       error_prefix: str) -> str:
    """Levée du blocage marqué fencée par le bail (L35) : "done" | "kept" | "lease"."""
    return storage.of(db).leases.clear_marked_block(name, owner, epoch, status_text,
                                                    error_prefix)


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
