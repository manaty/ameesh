# SPDX-License-Identifier: AGPL-3.0-only
"""Répartiteur de `/op` et `/session/op` (serveur de l'exécuteur médié, L108).

`PgDispatcher` implémente `interfaces.ExecDispatcher` sur le pilote Postgres
existant (`storage.of`). Pour une requête :

1. `Contract.validate_request` : opération de la table, bonne route,
   liaison des arguments, enveloppe de bail, `Idempotency-Key` d'une
   écriture ;
2. le genre de jeton doit être celui de la route (accès sur `/op`, session
   sur `/session/op`) ;
3. UNE transaction, sur une connexion du réservoir :

   * écriture : verrou consultatif sur (exécuteur, clé), puis relecture de
     `exec_idempotency` : même empreinte → réponse rejouée
     (`Idempotency-Replayed: true`) ; autre empreinte → 409 ;
   * portée (`scope.HostScopeRules.apply`), lue dans la transaction ;
   * fencing, recontrôlé sous `SELECT … FOR UPDATE` sur la ligne de
     l'agent :
       - portée B : `lease_owner` = owner, `lease_epoch` = epoch, échéance
         `clock_timestamp()`, hôte de l'exécuteur, owner préfixé par
         `exec:<executor_id>:` ; sinon la valeur de refus de la ligne et
         `fenced: true` (200, pas une erreur) ;
       - écriture de portée S : le bail de l'agent du jeton est toujours
         celui de l'epoch du jeton, tenu par cet exécuteur, vivant, sur cet
         hôte ; sinon 401 `token_expired` (le jeton de session meurt avec le
         bail) ;
     les écritures que l'interface ne fence pas (`agents.set_status`,
     `pending_spend.*`, `turn_resources.*`, `threads.index`…) le sont donc
     ici, dans la même transaction que l'écriture ;
   * appel du pilote, dans la transaction ;
   * réponse d'idempotence et journal d'audit, dans la transaction ;

4. `ScopeRules.filter_result` (rendu restreint), dans la transaction.

Les refus (portée, liaison, 409…) sont journalisés dans une transaction à
part, la transaction de l'opération étant annulée.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import logging
import queue
import re
import threading
import time
from decimal import Decimal
from collections.abc import Mapping
from typing import Any, Callable, Iterator, Optional

from .. import jcs, storage
from ..db import DbError, Unavailable
from . import contract
from .contract import Contract, Fence, NotSupportedRemotely, OpRequest, OpResult, Operation
from .interfaces import ExecDispatcher, Principal, ScopeError
from .scope import DEFAULT_LEASE_TTL_S, HostScopeRules, Immediate

log = logging.getLogger("ameesh.exec")

#: forme admise d'une `Idempotency-Key` (UUID conseillé)
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
#: conservation du journal d'audit (jours)
AUDIT_RETENTION_DAYS = 90
#: indices d'une erreur de base due aux arguments (classes SQLSTATE 22, 23)
_BAD_INPUT_HINTS = ("invalid input", "violates", "out of range", "invalid byte sequence",
                    "value too long", "malformed", "viole", "invalide")


class ConnectionPool:
    """Réservoir de connexions de `ameesh.db` : une connexion par requête en
    cours, au plus `size`. Une connexion qui a levé `Unavailable` est
    fermée et remplacée au prochain emprunt."""

    def __init__(self, connect: Callable[[], Any], size: int = 8):
        self._connect = connect
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(max(1, size))

    @contextlib.contextmanager
    def connection(self, timeout: float = 10.0) -> Iterator[Any]:
        if not self._slots.acquire(timeout=timeout):
            raise Unavailable("réservoir de connexions épuisé")
        conn = None
        try:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = self._connect()
            yield conn
        except Unavailable:
            if conn is not None:
                _close(conn)
                conn = None
            raise
        finally:
            if conn is not None:
                self._idle.put(conn)
            self._slots.release()

    def close(self) -> None:
        while True:
            try:
                _close(self._idle.get_nowait())
            except queue.Empty:
                return


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def jsonable(value: Any) -> Any:
    """La valeur de l'interface en JSON (instants déjà en `*_ts` flottants)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    return value



class _Refusal(Exception):
    """Refus de la requête : (code d'erreur du contrat, message)."""

    def __init__(self, code: str, message: str = "", *, retry_after: Optional[float] = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or contract.ERRORS[code].meaning
        self.retry_after = retry_after


def error_response(code: str, message: str, *, retry_after: Optional[float] = None
                   ) -> tuple[int, dict, dict]:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after) if float(retry_after).is_integer()
                                     else retry_after)
    return (contract.ERRORS[code].status,
            contract.error_body(code, message, retry_after=retry_after), headers)


# --------------------------------------------------------------------------
# fencing, réutilisé par la route `/session-token`
# --------------------------------------------------------------------------

