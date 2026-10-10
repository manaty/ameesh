# SPDX-License-Identifier: AGPL-3.0-only
"""Serveur de l'API d'exécuteur médiée, `/api/exec/v1` (lot L108).

`ameesh serve --exec-only` le lance seul, tant que `ameesh serve` (L84,
interface utilisateur et `/api/v1`) n'existe pas ; L84 montera `ExecApp`
sous le même préfixe. Assemblage de la voie B : l'identité des exécuteurs
(L110, `LockedIdentity` sur `identity.DbIdentityProvider`) sert `/enroll`,
`/token` et `/session-token` ; le relais de modèle (L111) est monté sous
`/llm/` (`extra_routes={"llm": relay}`) ; les limites de `GET /host`
viennent du canon (`resources.host_limits`) ; le canon est synchronisé par
le serveur pour chaque hôte médié enrôlé. Serveur de la bibliothèque standard
(`http.server`), aucune dépendance nouvelle ; défenses reprises
d'ameesh-approve (limitation de débit, journal masqué, TLS local
facultatif).

Routes (`contract.ROUTES`) :

| Route | Jeton | Rôle |
|---|---|---|
| `GET /health` | aucun | version du contrat, heure du serveur |
| `POST /enroll`, `POST /token` | (L110) | délégués à l'`IdentityProvider` ; 404 sans lui |
| `GET /host` | accès | `HostInfo` : limites, bail imposé, agents admis, disponibilité |
| `PUT /host/availability` | accès | état de la porte d'hôte (L112), 204 |
| `POST /op` | accès | une opération de la table (`repartiteur`) |
| `POST /session-token` | accès + enveloppe | jeton de session (L110), bail recontrôlé ici |
| `POST /session/op` | session | une opération « session » de la table |
| `GET /events` | accès | SSE, ou attente longue avec `?wait=` |
| `/llm/…` | session (`x-api-key`) | relais de modèle (L111), monté par `extra_routes["llm"]` |
| `/work/{agent}/bundle` | accès + bail (en-têtes) | dépôt de travail (L113, `work_repo.WorkDepot`), monté par `--work-repos` ; 404 sinon |

Bornes : corps ≤ `contract.MAX_BODY_BYTES` (413 `too_large`, sans lecture),
débit par adresse avant authentification et par exécuteur après (429
`rate_limited`, `Retry-After`), connexions simultanées bornées. Journal :
chaque refus et chaque écriture dans `exec_audit` ; le journal du processus
ne porte ni jeton, ni corps, ni argument.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Mapping, Optional

from ..approve.server import RateLimiter, redact
from ..db import Unavailable
from . import contract, events
from .contract import Fence, OpRequest
from .stream import EventHub
from .interfaces import AuthError, ExecutorAuth, HostInfo, IdentityProvider, Principal
from .gate import STATES
from .scope import DEFAULT_LEASE_RENEW_S, DEFAULT_LEASE_TTL_S, host_available
from .dispatcher import ConnectionPool, PgDispatcher, audit, error_response, fence_holds, purge

log = logging.getLogger("ameesh.exec")

#: durée maximale d'une connexion SSE (secondes) : le jeton d'accès vit 10 min
SSE_MAX_S = 600.0
#: cache des agents admis pour le filtrage du flux (secondes)
ADMITTED_CACHE_S = 5.0
#: élagage de l'idempotence et de l'audit (secondes)
PURGE_INTERVAL_S = 600.0
DEFAULT_RATE_PER_MINUTE = 600
DEFAULT_IP_RATE_PER_MINUTE = 1200


@dataclasses.dataclass
class Response:
    status: int
    body: Any = None                       # dict (JSON), ou None
    headers: dict = dataclasses.field(default_factory=dict)
    #: flux SSE : itérateur de blocs texte (la connexion se ferme à la fin)
    stream: Optional[Iterator[str]] = None
    #: corps binaire (archive du dépôt de travail, L113) ; `headers` porte
    #: son Content-Type
    raw: Optional[bytes] = None


@dataclasses.dataclass
class HostPolicy:
    """Ce que `GET /host` rend en plus des agents admis et de la porte."""

    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    lease_renew_s: float = DEFAULT_LEASE_RENEW_S
    harnesses: tuple = ()
    models: tuple = ()
    #: limites de l'hôte (format de `resources.host_limits`) ; None : {}
    limits: Optional[Callable[[str], Mapping]] = None


class LockedIdentity(IdentityProvider):
    """L'identité de L110 (`identity.DbIdentityProvider`) partagée par les
    fils du serveur : une connexion dédiée, un appel à la fois. Une
    connexion tombée (`Unavailable`) est rouverte à l'appel suivant."""

    def __init__(self, connect: Callable[[], Any], *, mesh: Optional[str] = None):
        self._connect = connect
        self._mesh = mesh
        self._lock = threading.Lock()
        self._provider = None

    def _call(self, name: str, *args, **kwargs):
        from .identity import DbIdentityProvider
        with self._lock:
            if self._provider is None:
                self._provider = DbIdentityProvider(self._connect(), mesh=self._mesh)
            try:
                return getattr(self._provider, name)(*args, **kwargs)
            except Unavailable:
                try:
                    self._provider.db.close()
                except Exception:
                    pass
                self._provider = None
                raise

    def verify(self, token: str) -> Principal:
        return self._call("verify", token)

    def verify_kind(self, token: str, kind: Optional[str]) -> Principal:
        return self._call("verify_kind", token, kind)

    def enroll(self, request, *, server_url: str) -> dict:
        return self._call("enroll", request, server_url=server_url)

    def issue_access_token(self, assertion: str, *, server_url: str):
        return self._call("issue_access_token", assertion, server_url=server_url)

    def issue_session_token(self, principal: Principal, fence: Fence):
        return self._call("issue_session_token", principal, fence)

    def revoke(self, executor_id: str, *, by: str, why: str) -> int:
        return self._call("revoke", executor_id, by=by, why=why)


