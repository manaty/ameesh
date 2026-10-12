# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh serve`: the mesh's read API, local mode (L84).

  ameesh serve [--port 8780] [--token-file PATH] [--human human:ID] [--organisation NAME]
  ameesh serve --openapi        prints the OpenAPI 3.1 document (docs/api/openapi-v1.json)

Embeddable interface study (docs/design/etudes/ui-integrable.md, §2.1, §2.4,
§3, §7). A standard-library HTTP server, separate from ameesh-approve (another
process, another origin), **bound to 127.0.0.1 only** (no host option),
**read-only**: GET and HEAD, anything else is 405, and the Postgres session is
`default_transaction_read_only`.

Local mode (v1): the human is the machine's owner (`--human`, otherwise
`notify.default_human`, otherwise the only declared human, otherwise
`human:$USER`); every `/api/v1` route except `openapi.json` requires
`Authorization: Bearer <token>`, the token being read from a `0600` file
(created at the first start, never overwritten, never printed). The host is
shared by several agents and accounts: 127.0.0.1 does not keep them out, the
token does.

Each response is the **same** serialisation as the matching `--json` output
of the CLI (one source of truth, the builders are shared); a list is wrapped
in an object that carries its `schema`. Errors are `ameesh-error/1`.

Defences (shared with ameesh-approve, `httpkit`): Host header checked
against the loopback names and the port (DNS rebinding), connections capped,
socket timeout, rate limit, no request echoed in errors or logs (the path
only, never the query string).

`GET /.well-known/ameesh` (discovery) and `GET /api/v1/openapi.json` need no
token: they say nothing about the mesh's state.

`GET /api/v1/events` (L85) streams the event log (`events`, migration 0396)
as Server-Sent Events, read through `fetch` (the client carries the
`Authorization` header). One `LISTEN ameesh_events` connection per process
(`Hub`) wakes the streams; each stream rereads the log after its own
position (tx, id) in commit order (`events`), keeps what its human may see
(`Context.sees`), and sends `id: <tx-id>` (the position of THAT event),
`event: <type>`, `data: <json>`. `Last-Event-ID` resumes strictly after it
(replay from the table, exactly once); `event: reset` says that events were
lost (retention, or a rebuilt base): the client reloads its views. A comment every 20 s keeps the connection
alive; a stream ends after an hour (the client resumes); at most
`MAX_STREAMS` streams at once, the others are 503 with `Retry-After`.
"""
from __future__ import annotations

import argparse
import dataclasses
import http
import http.server
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from typing import Callable

from . import __version__, events, httpkit, storage
from . import db as db_mod

BIND = "127.0.0.1"
DEFAULT_PORT = 8780
DEFAULT_TOKEN_FILE = "~/.config/ameesh/serve-token"
TOKEN_FILE_ENV = "AMEESH_SERVE_TOKEN_FILE"
API = "/api/v1"
OPENAPI_PATH = API + "/openapi.json"
WELL_KNOWN = "/.well-known/ameesh"
#: simultaneous connections, socket timeout, requests per minute (one human)
MAX_CONNECTIONS = 16
SOCKET_TIMEOUT = 15.0
RATE_PER_MINUTE = 600
#: lots listed at most (`?limit=`)
MAX_LOTS = 500
LANGUAGES = ("fr", "en")
#: an agent, host or project name; a lot id; a thread entry id; a duration
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{0,127}$")
_LOT_RE = re.compile(r"^[0-9]{1,9}$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9._/#:-]{1,256}$")
_SINCE_RE = re.compile(r"^[0-9]{1,6}[mhdj]$|^[0-9]{4}-[0-9]{2}-[0-9]{2}[0-9T:+.Z-]{0,20}$")
_LINE_RE = re.compile(r"^([A-Z]{1,10}) (/[A-Za-z0-9._/@:+-]{0,200})")
CSP = "default-src 'none'; frame-ancestors 'none'"
#: event stream (L85): streams at once (below MAX_CONNECTIONS: the other
#: routes stay reachable), heartbeat, lifetime, events read per query, the
#: client's reconnection delay, the poll without LISTEN
EVENTS_PATH = API + "/events"
MAX_STREAMS = 8
HEARTBEAT_S = 20.0
STREAM_MAX_S = 3600.0
EVENTS_BATCH = 200
RETRY_MS = 3000
POLL_S = 5.0
#: an event held back by an open transaction is looked for again this often
WAITING_POLL_S = 1.0

log = logging.getLogger("ameesh.serve")


class ApiError(Exception):
    """A refused request: HTTP status, error code, message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclasses.dataclass(frozen=True)
