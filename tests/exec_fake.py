# SPDX-License-Identifier: AGPL-3.0-only
"""Faux serveur d'exécuteur médié (L109), nourri des jeux dorés de L107.

* `GoldenTransport` : un `ExecTransport` en mémoire. Une requête qui
  correspond à un cas doré (route, op, args, kwargs, fence) reçoit sa réponse
  dorée ; `script[op]` remplace la réponse d'une opération (valeur, `Fenced`,
  exception, ou fonction) ; tout le reste est une erreur du test.
* `GoldenServer` : le même répondeur derrière un vrai serveur HTTP local
  (`http.server`), pour essayer `HttpTransport` et les commandes de session.

Aucune base : ces essais tournent sans Postgres.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ameesh.executeur_mediee import contrat as C
from ameesh.executeur_mediee import evenements as E
from ameesh.executeur_mediee.interfaces import ExecTransport, HostInfo, IssuedToken

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DORE = os.path.join(REPO, "tests", "dore", "executeur_mediee")
ACCESS = "amx1.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SESSION = "ams1.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
OWNER = "exec:7f3a:anna-portable:4121"


def golden(name: str) -> dict:
    with open(os.path.join(DORE, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def op_cases() -> list:
    out = []
    for fn in sorted(os.listdir(DORE)):
        g = golden(fn[:-5])
        if g["famille"] in ("erreurs", "evenements", "porte", "identite"):
            continue
        out.extend(g["cas"])
    return out


def error_cases() -> list:
    return golden("erreurs")["cas"]


def _key(route: str, body: dict) -> str:
    return json.dumps([route, body.get("op"), body.get("args") or [],
                       body.get("kwargs") or {}, body.get("fence")], sort_keys=True)


class Fenced:
    """Réponse scriptée : bail perdu (`fenced: true`, valeur de refus)."""

    def __init__(self, value: Any = None, *, use_refusal: bool = True):
        self.value = value
        self.use_refusal = use_refusal


class Seq:
    """Réponse scriptée : une suite de réponses, la dernière se répète."""

    def __init__(self, *items):
        self.items = list(items)

    def next(self):
        return self.items.pop(0) if len(self.items) > 1 else self.items[0]


class Responder:
    """Le répondeur commun au transport en mémoire et au serveur HTTP.

    `respond(route, body, idempotency_key)` → (statut, en-têtes, corps)."""

    def __init__(self, *, include_errors: bool = False):
        self.contract = C.load()
        self.cases: dict[str, list] = {}
        for case in op_cases() + (error_cases() if include_errors else []):
            route = "session/op" if case["requete"]["chemin"].endswith("/session/op") else "op"
            self.cases.setdefault(_key(route, case["requete"]["corps"]), []).append(case)
        #: réponses imposées par opération (prioritaires sur les cas dorés)
        self.script: dict[str, Any] = {}
        #: journal : (route, corps, clé d'idempotence)
        self.calls: list[tuple] = []
        self.lock = threading.Lock()
        self.server_ts = 1791640000.12

    def ops(self) -> list[str]:
        return [c[1]["op"] for c in self.calls]

    @classmethod
    def only(cls, case: dict) -> "Responder":
        """Un répondeur qui ne connaît que ce cas doré."""
        responder = cls()
        route = "session/op" if case["requete"]["chemin"].endswith("/session/op") else "op"
        responder.cases = {_key(route, case["requete"]["corps"]): [case]}
        return responder

    def _scripted(self, name: str, body: dict) -> Optional[tuple]:
        if name not in self.script:
            return None
        item = self.script[name]
        if isinstance(item, Seq):
            item = item.next()
        if callable(item) and not isinstance(item, type):
            item = item(body)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], int):
            status, corps = item  # erreur HTTP : (statut, corps d'erreur)
            return status, {}, corps
        if isinstance(item, Fenced):
            value = self.contract.get(name).refusal if item.use_refusal else item.value
            return 200, {}, C.OpResult(value, self.server_ts, fenced=True).to_json()
        return 200, {}, C.OpResult(item, self.server_ts).to_json()

    def respond(self, route: str, body: dict, idempotency_key: Optional[str]) -> tuple:
        with self.lock:
            self.calls.append((route, body, idempotency_key))
        name = body.get("op")
        scripted = self._scripted(name, body)
        if scripted is not None:
            return scripted
        found = self.cases.get(_key(route, body))
        if not found:
            raise AssertionError("requête sans cas doré ni script : %s %s"
                                 % (route, json.dumps(body, ensure_ascii=False)))
        case = found[0]
        rep = case["reponse"]
        return rep["statut"], dict(rep.get("entetes") or {}), rep["corps"]


class GoldenTransport(ExecTransport):
    """`ExecTransport` en mémoire sur un `Responder`."""

    def __init__(self, responder: Optional[Responder] = None):
        self.responder = responder or Responder()
        self.events: list = []          # événements rendus par `stream_events`
        self.stream_error: Optional[BaseException] = None
        self.stream_calls: list = []
        self.poll_calls: list = []
        self.polls: list = []           # [(événements, curseur)] rendus par `poll_events`
        self.host = HostInfo.from_json(golden("identite")["host"]["reponse"]["corps"])
        self.host_error: Optional[BaseException] = None
        self.session_tokens: list = []
        self.url = "https://mesh.exemple"

    @property
    def calls(self) -> list:
        return self.responder.calls

    def ops(self) -> list[str]:
        return self.responder.ops()

    def _result(self, route: str, request: C.OpRequest, key: Optional[str]) -> C.OpResult:
        status, _headers, corps = self.responder.respond(route, request.to_json(), key)
        if status != 200:
            raise C.client_exception(status, corps)
        return C.OpResult.from_json(corps)

    def call(self, request, *, idempotency_key):
        return self._result("op", request, idempotency_key)

    def session_call(self, request, *, idempotency_key):
        return self._result("session/op", request, idempotency_key)

    def poll_events(self, after, wait):
        self.poll_calls.append((after, wait))
        if not self.polls:
            threading.Event().wait(min(wait, 0.05))
            return [], after or ""
        item = self.polls.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def stream_events(self, after):
        self.stream_calls.append(after)
        events, self.events = list(self.events), []
        for event in events:
            yield event
        if self.stream_error is not None:
            error, self.stream_error = self.stream_error, None
            raise error
        threading.Event().wait(0.05)
        from ameesh import db as db_mod
        raise db_mod.Unavailable("flux fermé (faux serveur)")

    def host_info(self):
        if self.host_error is not None:
            raise self.host_error
        return self.host

    def put_availability(self, state):
        self.availability = state

    def session_token(self, fence):
        self.session_tokens.append(fence)
        body = golden("identite")["session_token"]["reponse"]["corps"]
        return IssuedToken(body["access_token"], body["expires_ts"], "session")


class GoldenServer:
    """Un serveur HTTP local (`http://127.0.0.1:<port>`) qui sert `/api/exec/v1`
    à partir d'un `Responder` ; journal des en-têtes reçus dans `requests`."""

    def __init__(self, responder: Optional[Responder] = None):
        self.responder = responder or Responder(include_errors=False)
        self.requests: list[dict] = []
        #: réponses imposées par (méthode, chemin sans requête) : liste de
        #: (statut, en-têtes, corps ou texte) consommée dans l'ordre
        self.raw: dict[tuple, list] = {}
        self.access_tokens = {ACCESS}
        self.session_tokens = {SESSION}
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, status: int, headers: dict, body: Any) -> None:
                if isinstance(body, (bytes, str)):
                    data = body.encode("utf-8") if isinstance(body, str) else body
                    ctype = headers.pop("Content-Type", "text/event-stream")
                else:
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    ctype = headers.pop("Content-Type", "application/json")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                path = self.path.split("?", 1)[0]
                server.requests.append({"method": method, "path": self.path,
                                        "headers": dict(self.headers), "body": body})
                queue = server.raw.get((method, path))
                if queue:
                    status, headers, payload = queue.pop(0)
                    return self._send(status, dict(headers), payload)
                auth = (self.headers.get("Authorization") or "")[len("Bearer "):]
                if path.endswith("/session/op"):
                    if auth not in server.session_tokens:
                        return self._send(401, {}, C.error_body("token_invalid", "jeton"))
                    route = "session/op"
                elif path.endswith("/op"):
                    if auth not in server.access_tokens:
                        return self._send(401, {}, C.error_body("token_expired", "jeton"))
                    route = "op"
                else:
                    return self._send(404, {}, C.error_body("op_not_allowed", path))
                try:
                    status, headers, corps = server.responder.respond(
                        route, body, self.headers.get(C.IDEMPOTENCY_HEADER))
                except AssertionError as exc:
                    return self._send(500, {}, C.error_body("internal", str(exc)[:300]))
                return self._send(status, dict(headers), corps)

            def do_POST(self):
                self._handle("POST")

            def do_GET(self):
                self._handle("GET")

            def do_PUT(self):
                self._handle("PUT")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "GoldenServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def queue(self, method: str, route_path: str, *responses) -> None:
        """Réponses imposées à `PREFIX + route_path`, dans l'ordre."""
        self.raw.setdefault((method, C.PREFIX + route_path), []).extend(responses)

    def ops(self) -> list:
        return [r["body"]["op"] for r in self.requests
                if isinstance(r.get("body"), dict) and "op" in r["body"]]


def make_db(transport=None, *, mode: str = "executor", cfg=None):
    """Une `RemoteDb` sur le faux transport (configuration de test minimale)."""
    import dataclasses

    from ameesh import config as config_mod
    from ameesh.storage.remote import RemoteDb
    if cfg is None:
        cfg = dataclasses.replace(config_mod.Config(), backend="mediated",
                                  exec_url="https://mesh.exemple")
    return RemoteDb(cfg, transport or GoldenTransport(), mode=mode,
                    url="https://mesh.exemple")


__all__ = ["ACCESS", "SESSION", "OWNER", "Fenced", "Seq", "Responder", "GoldenTransport",
           "GoldenServer", "golden", "op_cases", "error_cases", "make_db"]