def canon_limits(loader: Callable[[], Any]) -> Callable[[str], Mapping]:
    """`HostPolicy.limits` tiré du canon (contrat 1.1, `HostInfo.limits`) :
    `{"max_agents": int|None, "resources": {seuils}}`. Canon illisible :
    `{}` (l'exécuteur garde ses seuils par défaut prudents)."""
    from .. import resources as resources_mod

    def limits(host: str) -> Mapping:
        try:
            current = loader()
        except Exception as exc:
            log.warning("limites de l'hôte illisibles (canon) : %s", str(exc)[:200])
            return {}
        canons = [current] if current is not None else []
        found = resources_mod.host_limits(canons, host)
        return {"max_agents": found["max_agents"], "resources": dict(found["limits"])}
    return limits


def body_limit(path: str) -> int:
    """Taille maximale du corps d'une requête : `MAX_BUNDLE_BYTES` pour le
    dépôt d'un paquet git (L113), `MAX_BODY_BYTES` ailleurs."""
    if path.startswith(contract.PREFIX + "/work/") and path.endswith("/bundle"):
        return contract.MAX_BUNDLE_BYTES
    return contract.MAX_BODY_BYTES


def _error(code: str, message: str = "", *, retry_after: Optional[float] = None) -> Response:
    status, body, headers = error_response(code, message or contract.ERRORS[code].meaning,
                                           retry_after=retry_after)
    return Response(status, body, headers)