def _lease_row(db, name: str) -> Optional[dict]:
    rows = db.query(
        "SELECT lease_owner, lease_epoch, host,"
        "       (lease_expires_at IS NOT NULL"
        "        AND lease_expires_at > clock_timestamp()) AS live"
        "  FROM agent_registry WHERE name = %s FOR UPDATE",
        (name,))
    return rows[0] if rows else None


def fence_holds(db, principal: Principal, fence: Fence) -> bool:
    """Le bail de l'enveloppe est-il vivant, détenu par cet exécuteur, sur
    son hôte ? À appeler DANS la transaction de l'écriture : la ligne de
    l'agent reste verrouillée jusqu'à la fin de la transaction."""
    row = _lease_row(db, fence.agent)
    return bool(row and row["live"]
                and row["lease_owner"] == fence.owner
                and int(row["lease_epoch"]) == fence.epoch
                and row["host"] == principal.host
                and fence.owner.startswith("exec:%s:" % principal.executor_id))


def session_lease_holds(db, principal: Principal) -> bool:
    """Le bail lié au jeton de session tient-il encore (même epoch, même
    exécuteur, vivant, même hôte) ? Sous verrou de ligne."""
    if not principal.agent or principal.epoch is None:
        return False
    row = _lease_row(db, principal.agent)
    return bool(row and row["live"]
                and int(row["lease_epoch"]) == int(principal.epoch)
                and str(row["lease_owner"] or "").startswith("exec:%s:" % principal.executor_id)
                and row["host"] == principal.host)


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def audit(db, principal: Optional[Principal], *, op: str, route: str, status: int,
          error: Optional[str] = None, agent: Optional[str] = None, fenced: bool = False,
          replayed: bool = False, idempotency_key: Optional[str] = None) -> None:
    db.query(
        "INSERT INTO exec_audit (executor_id, host, principal, agent, op, route, status,"
        "                        error, fenced, replayed, idempotency_key)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (principal.executor_id if principal else "", principal.host if principal else "",
         principal.kind if principal else "none", agent, op[:120], route[:40], int(status),
         error, bool(fenced), bool(replayed), idempotency_key))


def purge(db, *, now_s: Optional[float] = None) -> None:
    """Élague les clés d'idempotence de plus de 24 h et l'audit trop ancien."""
    db.execute("DELETE FROM exec_idempotency WHERE at < now() - make_interval(secs => %s)",
               (float(contract.IDEMPOTENCY_RETENTION_S),))
    db.execute("DELETE FROM exec_audit WHERE at < now() - make_interval(days => %s)",
               (AUDIT_RETENTION_DAYS,))


# --------------------------------------------------------------------------
# le répartiteur
# --------------------------------------------------------------------------

