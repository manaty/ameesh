# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh serve`: the mesh's API, local mode (L84 read, L87 human actions).

  ameesh serve [--port 8780] [--token-file PATH] [--human human:ID] [--organisation NAME]
  ameesh serve --openapi        prints the OpenAPI 3.1 document (docs/api/openapi-v1.json)

Embeddable interface study (docs/design/etudes/ui-integrable.md, §2.1, §2.4,
§3, §7). A standard-library HTTP server, separate from ameesh-approve (another
process, another origin), **bound to 127.0.0.1 only** (no host option). Reads
are GET and HEAD, on a Postgres session in `default_transaction_read_only`.
The human actions (L87, `serve_actions`) are POST, on a writable session of
their own, each with an `Idempotency-Key` (required) reserved in the database
before the action runs (`api_idempotency`, migration 0440): a retry reads the
first response back and never acts twice. Any other method is 405.

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

`GET /ui/` (L88) is the standalone page: the embeddable components
(`ameesh-ui.js`, built from `ui/` and committed under `ameesh/ui/static/`)
assembled full screen. Static files of a fixed list, no token (the page asks
for it and keeps it in memory), under a strict CSP: scripts and styles of
this origin only, no inline code, Trusted Types, API calls to this origin.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http
import http.server
import json
import logging
import os
import re
import stat
import sys
import urllib.parse
from typing import Callable

from . import __version__, httpkit, serve_actions, storage
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
#: conversation page of an agent card (L83): default and hard bound
CHAT_PAGE = 100
MAX_CHAT = 500
LANGUAGES = ("fr", "en")
#: an agent, host or project name; a lot id; a thread entry id; a duration
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{0,127}$")
_LOT_RE = re.compile(r"^[0-9]{1,9}$")
#: a conversation cursor or page size (`?after=`, `?limit=`)
_SEQ_RE = re.compile(r"^(0|[1-9][0-9]{0,15})$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9._/#:-]{1,256}$")
_SINCE_RE = re.compile(r"^[0-9]{1,6}[mhdj]$|^[0-9]{4}-[0-9]{2}-[0-9]{2}[0-9T:+.Z-]{0,20}$")
_LINE_RE = re.compile(r"^([A-Z]{1,10}) (/[A-Za-z0-9._/@:+-]{0,200})")
CSP = "default-src 'none'; frame-ancestors 'none'"
#: `Idempotency-Key` of an action (L87), and how long its response is kept
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
IDEMPOTENCY_RETENTION_S = 86400
#: the standalone page (L88): its own files and the API, nothing inline
UI = "/ui"
# the package's directory, then ui/static opened one name at a time from it,
# never through a link (a directory link included)
UI_ROOT = os.path.dirname(os.path.abspath(__file__))
UI_PARTS = ("ui", "static")
UI_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "ameesh-ui.js": ("ameesh-ui.js", "text/javascript; charset=utf-8"),
    "ameesh-ui.css": ("ameesh-ui.css", "text/css; charset=utf-8"),
    "page.css": ("page.css", "text/css; charset=utf-8"),
}
UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
          "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
          "require-trusted-types-for 'script'; trusted-types lit-html")

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
    #: the principal a deputy acts for (0033 §7, team mode L86); local mode: None
    on_behalf_of: str | None = None


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


