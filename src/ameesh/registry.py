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
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from .config import NAME_RE
from .db import Db

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: profondeur maximale d'une lignée d'éphémères suivie (au-delà : gouverné)
_LINEAGE_MAX = 32

AGENT_COLUMNS = """
    name, chantier, harness, host, cwd, session_id, status, status_text, model,
    budget_usd, spent_usd, turns, pending_prompt, current_prompt, last_error,
    lease_owner, lease_epoch,
    extract(epoch from lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from last_seen)::float8         as last_seen_ts,
    extract(epoch from last_turn_at)::float8      as last_turn_ts,
    responsible, team, provider, credential_mode, capabilities, canon_ref,
    ephemeral, created_by,
    extract(epoch from ephemeral_expires_at)::float8 as ephemeral_expires_ts
"""


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("nom d'agent invalide : %r" % (name,))
    return name


def _name_list(names: Sequence[str]) -> str:
    return ", ".join("'%s'" % _check_name(n).replace("'", "''") for n in names)


# --------------------------------------------------------------------------
# canon (§4.1, C2/C3) : condition de réclamation, partagée par claimable(),
# claim() et `ameesh attach`
# --------------------------------------------------------------------------

def _alias(alias: str) -> str:
    if not _ALIAS_RE.match(alias or ""):
        raise ValueError("alias SQL invalide : %r" % (alias,))
    return alias


def canon_governed_sql(alias: str = "agent_registry") -> str:
    """Expression SQL : l'agent de la ligne `alias` est-il gouverné par le canon ?

    Oui s'il vient du canon (`canon_ref` posé), ou si c'est un éphémère dont la
    lignée de créateurs (`created_by`, suivie de proche en proche) atteint un
    agent du canon. Fail closed : un éphémère dont la lignée est rompue
    (créateur absent) ou trop longue est tenu pour gouverné. Seuls les agents
    inscrits à la main (ni `canon_ref`, ni éphémère) et les éphémères issus
    d'eux ne le sont pas.
    """
    a = _alias(alias)
    return """(%(a)s.canon_ref IS NOT NULL
       OR (%(a)s.ephemeral AND NOT EXISTS (
           WITH RECURSIVE lignee (name, created_by, canon_ref, ephemeral, depth) AS (
               SELECT c.name, c.created_by, c.canon_ref, c.ephemeral, 1
                 FROM agent_registry c WHERE c.name = %(a)s.created_by
               UNION ALL
               SELECT c.name, c.created_by, c.canon_ref, c.ephemeral, l.depth + 1
                 FROM lignee l JOIN agent_registry c ON c.name = l.created_by
                WHERE l.ephemeral AND l.canon_ref IS NULL AND l.depth < %(max)d
           )
           SELECT 1 FROM lignee
            WHERE NOT lignee.ephemeral AND lignee.canon_ref IS NULL)))""" % {
        "a": a, "max": _LINEAGE_MAX}


