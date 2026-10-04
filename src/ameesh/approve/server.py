# SPDX-License-Identifier: AGPL-3.0-only
"""Serveur HTTP d'ameesh-approve (stdlib `http.server`).

Routes :

    POST /requests            jeton de service — crée une demande, rend le lien
                              ({action_id, approver, requested_by,
                              assume_duplicate?})
    GET  /receipts/<id>       jeton de service — le reçu (200), en attente (202),
                              échu sans reçu (410)
    GET  /a/<jeton>           page d'approbation
    POST /a/<jeton>           assertion WebAuthn → reçu (approve | deny)
    GET  /enroll/<jeton>      page d'enrôlement
    POST /enroll/<jeton>      attestation → proposition de canon
    GET  /static/<fichier>    approve.js, approve.css

Défenses (toutes les réponses, y compris les erreurs) : CSP stricte sans
script en ligne, `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, `Cache-Control: no-store`, `X-Frame-Options: DENY`, COOP/CORP.
En plus :

* en-tête Host contrôlé : pages humaines sous un hôte des origines
  autorisées ; API de service sous l'hôte local (127.0.0.1:port) seulement,
  sauf `api_via_public` (anti-rebinding DNS) ;
* POST humains : `Origin` doit être une origine autorisée (et
  `Sec-Fetch-Site`, s'il est présent, same-origin) ; API : aucun `Origin`
  (un navigateur n'a rien à y faire) ;
* corps : `Content-Length` obligatoire, borné (413 sans lecture), JSON seul ;
* limitation de débit par IP et par jeton (seaux à jetons en mémoire) ;
* nombre de connexions simultanées borné, délai de socket ;
* journal sans secrets : chemins à jeton masqués, toute suite base64url
  longue masquée, jamais d'en-tête ni de corps.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import render
from .service import ApproveError, ApproveService, load_json_body
from .store import REQUEST_ID_RE, TOKEN_RE

log = logging.getLogger("ameesh.approve")

CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Pragma", "no-cache"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "publickey-credentials-get=(self), "
                           "publickey-credentials-create=(self), camera=(), microphone=(), "
                           "geolocation=()"),
)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "approve.js": "text/javascript; charset=utf-8",
    "approve.css": "text/css; charset=utf-8",
}
_TOKEN_PATH = r"([A-Za-z0-9_-]{43})"
_ROUTES = (
    ("api", "POST", re.compile(r"^/requests$")),
    ("api", "GET", re.compile(r"^/receipts/(req_[0-9a-z]{26})$")),
    ("approve", "GET POST", re.compile(r"^/a/%s$" % _TOKEN_PATH)),
    ("enroll", "GET POST", re.compile(r"^/enroll/%s$" % _TOKEN_PATH)),
    ("static", "GET", re.compile(r"^/static/([a-z]+\.(?:js|css))$")),
)
#: un corps refusé mais pas plus gros que ceci est lu et jeté avant la
#: fermeture (sinon le noyau répond RST et le client perd la réponse)
DRAIN_LIMIT = 1024 * 1024
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,}")
_PATH_SECRET_RE = re.compile(r"/(a|enroll)/[^/?\s]*")


def redact(text: str) -> str:
    """Masque les jetons de lien et toute longue suite base64url."""
    text = _PATH_SECRET_RE.sub(lambda m: "/%s/<jeton>" % m.group(1), str(text))
    return _SECRET_RE.sub("<masqué>", text)


class RateLimiter:
    """Seaux à jetons : `per_minute` requêtes par minute, rafale = per_minute."""

    MAX_KEYS = 10000

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > self.MAX_KEYS:
                # les seaux pleins n'apprennent rien : on les oublie
                for stale in [k for k, (t, l) in self._buckets.items()
                              if t + (now - l) * self.rate >= self.capacity]:
                    del self._buckets[stale]
                if len(self._buckets) > self.MAX_KEYS:
                    self._buckets.clear()
            return allowed


class ApproveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, service: ApproveService, address: tuple[str, int]):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.service = service
        cfg = service.cfg
        self.ip_limiter = RateLimiter(cfg.rate_ip_per_minute)
        self.token_limiter = RateLimiter(cfg.rate_token_per_minute)
        self._slots = threading.BoundedSemaphore(cfg.max_connections)
        super().__init__(address, ApproveHandler)
        port = self.server_address[1]
        self.local_hosts = frozenset(
            "%s:%d" % (host, port) for host in ("127.0.0.1", "localhost", "[::1]"))
        self.public_hosts = cfg.public_hosts
        self.static = {}
        for name, content_type in STATIC_FILES.items():
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                self.static[name] = (fh.read(), content_type)

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
        # pas de trace complète sur stderr : une ligne, sans donnée de requête
        log.warning("connexion interrompue : %s", sys.exc_info()[0].__name__)


class ApproveHandler(BaseHTTPRequestHandler):
    server: ApproveHTTPServer
    server_version = "ameesh-approve"
    sys_version = ""
    timeout = 15.0
    error_content_type = "text/plain; charset=utf-8"
    error_message_format = "%(code)d %(message)s\n"

    def setup(self) -> None:
        self.timeout = self.server.service.cfg.socket_timeout
        super().setup()

    def version_string(self) -> str:
        return self.server_version   # ni version de Python, ni de bibliothèque

    # -- en-têtes et journal ----------------------------------------------
    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        if all(origin.startswith("https://") for origin in self.server.service.cfg.origins):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def log_request(self, code="-", size="-") -> None:
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        log.info("%s %s %s %s", self.client_ip(), getattr(self, "command", None), redact(path)[:120],
                 code)

    def log_error(self, format, *args) -> None:  # noqa: A002 (signature de http.server)
        log.warning("%s erreur HTTP : %s", self.client_ip(), redact(format % args)[:200])

    def log_message(self, format, *args) -> None:  # noqa: A002
        log.info("%s %s", self.client_ip(), redact(format % args)[:200])

    def client_ip(self) -> str:
        ip = self.client_address[0] if self.client_address else "?"
        if self.server.service.cfg.trust_forwarded:
            headers = getattr(self, "headers", None)
            forwarded = headers.get("X-Forwarded-For", "") if headers else ""
            last = forwarded.split(",")[-1].strip()
            if re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", last):
                ip = last
        return ip

    # -- réponses ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload, extra: tuple = ()) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _html(self, status: int, page: str) -> None:
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _error(self, kind: str, exc: ApproveError, extra: tuple = ()) -> None:
        if kind in ("approve", "enroll") and self.command == "GET":
            titles = {404: "Lien inconnu", 410: "Lien inutilisable", 409: "Demande caduque",
                      429: "Trop de requêtes", 503: "Service indisponible"}
            self._html(exc.status, render.message_page(
                titles.get(exc.status, "Erreur"), exc.message))
        else:
            self._json(exc.status, {"error": exc.code, "message": exc.message}, extra)

    # -- méthodes ----------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _not_allowed(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "method", "message": "méthode non admise"},
                   (("Allow", "GET, POST"),))

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed

    # -- aiguillage --------------------------------------------------------
    def _route(self, path: str):
        for kind, methods, pattern in _ROUTES:
            match = pattern.fullmatch(path)
            if match:
                if self.command not in methods.split():
                    return kind, None, "method"
                return kind, (match.group(1) if pattern.groups else None), ""
        return None, None, "not_found"

    def _host_ok(self, kind: str) -> bool:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1:
            return False
        host = hosts[0].strip().lower()
        if kind == "api":
            allowed = self.server.local_hosts
            if self.server.service.cfg.api_via_public:
                allowed = allowed | self.server.public_hosts
            return host in allowed
        return host in self.server.public_hosts

    def _discard_body(self) -> None:
        """Jette un petit corps non lu (réponse d'erreur) ; jamais un gros."""
        if self._body_consumed or self.command != "POST":
            return
        self._body_consumed = True
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 \
                or not lengths[0].strip().isdigit():
            return
        remaining = int(lengths[0].strip())
        if remaining > DRAIN_LIMIT:
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApproveError(411, "length_required", "Transfer-Encoding refusé : "
                                                       "Content-Length obligatoire")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not lengths[0].strip().isdigit():
            self.close_connection = True
            raise ApproveError(411, "length_required", "Content-Length obligatoire")
        length = int(lengths[0].strip())
        if length > self.server.service.cfg.max_body:
            self.close_connection = True
            raise ApproveError(413, "too_large", "corps trop gros (max %d octets)"
                               % self.server.service.cfg.max_body)
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.close_connection = True
            raise ApproveError(415, "content_type", "application/json attendu")
        self._body_consumed = True
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            raise ApproveError(400, "truncated", "corps tronqué")
        return body

    def _browser_origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin not in self.server.service.cfg.origins:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin")

    def _dispatch(self) -> None:
        self._body_consumed = False
        raw_path = self.path or ""
        path = raw_path.split("?", 1)[0]
        kind, value, problem = self._route(path) if path.startswith("/") else (None, None,
                                                                               "not_found")
        try:
            if not self.server.ip_limiter.allow("ip:" + self.client_ip()):
                raise ApproveError(429, "rate_limited", "trop de requêtes, réessayez plus tard")
            if problem == "method":
                self._discard_body()
                return self._not_allowed()
            if kind is None:
                raise ApproveError(404, "not_found", "introuvable")
            if not self._host_ok(kind):
                self.close_connection = True
                raise ApproveError(421, "host", "en-tête Host non admis")
            if kind == "static":
                return self._static(value)
            # corps lu (borné) AVANT les contrôles d'autorisation : la réponse
            # d'erreur part sur une connexion propre
            body = self._read_body() if self.command == "POST" else b""
            if kind == "api":
                return self._api(value, body)
            return self._human(kind, value, body)
        except ApproveError as exc:
            self._discard_body()
            extra = (("Retry-After", "60"),) if exc.status == 429 else ()
            self._error(kind or "api", exc, extra)
        except (ConnectionError, socket.timeout):
            self.close_connection = True       # client parti : rien à répondre
        except Exception as exc:  # pas de trace (ni de donnée) vers le client
            log.error("erreur interne %s sur %s %s", type(exc).__name__, self.command,
                      redact(path)[:120])
            self.close_connection = True
            self._json(500, {"error": "internal", "message": "erreur interne"})

    def _static(self, name: str) -> None:
        entry = self.server.static.get(name)
        if entry is None:
            raise ApproveError(404, "not_found", "introuvable")
        self._send(200, entry[0], entry[1])

    def _api(self, request_id: str | None, raw: bytes) -> None:
        if self.headers.get("Origin") is not None:
            raise ApproveError(403, "origin", "API de service : aucun navigateur admis")
        authorization = self.headers.get("Authorization") or ""
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() != "bearer" or not self.server.service.check_service_token(
                presented.strip()):
            raise ApproveError(401, "unauthorized", "jeton de service absent ou invalide")
        service = self.server.service
        if self.command == "POST":
            return self._json(201, service.create_request(load_json_body(raw)))
        if not REQUEST_ID_RE.fullmatch(request_id or ""):
            raise ApproveError(404, "not_found", "introuvable")
        status, payload = service.receipt(request_id)
        if isinstance(payload, bytes):
            return self._send(status, payload, "application/json; charset=utf-8")
        return self._json(status, payload)

    def _human(self, kind: str, token: str, raw: bytes) -> None:
        if not TOKEN_RE.fullmatch(token or ""):
            raise ApproveError(404, "unknown_link", "lien inconnu")
        token_key = "tok:" + hashlib.sha256(token.encode("ascii")).hexdigest()
        if not self.server.token_limiter.allow(token_key):
            raise ApproveError(429, "rate_limited", "trop de tentatives sur ce lien")
        service = self.server.service
        if self.command == "POST":
            if not self._browser_origin_ok():
                raise ApproveError(403, "origin", "origine de la requête non autorisée")
            body = load_json_body(raw)
            if kind == "approve":
                return self._json(200, service.submit(token, body))
            return self._json(200, service.enroll_submit(
                token, body, origin=self.headers.get("Origin")))
        if kind == "approve":
            return self._html(200, render.approval_page(service.link_view(token)))
        return self._html(200, render.enroll_page(service.enroll_view(token)))


def make_server(service: ApproveService, *, bind: str | None = None,
                port: int | None = None) -> ApproveHTTPServer:
    cfg = service.cfg
    return ApproveHTTPServer(service, (bind or cfg.bind, cfg.port if port is None else port))


def serve_in_thread(server: ApproveHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              daemon=True, name="ameesh-approve")
    thread.start()
    return thread