class Context:
    """What a route reads: the configuration (read-only) and who asks."""

    cfg: object
    human: str
    organisation: str | None = None

    def sees(self, event: dict) -> bool:
        """Whether this human may see `event` (local mode: the owner sees
        everything; L86 filters by project and role)."""
        return True


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Route:
    path: str                       # OpenAPI template, /api/v1/lots/{id}
    schema: str                     # schema of the response
    summary: str
    build: Callable                 # (ctx, db, params, query) -> dict
    query: tuple = ()               # (name, OpenAPI schema, description)
    db: bool = True                 # reads the database
    auth: bool = True
    stream: bool = False            # Server-Sent Events (L85), `build` unused

    @property
    def regex(self) -> re.Pattern:
        pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(self.path))
        return re.compile("^" + pattern + "$")


def _one(query: dict, name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise ApiError(400, "bad_query", "%s given more than once" % name)
    return values[0]


def _checked(value: str | None, pattern: re.Pattern, what: str) -> str | None:
    if value is None or value == "":
        return None
    if not pattern.match(value):
        raise ApiError(400, "bad_query", "%s refused" % what)
    return value


def _flag(query: dict, name: str) -> bool:
    value = _one(query, name)
    if value in (None, "", "0", "false"):
        return False
    if value in ("1", "true"):
        return True
    raise ApiError(400, "bad_query", "%s: 1 or 0" % name)


def _me(ctx: Context, db, params, query) -> dict:
    return {"schema": "ameesh-me/1", "human": ctx.human, "organisation": ctx.organisation,
            "mode": "local", "roles": ["owner"], "deputyships": [],
            "language": LANGUAGES[0]}


def _projects(ctx: Context, db, params, query) -> dict:
    from . import projects
    return projects.snapshot(db, project=_checked(_one(query, "project"), _NAME_RE, "project"),
                             viewer=ctx.human)


def _progress(ctx: Context, db, params, query) -> dict:
    from . import progress, stagnation
    since = _checked(_one(query, "since"), _SINCE_RE, "since") or "24h"
    try:
        return db_mod.batched(db, lambda db: progress.snapshot(
            db, ctx.cfg, since=since, project=params["project"],
            stale_after=stagnation.stale_after(None)))
    except progress.ProgressError as exc:
        raise ApiError(400, "bad_query", str(exc)) from exc


def _agents(ctx: Context, db, params, query) -> dict:
    from . import mesh_cli
    return {"schema": "ameesh-agents/1", "agents": mesh_cli.agent_rows(ctx.cfg, db)}


def _agent(ctx: Context, db, params, query) -> dict:
    from . import mesh_cli
    row = mesh_cli.agent_detail(db, params["agent"])
    if row is None:
        raise ApiError(404, "not_found", "unknown agent")
    return {"schema": "ameesh-agent-detail/1", "agent": row}


def _lots(ctx: Context, db, params, query) -> dict:
    from . import mesh_cli, stagnation
    limit = _checked(_one(query, "limit"), _LOT_RE, "limit")
    limit = min(int(limit), MAX_LOTS) if limit else 100
    return {"schema": "ameesh-lots/1", "lots": mesh_cli.work_rows(
        db, state=_checked(_one(query, "state"), _NAME_RE, "state"),
        assignee=_checked(_one(query, "assignee"), _NAME_RE, "assignee"),
        limit=limit, threshold=stagnation.stale_after(None))}


def _lot(ctx: Context, db, params, query) -> dict:
    from . import mesh_cli
    item = mesh_cli.work_detail(ctx.cfg, db, int(params["id"]))
    if item is None:
        raise ApiError(404, "not_found", "unknown lot")
    return {"schema": "ameesh-lot/1", "lot": item}


def _decisions(ctx: Context, db, params, query) -> dict:
    from . import actions_cli
    view = actions_cli.decisions_view(db, human=ctx.human, viewer=ctx.human,
                                      include_closed=_flag(query, "all"))
    return dict(view, schema="ameesh-decisions/1")


def _alerts(ctx: Context, db, params, query) -> dict:
    from . import exploitation
    thresholds = exploitation.thresholds(exploitation.build_parser().parse_args(["alerts"]))
    return {"schema": "ameesh-alerts/1", "alerts": db_mod.batched(
        db, lambda db: exploitation.alerts(ctx.cfg, db, **thresholds))}


def _hosts(ctx: Context, db, params, query) -> dict:
    from . import mesh_cli
    return {"schema": "ameesh-hosts/1", "hosts": mesh_cli.host_views(ctx.cfg, db)}


def _host(ctx: Context, db, params, query) -> dict:
    from . import mesh_cli
    views = mesh_cli.host_views(ctx.cfg, db, params["host"])
    if not views or (views[0]["latest"] is None and not views[0]["fiches"]):
        raise ApiError(404, "not_found", "unknown host")
    return views[0]


def _budget(ctx: Context, db, params, query) -> dict:
    from . import budget
    return budget.status(ctx.cfg, db)


def _thread(ctx: Context, db, params, query) -> dict:
    from . import fil
    transport = fil.transport_for(ctx.cfg)
    thread = fil.ThreadRef(params["project"], params.get("lot"))
    if not transport.exists(thread):
        raise ApiError(404, "not_found", "no such thread")
    since = _checked(_one(query, "since"), _ENTRY_RE, "since")
    try:
        entries = transport.read(thread, since=since)
    except fil.ThreadError as exc:
        raise ApiError(400, "bad_query", str(exc)) from exc
    return {"schema": "ameesh-thread/1", "project": thread.project, "lot": thread.lot,
            "entries": [{"id": e.id, "author": e.author, "recipient": e.recipient,
                         "text": e.text, "ts": e.ts, "meta": e.meta} for e in entries]}


def _openapi(ctx: Context, db, params, query) -> dict:
    return openapi()


def event_message(event: dict) -> str:
    """One SSE message: `id` is the event's position "tx-id", where a resume
    starts (strictly after it)."""
    data = {"id": int(event["id"]), "type": event["type"], "object": event["object"],
            "project": event.get("project"), "ts": event.get("ts"),
            "data": event.get("data") or {}}
    return "id: %s\nevent: %s\ndata: %s\n\n" % (
        events.format_position((event["tx"], event["id"])), event["type"],
        json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))