def canon_claim_predicate_sql(alias: str = "agent_registry") -> str:
    """Condition SQL de réclamation liée au canon (§4.1), pour un `WHERE`.

    Vraie si l'agent de la ligne `alias` (une ligne de `agent_registry`, ou de
    la vue `agent_mesh_overview`) n'est pas gouverné par le canon
    (`canon_governed_sql`), ou si le dernier état du canon de **son hôte**
    (`canon_state`, écrit par `ameesh canon sync`) est `ok`. Un canon
    `invalid` ou `unreadable`, ou l'absence d'état, refuse donc toute
    NOUVELLE réclamation d'un agent du canon (et de ses éphémères), sans rien
    toucher aux baux, sessions ni tours en cours : fail closed.

    Indépendante de la configuration (AMEESH_CANON, AMEESH_REQUIRE_RESPONSIBLE) :
    un agent venu du canon sur un hôte où aucune synchronisation n'a constaté
    un canon valide n'est jamais réclamable. Sans paramètre lié (`%s`) : elle
    s'insère telle quelle dans une requête paramétrée, avec les deux pilotes.
    Utilisée par `claimable()` et `claim()` ; à réutiliser telle quelle pour
    `ameesh attach` (C9).
    """
    a = _alias(alias)
    return """(NOT %s
       OR EXISTS (SELECT 1 FROM canon_state s
                   WHERE s.host = %s.host AND s.status = 'ok'))""" % (
        canon_governed_sql(a), a)


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
    rows = db.query(
        """
        INSERT INTO agent_registry
            (name, chantier, harness, host, cwd, session_id, status, status_text,
             model, budget_usd, last_seen, updated_at)
        VALUES (%s, coalesce(%s,''), coalesce(%s,'other'), coalesce(%s,''), %s, %s,
                coalesce(%s,'idle'), coalesce(%s,''), %s, %s, now(), now())
        ON CONFLICT (name) DO UPDATE SET
            chantier    = coalesce(nullif(%s,''), agent_registry.chantier),
            harness     = coalesce(nullif(%s,''), agent_registry.harness),
            host        = coalesce(nullif(%s,''), agent_registry.host),
            cwd         = coalesce(excluded.cwd,        agent_registry.cwd),
            session_id  = coalesce(excluded.session_id, agent_registry.session_id),
            status      = coalesce(%s, agent_registry.status),
            status_text = coalesce(nullif(excluded.status_text,''), agent_registry.status_text),
            model       = coalesce(excluded.model,      agent_registry.model),
            budget_usd  = coalesce(excluded.budget_usd, agent_registry.budget_usd),
            last_seen   = now(),
            updated_at  = now()
        RETURNING __COLUMNS__
        """.replace("__COLUMNS__", AGENT_COLUMNS),
        (name, chantier, harness, host, cwd, session_id, status, status_text,
         model, budget_usd, chantier, harness, host, status),
    )
    return rows[0]


def get(db: Db, name: str) -> dict | None:
    rows = db.query("SELECT %s FROM agent_registry WHERE name = %%s" % AGENT_COLUMNS, (name,))
    return rows[0] if rows else None


def overview(db: Db) -> list[dict]:
    """Vue d'observabilité (`mesh list --json`).

    Colonnes du canon : `canon_governed` (l'agent relève du canon),
    `canon_status` / `canon_diagnostic` / `canon_checked_ts` (dernier état du
    canon de son hôte, NULL si aucun), `canon_claim_ok` (la condition de
    réclamation liée au canon est remplie).
    """
    return db.query(
        """
        SELECT name, chantier, harness, host, cwd, session_id, status, status_text,
               model, budget_usd, spent_usd, turns, pending_prompt, last_error,
               lease_owner, lease_epoch, lease_expires_ts, last_seen_ts, last_turn_ts,
               public_key_fingerprint, key_role, key_ready, has_owner_key, unread,
               responsible, team, provider, credential_mode, capabilities, canon_ref,
               ephemeral, created_by, ephemeral_expires_ts, responsible_ok,
               __GOVERNED__ AS canon_governed,
               (SELECT s.status FROM canon_state s WHERE s.host = o.host) AS canon_status,
               (SELECT s.diagnostic FROM canon_state s
                 WHERE s.host = o.host) AS canon_diagnostic,
               (SELECT extract(epoch from s.checked_at)::float8 FROM canon_state s
                 WHERE s.host = o.host) AS canon_checked_ts,
               __CLAIM_OK__ AS canon_claim_ok
        FROM agent_mesh_overview o
        ORDER BY last_seen_ts DESC NULLS LAST
        """.replace("__GOVERNED__", canon_governed_sql("o"))
           .replace("__CLAIM_OK__", canon_claim_predicate_sql("o"))
    )


