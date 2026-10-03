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

from typing import Any, Sequence

from .config import NAME_RE
from .db import Db

AGENT_COLUMNS = """
    name, chantier, harness, host, cwd, session_id, status, status_text, model,
    budget_usd, spent_usd, turns, pending_prompt, last_error,
    lease_owner, lease_epoch,
    extract(epoch from lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from last_seen)::float8         as last_seen_ts,
    extract(epoch from last_turn_at)::float8      as last_turn_ts
"""


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("nom d'agent invalide : %r" % (name,))
    return name


def _name_list(names: Sequence[str]) -> str:
    return ", ".join("'%s'" % _check_name(n).replace("'", "''") for n in names)


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
    return db.query(
        """
        SELECT name, chantier, harness, host, cwd, session_id, status, status_text,
               model, budget_usd, spent_usd, turns, pending_prompt, last_error,
               lease_owner, lease_epoch, lease_expires_ts, last_seen_ts, last_turn_ts,
               unread
        FROM agent_mesh_overview
        ORDER BY last_seen_ts DESC NULLS LAST
        """
    )


def claim(db: Db, name: str, owner: str, ttl_seconds: float) -> dict | None:
    """Prend le bail s'il est libre ou expiré. Renvoie la ligne, sinon None."""
    _check_name(name)
    rows = db.query(
        """
        UPDATE agent_registry
           SET lease_owner      = %s,
               lease_expires_at = now() + make_interval(secs => %s),
               lease_epoch      = lease_epoch + 1,
               status           = CASE WHEN status = 'dead' THEN 'idle' ELSE status END,
               updated_at       = now()
         WHERE name = %s
           AND (lease_owner IS NULL OR lease_owner = %s OR lease_expires_at < now())
        RETURNING name, lease_owner, lease_epoch,
                  extract(epoch from lease_expires_at)::float8 as lease_expires_ts
        """,
        (owner, ttl_seconds, name, owner),
    )
    return rows[0] if rows else None


def renew(db: Db, name: str, owner: str, epoch: int, ttl_seconds: float) -> float | None:
    """Prolonge le bail. None si on ne le détient plus (fencing par epoch)."""
    rows = db.query(
        """
        UPDATE agent_registry
           SET lease_expires_at = now() + make_interval(secs => %s),
               last_seen        = now(),
               updated_at       = now()
         WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
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


def claimable(db: Db, host: str, names: Sequence[str] | None = None) -> list[dict]:
    """Agents de cet hôte sans bail vivant (libres, expirés, ou morts)."""
    sql = """
        SELECT %s
        FROM agent_registry
        WHERE host = %%s
          AND status <> 'stopped'
          AND (lease_owner IS NULL OR lease_expires_at < now())
    """ % AGENT_COLUMNS
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
             FOR UPDATE
        )
        UPDATE agent_registry AS r
           SET pending_prompt = NULL,
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
        RETURNING name
        """,
        (status_text, name, owner, epoch),
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
