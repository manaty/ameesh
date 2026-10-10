# SPDX-License-Identifier: AGPL-3.0-only
"""Transport HTTP de l'exécuteur médié (`/api/exec/v1`, lot L109).

`HttpTransport` implémente `mediated_executor.interfaces.ExecTransport` avec
la bibliothèque standard (`http.client`) :

* **jeton** : `Authorization: Bearer …`, tiré d'une `TokenSource` (L110 en
  fournit l'implémentation réelle ; ici, une source fixe et une source
  fichier). Un 401 `token_expired` / `token_invalid` déclenche UN
  renouvellement (`access_token(refresh=True)`) et un seul nouvel essai ;
* **idempotence** : l'appelant tire la clé une fois par appel logique ; le
  transport la renvoie telle quelle à chaque nouvel essai ;
* **nouvelles tentatives** : coupure, délai, 429, 5xx — quelques essais
  rapprochés (attente bornée, `Retry-After` respecté jusqu'à la borne), puis
  `Unavailable` : les reprises L72 de l'exécuteur prennent le relais ;
* **erreurs** : `contract.client_exception` (`Forbidden`, `ExecutorRevoked`,
  `NotSupportedRemotely`, `DbError`, `Unavailable`).

TLS obligatoire, sauf vers la boucle locale (essais, mandataire local).
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse
from typing import Any, Callable, Iterator, Mapping, Optional

from ... import db as db_mod
from ...mediated_executor import contract as C
from ...mediated_executor import events as E
from ...mediated_executor import gate as P
from ...mediated_executor.interfaces import (
    ENV_SESSION_TOKEN, ExecTransport, HostInfo, IssuedToken, TokenSource)

#: délai d'une requête ordinaire (secondes)
REQUEST_TIMEOUT_S = 20.0
#: délai de lecture du flux SSE : au-delà de trois battements manqués, la
#: connexion est tenue pour morte
STREAM_READ_TIMEOUT_S = 3 * E.PING_INTERVAL_S
#: nouveaux essais d'une requête en panne passagère (en plus du premier)
RETRIES = 2
#: attente entre deux essais (doublée), et borne de `Retry-After`
RETRY_BASE_S = 0.5
RETRY_MAX_S = 5.0

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


class StaticTokenSource(TokenSource):
    """Un jeton fixe (jeton de session `AMEESH_EXEC_TOKEN`, essais). Il ne se
    renouvelle pas : un 401 après `refresh` reste un refus."""

    def __init__(self, token: str):
        self._token = token or ""

    def access_token(self, *, refresh: bool = False) -> str:
        if not self._token:
            raise db_mod.DbError("aucun jeton d'exécuteur médié")
        return self._token


class FileTokenSource(TokenSource):
    """Le jeton lu dans un fichier 0600 (`exec_token_file`) ; `refresh` le
    relit. Point d'attache de L110 : son agent d'identité y dépose le jeton
    d'accès (ou `remote.set_token_source_factory` le remplace)."""

    def __init__(self, path: str):
        self.path = path
        self._token = ""
        self._lock = threading.Lock()

    def access_token(self, *, refresh: bool = False) -> str:
        with self._lock:
            if refresh or not self._token:
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        self._token = fh.read().strip()
                except OSError as exc:
                    raise db_mod.Unavailable("jeton d'exécuteur illisible (%s) : %s"
                                             % (self.path, exc.strerror or exc))
            if not self._token:
                raise db_mod.Unavailable("jeton d'exécuteur vide (%s)" % self.path)
            return self._token


def session_token_from_env(env: Mapping[str, str] | None = None) -> Optional[StaticTokenSource]:
    """La source du jeton de session posé par l'exécuteur pour le harnais."""
    token = (env if env is not None else os.environ).get(ENV_SESSION_TOKEN) or ""
    return StaticTokenSource(token) if token else None


