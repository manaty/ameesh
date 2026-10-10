# SPDX-License-Identifier: AGPL-3.0-only
"""Serveur de l'API d'exécuteur médiée, `/api/exec/v1` (lot L108).

`ameesh serve --exec-only` le lance seul, tant que `ameesh serve` (L84,
interface utilisateur et `/api/v1`) n'existe pas ; L84 montera `ExecApp`
sous le même préfixe. Serveur de la bibliothèque standard
(`http.server`), aucune dépendance nouvelle ; défenses reprises
d'ameesh-approve (limitation de débit, journal masqué, TLS local
facultatif).

Routes (`contrat.ROUTES`) :

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
| `/work/{agent}/bundle`, `/llm/…` | — | montés par L113 et L111 (`extra_routes`) ; 404 sinon |

Bornes : corps ≤ `contrat.MAX_BODY_BYTES` (413 `too_large`, sans lecture),
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
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Mapping, Optional

from ..approve.server import RateLimiter, redact
from ..db import Unavailable
from . import contrat, evenements
from .contrat import Fence, OpRequest
from .flux import EventHub
from .interfaces import AuthError, ExecutorAuth, HostInfo, IdentityProvider, Principal
from .porte import STATES
from .portee import DEFAULT_LEASE_RENEW_S, DEFAULT_LEASE_TTL_S, host_available
from .repartiteur import ConnectionPool, PgDispatcher, audit, error_response, fence_holds, purge

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


@dataclasses.dataclass
class HostPolicy:
    """Ce que `GET /host` rend en plus des agents admis et de la porte."""

    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    lease_renew_s: float = DEFAULT_LEASE_RENEW_S
    harnesses: tuple = ()
    models: tuple = ()
    #: limites de l'hôte (format de `resources.host_limits`) ; None : {}
    limits: Optional[Callable[[str], Mapping]] = None


def _error(code: str, message: str = "", *, retry_after: Optional[float] = None) -> Response:
    status, body, headers = error_response(code, message or contrat.ERRORS[code].meaning,
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
        if not path.startswith(contrat.PREFIX + "/"):
            return _error("op_not_allowed", "route inconnue")
        sub = path[len(contrat.PREFIX):]
        if not self.ip_limiter.allow(client_ip):
            return _error("rate_limited", "trop de requêtes", retry_after=2)
        if len(body) > contrat.MAX_BODY_BYTES:
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
            return Response(200, {"schema": contrat.SCHEMA_HEALTH,
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

    def _extra_name(self, sub: str) -> Optional[str]:
        if sub.startswith("/work/") and sub.endswith("/bundle"):
            return "bundle" if "bundle" in self.extra_routes else None
        if sub.startswith("/llm/"):
            return "llm" if "llm" in self.extra_routes else None
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
            code = exc.code if exc.code in contrat.ERRORS else "token_invalid"
            self._audit(None, sub, contrat.ERRORS[code].status, code)
            return None, _error(code, contrat.ERRORS[code].meaning)

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
            return _error(exc.code if exc.code in contrat.ERRORS else "token_invalid")

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
            return _error(exc.code if exc.code in contrat.ERRORS else "token_invalid")
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
            return _error("bad_request", "corps %s attendu" % contrat.SCHEMA_OP)
        key = headers.get(contrat.IDEMPOTENCY_HEADER.lower())
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
        if (data.get("schema") != contrat.SCHEMA_AVAILABILITY or state not in STATES
                or not isinstance(seq, int) or isinstance(seq, bool)
                or not isinstance(caps, Mapping)
                or not (until is None or isinstance(until, (int, float)))):
            self._audit(principal, "/host/availability", 400, "bad_request")
            return _error("bad_request", "corps %s attendu" % contrat.SCHEMA_AVAILABILITY)
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
                wait = float(wait_raw) if wait_raw is not None else evenements.MAX_WAIT_S
            except ValueError:
                return _error("bad_request", "wait : nombre attendu")
            if not math.isfinite(wait):
                return _error("bad_request", "wait : nombre attendu")
            wait = min(max(wait, 0.0), float(evenements.MAX_WAIT_S))
            events, last = self.hub.poll(after, admitted, wait)
            return Response(200, evenements.long_poll_body(events, last_id=last))
        return Response(200, None, {"Content-Type": "text/event-stream; charset=utf-8",
                                    "Cache-Control": "no-store"},
                        stream=self._sse(principal, after, admitted))

    def _sse(self, principal, after, admitted) -> Iterator[str]:
        yield evenements.format_retry()
        end = time.monotonic() + SSE_MAX_S
        token_end = principal.expires_ts - self.clock() if principal.expires_ts else SSE_MAX_S
        end = min(end, time.monotonic() + max(0.0, token_end))
        cursor = after
        while not self.closing.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            events, cursor = self.hub.poll(cursor, admitted,
                                           min(float(evenements.PING_INTERVAL_S), remaining))
            if events:
                for event in events:
                    yield evenements.format_sse(event)
            else:
                yield evenements.format_ping()

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
        if length > contrat.MAX_BODY_BYTES:
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
        if response.body is not None:
            payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        if payload:
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

USAGE = """ameesh serve --exec-only --auth-file FICHIER [--listen HÔTE:PORT]
        [--mesh NOM] [--server-url URL] [--lease-ttl S] [--lease-renew S]
        [--credential-modes relay,...] [--harnesses dsh,...] [--models m,...]
        [--rate-per-minute N] [--pool N] [--tls-cert F --tls-key F] [--allow-plain]