class PgDispatcher(ExecDispatcher):
    """Répartiteur sur le pilote Postgres. `pool` : `ConnectionPool`."""

    def __init__(self, pool: ConnectionPool, *, contract: Optional[Contract] = None,
                 credential_modes: Optional[frozenset] = None,
                 lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
                 clock: Callable[[], float] = time.time):
        self.pool = pool
        self.contract = contract or contract.load()
        self.credential_modes = credential_modes
        self.lease_ttl_s = lease_ttl_s
        self.clock = clock

    def rules(self, db) -> HostScopeRules:
        return HostScopeRules(db, credential_modes=self.credential_modes,
                              lease_ttl_s=self.lease_ttl_s)

    def dispatch(self, principal: Principal, request: OpRequest, *, route: str,
                 idempotency_key: Optional[str]) -> tuple[int, dict, dict]:
        agent = request.fence.agent if request.fence else principal.agent
        try:
            return self._dispatch(principal, request, route, idempotency_key)
        except _Refusal as exc:
            self._audit_refusal(principal, request, route, exc.code, agent, idempotency_key)
            return error_response(exc.code, exc.message, retry_after=exc.retry_after)

    # -- étapes ---------------------------------------------------------------
    def _dispatch(self, principal, request, route, key):
        try:
            op = self.contract.get(request.op)
            bound = self.contract.validate_request(request, route=route, idempotency_key=key)
        except NotSupportedRemotely:
            raise _Refusal("op_not_allowed", "opération hors de la table, ou mauvaise route")
        except ValueError as exc:
            code = str(exc) if str(exc) in contract.ERRORS else "bad_args"
            raise _Refusal(code, "requête refusée : %s" % code)
        expected = "session" if route == "session/op" else "executor"
        if principal.kind != expected:
            raise _Refusal("token_invalid", "jeton %s attendu sur %s" % (expected, route))
        if op.write and not IDEMPOTENCY_KEY_RE.match(key or ""):
            raise _Refusal("bad_request", "Idempotency-Key mal formée")
        digest = request_digest(request.to_json()) if op.write else None
        try:
            with self.pool.connection() as conn:
                with conn.transaction() as tx:
                    return self._in_transaction(tx, principal, op, request, bound, route,
                                                key, digest)
        except _Refusal:
            raise
        except ScopeError as exc:
            code = exc.code if exc.code in contract.ERRORS else "forbidden_scope"
            raise _Refusal(code, str(exc))
        except Unavailable as exc:
            log.warning("base indisponible : %s", exc)
            raise _Refusal("unavailable", "base indisponible", retry_after=5)
        except (ValueError, TypeError):
            raise _Refusal("bad_args", "arguments refusés par l'opération")
        except DbError as exc:
            text = str(exc).lower()
            if any(hint in text for hint in _BAD_INPUT_HINTS):
                raise _Refusal("bad_args", "arguments refusés par la base")
            log.error("erreur de base sur %s : %s", request.op, str(exc)[:300])
            raise _Refusal("internal", "erreur du serveur")
        except Exception:
            log.exception("erreur interne sur %s", request.op)
            raise _Refusal("internal", "erreur du serveur")

    def _in_transaction(self, tx, principal: Principal, op: Operation, request: OpRequest,
                        bound: dict, route: str, key: Optional[str],
                        digest: Optional[str]) -> tuple[int, dict, dict]:
        if op.write:
            replay = self._idempotency(tx, principal, op, key, digest)
            if replay is not None:
                audit(tx, principal, op=op.name, route=route, status=replay[0],
                      agent=request.fence.agent if request.fence else principal.agent,
                      replayed=True, idempotency_key=key)
                return replay
        rules = self.rules(tx)
        fenced = False
        try:
            kwargs = rules.apply(principal, op, bound, request.fence)
            if op.fence:
                fenced = not fence_holds(tx, principal, request.fence)
            elif op.write and op.transport == "session/op":
                if not session_lease_holds(tx, principal):
                    raise _Refusal("token_expired", "jeton de session mort : bail perdu")
            if fenced:
                value = op.refusal
            else:
                domain = getattr(storage.of(tx), op.domain)
                value = getattr(domain, op.method)(**kwargs)
                value = rules.filter_result(principal, op, value)
        except Immediate as imm:
            value = imm.value
        result = OpResult(jsonable(value), round(self.clock(), 3), fenced).to_json()
        if op.write:
            tx.query(
                "INSERT INTO exec_idempotency (executor_id, key, op, request_sha256, status,"
                "                              response)"
                " VALUES (%s, %s, %s, %s, 200, %s::jsonb)"
                " ON CONFLICT (executor_id, key) DO UPDATE SET op = excluded.op,"
                "   request_sha256 = excluded.request_sha256, status = excluded.status,"
                "   response = excluded.response, at = now()"
                " RETURNING key",
                (principal.executor_id, key, op.name, digest,
                 _dumps(result)))
            audit(tx, principal, op=op.name, route=route, status=200,
                  agent=request.fence.agent if request.fence else principal.agent,
                  fenced=fenced, idempotency_key=key)
        return 200, result, {}

    def _idempotency(self, tx, principal, op, key, digest):
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                   ("exec_idem:%s:%s" % (principal.executor_id, key),))
        rows = tx.query(
            "SELECT op, request_sha256, status, response FROM exec_idempotency"
            " WHERE executor_id = %s AND key = %s"
            "   AND at > now() - make_interval(secs => %s)",
            (principal.executor_id, key, float(contract.IDEMPOTENCY_RETENTION_S)))
        if not rows:
            return None
        row = rows[0]
        if row["request_sha256"] != digest:
            raise _Refusal("idempotency_mismatch", "même Idempotency-Key, autre requête")
        body = row["response"]
        if isinstance(body, str):
            body = json.loads(body)
        return int(row["status"]), body, {contract.IDEMPOTENCY_REPLAYED_HEADER: "true"}

    def _audit_refusal(self, principal, request, route, code, agent, key) -> None:
        if code in ("unavailable",):
            return  # la base est justement indisponible
        try:
            with self.pool.connection() as conn:
                with conn.transaction() as tx:
                    audit(tx, principal, op=request.op, route=route,
                          status=contract.ERRORS[code].status, error=code, agent=agent,
                          idempotency_key=key)
        except Exception as exc:  # l'audit ne masque jamais la réponse
            log.warning("audit du refus impossible : %s", str(exc)[:200])


def request_digest(body: Mapping) -> str:
    """Empreinte d'idempotence d'un corps : `contract.request_sha256` (JCS,
    doubles à la manière d'ECMAScript, contrat 1.1). Un nombre que JCS
    refuse (NaN, infini, entier hors de ±(2^53 − 1)) : 400 `bad_args`."""
    try:
        return contract.request_sha256(body)
    except jcs.JcsError:
        raise _Refusal("bad_args", "nombre non canonisable (JCS)")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