_LIMIT = ("limit", {"type": "integer", "minimum": 1, "maximum": MAX_LOTS},
          "lots listed at most (default 100)")
ROUTES = (
    Route(API + "/me", "ameesh-me/1", "The human of this API (local mode: the owner)", _me,
          db=False),
    Route(API + "/projects", "ameesh-projects/1", "Per-project view (`ameesh projects --json`)",
          _projects, query=(("project", {"type": "string"}, "one project only"),)),
    Route(API + "/projects/{project}/progress", "ameesh-progress/1",
          "Progress of a project (`ameesh progress --json --project`)", _progress,
          query=(("since", {"type": "string"}, "window: 90m, 24h, 2d or an ISO date"),)),
    Route(API + "/agents", "ameesh-agents/1", "Agents (`ameesh list --json`)", _agents),
    Route(API + "/agents/{agent}", "ameesh-agent-detail/1", "One agent (`ameesh show --json`)",
          _agent),
    Route(API + "/lots", "ameesh-lots/1", "Lots (`ameesh work list --json`)", _lots,
          query=(("state", {"type": "string"}, "state of the lots"),
                 ("assignee", {"type": "string"}, "assignee of the lots"), _LIMIT)),
    Route(API + "/lots/{id}", "ameesh-lot/1", "One lot (`ameesh work show --json`)", _lot),
    Route(API + "/decisions", "ameesh-decisions/1",
          "Decisions waiting for the human (`ameesh decisions --for --json`)", _decisions,
          query=(("all", {"type": "string", "enum": ["0", "1"]},
                  "1: the closed requests too"),)),
    Route(API + "/alerts", "ameesh-alerts/1", "Current alerts (`ameesh alerts --json`)",
          _alerts),
    Route(API + "/hosts", "ameesh-hosts/1", "Hosts (`ameesh hosts --json`)", _hosts),
    Route(API + "/hosts/{host}", "ameesh-host/1", "One host (`ameesh hosts --json HOST`)",
          _host),
    Route(API + "/budget", "ameesh-budget/1", "Budget (`ameesh budget --json`)", _budget),
    Route(API + "/threads/{project}", "ameesh-thread/1",
          "Project thread (`ameesh fil show PROJECT`)", _thread, db=False,
          query=(("since", {"type": "string"}, "entries after this entry id"),)),
    Route(API + "/threads/{project}/{lot}", "ameesh-thread/1",
          "Lot thread (`ameesh fil show PROJECT LOT`)", _thread, db=False,
          query=(("since", {"type": "string"}, "entries after this entry id"),)),
    Route(EVENTS_PATH, "ameesh-event/1",
          "Event stream (Server-Sent Events): lots, turns, costs, threads, decisions, alerts",
          None, stream=True),
    Route(OPENAPI_PATH, "openapi", "This document", _openapi, db=False, auth=False),
)
_PARAM_RE = {"id": _LOT_RE}


def match(path: str) -> tuple[Route, dict] | None:
    for route in ROUTES:
        m = route.regex.match(path)
        if m:
            params = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
            for name, value in params.items():
                if not _PARAM_RE.get(name, _NAME_RE).match(value):
                    raise ApiError(400, "bad_path", "%s refused" % name)
            return route, params
    return None