Le serveur de l'API d'exécuteur médiée (/api/exec/v1, lot L108). Tant que
`ameesh serve` (L84) n'existe pas, seul --exec-only est servi. --auth-file :
jetons fixes (bouchon, en attendant l'enrôlement de L110). Une écoute hors
de la boucle locale exige TLS (--tls-cert/--tls-key) ou --allow-plain
derrière un mandataire inverse qui termine TLS."""


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
    return p.parse_args(argv)


def _split(text: str) -> tuple:
    return tuple(s.strip() for s in (text or "").split(",") if s.strip())


def build(cfg, auth: ExecutorAuth, *, mesh: str, pool_size: int = 8,
          policy: Optional[HostPolicy] = None, credential_modes=None,
          server_url: str = "", rate_per_minute: int = DEFAULT_RATE_PER_MINUTE,
          listen: bool = True) -> ExecApp:
    """Assemble le serveur sur la base de `cfg` (réservoir, répartiteur,
    écoute LISTEN). `listen=False` : flux sans écoute (essais)."""
    from .. import db as db_mod
    from .. import storage
    pool = ConnectionPool(lambda: db_mod.connect(cfg), size=pool_size)
    policy = policy or HostPolicy()
    dispatcher = PgDispatcher(pool, credential_modes=credential_modes,
                              lease_ttl_s=policy.lease_ttl_s)

    def subscribe():
        return storage.of(db_mod.connect(cfg)).wakeups.subscribe(list(evenements.CHANNELS))

    hub = EventHub(subscribe if listen else None)
    hub.start()
    return ExecApp(dispatcher, auth, hub, mesh=mesh, policy=policy, server_url=server_url,
                   rate_per_minute=rate_per_minute)


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
    if not args.auth_file:
        print("ameesh serve : --auth-file requis tant que l'identité de L110 manque",
              file=sys.stderr)
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
    from .bouchon import StaticAuth
    try:
        auth = StaticAuth.from_file(args.auth_file)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("ameesh serve : %s" % exc, file=sys.stderr)
        return 2
    cfg = config_mod.load()
    probe = db_mod.connect(cfg)
    try:
        db_mod.require_schema(probe)
    finally:
        probe.close()
    policy = HostPolicy(lease_ttl_s=args.lease_ttl, lease_renew_s=args.lease_renew,
                        harnesses=_split(args.harnesses), models=_split(args.models))
    modes = frozenset(_split(args.credential_modes)) or None
    app = build(cfg, auth, mesh=args.mesh, pool_size=args.pool, policy=policy,
                credential_modes=modes, server_url=args.server_url,
                rate_per_minute=args.rate_per_minute)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = ExecHTTPServer(app, (host, int(port)), tls=tls)
    threading.Thread(target=_purge_loop, args=(app,), daemon=True).start()
    log.info("API d'exécuteur médiée sur %s:%s%s (contrat %s)", host, port, contrat.PREFIX,
             app.contract.version)
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
