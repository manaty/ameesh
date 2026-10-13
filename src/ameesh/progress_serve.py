# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh progress --serve`: the progress page served locally (lot L304).

  ameesh progress --serve [--port 8765] [--project P] [--since 24h] [--stale-after 6h] [--no-gantt]

A standard-library HTTP server, **bound to 127.0.0.1 only** (no host
option), **read-only**: GET and HEAD, nothing else (405), and a Postgres
session with `default_transaction_read_only` — a write would fail in the
database itself.

  /                      the page, **without data** (no token needed): it
                         reads /api/progress.json, again at each Refresh
  /api/progress.json     the `ameesh-progress/1` snapshot, with
                         `Authorization: Bearer <token>` only (`?project=P`:
                         a known project, 400 otherwise)

The host is shared (several agents, several Unix accounts): 127.0.0.1 does
not keep them out. A random **token**, drawn at each start, is required by
the API. The URL printed at start carries it in the **fragment** (`/#t=…`),
which the browser never sends: the page moves it to `sessionStorage`
(isolated per origin, hence per port) and sends it in an `Authorization`
header. No cookie: browsers share localhost cookies across ports, so any
local server on another port would receive them. A custom header also makes
any cross-origin fetch a preflight, which gets 405. The token is compared in
constant time and only written on the command's output, never in a log nor
in an error page: request lines are logged without their query string, and
error pages never echo the request.