def _chat(ctx: Context, db, params, query) -> dict:
    """An agent's shared conversation, ascending from `after` (L83): the same
    `ameesh-conversation/1` history `ameesh chat` reads. No relation filter
    yet: the API's token is the machine owner's; filtering by the 0033 §9
    relations arrives with the OIDC tokens of team mode (L86)."""
    from . import conversation, registry
    if registry.get(db, params["agent"]) is None:
        raise ApiError(404, "not_found", "unknown agent")
    after = _checked(_one(query, "after"), _SEQ_RE, "after")
    limit = _checked(_one(query, "limit"), _SEQ_RE, "limit")
    try:
        return conversation.history(
            db, params["agent"], after=int(after) if after is not None else 0,
            limit=min(int(limit), MAX_CHAT) if limit is not None else CHAT_PAGE)
    except ValueError as exc:
        raise ApiError(400, "bad_query", str(exc)) from exc


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
    Route(API + "/agents/{agent}/chat", "ameesh-conversation/1",
          "An agent's shared conversation (`ameesh chat <agent>`, L78/L83)", _chat,
          query=(("after", {"type": "integer", "minimum": 0},
                  "entries after this sequence number (default 0)"),
                 ("limit", {"type": "integer", "minimum": 1, "maximum": MAX_CHAT},
                  "entries at most (default %d)" % CHAT_PAGE))),
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
    Route(OPENAPI_PATH, "openapi", "This document", _openapi, db=False, auth=False),
)
_PARAM_RE = {"id": _LOT_RE}
#: the human actions (L87), under the API prefix
ACTIONS = tuple(dataclasses.replace(action, path=API + action.path)
                for action in serve_actions.ACTIONS)


def _params(m: re.Match) -> dict:
    params = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
    for name, value in params.items():
        if not _PARAM_RE.get(name, _NAME_RE).match(value):
            raise ApiError(400, "bad_path", "%s refused" % name)
    return params


def match(path: str) -> tuple[Route, dict] | None:
    for route in ROUTES:
        m = route.regex.match(path)
        if m:
            return route, _params(m)
    return None


def match_action(path: str) -> tuple[serve_actions.Action, dict] | None:
    for action in ACTIONS:
        m = action.regex.match(path)
        if m:
            return action, _params(m)
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
    "ameesh-conversation/1": _obj(
        "ameesh-conversation/1",
        ["agent", "entries", "last_seq", "oldest_seq", "next_seq", "more", "gap"],
        agent=_STR,
        entries={"type": "array", "items": _obj(
            None, ["seq", "kind", "author", "text", "ts"],
            seq={"type": "integer"}, kind=_STR, author=_STR, text=_STR, ts=_NUM,
            truncated={"type": "boolean"}, session=_STR_NULL, turn=_STR_NULL,
            event_id=_STR_NULL, project=_STR, lot=_STR_NULL, meta=_OBJ)},
        last_seq={"type": "integer"}, oldest_seq={"type": ["integer", "null"]},
        next_seq={"type": "integer"}, more={"type": "boolean"}, gap={"type": "boolean"}),
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
    "ameesh-action/1": _obj("ameesh-action/1", ["action", "by", "for", "result"],
                            action=_STR, by=_STR, result=_OBJ, **{"for": _STR}),
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
        operation = {"operationId": re.sub(r"[^A-Za-z0-9]+", "_",
                                           route.path[len(API) + 1:]).strip("_"),
                     "summary": route.summary, "responses": responses}
        if params:
            operation["parameters"] = params
        paths[route.path] = {"get": operation}
    for action in ACTIONS:
        params = [{"name": name, "in": "path", "required": True,
                   "schema": {"type": "integer"} if name == "id" else {"type": "string"}}
                  for name in re.findall(r"\{(\w+)\}", action.path)]
        params.append({"name": "Idempotency-Key", "in": "header", "required": True,
                       "schema": {"type": "string", "pattern": _IDEMPOTENCY_RE.pattern},
                       "description": "one key per action; a retry with the same key and "
                                      "the same request reads the first response back"})
        body = {"type": "object", "additionalProperties": False,
                "required": [name for name, (_schema, required) in action.body.items()
                             if required],
                "properties": {name: schema for name, (schema, _required)
                               in action.body.items()}}
        responses = {"200": _response(action.summary, "ameesh-action/1"),
                     "400": _response("bad request", "ameesh-error/1"),
                     "401": _response("token missing or refused", "ameesh-error/1"),
                     "403": _response("not allowed", "ameesh-error/1"),
                     "404": _response("not found", "ameesh-error/1"),
                     "409": _response("refused, or the key's first request still running",
                                      "ameesh-error/1"),
                     "413": _response("body too large", "ameesh-error/1"),
                     "415": _response("body not JSON", "ameesh-error/1"),
                     "422": _response("key already used for another request",
                                      "ameesh-error/1"),
                     "503": _response("database unavailable", "ameesh-error/1")}
        operation = {"operationId": "post_" + re.sub(r"[^A-Za-z0-9]+", "_",
                                                     action.path[len(API) + 1:]).strip("_"),
                     "summary": action.summary, "parameters": params,
                     "requestBody": {"required": True, "content": {
                         "application/json": {"schema": body}}},
                     "responses": responses}
        paths.setdefault(action.path, {})["post"] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "ameesh API", "version": "1",
                 "description": "API of an ameesh mesh, local mode: reads (L84), each the "
                                "serialisation of the matching `--json` output of the CLI "
                                "(`schema` names it), and the human actions (L87), POST "
                                "with a required `Idempotency-Key`. No route approves, "
                                "produces or attaches a receipt.",
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
# actions (L87)
# --------------------------------------------------------------------------