def discovery(ctx: Context) -> dict:
    """`GET /.well-known/ameesh` (study §3)."""
    return {"schema": "ameesh-discovery/1", "organisation": ctx.organisation,
            "api": API, "versions": ["v1"], "version": __version__, "mode": "local",
            "oidc_issuer": None, "approve_url": getattr(ctx.cfg, "approve_url", None) or None,
            "languages": list(LANGUAGES), "openapi": OPENAPI_PATH}


# --------------------------------------------------------------------------
# OpenAPI 3.1
# --------------------------------------------------------------------------

def _obj(schema_id: str | None, required=(), /, **props) -> dict:
    """An object schema: `schema` (const `schema_id`, if any), the keys
    `required`, and what is known of `props`."""
    properties = {"schema": {"const": schema_id, "type": "string"}} if schema_id else {}
    properties.update(props)
    return {"type": "object",
            "required": (["schema"] if schema_id else []) + list(required),
            "properties": properties}


_STR = {"type": "string"}
_STR_NULL = {"type": ["string", "null"]}
_LIST = {"type": "array", "items": {"type": "object"}}
_OBJ = {"type": "object"}
_NUM = {"type": "number"}
_HOST = _obj("ameesh-host/1", ["host", "limits", "pressure", "history"],
             host=_STR, limits=_OBJ, history=_LIST)
#: the response schemas: the keys every response carries (the objects they
#: hold are the CLI's, documented in docs/EXPLOITATION.md and docs/PROGRESS.md)
SCHEMAS = {
    "ameesh-me/1": _obj("ameesh-me/1", ["human", "organisation", "mode", "roles",
                                        "deputyships", "language"],
                        human=_STR, organisation=_STR_NULL, mode={"enum": ["local"]},
                        roles={"type": "array", "items": _STR},
                        deputyships={"type": "array", "items": _STR}, language=_STR),
    "ameesh-projects/1": _obj("ameesh-projects/1", ["projects"], projects=_LIST),
    "ameesh-progress/1": _obj("ameesh-progress/1", ["generated_ts", "lots"],
                              generated_ts=_NUM, lots=_LIST),
    "ameesh-agents/1": _obj("ameesh-agents/1", ["agents"], agents={
        "type": "array", "items": _obj(None, ["name"], name=_STR)}),
    "ameesh-agent-detail/1": _obj("ameesh-agent-detail/1", ["agent"],
                                  agent=_obj(None, ["name", "key_ready"], name=_STR,
                                             key_ready={"type": "boolean"})),
    "ameesh-lots/1": _obj("ameesh-lots/1", ["lots"], lots={
        "type": "array", "items": _obj(None, ["id", "title", "state", "delays"],
                                       id={"type": "integer"}, title=_STR, state=_STR)}),
    "ameesh-lot/1": _obj("ameesh-lot/1", ["lot"], lot=_obj(
        None, ["id", "title", "state", "events", "milestones", "planned", "estimate"],
        id={"type": "integer"}, title=_STR, state=_STR, events=_LIST, milestones=_LIST,
        planned=_OBJ)),
    "ameesh-decisions/1": _obj("ameesh-decisions/1", ["requests", "prose", "approvals"],
                               requests=_LIST, prose=_LIST, approvals=_LIST),
    "ameesh-alerts/1": _obj("ameesh-alerts/1", ["alerts"], alerts={
        "type": "array", "items": _obj("ameesh-alert/1", ["type"], type=_STR)}),
    "ameesh-hosts/1": _obj("ameesh-hosts/1", ["hosts"], hosts={
        "type": "array", "items": _HOST}),
    "ameesh-host/1": _HOST,
    "ameesh-budget/1": _obj("ameesh-budget/1", ["limits"], limits=_OBJ),
    "ameesh-thread/1": _obj("ameesh-thread/1", ["project", "lot", "entries"], project=_STR,
                            lot=_STR_NULL, entries={"type": "array", "items": _obj(
                                None, ["id", "author", "recipient", "text", "ts"],
                                id=_STR_NULL, author=_STR, recipient=_STR, text=_STR,
                                ts=_NUM, meta=_OBJ)}),
    "ameesh-discovery/1": _obj("ameesh-discovery/1", ["organisation", "api", "versions",
                                                      "version", "mode", "oidc_issuer",
                                                      "approve_url", "languages", "openapi"],
                               organisation=_STR_NULL, api=_STR,
                               versions={"type": "array", "items": _STR}, version=_STR,
                               mode=_STR, oidc_issuer=_STR_NULL, approve_url=_STR_NULL,
                               languages={"type": "array", "items": _STR}, openapi=_STR),
    "ameesh-error/1": _obj("ameesh-error/1", ["error", "message"], error=_STR, message=_STR),
    # the `data` of one SSE message of /events (not a JSON response)
    "ameesh-event/1": _obj(None, ["id", "type", "object", "project", "ts", "data"],
                           id={"type": "integer"}, type={"enum": list(events.TYPES)},
                           object=_STR, project=_STR_NULL, ts=_NUM, data=_OBJ),
}