class ExecApp:
    """Le cœur HTTP, indépendant des sockets (essais, montage par L84)."""

    def __init__(self, dispatcher: PgDispatcher, auth: ExecutorAuth, hub: EventHub, *,
                 mesh: str, policy: Optional[HostPolicy] = None, server_url: str = "",
                 rate_per_minute: int = DEFAULT_RATE_PER_MINUTE,
                 ip_rate_per_minute: int = DEFAULT_IP_RATE_PER_MINUTE,
                 extra_routes: Optional[Mapping[str, Callable]] = None,
                 clock: Callable[[], float] = time.time):
        self.dispatcher = dispatcher
        self.pool = dispatcher.pool
        self.contract = dispatcher.contract
        self.auth = auth
        self.hub = hub
        self.mesh = mesh
        self.policy = policy or HostPolicy(lease_ttl_s=dispatcher.lease_ttl_s)
        self.server_url = server_url
        self.limiter = RateLimiter(rate_per_minute)
        self.ip_limiter = RateLimiter(ip_rate_per_minute)
        self.extra_routes = dict(extra_routes or {})
        self.clock = clock
        self._admitted_cache: dict = {}
        self._cache_lock = threading.Lock()
        self.closing = threading.Event()

    # -- entrée -------------------------------------------------------------------
    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes,
               *, client_ip: str = "?") -> Response:
        url = urllib.parse.urlsplit(target)
        path = url.path
        query = urllib.parse.parse_qs(url.query)
        if not path.startswith(contract.PREFIX + "/"):
            return _error("op_not_allowed", "route inconnue")
        sub = path[len(contract.PREFIX):]
        if not self.ip_limiter.allow(client_ip):
            return _error("rate_limited", "trop de requêtes", retry_after=2)
        if len(body) > body_limit(path):
            return _error("too_large", "corps trop gros")
        try:
            return self._route(method, sub, query, _lower(headers), body)
        except Unavailable:
            return _error("unavailable", "base indisponible", retry_after=5)
        except Exception:
            log.exception("erreur interne sur %s %s", method, redact(sub)[:80])
            return _error("internal", "erreur du serveur")

    def _route(self, method, sub, query, headers, body) -> Response:
        if (method, sub) == ("GET", "/health"):
            return Response(200, {"schema": contract.SCHEMA_HEALTH,
                                  "contract": self.contract.version,
                                  "server_ts": round(self.clock(), 3)})
        if (method, sub) == ("POST", "/enroll"):
            return self._enroll(body)
        if (method, sub) == ("POST", "/token"):
            return self._token(body)
        name = self._extra_name(sub)
        known = {("GET", "/host"), ("PUT", "/host/availability"), ("POST", "/op"),
                 ("POST", "/session-token"), ("POST", "/session/op"), ("GET", "/events")}
        if (method, sub) not in known and name is None:
            return _error("op_not_allowed", "route inconnue")
        principal, refusal = self._authenticate(headers, sub)
        if refusal is not None:
            return refusal
        if not self.limiter.allow("exec:%s" % principal.executor_id):
            self._audit(principal, sub, 429, "rate_limited")
            return _error("rate_limited", "trop de requêtes", retry_after=2)
        if name is not None:
            return self.extra_routes[name](self, principal, method, sub, headers, body)
        if sub == "/op" or sub == "/session/op":
            return self._op(principal, sub[1:], headers, body)
        if principal.kind != "executor":
            self._audit(principal, sub, 401, "token_invalid")
            return _error("token_invalid", "jeton d'accès attendu")
        if sub == "/host":
            return self._host(principal)
        if sub == "/host/availability":
            return self._availability(principal, body)
        if sub == "/session-token":
            return self._session_token(principal, body)
        return self._events(principal, query, headers)

    def raw_route(self, target: str) -> Optional[Any]:
        """Une route montée au niveau HTTP (objet doté de `handle(handler)`,
        le relais de modèle de L111), ou None."""
        relay = self.extra_routes.get("llm")
        if relay is None or not hasattr(relay, "handle"):
            return None
        path = urllib.parse.urlsplit(target).path
        return relay if path.startswith(contract.PREFIX + "/llm/") else None

    def _extra_name(self, sub: str) -> Optional[str]:
        if sub.startswith("/work/") and sub.endswith("/bundle"):
            return "bundle" if "bundle" in self.extra_routes else None
        if sub.startswith("/llm/"):
            route = self.extra_routes.get("llm")
            return "llm" if route is not None and callable(route) \
                and not hasattr(route, "handle") else None
        return None

    # -- authentification ---------------------------------------------------------
    def _authenticate(self, headers, sub) -> tuple[Optional[Principal], Optional[Response]]:
        value = headers.get("authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            self._audit(None, sub, 401, "token_invalid")
            return None, _error("token_invalid", "jeton absent")
        try:
            return self.auth.verify(token.strip()), None
        except AuthError as exc:
            code = exc.code if exc.code in contract.ERRORS else "token_invalid"
            self._audit(None, sub, contract.ERRORS[code].status, code)
            return None, _error(code, contract.ERRORS[code].meaning)

    def _identity_provider(self) -> Optional[IdentityProvider]:
        return self.auth if isinstance(self.auth, IdentityProvider) else None

    def _enroll(self, body) -> Response:
        provider = self._identity_provider()
        if provider is None:
            return _error("op_not_allowed", "enrôlement non servi (L110)")
        data, refusal = _json_body(body)
        if refusal:
            return refusal
        try:
            return Response(201, provider.enroll(data, server_url=self.server_url))
        except AuthError as exc:
            return _error(exc.code if exc.code in contract.ERRORS else "token_invalid")

    def _token(self, body) -> Response:
        provider = self._identity_provider()
        if provider is None:
            return _error("op_not_allowed", "émission de jeton non servie (L110)")
        data, refusal = _json_body(body)
        if refusal:
            return refusal
        try:
            issued = provider.issue_access_token(str(data.get("assertion") or ""),
                                                 server_url=self.server_url)
        except AuthError as exc:
            return _error(exc.code if exc.code in contract.ERRORS else "token_invalid")
        return Response(200, issued.to_json())

    # -- opérations -------------------------------------------------------------------
    def _op(self, principal, route, headers, body) -> Response:
        data, refusal = _json_body(body)
        if refusal:
            self._audit(principal, "/" + route, 400, "bad_request")
            return refusal
        try:
            request = OpRequest.from_json(data)
        except ValueError:
            self._audit(principal, "/" + route, 400, "bad_request")
            return _error("bad_request", "corps %s attendu" % contract.SCHEMA_OP)
        key = headers.get(contract.IDEMPOTENCY_HEADER.lower())
        status, body_out, extra = self.dispatcher.dispatch(principal, request, route=route,
                                                           idempotency_key=key)
        return Response(status, body_out, extra)

    def admitted(self, principal: Principal) -> frozenset:
        """Agents admis (cache court, pour le flux et `GET /host`)."""
        key = (principal.executor_id, principal.host, principal.agents_allowlist)
        now = time.monotonic()
        with self._cache_lock:
            hit = self._admitted_cache.get(key)
            if hit and now - hit[0] < ADMITTED_CACHE_S:
                return hit[1]
        with self.pool.connection() as conn:
            value = self.dispatcher.rules(conn).admitted_agents(principal)
        with self._cache_lock:
            if len(self._admitted_cache) > 1000:
                self._admitted_cache.clear()
            self._admitted_cache[key] = (now, value)
        return value

    def _host(self, principal) -> Response:
        with self.pool.connection() as conn:
            agents = sorted(self.dispatcher.rules(conn).admitted_agents(principal))
            available = host_available(conn, principal.host)
        limits = dict(self.policy.limits(principal.host)) if self.policy.limits else {}
        info = HostInfo(host=principal.host, mesh=principal.mesh or self.mesh,
                        executor_id=principal.executor_id, limits=limits,
                        lease_ttl_s=float(self.policy.lease_ttl_s),
                        lease_renew_s=float(self.policy.lease_renew_s),
                        harnesses=tuple(self.policy.harnesses), models=tuple(self.policy.models),
                        agents=tuple(agents), available=available,
                        contract_version=self.contract.version)
        return Response(200, _jsonable_info(info.to_json()))

    def _availability(self, principal, body) -> Response:
        data, refusal = _json_body(body)
        if refusal:
            return refusal
        state = data.get("state")
        seq = data.get("seq")
        caps = data.get("caps") or {}
        until = data.get("until_ts")
        if (data.get("schema") != contract.SCHEMA_AVAILABILITY or state not in STATES
                or not isinstance(seq, int) or isinstance(seq, bool)
                or not isinstance(caps, Mapping)
                or not (until is None or isinstance(until, (int, float)))):
            self._audit(principal, "/host/availability", 400, "bad_request")
            return _error("bad_request", "corps %s attendu" % contract.SCHEMA_AVAILABILITY)
        # `available` n'est jamais cru seul : seul l'état `available` ouvre la porte
        available = state == "available" and data.get("available") is not False
        with self.pool.connection() as conn:
            with conn.transaction() as tx:
                tx.query(
                    "INSERT INTO exec_host_availability"
                    " (host, executor_id, available, state, seq, until_at, caps, reason)"
                    " VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s::jsonb, %s)"
                    " ON CONFLICT (host) DO UPDATE SET executor_id = excluded.executor_id,"
                    "   available = excluded.available, state = excluded.state,"
                    "   seq = excluded.seq, until_at = excluded.until_at,"
                    "   caps = excluded.caps, reason = excluded.reason, updated_at = now()"
                    " WHERE exec_host_availability.seq <= excluded.seq"
                    "    OR exec_host_availability.executor_id <> excluded.executor_id"
                    " RETURNING host",
                    (principal.host, principal.executor_id, available, state, seq, until,
                     json.dumps(dict(caps), ensure_ascii=False),
                     str(data.get("reason") or "")[:200]))
                audit(tx, principal, op="host.availability:%s" % state,
                      route="/host/availability", status=204)
        return Response(204)

    def _session_token(self, principal, body) -> Response:
        provider = self._identity_provider()
        if provider is None:
            return _error("op_not_allowed", "jeton de session non servi (L110)")
        data, refusal = _json_body(body)
        if refusal:
            return refusal
        try:
            fence = Fence.from_json(data.get("fence"))
        except ValueError:
            return _error("fence_required", "enveloppe de bail attendue")
        with self.pool.connection() as conn:
            with conn.transaction() as tx:
                rules = self.dispatcher.rules(tx)
                if fence.agent not in rules.admitted_agents(principal):
                    issued = None
                    code = "forbidden_scope"
                elif not fence_holds(tx, principal, fence):
                    issued = None
                    code = "forbidden_scope"
                else:
                    issued = provider.issue_session_token(principal, fence)
                    code = None
                audit(tx, principal, op="session-token", route="/session-token",
                      status=200 if issued else 403, error=code, agent=fence.agent)
        if issued is None:
            return _error("forbidden_scope", "bail non détenu par cet exécuteur")
        return Response(200, issued.to_json())

    # -- événements -------------------------------------------------------------------
    def _events(self, principal, query, headers) -> Response:
        after = (query.get("after") or [None])[0] or headers.get("last-event-id") or None
        wait_raw = (query.get("wait") or [None])[0]
        sse = wait_raw is None and "text/event-stream" in headers.get("accept", "")
        admitted = lambda: self.admitted(principal)  # noqa: E731
        if not sse:
            try:
                wait = float(wait_raw) if wait_raw is not None else events.MAX_WAIT_S
            except ValueError:
                return _error("bad_request", "wait : nombre attendu")
            if not math.isfinite(wait):
                return _error("bad_request", "wait : nombre attendu")
            wait = min(max(wait, 0.0), float(events.MAX_WAIT_S))
            events, last = self.hub.poll(after, admitted, wait)
            return Response(200, events.long_poll_body(events, last_id=last))
        return Response(200, None, {"Content-Type": "text/event-stream; charset=utf-8",
                                    "Cache-Control": "no-store"},
                        stream=self._sse(principal, after, admitted))

    def _sse(self, principal, after, admitted) -> Iterator[str]:
        yield events.format_retry()
        end = time.monotonic() + SSE_MAX_S
        token_end = principal.expires_ts - self.clock() if principal.expires_ts else SSE_MAX_S
        end = min(end, time.monotonic() + max(0.0, token_end))
        cursor = after
        while not self.closing.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            events, cursor = self.hub.poll(cursor, admitted,
                                           min(float(events.PING_INTERVAL_S), remaining))
            if events:
                for event in events:
                    yield events.format_sse(event)
            else:
                yield events.format_ping()

    # -- audit ----------------------------------------------------------------------------
    def _audit(self, principal, route, status, error) -> None:
        try:
            with self.pool.connection() as conn:
                with conn.transaction() as tx:
                    audit(tx, principal, op=route, route=route, status=status, error=error,
                          agent=principal.agent if principal else None)
        except Exception as exc:
            log.warning("audit impossible : %s", str(exc)[:200])


def _lower(headers: Mapping[str, str]) -> dict:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _json_body(body: bytes) -> tuple[dict, Optional[Response]]:
    try:
        data = json.loads(body.decode("utf-8") or "null", parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        return {}, _error("bad_request", "JSON illisible")
    if not isinstance(data, dict):
        return {}, _error("bad_request", "objet JSON attendu")
    return data, None


def _no_constant(name: str):
    raise ValueError("%s refusé" % name)   # NaN, Infinity : pas du JSON


def _jsonable_info(body: dict) -> dict:
    return json.loads(json.dumps(body, default=str))


# --------------------------------------------------------------------------
# HTTP (bibliothèque standard)
# --------------------------------------------------------------------------

class ExecHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, app: ExecApp, address: tuple, *, tls=None, max_connections: int = 256,
                 socket_timeout: float = 30.0):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.app = app
        self.tls = tls
        self.socket_timeout = socket_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, ExecHandler)

    def get_request(self):
        sock, address = super().get_request()
        if self.tls is not None:
            try:
                sock = self.tls.wrap(sock)
            except Exception:
                sock.close()
                raise
        return sock, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log.warning("trop de connexions simultanées : connexion fermée")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        log.warning("connexion interrompue : %s", sys.exc_info()[0].__name__)