def claim(db: Db, name: str, owner: str, ttl_seconds: float) -> dict | None:
    """Prend le bail s'il est libre ou expiré. Renvoie la ligne, sinon None.

    La condition du canon (`canon_claim_predicate_sql`) est revérifiée ici,
    atomiquement : un `canon sync` qui constate un canon invalide entre
    `claimable()` et `claim()` empêche la réclamation. Un bail déjà détenu
    n'est pas touché par ce refus : il se renouvelle (`renew`) et se rend
    (`release`) comme avant.
    """
    _check_name(name)
    rows = db.query(
        """
        UPDATE agent_registry
           SET lease_owner      = %s,
               lease_expires_at = now() + make_interval(secs => %s),
               lease_epoch      = lease_epoch + 1,
               -- R5 : si le bail n'était plus vivant (autre main, ou même
               -- exécuteur relancé avec le même identifiant), la consigne
               -- inachevée repart en attente. Si une consigne était déjà en
               -- attente, les deux sont conservées (B6b, B6c).
               pending_prompt   = CASE
                   WHEN current_prompt IS NOT NULL
                        AND (lease_owner IS DISTINCT FROM %s
                             OR lease_expires_at IS NULL
                             OR lease_expires_at < now())
                   THEN CASE
                       WHEN pending_prompt IS NULL THEN current_prompt
                       ELSE current_prompt || E'\n\n' || pending_prompt
                   END
                   ELSE pending_prompt END,
               current_prompt   = CASE
                   WHEN lease_owner IS DISTINCT FROM %s
                        OR lease_expires_at IS NULL
                        OR lease_expires_at < now()
                   THEN NULL
                   ELSE current_prompt END,
               status           = CASE WHEN status = 'dead' THEN 'idle' ELSE status END,
               updated_at       = now()
         WHERE name = %s
           AND (lease_owner IS NULL OR lease_owner = %s OR lease_expires_at < now())
           AND __CANON__
        RETURNING name, lease_owner, lease_epoch,
                  extract(epoch from lease_expires_at)::float8 as lease_expires_ts
        """.replace("__CANON__", canon_claim_predicate_sql("agent_registry")),
        (owner, ttl_seconds, owner, owner, name, owner),
    )
    return rows[0] if rows else None


def lease_matches(db: Db, name: str, owner: str, epoch: int) -> tuple[bool, str]:
    """Le bail est-il vivant et détenu par ce runner à cet epoch ?

    C'est ce qui lie une identité agent-mail à un bail (et non à un dossier) :
    une session dont le bail a expiré ou a changé de main ne peut plus
    consommer le courrier de l'agent.
    """
    rows = db.query(
        """
        SELECT lease_owner, lease_epoch, status,
               (lease_expires_at IS NOT NULL AND lease_expires_at > now()) AS live
          FROM agent_registry WHERE name = %s
        """,
        (name,),
    )
    if not rows:
        return False, "agent %s inconnu du registre" % name
    row = rows[0]
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
    """Prolonge le bail. None si on ne le détient plus (fencing par epoch)."""
    rows = db.query(
        """
        UPDATE agent_registry
           SET lease_expires_at = now() + make_interval(secs => %s),
               last_seen        = now(),
               updated_at       = now()
         WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
           AND lease_expires_at > now()
        RETURNING extract(epoch from lease_expires_at)::float8 as lease_expires_ts
        """,
        (ttl_seconds, name, owner, epoch),
    )
    return rows[0]["lease_expires_ts"] if rows else None