def _ref(schema: str) -> dict:
    return {"$ref": "#/components/schemas/" + schema.replace("/", "-")}


def _response(description: str, schema: str) -> dict:
    return {"description": description, "content": {"application/json": {
        "schema": _ref(schema)}}}


def openapi() -> dict:
    """The OpenAPI 3.1 document, generated from `ROUTES` and `SCHEMAS`."""
    paths: dict = {WELL_KNOWN: {"get": {
        "operationId": "discovery", "summary": "Discovery of this mesh's API",
        "security": [], "responses": {"200": _response("discovery", "ameesh-discovery/1")}}}}
    for route in ROUTES:
        if route.path == OPENAPI_PATH:
            paths[route.path] = {"get": {
                "operationId": "openapi", "summary": route.summary, "security": [],
                "responses": {"200": {"description": "OpenAPI 3.1",
                                      "content": {"application/json": {
                                          "schema": {"type": "object"}}}}}}}
            continue
        params = [{"name": name, "in": "path", "required": True,
                   "schema": {"type": "integer"} if name == "id" else {"type": "string"}}
                  for name in re.findall(r"\{(\w+)\}", route.path)]
        params += [{"name": name, "in": "query", "required": False, "schema": schema,
                    "description": text} for name, schema, text in route.query]
        responses = {"200": _response(route.summary, route.schema),
                     "400": _response("bad request", "ameesh-error/1"),
                     "401": _response("token missing or refused", "ameesh-error/1"),
                     "503": _response("database unavailable", "ameesh-error/1")}
        if "{" in route.path:
            responses["404"] = _response("not found", "ameesh-error/1")
        if route.stream:
            responses["200"] = {
                "description": "text/event-stream: `id: <tx-id>` (the event's position, "
                               "in commit order), `event: <type>`, "
                               "`data:` an ameesh-event/1 object; `event: reset` (events "
                               "lost: reload), `event: error` (stream ended); a comment "
                               "every %d s" % HEARTBEAT_S,
                "content": {"text/event-stream": {"schema": {"type": "string"},
                                                  "x-ameesh-data": _ref(route.schema)}}}
            responses["503"] = _response("database unavailable, or too many streams "
                                         "(Retry-After)", "ameesh-error/1")
            params.append({"name": "Last-Event-ID", "in": "header", "required": False,
                           "schema": {"type": "string",
                                      "pattern": "^[0-9]{1,20}-[0-9]{1,19}$"},
                           "description": "resume strictly after this position (the last "
                                          "`id:` received, \"tx-id\"); without it, the "
                                          "new events only"})
        operation = {"operationId": re.sub(r"[^A-Za-z0-9]+", "_",
                                           route.path[len(API) + 1:]).strip("_"),
                     "summary": route.summary, "responses": responses}
        if params:
            operation["parameters"] = params
        paths[route.path] = {"get": operation}
    return {
        "openapi": "3.1.0",
        "info": {"title": "ameesh API", "version": "1",
                 "description": "Read API of an ameesh mesh, local mode (L84). Each response "
                                "is the serialisation of the matching `--json` output of the "
                                "CLI; `schema` names it. `/events` streams the event log "
                                "(L85).",
                 "license": {"name": "AGPL-3.0-only", "identifier": "AGPL-3.0-only"}},
        "servers": [{"url": "http://127.0.0.1:%d" % DEFAULT_PORT}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {name.replace("/", "-"): schema for name, schema in SCHEMAS.items()},
        },
    }


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

def db_reader(cfg) -> Callable:
    """`read(fn)`: `fn(db)` on a read-only connection of its own."""
    ro = dataclasses.replace(cfg, read_only=True)

    def read(fn):
        db = db_mod.connect(ro)
        try:
            db_mod.require_schema(db, defer=True)
            return fn(db)
        finally:
            db.close()
    return read