class ExecHandler(BaseHTTPRequestHandler):
    server: ExecHTTPServer
    server_version = "ameesh-exec"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self.timeout = self.server.socket_timeout
        if self.server.tls is not None:
            self.request.settimeout(self.timeout)
            self.request.do_handshake()
        super().setup()

    def version_string(self) -> str:
        return self.server_version

    def log_request(self, code="-", size="-") -> None:
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        log.info("%s %s %s %s", self.client_address[0] if self.client_address else "?",
                 getattr(self, "command", None), redact(path)[:120], code)

    def log_message(self, format, *args) -> None:  # noqa: A002
        log.info("%s", redact(format % args)[:200])

    def _serve(self) -> None:
        relay = self.server.app.raw_route(self.path)
        if relay is not None:
            # relais de modèle (L111) : il lit son corps, authentifie le jeton
            # de session (`x-api-key`) et recopie le flux du fournisseur
            relay.handle(self)
            return
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding"):
            self._write(_error("bad_request", "Content-Length obligatoire"), close=True)
            return
        length = 0
        if lengths:
            if len(lengths) != 1 or not lengths[0].strip().isdigit():
                self._write(_error("bad_request", "Content-Length invalide"), close=True)
                return
            length = int(lengths[0].strip())
        if length > body_limit(urllib.parse.urlsplit(self.path).path):
            self._write(_error("too_large", "corps trop gros"), close=True)
            return
        body = self.rfile.read(length) if length else b""
        response = self.server.app.handle(
            self.command, self.path, dict(self.headers.items()), body,
            client_ip=self.client_address[0] if self.client_address else "?")
        self._write(response)

    do_GET = do_POST = do_PUT = _serve

    def _write(self, response: Response, *, close: bool = False) -> None:
        if response.stream is not None:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in response.stream:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        payload = b""
        if response.raw is not None:
            payload = response.raw
        elif response.body is not None:
            payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        if payload and response.raw is None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in response.headers.items():
            self.send_header(name, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if payload:
            self.wfile.write(payload)


# --------------------------------------------------------------------------
# `ameesh serve --exec-only`
# --------------------------------------------------------------------------

USAGE = """ameesh serve --exec-only --server-url URL [--listen HÔTE:PORT]
        [--mesh NOM] [--lease-ttl S] [--lease-renew S]
        [--credential-modes relay,...] [--harnesses dsh,...] [--models m,...]
        [--rate-per-minute N] [--pool N] [--tls-cert F --tls-key F] [--allow-plain]
        [--no-relay] [--canon-sync S] [--auth-file FICHIER]
        [--work-repos FICHIER [--work-cache DOSSIER]]

Le serveur de l'API d'exécuteur médiée (/api/exec/v1, lots L108 à L111).
Tant que `ameesh serve` (L84) n'existe pas, seul --exec-only est servi.
Identité : celle des exécuteurs enrôlés (L110) ; --auth-file remplace par
des jetons fixes (banc d'essai seulement). --server-url : l'URL publique,
celle que l'appareil a reçue avec son code (audience des assertions). Le
relais de modèle (L111) est monté sous /api/exec/v1/llm/ (--no-relay pour
s'en passer). --canon-sync S : canon synchronisé pour chaque hôte médié
enrôlé toutes les S secondes (défaut 60 ; 0 : jamais). --work-repos : le
dépôt de travail (L113, `mediated_executor.work_repo`) est monté sous
/api/exec/v1/work/{agent}/bundle ; --work-cache garde ses clones nus
(défaut ~/.local/state/ameesh/depot). Une écoute hors de
la boucle locale exige TLS (--tls-cert/--tls-key) ou --allow-plain derrière
un mandataire inverse qui termine TLS."""


def _parse(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ameesh serve", usage=USAGE, add_help=True)
    p.add_argument("--exec-only", action="store_true")
    p.add_argument("--listen", default="127.0.0.1:8471")
    p.add_argument("--auth-file")
    p.add_argument("--mesh", default="")
    p.add_argument("--server-url", default="")
    p.add_argument("--lease-ttl", type=float, default=DEFAULT_LEASE_TTL_S)
    p.add_argument("--lease-renew", type=float, default=DEFAULT_LEASE_RENEW_S)
    p.add_argument("--credential-modes", default="")
    p.add_argument("--harnesses", default="")
    p.add_argument("--models", default="")
    p.add_argument("--rate-per-minute", type=int, default=DEFAULT_RATE_PER_MINUTE)
    p.add_argument("--pool", type=int, default=8)
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    p.add_argument("--allow-plain", action="store_true")
    p.add_argument("--no-relay", action="store_true")
    p.add_argument("--canon-sync", type=float, default=60.0)
    p.add_argument("--work-repos", default="")
    p.add_argument("--work-cache", default="")
    return p.parse_args(argv)


def _split(text: str) -> tuple:
    return tuple(s.strip() for s in (text or "").split(",") if s.strip())


def build(cfg, auth: ExecutorAuth, *, mesh: str, pool_size: int = 8,
          policy: Optional[HostPolicy] = None, credential_modes=None,
          server_url: str = "", rate_per_minute: int = DEFAULT_RATE_PER_MINUTE,
          listen: bool = True, extra_routes: Optional[Mapping[str, Any]] = None) -> ExecApp:
    """Assemble le serveur sur la base de `cfg` (réservoir, répartiteur,
    écoute LISTEN). `listen=False` : flux sans écoute (essais)."""
    from .. import db as db_mod
    from .. import storage
    pool = ConnectionPool(lambda: db_mod.connect(cfg), size=pool_size)
    policy = policy or HostPolicy()
    dispatcher = PgDispatcher(pool, credential_modes=credential_modes,
                              lease_ttl_s=policy.lease_ttl_s)

    def subscribe():
        return storage.of(db_mod.connect(cfg)).wakeups.subscribe(list(events.CHANNELS))

    hub = EventHub(subscribe if listen else None)
    hub.start()
    return ExecApp(dispatcher, auth, hub, mesh=mesh, policy=policy, server_url=server_url,
                   rate_per_minute=rate_per_minute, extra_routes=extra_routes)


def build_relay(cfg, identity: LockedIdentity, pool: ConnectionPool):
    """Le relais de modèle (L111) branché sur l'identité des exécuteurs :
    jeton de session vérifié par L110, bail recontrôlé en base."""
    from .. import db as db_mod
    from .. import relay as relay_mod
    verifier = relay_mod.ExecutorTokens(
        lambda token: identity.verify_kind(token, "session"),
        lambda principal: lease_owner(pool, principal))
    return relay_mod.Relay(verifier=verifier,
                           policy=relay_mod.CanonPolicy(relay_mod._canon_loader(cfg)),
                           db=db_mod.connect(cfg), cfg=cfg)


def lease_owner(pool: ConnectionPool, principal: Principal) -> Optional[str]:
    """L'owner du bail lié à un jeton de session, s'il vit encore : même
    agent, même epoch, owner de cet exécuteur, même hôte. None sinon."""
    if not principal.agent or principal.epoch is None:
        return None
    with pool.connection() as conn:
        rows = conn.query(
            "SELECT lease_owner FROM agent_registry WHERE name = %s AND lease_epoch = %s"
            "   AND lease_expires_at > clock_timestamp() AND host = %s"
            "   AND starts_with(lease_owner, %s)",
            (principal.agent, int(principal.epoch), principal.host,
             "exec:%s:" % principal.executor_id))
    return rows[0]["lease_owner"] if rows else None


def mediated_hosts(db) -> list:
    """Les hôtes qui ont au moins un exécuteur enrôlé non révoqué."""
    rows = db.query("SELECT DISTINCT host FROM executors WHERE revoked_at IS NULL"
                    " ORDER BY host")
    return [r["host"] for r in rows]


def sync_canon_once(cfg, db) -> list:
    """Synchronise le canon par défaut pour chaque hôte médié (les
    admissions, la politique et la visibilité de ses agents) ; rend les
    hôtes synchronisés. Jamais fatal : un échec est journalisé."""
    from .. import canon as canon_mod
    from .. import canon_sync
    done = []
    try:
        hosts = mediated_hosts(db)
        if not hosts or not canon_mod.configured(cfg):
            return done
        canon = canon_mod.from_config(cfg)
    except Exception as exc:
        log.warning("canon des hôtes médiés illisible : %s", " ".join(str(exc).split())[:200])
        return done
    for host in hosts:
        try:
            canon_sync.sync(db, canon, host,
                            trusted_ref=canon_sync.configured_ref_for(cfg, canon))
            done.append(host)
        except Exception as exc:
            log.warning("canon sync de %s en échec : %s", host,
                        " ".join(str(exc).split())[:200])
    return done


def _canon_loop(app: ExecApp, cfg, interval: float) -> None:
    while True:
        try:
            with app.pool.connection() as conn:
                sync_canon_once(cfg, conn)
        except Exception as exc:
            log.warning("canon des hôtes médiés : %s", str(exc)[:200])
        if app.closing.wait(interval):
            return


def _purge_loop(app: ExecApp) -> None:
    while not app.closing.wait(PURGE_INTERVAL_S):
        try:
            with app.pool.connection() as conn:
                purge(conn)
        except Exception as exc:
            log.warning("élagage impossible : %s", str(exc)[:200])


def main(argv=None) -> int:
    from .. import config as config_mod
    from .. import db as db_mod
    args = _parse(sys.argv[1:] if argv is None else argv)
    if not args.exec_only:
        print("ameesh serve : seul --exec-only est servi (l'interface de L84 viendra)",
              file=sys.stderr)
        return 2
    if not args.auth_file and not args.server_url:
        print("ameesh serve : --server-url requis (URL publique du serveur, audience des"
              " assertions des exécuteurs)", file=sys.stderr)
        return 2
    host, _, port = args.listen.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    loopback = host in ("127.0.0.1", "::1", "localhost")
    tls = None
    if args.tls_cert or args.tls_key:
        from ..approve.tls import TlsReloader
        tls = TlsReloader(args.tls_cert, args.tls_key)
    elif not loopback and not args.allow_plain:
        print("ameesh serve : écoute hors boucle locale sans TLS refusée"
              " (--tls-cert/--tls-key, ou --allow-plain derrière un mandataire TLS)",
              file=sys.stderr)
        return 2
    cfg = config_mod.load()
    if args.auth_file:
        from .stub import StaticAuth
        try:
            auth = StaticAuth.from_file(args.auth_file)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print("ameesh serve : %s" % exc, file=sys.stderr)
            return 2
    else:
        auth = LockedIdentity(lambda: db_mod.connect(cfg), mesh=args.mesh or None)
    probe = db_mod.connect(cfg)
    try:
        db_mod.require_schema(probe)
    finally:
        probe.close()
    from .. import relay as relay_mod
    policy = HostPolicy(lease_ttl_s=args.lease_ttl, lease_renew_s=args.lease_renew,
                        harnesses=_split(args.harnesses), models=_split(args.models),
                        limits=canon_limits(relay_mod._canon_loader(cfg)))
    modes = frozenset(_split(args.credential_modes)) or None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = build(cfg, auth, mesh=args.mesh, pool_size=args.pool, policy=policy,
                credential_modes=modes, server_url=args.server_url,
                rate_per_minute=args.rate_per_minute)
    if not args.no_relay:
        if not isinstance(auth, LockedIdentity):
            print("ameesh serve : le relais exige l'identité des exécuteurs (sans"
                  " --auth-file), ou --no-relay", file=sys.stderr)
            return 2
        app.extra_routes["llm"] = build_relay(cfg, auth, app.pool)
    if args.work_repos:
        from . import work_repo
        try:
            repos = work_repo.RepoMap.from_file(args.work_repos)
        except (OSError, ValueError) as exc:
            print("ameesh serve : --work-repos : %s" % exc, file=sys.stderr)
            return 2
        cache = args.work_cache or os.path.expanduser("~/.local/state/ameesh/depot")
        app.extra_routes["bundle"] = work_repo.WorkDepot(repos, cache,
                                                     work_repo.lease_checker(app.pool))
    if args.canon_sync > 0 and isinstance(auth, LockedIdentity):
        threading.Thread(target=_canon_loop, args=(app, cfg, args.canon_sync),
                         daemon=True, name="canon-hotes-medies").start()
    server = ExecHTTPServer(app, (host, int(port)), tls=tls)
    threading.Thread(target=_purge_loop, args=(app,), daemon=True).start()
    log.info("API d'exécuteur médiée sur %s:%s%s (contrat %s, identité %s, relais %s,"
             " dépôt de travail %s)",
             host, port, contract.PREFIX, app.contract.version,
             "L110" if isinstance(auth, LockedIdentity) else "jetons fixes",
             "monté" if "llm" in app.extra_routes else "absent",
             "monté" if "bundle" in app.extra_routes else "absent")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.closing.set()
        app.hub.stop()
        server.server_close()
        app.pool.close()
    return 0