class _Response:
    __slots__ = ("status", "headers", "body", "raw")

    def __init__(self, status: int, headers: Mapping[str, str], raw: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.raw = raw
        try:
            self.body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            self.body = None


def check_url(url: str) -> urllib.parse.SplitResult:
    """L'URL du serveur : https, ou http vers la boucle locale seulement."""
    parts = urllib.parse.urlsplit((url or "").rstrip("/"))
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("URL du serveur d'exécuteur invalide : %r" % url)
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ValueError("URL du serveur d'exécuteur : https obligatoire hors de la "
                         "boucle locale (%s)" % url)
    return parts


def https_proxy_for(host: str, environ: Optional[Mapping[str, str]] = None
                    ) -> Optional[tuple]:
    """Le mandataire HTTPS à prendre pour joindre `host` : `(hôte, port,
    en-têtes du CONNECT)`, ou None (pas de `HTTPS_PROXY`, hôte dans
    `NO_PROXY`, boucle locale). Un mandataire `user:mot@hôte` donne
    `Proxy-Authorization: Basic`. Seul un mandataire `http://` est pris
    (le CONNECT part en clair jusqu'à lui, le TLS reste de bout en bout)."""
    env = os.environ if environ is None else environ
    if host in _LOOPBACK:
        return None
    raw = ""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if env.get(name):
            raw = env[name].strip()
            break
    if not raw:
        return None
    no_proxy = env.get("NO_PROXY", env.get("no_proxy", ""))
    for entry in (e.strip().lower() for e in no_proxy.split(",")):
        if not entry:
            continue
        if entry == "*":
            return None
        entry = entry.split(":")[0].lstrip(".")
        h = host.lower()
        if h == entry or h.endswith("." + entry):
            return None
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError("HTTPS_PROXY : mandataire http://hôte:port attendu (%s)"
                         % parts.scheme)
    headers = {}
    if parts.username is not None:
        user = urllib.parse.unquote(parts.username)
        word = urllib.parse.unquote(parts.password or "")
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
            ("%s:%s" % (user, word)).encode("utf-8")).decode("ascii")
    return parts.hostname, parts.port or 3128, headers