class Hub:
    """The process's single `LISTEN ameesh_events` connection: each
    notification (or, without LISTEN, each poll that sees a new event) moves
    `generation` on and wakes the streams, which reread the log themselves.

    The connection is reopened after a cut (the streams then reread, in case
    a notification was lost); without LISTEN (driver or base refusing it),
    the last id is polled every `poll_s` seconds."""

    def __init__(self, cfg, read: Callable, *, poll_s: float = POLL_S, listen: bool = True):
        self.cfg = cfg
        self.read = read
        self.poll_s = poll_s
        self.listen = listen
        #: whether the LISTEN connection is up (otherwise: polling)
        self.listening = False
        self.generation = 0
        self._last = None
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ameesh-serve-events",
                                        daemon=True)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def start(self) -> "Hub":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.wake()
        self._thread.join(5)

    def wake(self) -> None:
        with self._cond:
            self.generation += 1
            self._cond.notify_all()

    def wait(self, generation: int, timeout: float) -> int:
        """The generation, once it differs from `generation` (or at `timeout`)."""
        with self._cond:
            self._cond.wait_for(lambda: self.generation != generation or self.stopped,
                                timeout)
            return self.generation

    def _poll(self) -> None:
        last = self.read(lambda db: storage.of(db).events.bounds()["head"])
        if last != self._last:
            self._last = last
            self.wake()

    def _run(self) -> None:
        while not self.stopped:
            db = listener = None
            try:
                if self.listen:
                    db = db_mod.connect(self.cfg)
                    listener = db_mod.listener(db, [events.CHANNEL])
                self.listening = listener is not None
                self.wake()   # (re)connected: what came meanwhile is reread
                started = time.monotonic()
                while not self.stopped:
                    if listener is None:
                        self._poll()
                        if self._stop.wait(self.poll_s):
                            break
                        if self.listen and time.monotonic() - started > 60:
                            break   # LISTEN tried again every minute
                        continue
                    message = listener.wait(1.0)
                    if message is None:
                        continue
                    if message.get("event") == "down":
                        log.warning("event listener down: polling, then reconnecting")
                        break
                    self.wake()
            except (db_mod.DbError, OSError) as exc:
                log.warning("event listener: %s", type(exc).__name__)
            finally:
                self.listening = False
                for closable in (listener, db):
                    if closable is not None:
                        try:
                            closable.close()
                        except Exception:  # noqa: BLE001
                            pass
            self._stop.wait(self.poll_s)