def _err(code: str, message: str) -> dict:
    return {"schema": "ameesh-error/1", "error": code, "message": message}


def fingerprint(path: str, body: dict) -> str:
    """What an `Idempotency-Key` is bound to: the method, the path and the
    canonical body."""
    canonical = json.dumps({"method": "POST", "path": path, "body": body}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def act(ctx: Context, db, action, params: dict, body: dict, key: str,
        digest: str) -> tuple[int, dict, bool]:
    """Run `action` once per (human, `key`): (status, response, replayed).

    The key is reserved in the database BEFORE the action runs. Already
    answered with the same request: its response, replayed, nothing done. Used
    for another request: 422. Its first request still running, or its outcome
    unknown (the server stopped in between): 409, nothing done — retrying with
    a new key could act twice, so check first. A refusal is stored like a
    success (the same request is refused the same way); a transient failure
    (5xx) releases the key, so the same request can be retried with it."""
    requests = storage.of(db).api_requests
    row = requests.reserve(ctx.human, key, digest, retention_s=IDEMPOTENCY_RETENTION_S)
    if row is not None:
        if row["fingerprint"] != digest:
            return 422, _err("idempotency_mismatch",
                             "this Idempotency-Key was used for another request"), False
        if row["status"] is None:
            return 409, _err("idempotency_in_progress",
                             "the first request with this Idempotency-Key has not answered, "
                             "or its outcome is unknown: check before acting again"), False
        return row["status"], row["response"], True
    try:
        result = action.build(ctx, db, params, body)
    except serve_actions.ActionError as exc:
        status, data = exc.status, _err(exc.code, str(exc))
    else:
        status, data = 200, {"schema": "ameesh-action/1", "action": action.name,
                             "by": ctx.human, "for": serve_actions.principal(ctx),
                             "result": result}
    # any other exception leaves the key reserved: the action may have been
    # done, a retry must not do it twice (409 above)
    if status >= 500:
        requests.release(ctx.human, key)
        return status, data, False
    requests.complete(ctx.human, key, status, data)
    return status, data, False


def _no_constant(name: str):
    raise ValueError("%s is not JSON" % name)


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for name, value in pairs:
        if name in out:
            raise ValueError("duplicate key %r" % name)
        out[name] = value
    return out


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

def db_writer(cfg) -> Callable:
    """`write(fn)`: `fn(db)` on a writable connection of its own (L87)."""
    rw = dataclasses.replace(cfg, read_only=False)

    def write(fn):
        db = db_mod.connect(rw)
        try:
            db_mod.require_schema(db, defer=True)
            return fn(db)
        finally:
            db.close()
    return write


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


class Handler(http.server.BaseHTTPRequestHandler):
    """GET and HEAD read, POST acts (L87); anything else is 405."""

    server_version = "ameesh-serve"
    sys_version = ""
    timeout = SOCKET_TIMEOUT
    #: set by `make_server`
    ctx: Context
    token: str
    read: Callable
    #: the writable connection of the actions; None: actions unavailable
    write: Callable | None = None

    def version_string(self) -> str:
        return self.server_version

    # -- guard --------------------------------------------------------------

    def _host_ok(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        port = self.server.server_address[1]
        return len(hosts) == 1 and hosts[0].strip().lower() in (
            "127.0.0.1:%d" % port, "localhost:%d" % port)

    # -- responses ----------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra=(), csp: str = CSP) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in httpkit.BASE_HEADERS + (("Content-Security-Policy", csp),) \
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
        self.close_connection = True
        self._error(405, "method_not_allowed", "GET and HEAD read, POST acts",
                    (("Allow", "GET, HEAD, POST"),))

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = \
        lambda self: self._refuse()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._serve(self._get)

    def do_POST(self) -> None:
        # one action per connection: an error before the body is read never
        # leaves it to be parsed as the next request
        self.close_connection = True
        self._serve(self._post)

    def _serve(self, handle: Callable) -> None:
        try:
            handle()
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

    def _guard(self) -> None:
        if not self._host_ok():
            raise ApiError(403, "host_refused", "host refused: http://127.0.0.1:%d/"
                           % self.server.server_address[1])
        if not self.server.limiter.allow(self.client_address[0]):
            raise ApiError(429, "rate_limited", "too many requests")

    def _get(self) -> None:
        self._guard()
        url = urllib.parse.urlsplit(self.path)
        if url.path == WELL_KNOWN:
            self._json(200, discovery(self.ctx))
            return
        if url.path == UI or url.path.startswith(UI + "/"):
            self._ui(url.path)
            return
        found = match(url.path)
        if found is None:
            if match_action(url.path) is not None:
                self._error(405, "method_not_allowed", "this route acts: POST",
                            (("Allow", "POST"),))
                return
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
        if route.db:
            data = self.read(lambda db: route.build(self.ctx, db, params, query))
        else:
            data = route.build(self.ctx, None, params, query)
        self._json(200, data)

    def _post(self) -> None:
        self._guard()
        url = urllib.parse.urlsplit(self.path)
        found = match_action(url.path)
        if found is None:
            if (match(url.path) is not None or url.path == WELL_KNOWN or url.path == UI
                    or url.path.startswith(UI + "/")):
                self._error(405, "method_not_allowed", "this route reads: GET or HEAD",
                            (("Allow", "GET, HEAD"),))
                return
            raise ApiError(404, "not_found", "no such route (see %s)" % OPENAPI_PATH)
        action, params = found
        if not httpkit.bearer_ok(self.headers.get("Authorization"), self.token):
            self._error(401, "unauthorized", "token missing or refused",
                        (("WWW-Authenticate", "Bearer"),))
            return
        if url.query:
            raise ApiError(400, "bad_query", "an action takes no query string")
        if self.write is None:
            raise ApiError(503, "unavailable", "actions unavailable on this server")
        keys = self.headers.get_all("Idempotency-Key") or []
        if len(keys) != 1 or not _IDEMPOTENCY_RE.match(keys[0]):
            raise ApiError(400, "idempotency_key", "one Idempotency-Key header is required "
                           "(8 to 128 characters: letters, digits, _ and -)")
        body = self._body(action)
        status, data, replayed = self.write(lambda db: act(
            self.ctx, db, action, params, body, keys[0], fingerprint(url.path, body)))
        self._json(status, data, (("Idempotent-Replayed", "true"),) if replayed else ())

    def _body(self, action) -> dict:
        """The JSON object of the request: Content-Length bounded, no chunk,
        no duplicate key, no NaN/Infinity, only the action's fields."""
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", "a Content-Length body is required")
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "unsupported_media_type", "Content-Type: application/json")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not lengths[0].strip().isdigit():
            raise ApiError(411, "length_required", "a Content-Length body is required")
        length = int(lengths[0])
        if length > serve_actions.MAX_BODY:
            raise ApiError(413, "too_large", "body over %d bytes" % serve_actions.MAX_BODY)
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise ApiError(400, "bad_body", "body shorter than its Content-Length")
        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=_no_constant,
                              object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError(400, "bad_body", "unreadable JSON body") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_body", "a JSON object is required")
        unknown = sorted(set(data) - set(action.body))
        if unknown:
            raise ApiError(400, "bad_body", "unknown field(s): %s" % ", ".join(unknown)[:200])
        missing = [name for name, (_schema, required) in action.body.items()
                   if required and name not in data]
        if missing:
            raise ApiError(400, "bad_body", "missing field(s): %s" % ", ".join(missing))
        return data

    def _ui(self, path: str) -> None:
        """The standalone page and its files: a fixed list, nothing else."""
        if path == UI:
            self._send(308, b"", "text/plain; charset=utf-8", (("Location", UI + "/"),))
            return
        found = UI_FILES.get(path[len(UI) + 1:])
        if found is None:
            raise ApiError(404, "not_found", "no such file")
        name, ctype = found
        # served without a token: never through a link, never out of the package
        try:
            fd = _ui_open(name)
        except OSError:  # missing, or a link (ELOOP, ENOTDIR)
            raise ApiError(404, "not_found", "interface not installed") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ApiError(404, "not_found", "interface not installed")
            fh = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            body = fh.read()
        self._send(200, body, ctype, csp=UI_CSP)

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


def _ui_open(name: str) -> int:
    """A descriptor of UI_ROOT/ui/static/<name>, each name opened from its
    parent's descriptor without following a link: no race, no way out."""
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nofollow = os.O_RDONLY | os.O_NOFOLLOW | cloexec
    fd = os.open(UI_ROOT, os.O_RDONLY | os.O_DIRECTORY | cloexec)
    try:
        for part in UI_PARTS:
            parent, fd = fd, -1
            try:
                fd = os.open(part, nofollow | os.O_DIRECTORY, dir_fd=parent)
            finally:
                os.close(parent)
        # non-blocking: a FIFO in place of a file must not hold the request
        return os.open(name, nofollow | os.O_NONBLOCK, dir_fd=fd)
    finally:
        if fd >= 0:
            os.close(fd)


class ServeHTTPServer(httpkit.BoundedHTTPServer):
    log = log


def make_server(ctx: Context, *, token: str, read: Callable, port: int = DEFAULT_PORT,
                max_connections: int = MAX_CONNECTIONS,
                rate_per_minute: int = RATE_PER_MINUTE,
                write: Callable | None = None) -> ServeHTTPServer:
    """The server, bound to 127.0.0.1 (port 0: a free port). Without `write`,
    the actions answer 503."""
    if not token:
        raise ValueError("a token is required")
    attrs = {"ctx": ctx, "token": token, "read": staticmethod(read)}
    if write is not None:
        attrs["write"] = staticmethod(write)
    handler = type("ServeHandler", (Handler,), attrs)
    server = ServeHTTPServer((BIND, int(port)), handler, max_connections=max_connections)
    server.limiter = httpkit.RateLimiter(rate_per_minute)
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
    try:
        server = make_server(ctx, token=token, read=read, port=args.port,
                             write=db_writer(cfg))
    except OSError as exc:
        print("ameesh serve: port %s unavailable on %s: %s"
              % (args.port, BIND, exc.strerror or exc), file=sys.stderr)
        return 1
    print("ameesh serve: http://%s:%d%s for %s (Bearer token in %s)"
          % (BIND, server.server_address[1], API, human, path), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["ACTIONS", "API", "BIND", "Context", "DEFAULT_PORT", "ROUTES", "SCHEMAS", "act",
           "discovery", "main", "make_server", "openapi", "owner"]