class HttpTransport(ExecTransport):
    """`ExecTransport` sur HTTP(S). `tokens` : jeton d'exécuteur (routes
    `op`, `events`, `host`…) ; `session_tokens` : jeton de session
    (`session/op`). L'un ou l'autre peut manquer : la route concernée lève
    alors `DbError` sans appel réseau."""

    def __init__(self, url: str, *, tokens: Optional[TokenSource] = None,
                 session_tokens: Optional[TokenSource] = None,
                 timeout: float = REQUEST_TIMEOUT_S, retries: int = RETRIES,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.parts = check_url(url)
        self.url = urllib.parse.urlunsplit((self.parts.scheme, self.parts.netloc,
                                            self.parts.path, "", ""))
        self.tokens = tokens
        self.session_tokens = session_tokens
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.ssl_context = ssl_context
        self._sleep = sleep

    # -- bas niveau ----------------------------------------------------------
    def _path(self, route: str, **values: str) -> str:
        method, path = C.ROUTES[route]
        path = path.format(**{k: urllib.parse.quote(v, safe="") for k, v in values.items()})
        return self.parts.path.rstrip("/") + C.PREFIX + path

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        host, port = self.parts.hostname, self.parts.port
        if self.parts.scheme == "https":
            context = self.ssl_context or ssl.create_default_context()
            proxy = https_proxy_for(host)
            if proxy is not None:
                # L114b : mandataire de l'appareil (`HTTPS_PROXY`, `NO_PROXY`
                # respecté) : tunnel CONNECT, TLS de bout en bout jusqu'au
                # serveur du mesh
                conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout,
                                                   context=context)
                conn.set_tunnel(host, port or 443, headers=proxy[2])
                return conn
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _token(self, source: Optional[TokenSource], refresh: bool = False) -> str:
        if source is None:
            raise db_mod.DbError("jeton absent pour cette route de l'exécuteur médié")
        return source.access_token(refresh=refresh)

    def _once(self, method: str, path: str, body: Any, headers: dict,
              timeout: Optional[float] = None) -> _Response:
        data = None
        if isinstance(body, (bytes, bytearray)):
            # dépôt de travail (L113) : paquet git brut
            data = bytes(body)
            if len(data) > C.MAX_BUNDLE_BYTES:
                raise db_mod.DbError("paquet trop gros pour %s (%d octets)" % (path, len(data)))
            headers = dict(headers, **{"Content-Type": "application/octet-stream"})
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > C.MAX_BODY_BYTES:
                raise db_mod.DbError("corps trop gros pour %s (%d octets)" % (path, len(data)))
            headers = dict(headers, **{"Content-Type": "application/json"})
        conn = self._connection(self.timeout if timeout is None else timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return _Response(resp.status, dict(resp.getheaders()), resp.read())
        except (OSError, http.client.HTTPException) as exc:
            raise db_mod.Unavailable("serveur d'exécuteur injoignable (%s %s) : %s"
                                     % (method, path, " ".join(str(exc).split()) or
                                        type(exc).__name__))
        finally:
            conn.close()

    def _retry_wait(self, attempt: int, resp: Optional[_Response]) -> float:
        wait = RETRY_BASE_S * (2 ** attempt)
        if resp is not None:
            hint = resp.headers.get("retry-after") or (
                resp.body.get("retry_after") if isinstance(resp.body, dict) else None)
            try:
                if hint is not None:
                    wait = max(wait, float(hint))
            except (TypeError, ValueError):
                pass
        return min(RETRY_MAX_S, wait)

    def request(self, method: str, path: str, body: Any = None, *,
                source: Optional[TokenSource] = None, auth: bool = True,
                headers: Optional[Mapping[str, str]] = None,
                timeout: Optional[float] = None) -> _Response:
        """Une requête, avec renouvellement du jeton (une fois) et nouveaux
        essais en panne passagère. Rend une réponse 2xx, sinon lève
        l'exception de `contract.client_exception`."""
        refreshed = False
        attempt = 0
        while True:
            hdrs = dict(headers or {})
            hdrs.setdefault("Accept", "application/json")
            if auth:
                hdrs["Authorization"] = "Bearer " + self._token(source, refresh=False)
            resp = None
            try:
                resp = self._once(method, path, body, hdrs, timeout)
            except db_mod.Unavailable:
                if attempt >= self.retries:
                    raise
                self._sleep(self._retry_wait(attempt, None))
                attempt += 1
                continue
            if 200 <= resp.status < 300:
                return resp
            code = resp.body.get("error") if isinstance(resp.body, dict) else None
            spec = C.ERRORS.get(code or "")
            if auth and spec is not None and spec.client == "retry_token" and not refreshed:
                refreshed = True
                self._token(source, refresh=True)
                continue
            transient = resp.status == 429 or resp.status >= 500
            if transient and attempt < self.retries:
                self._sleep(self._retry_wait(attempt, resp))
                attempt += 1
                continue
            raise C.client_exception(resp.status, resp.body)

    # -- ExecTransport ---------------------------------------------------------
    def _op(self, route: str, request: C.OpRequest, idempotency_key: Optional[str],
            source: Optional[TokenSource]) -> C.OpResult:
        headers = {}
        if idempotency_key:
            headers[C.IDEMPOTENCY_HEADER] = idempotency_key
        resp = self.request("POST", self._path(route), request.to_json(), source=source,
                            headers=headers)
        try:
            return C.OpResult.from_json(resp.body)
        except (ValueError, TypeError) as exc:
            raise db_mod.DbError("réponse illisible du serveur pour %s : %s"
                                 % (request.op, exc))

    def call(self, request: C.OpRequest, *, idempotency_key: Optional[str]) -> C.OpResult:
        return self._op("op", request, idempotency_key, self.tokens)

    def session_call(self, request: C.OpRequest, *,
                     idempotency_key: Optional[str]) -> C.OpResult:
        return self._op("session_op", request, idempotency_key, self.session_tokens)

    def poll_events(self, after: Optional[str], wait: float) -> tuple[list, str]:
        query = {"wait": "%g" % max(0.0, min(float(wait), E.MAX_WAIT_S))}
        if after:
            query["after"] = after
        path = self._path("events") + "?" + urllib.parse.urlencode(query, safe=":")
        # l'attente longue tient la connexion `wait` secondes : délai allongé
        resp = self.request("GET", path, source=self.tokens,
                            timeout=max(self.timeout, float(wait) + 10.0))
        try:
            return E.parse_long_poll(resp.body or {})
        except (ValueError, KeyError, TypeError) as exc:
            raise db_mod.DbError("flux d'événements illisible : %s" % exc)

    def stream_events(self, after: Optional[str]) -> Iterator[E.Event]:
        """SSE. Lève `NotSupportedRemotely` si le serveur ne sert pas de flux
        (réponse qui n'est pas `text/event-stream`) : l'appelant passe alors
        à l'attente longue ; `Unavailable` à la coupure."""
        headers = {"Accept": "text/event-stream",
                   "Authorization": "Bearer " + self._token(self.tokens)}
        if after:
            headers["Last-Event-ID"] = after
        conn = self._connection(STREAM_READ_TIMEOUT_S)
        try:
            try:
                conn.request("GET", self._path("events"), headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise db_mod.Unavailable("flux d'événements injoignable : %s"
                                         % (" ".join(str(exc).split()) or type(exc).__name__))
            if resp.status != 200:
                raw = resp.read()
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, ValueError):
                    body = None
                code = body.get("error") if isinstance(body, dict) else None
                spec = C.ERRORS.get(code or "")
                if spec is not None and spec.client == "retry_token":
                    # le jeton est renouvelé ; la reconnexion de l'appelant le prendra
                    self._token(self.tokens, refresh=True)
                    raise db_mod.Unavailable("flux d'événements : jeton renouvelé")
                raise C.client_exception(resp.status, body)
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip()
            if ctype != "text/event-stream":
                resp.read()
                raise C.NotSupportedRemotely("flux SSE non servi (%s)" % (ctype or "?"))

            def lines() -> Iterator[str]:
                while True:
                    try:
                        raw = resp.readline()
                    except (OSError, http.client.HTTPException) as exc:
                        raise db_mod.Unavailable("flux d'événements coupé : %s"
                                                 % (" ".join(str(exc).split())
                                                    or type(exc).__name__))
                    if not raw:
                        raise db_mod.Unavailable("flux d'événements fermé par le serveur")
                    yield raw.decode("utf-8", "replace")

            for event in E.parse_sse(lines()):
                yield event
        finally:
            conn.close()

    def host_info(self) -> HostInfo:
        resp = self.request("GET", self._path("host"), source=self.tokens)
        try:
            return HostInfo.from_json(resp.body or {})
        except TypeError as exc:
            raise db_mod.DbError("fiche d'hôte illisible : %s" % exc)

    def put_availability(self, state: P.GateState) -> None:
        self.request("PUT", self._path("availability"), P.availability_body(state),
                     source=self.tokens)

    def session_token(self, fence: C.Fence) -> IssuedToken:
        resp = self.request("POST", self._path("session_token"), {"fence": fence.to_json()},
                            source=self.tokens)
        body = resp.body or {}
        try:
            return IssuedToken(token=str(body["access_token"]),
                               expires_ts=float(body.get("expires_ts") or 0.0),
                               kind="session")
        except (KeyError, TypeError, ValueError) as exc:
            raise db_mod.DbError("jeton de session illisible : %s" % exc)

    # -- dépôt de travail (L113) -------------------------------------------------
    def _bundle_headers(self, fence: C.Fence) -> dict:
        return {C.HDR_LEASE_OWNER: fence.owner, C.HDR_LEASE_EPOCH: str(int(fence.epoch))}

    def get_work(self, fence: C.Fence) -> tuple:
        """`GET /work/{agent}/bundle` : (archive tar du commit, en-têtes)."""
        resp = self.request("GET", self._path("bundle_in", agent=fence.agent),
                            source=self.tokens, headers=dict(self._bundle_headers(fence),
                                                             Accept="application/x-tar"),
                            timeout=max(self.timeout, 120.0))
        return resp.raw, resp.headers

    def put_work(self, fence: C.Fence, base: str, bundle: bytes) -> dict:
        """`POST /work/{agent}/bundle` : le paquet git des commits de
        l'appareil après `base` ; rend `ameesh-exec-bundle/1`."""
        headers = dict(self._bundle_headers(fence), **{C.HDR_BASE: base})
        resp = self.request("POST", self._path("bundle_out", agent=fence.agent), bundle,
                            source=self.tokens, headers=headers,
                            timeout=max(self.timeout, 120.0))
        return resp.body or {}

    # -- hors interface --------------------------------------------------------
    def health(self) -> dict:
        """`GET /health` (sans jeton) : version du contrat et heure du serveur."""
        return self.request("GET", self._path("health"), auth=False).body or {}