class Handler(http.server.BaseHTTPRequestHandler):
    """Read-only: GET and HEAD; anything else is 405."""

    server_version = "ameesh-serve"
    sys_version = ""
    timeout = SOCKET_TIMEOUT
    #: set by `make_server`
    ctx: Context
    token: str
    read: Callable
    hub: "Hub | None"

    def version_string(self) -> str:
        return self.server_version

    # -- guard --------------------------------------------------------------

    def _host_ok(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        port = self.server.server_address[1]
        return len(hosts) == 1 and hosts[0].strip().lower() in (
            "127.0.0.1:%d" % port, "localhost:%d" % port)

    # -- responses ----------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in httpkit.BASE_HEADERS + (("Content-Security-Policy", CSP),) \
                + tuple(extra):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict, extra=()) -> None:
        body = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        self._send(status, body.encode("utf-8"), "application/json; charset=utf-8", extra)

    def _error(self, status: int, code: str, message: str, extra=()) -> None:
        self._json(status, {"schema": "ameesh-error/1", "error": code, "message": message},
                   extra)

    def _refuse(self) -> None:
        self._error(405, "method_not_allowed", "read-only: GET and HEAD only",
                    (("Allow", "GET, HEAD"),))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = \
        lambda self: self._refuse()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self._get()
        except ApiError as exc:
            self._error(exc.status, exc.code, str(exc))
        except (db_mod.DbError, db_mod.Unavailable, db_mod.SchemaMissing) as exc:
            log.warning("database unavailable: %s", type(exc).__name__)
            self._error(503, "unavailable", "database unavailable")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 — never a trace to the client
            log.warning("internal error: %s", type(exc).__name__)
            self._error(500, "internal", "internal error")

    def _get(self) -> None:
        if not self._host_ok():
            raise ApiError(403, "host_refused", "host refused: http://127.0.0.1:%d/"
                           % self.server.server_address[1])
        if not self.server.limiter.allow(self.client_address[0]):
            raise ApiError(429, "rate_limited", "too many requests")
        url = urllib.parse.urlsplit(self.path)
        if url.path == WELL_KNOWN:
            self._json(200, discovery(self.ctx))
            return
        found = match(url.path)
        if found is None:
            raise ApiError(404, "not_found", "no such route (see %s)" % OPENAPI_PATH)
        route, params = found
        if route.auth and not httpkit.bearer_ok(self.headers.get("Authorization"), self.token):
            self._error(401, "unauthorized", "token missing or refused",
                        (("WWW-Authenticate", "Bearer"),))
            return
        try:
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True,
                                          strict_parsing=bool(url.query), max_num_fields=20)
        except ValueError as exc:
            raise ApiError(400, "bad_query", "unreadable query string") from exc
        known = {name for name, _schema, _text in route.query}
        if set(query) - known:
            raise ApiError(400, "bad_query", "unknown parameter(s): %s"
                           % ", ".join(sorted(set(query) - known))[:200])
        for name, values in query.items():
            if len(values) > 1:
                raise ApiError(400, "bad_query", "%s given more than once" % name)
        if route.stream:
            self._stream()
            return
        if route.db:
            data = self.read(lambda db: route.build(self.ctx, db, params, query))
        else:
            data = route.build(self.ctx, None, params, query)
        self._json(200, data)

    # -- event stream (L85) --------------------------------------------------

    def _stream(self) -> None:
        last = self.headers.get_all("Last-Event-ID") or []
        resume = events.parse_position(last[0]) if len(last) == 1 else None
        if len(last) > 1 or (last and resume is None):
            raise ApiError(400, "bad_header", "Last-Event-ID: a position tx-id expected")
        if self.hub is None:
            raise ApiError(503, "unavailable", "event stream unavailable")
        if not self.server.streams.acquire(blocking=False):
            self._error(503, "too_many_streams", "too many event streams",
                        (("Retry-After", "10"),))
            return
        try:
            bounds = self.read(lambda db: storage.of(db).events.bounds())
            reset = None
            if resume is None:
                position = bounds["head"]
            elif resume > bounds["top"]:
                # a position this base never gave: rebuilt, or another mesh
                position, reset = bounds["head"], "unknown position"
            else:
                position = resume
                # every position given is an event's: older than the oldest
                # kept one, that event (and maybe others) went with the retention
                if resume != (0, 0) and resume < bounds["oldest"]:
                    reset = "events lost (retention)"
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            for name, value in httpkit.BASE_HEADERS + (("Content-Security-Policy", CSP),
                                                       ("X-Accel-Buffering", "no")):
                self.send_header(name, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            head = "retry: %d\n\n" % RETRY_MS
            if reset:
                head += "id: %s\nevent: reset\ndata: %s\n\n" % (
                    events.format_position(position),
                    json.dumps({"reason": reset, "first": bounds["first"],
                                "last": bounds["last"]}))
            try:
                self._write(head)
                self._follow(position)
            except OSError:
                pass   # the client left (or stopped reading: socket timeout)
        finally:
            self.server.streams.release()

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def _drain(self, position: tuple) -> tuple:
        """Sends the readable events after `position`: (new position, whether
        anything was written, whether events wait for an open transaction)."""
        wrote = False
        while True:
            rows, waiting = self.read(lambda db: (
                storage.of(db).events.readable(position, EVENTS_BATCH),
                storage.of(db).events.waiting(position)))
            for row in rows:
                row["tx"] = int(row["tx"])
            out = "".join(event_message(row) for row in rows if self.ctx.sees(row))
            if out:
                self._write(out)
                wrote = True
            if rows:
                position = (rows[-1]["tx"], int(rows[-1]["id"]))
            if len(rows) < EVENTS_BATCH:
                return position, wrote, waiting

    def _follow(self, position: tuple) -> None:
        hub = self.hub
        start = written = time.monotonic()
        generation = hub.generation
        pending, waiting = True, False
        while not hub.stopped and time.monotonic() - start < STREAM_MAX_S:
            if pending or waiting:
                try:
                    position, wrote, waiting = self._drain(position)
                except (db_mod.DbError, db_mod.Unavailable, db_mod.SchemaMissing) as exc:
                    log.warning("event stream: database unavailable: %s", type(exc).__name__)
                    self._write("event: error\ndata: %s\n\n" % json.dumps(
                        {"error": "unavailable", "message": "database unavailable"}))
                    return
                if wrote:
                    written = time.monotonic()
                pending = False
            idle = time.monotonic() - written
            if idle >= HEARTBEAT_S:
                self._write(": heartbeat\n\n")
                written = time.monotonic()
                continue
            timeout = HEARTBEAT_S - idle
            if waiting:
                # an older open transaction holds events back: its end sends
                # no notification if it wrote no event
                timeout = min(timeout, WAITING_POLL_S)
            current = hub.wait(generation, timeout)
            if current != generation:
                generation, pending = current, True

    def send_error(self, code, message=None, explain=None) -> None:
        # never the request in the error page nor in the log
        try:
            phrase = http.HTTPStatus(code).phrase
        except ValueError:
            phrase = "error"
        self.close_connection = True
        self.log_message("code %d, %s", code, phrase)
        self._error(code, "http_%d" % code, phrase)

    def log_message(self, fmt: str, *args) -> None:
        # never the query string: the method, the path and the status only
        if fmt.startswith('"%s"') and args:
            m = _LINE_RE.match(str(args[0]))
            args = ((m.group(1) + " " + m.group(2)) if m else "(malformed request)",) \
                + tuple(args[1:])
        log.info(fmt, *args)


class ServeHTTPServer(httpkit.BoundedHTTPServer):
    log = log
    hub: "Hub | None" = None

    def server_close(self) -> None:
        if self.hub is not None:
            self.hub.stop()
        super().server_close()


def make_server(ctx: Context, *, token: str, read: Callable, port: int = DEFAULT_PORT,
                max_connections: int = MAX_CONNECTIONS,
                rate_per_minute: int = RATE_PER_MINUTE, hub: Hub | None = None,
                max_streams: int = MAX_STREAMS) -> ServeHTTPServer:
    """The server, bound to 127.0.0.1 (port 0: a free port). Without `hub`,
    `/events` answers 503; the server stops the hub when closed."""
    if not token:
        raise ValueError("a token is required")
    handler = type("ServeHandler", (Handler,), {"ctx": ctx, "token": token,
                                                "read": staticmethod(read), "hub": hub})
    server = ServeHTTPServer((BIND, int(port)), handler, max_connections=max_connections)
    server.limiter = httpkit.RateLimiter(rate_per_minute)
    server.streams = threading.BoundedSemaphore(max_streams)
    server.hub = hub
    return server


def owner(cfg, explicit: str | None = None) -> str:
    """The human of local mode: `--human`, `notify.default_human`, the only
    declared human, otherwise `human:$USER`."""
    from . import decisions, notify
    if explicit:
        human = notify.normalize_human(explicit)
        if not human:
            raise ValueError("--human: human:<id> expected")
        return human
    try:
        default = notify.parse_config(cfg.notify).default_human
    except (notify.NotifyConfigError, ValueError, TypeError):
        default = None
    if default:
        return default
    names = sorted(cfg.human_names)
    if len(names) == 1 and notify.normalize_human(names[0]):
        return notify.normalize_human(names[0])
    return decisions.current_human()


def token_file(explicit: str | None = None) -> str:
    return httpkit.expand(explicit or os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE)


def load_token(path: str, *, out=None) -> str:
    """The token of `path`, created (0600) at the first start."""
    if not os.path.lexists(path):
        httpkit.write_token_file(path)
        print("ameesh serve: token created in %s (0600)" % path, file=out or sys.stderr)
    return httpkit.read_token_file(path, what="API token", hint="ameesh serve creates it")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh serve", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port on 127.0.0.1 (default %d)" % DEFAULT_PORT)
    parser.add_argument("--token-file", help="token file (default %s, or $%s)"
                        % (DEFAULT_TOKEN_FILE, TOKEN_FILE_ENV))
    parser.add_argument("--human", help="the human of local mode (human:<id>)")
    parser.add_argument("--organisation", help="organisation named by /.well-known/ameesh")
    parser.add_argument("--openapi", action="store_true",
                        help="print the OpenAPI 3.1 document and exit")
    return parser