def release(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Rend le bail (arrêt propre). L'epoch avance : tout ancien détenteur est caduc."""
    rows = db.query(
        """
        UPDATE agent_registry
           SET lease_owner      = NULL,
               lease_expires_at = NULL,
               lease_epoch      = lease_epoch + 1,
               status           = CASE WHEN status = 'running' THEN 'idle' ELSE status END,
               updated_at       = now()
         WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
        RETURNING lease_epoch
        """,
        (name, owner, epoch),
    )
    return bool(rows)


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
    sql = """
        SELECT %s
        FROM agent_registry
        WHERE host = %%s
          AND status <> 'stopped'
          AND (lease_owner IS NULL OR lease_expires_at < now())
          AND (NOT ephemeral OR ephemeral_expires_at > now())
          AND %s
    """ % (AGENT_COLUMNS, canon_claim_predicate_sql("agent_registry"))
    if require_responsible:
        sql += " AND coalesce(responsible, '') <> ''"
    params: list[Any] = [host]
    if names:
        sql += " AND name IN (%s)" % _name_list(names)
    sql += " ORDER BY last_seen"
    return db.query(sql, tuple(params))


def reap(db: Db, host: str | None = None) -> list[dict]:
    """Marque morts les agents dont le bail a expiré en plein tour."""
    sql = """
        UPDATE agent_registry
           SET status = 'dead', status_text = 'bail expiré', updated_at = now()
         WHERE status = 'running' AND lease_expires_at < now()
    """
    params: tuple = ()
    if host is not None:
        sql += " AND host = %s"
        params = (host,)
    sql += " RETURNING name, lease_owner, lease_epoch"
    return db.query(sql, params)


def set_session(db: Db, name: str, session_id: str) -> None:
    db.execute(
        "UPDATE agent_registry SET session_id = %s, updated_at = now() WHERE name = %s",
        (session_id, name),
    )


def set_status(
    db: Db, name: str, status: str, status_text: str | None = None, error: str | None = None
) -> None:
    db.execute(
        """
        UPDATE agent_registry
           SET status = %s,
               status_text = coalesce(%s, status_text),
               last_error = %s,
               last_seen = now(),
               updated_at = now()
         WHERE name = %s
        """,
        (status, status_text, error, name),
    )


def set_pending_prompt(db: Db, name: str, prompt: str | None) -> None:
    db.execute(
        """
        UPDATE agent_registry
           SET pending_prompt = %s,
               status = CASE WHEN %s::text IS NULL THEN status ELSE 'queued' END,
               updated_at = now()
         WHERE name = %s
        """,
        (prompt, prompt, name),
    )


def take_pending_prompt(db: Db, name: str, owner: str, epoch: int) -> str | None:
    """Consomme la consigne en attente, atomiquement, et passe l'agent en `running`.

    Le `RETURNING` d'un UPDATE renvoie la *nouvelle* valeur (NULL) : d'où le
    CTE, qui capture l'ancienne valeur avant de l'effacer.
    """
    rows = db.query(
        """
        WITH taken AS (
            SELECT name, pending_prompt
              FROM agent_registry
             WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
               AND pending_prompt IS NOT NULL
               AND lease_expires_at > now()
             FOR UPDATE
        )
        UPDATE agent_registry AS r
           SET pending_prompt = NULL,
               current_prompt = taken.pending_prompt,
               status = 'running',
               status_text = 'tour en cours',
               updated_at = now()
          FROM taken
         WHERE r.name = taken.name
        RETURNING taken.pending_prompt
        """,
        (name, owner, epoch),
    )
    return rows[0]["pending_prompt"] if rows else None


def begin_turn(db: Db, name: str, owner: str, epoch: int, status_text: str) -> bool:
    rows = db.query(
        """
        UPDATE agent_registry
           SET status = 'running', status_text = %s, updated_at = now()
         WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
           AND lease_expires_at > now()
        RETURNING name
        """,
        (status_text, name, owner, epoch),
    )
    return bool(rows)


def restore_prompt(db: Db, name: str, owner: str, epoch: int) -> bool:
    """Remet en attente la consigne d'un tour qui n'a pas abouti.

    Si une consigne était déjà en attente, les deux sont conservées (l'ancienne
    d'abord) : un `coalesce` perderait la consigne du tour interrompu.
    """
    rows = db.query(
        """
        UPDATE agent_registry
           SET pending_prompt = CASE
                   WHEN current_prompt IS NULL THEN pending_prompt
                   WHEN pending_prompt IS NULL THEN current_prompt
                   ELSE current_prompt || E'\n\n' || pending_prompt
               END,
               current_prompt = NULL,
               updated_at = now()
         WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
           AND current_prompt IS NOT NULL
        RETURNING name
        """,
        (name, owner, epoch),
    )
    return bool(rows)


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
    rows = db.query(
        """
        UPDATE agent_registry
           SET status      = %s,
               status_text = coalesce(%s, status_text),
               last_error  = %s,
               current_prompt = NULL,
               turns       = turns + 1,
               spent_usd   = spent_usd + coalesce(%s, 0),
               last_turn_at = now(),
               last_seen   = now(),
               updated_at  = now()
         WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
        RETURNING name
        """,
        (status, status_text, error, cost_usd, name, owner, epoch),
    )
    return bool(rows)