The Host header must name 127.0.0.1 or localhost on the right port (against
DNS rebinding). A snapshot is kept `CACHE_S` seconds per project: an open
tab does not hammer the database.
"""
from __future__ import annotations

import dataclasses
import hmac
import http.server
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse

from . import db as db_mod
from . import progress

BIND = "127.0.0.1"
DEFAULT_PORT = 8765
#: durée de vie d'un instantané servi (secondes)
CACHE_S = 5.0
API = "/api/progress.json"
_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_LINE_RE = re.compile(r"^([A-Z]{1,10}) (/[A-Za-z0-9._/-]{0,200})")
_HEADERS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
            ("Cross-Origin-Resource-Policy", "same-origin"))


class ServeError(ValueError):
    """Requête refusée : (statut HTTP, message)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Snapshots:
    """Les instantanés servis : construits à la demande, gardés `cache_s`.

    `build(project)` rend l'instantané d'un projet (None : tous). Une seule
    construction à la fois : deux onglets ne doublent pas la lecture."""

    def __init__(self, build, *, cache_s: float = CACHE_S, clock=time.monotonic):
        self._build = build
        self._cache_s = float(cache_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict = {}

    def get(self, project: str | None) -> dict:
        with self._lock:
            hit = self._cache.get(project)
            if hit is not None and self._clock() - hit[0] < self._cache_s:
                return hit[1]
            snap = self._build(project)
            self._cache[project] = (self._clock(), snap)
            return snap

    def known_projects(self) -> set:
        """Les noms de projet que `?project=` accepte : ceux de l'instantané
        sans filtre (projets des agents et des lots ouverts)."""
        snap = self.get(None)
        names = {p.get("name") for p in snap.get("projects") or []}
        names |= {l.get("project") for l in snap.get("lots") or []}
        return {n for n in names if n}


def db_builder(cfg, *, since: str, stale_after: float, gantt: bool,
               versions: bool = True):
    """La construction réelle : une connexion en lecture seule par instantané.

    L160f (#497) : un seul lecteur GitHub pour tout le serveur (son cache
    mémoire n'est pas reconstruit à chaque instantané). Le serveur lit GitHub
    par le jeton (`GH_TOKEN`/`GITHUB_TOKEN`) : il n'appelle jamais la CLI
    `gh` (mesh-design) ; sans jeton, la vue dit le manque."""
    ro = dataclasses.replace(cfg, read_only=True)
    reader = None
    if versions:
        from . import versions as versions_mod
        reader = versions_mod.Reader(allow_gh=False)

    def build(project):
        db = db_mod.connect(ro)
        try:
            db_mod.require_schema(db, defer=True)
            return db_mod.batched(db, lambda db: progress.snapshot(
                db, ro, since=since, project=project, stale_after=stale_after, gantt=gantt,
                github=reader))
        finally:
            db.close()
    return build


class Handler(http.server.BaseHTTPRequestHandler):
    """Lecture seule : GET et HEAD ; tout le reste rend 405."""

    server_version = "ameesh-progress"
    sys_version = ""
    #: posés par `make_server`
    snapshots: Snapshots
    token: str
    default_project: str | None = None

    # -- garde --------------------------------------------------------------

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        return host in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _bearer(self) -> str:
        scheme, _, value = (self.headers.get("Authorization") or "").strip().partition(" ")
        return value.strip() if scheme.lower() == "bearer" else ""

    def _token_ok(self, value: str) -> bool:
        return bool(value) and hmac.compare_digest(value.encode(), self.token.encode())

    # -- réponses -----------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in tuple(_HEADERS) + tuple(extra):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, extra=()) -> None:
        self._send(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8", extra)

    def _refuse(self) -> None:
        self._error(405, "read-only: GET and HEAD only", (("Allow", "GET, HEAD"),))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = \
        lambda self: self._refuse()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            self._error(403, "host refused: open http://127.0.0.1:%d/"
                        % self.server.server_address[1])
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            # la page seule, sans données : rien à protéger
            html = progress.render_html(None, served=True)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if url.path != API:
            self._error(404, "not found: / or %s" % API)
            return
        if not self._token_ok(self._bearer()):
            self._error(401, "token missing or refused: open the URL printed at start "
                             "by ameesh progress --serve", (("WWW-Authenticate", "Bearer"),))
            return
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        try:
            project = self._project(query)
            data = json.dumps(self.snapshots.get(project), ensure_ascii=False, sort_keys=True)
            self._send(200, data.encode("utf-8"), "application/json; charset=utf-8")
        except ServeError as exc:
            self._error(exc.status, str(exc))
        except (progress.ProgressError, db_mod.DbError, db_mod.Unavailable,
                db_mod.SchemaMissing) as exc:
            self._error(503, "snapshot unavailable: %s" % exc)

    def _project(self, query: dict) -> str | None:
        values = query.get("project")
        if not values:
            return self.default_project
        name = values[0]
        if not name:
            return None
        if not _PROJECT_RE.match(name) or name not in self.snapshots.known_projects():
            raise ServeError(400, "unknown project: %s" % name[:64])
        return name

    def send_error(self, code, message=None, explain=None) -> None:
        # jamais la requête dans la page d'erreur ni dans le journal : le
        # module standard y recopie la ligne reçue (« Bad request syntax
        # ('GET /?t=… ') »), qui pourrait porter un jeton
        try:
            phrase = http.HTTPStatus(code).phrase
        except ValueError:
            phrase = "error"
        self.close_connection = True
        self.log_message("code %d, %s", code, phrase)
        self._error(code, "%d %s" % (code, phrase))

    def log_message(self, fmt: str, *args) -> None:
        # jamais la chaîne de requête (elle peut porter le jeton) : la
        # méthode, le chemin et le statut seulement
        if fmt.startswith('"%s"') and args:
            # une ligne mal formée ne passe pas : rien que « MÉTHODE /chemin »
            m = _LINE_RE.match(str(args[0]))
            args = ((m.group(1) + " " + m.group(2)) if m else "(malformed request)",) + tuple(args[1:])
        sys.stderr.write("progress --serve: %s\n" % (fmt % args))


def make_server(snapshots: Snapshots, *, port: int = DEFAULT_PORT, token: str | None = None,
                project: str | None = None) -> http.server.ThreadingHTTPServer:
    """Le serveur, lié à 127.0.0.1 (port 0 : un port libre). Son jeton est
    `server.token` ; un nouveau à chaque appel si `token` n'est pas donné."""
    token = token or secrets.token_urlsafe(24)
    handler = type("ProgressHandler", (Handler,), {
        "snapshots": snapshots, "token": token, "default_project": project})
    try:
        server = http.server.ThreadingHTTPServer((BIND, int(port)), handler)
    except OSError as exc:
        raise progress.ProgressError("port %s unavailable on %s: %s"
                                     % (port, BIND, exc.strerror or exc))
    server.daemon_threads = True
    server.token = token
    return server


def url_of(server) -> str:
    """L'URL à ouvrir : le jeton dans le fragment, que le navigateur n'envoie pas."""
    return "http://%s:%d/#t=%s" % (BIND, server.server_address[1], server.token)


def serve(cfg, *, port: int, since: str, stale_after: float, gantt: bool,
          project: str | None = None, versions: bool = True, out=None) -> int:
    """`ameesh progress --serve` : sert jusqu'à Ctrl-C."""
    out = out or sys.stdout
    snapshots = Snapshots(db_builder(cfg, since=since, stale_after=stale_after, gantt=gantt,
                                     versions=versions))
    # une première lecture : base injoignable ou schéma absent, dit tout de suite
    snapshots.get(project)
    server = make_server(snapshots, port=port, project=project)
    print("progress page (read-only): %s" % url_of(server),
          file=out, flush=True)
    print("Ctrl-C to stop; a restart draws a new token.", file=out, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["BIND", "DEFAULT_PORT", "Snapshots", "make_server", "serve", "url_of"]