def dump_openapi() -> str:
    return json.dumps(openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.openapi:
        sys.stdout.write(dump_openapi())
        return 0
    from . import config as config_mod
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="ameesh serve: %(message)s")
    cfg = config_mod.load()
    try:
        human = owner(cfg, args.human)
        path = token_file(args.token_file)
        token = load_token(path)
    except ValueError as exc:
        print("ameesh serve: %s" % exc, file=sys.stderr)
        return 2
    ctx = Context(cfg=dataclasses.replace(cfg, read_only=True), human=human,
                  organisation=args.organisation)
    read = db_reader(cfg)
    try:
        # a first read: an unreachable database or a missing schema, said at once
        read(lambda db: None)
    except (db_mod.DbError, db_mod.Unavailable, db_mod.SchemaMissing) as exc:
        print("ameesh serve: database unavailable: %s" % exc, file=sys.stderr)
        return 1
    hub = Hub(ctx.cfg, read)
    try:
        server = make_server(ctx, token=token, read=read, port=args.port, hub=hub)
    except OSError as exc:
        print("ameesh serve: port %s unavailable on %s: %s"
              % (args.port, BIND, exc.strerror or exc), file=sys.stderr)
        return 1
    hub.start()
    print("ameesh serve: http://%s:%d%s for %s (Bearer token in %s)"
          % (BIND, server.server_address[1], API, human, path), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["API", "BIND", "Context", "DEFAULT_PORT", "EVENTS_PATH", "Hub", "ROUTES", "SCHEMAS", "discovery",
           "main", "make_server", "openapi", "owner"]
