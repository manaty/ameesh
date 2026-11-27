# SPDX-License-Identifier: AGPL-3.0-only
"""#8344 — alerts that an application raises and resolves itself.

`ameesh notify` used to push only the alerts derived from the mesh
(`ameesh alerts`). An application (issue-manager-agent, IMA#1804) had no way to
hand its own durable conditions to the same delivery: routes, avatar first
(#7634), rate limit per human, retries, the `notified` ledger, liveness.

The vocabulary lives in the canon, never in the core: an `Application` card
declares its alert types and its routes in an `alerts:` block:

    alerts:
      types:
        triage_stalled:              # lowercase letters, digits, `_`
          urgent: false              # optional; default false
      routes:
        triage-team: [team-slack]    # channels of the delivering host

A route names channels of `notify.channels` on the host that delivers the
alerts (the secrets stay in the host configuration); `desktop`, `slack` and
`app` need no declaration, as in `notify.routes`.

`raise_alert` opens one row per (application, type, deduplication key) in
`app_alerts` (migration 0612): a durable condition raised again while open
changes nothing, so it is pushed once. `resolve_alert` closes the open row
once. A raise after a resolution opens a new episode (`raised_at`). The
notifier's `alerts` pass reads the open rows (`alerts`) as `app_alert` alerts
whose key carries the episode: a resolution is the row leaving the open set,
and the ledger keeps one notification per episode and recipient.

Refusals (`AppAlertRefused`, a code and a reason) happen at raise time: an
unknown application, an undeclared type or route, a recipient that is not a
`human:<id>`. A refusal never reaches the notifier, so the mesh alerts keep
flowing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: the alert type the notifier sees; the application's own type is `app_type`
ALERT_TYPE = "app_alert"
#: the family named in `unreadable` when the table cannot be read: the
#: notifier then resolves none of these alerts on an absence it did not read
FAMILY = "app_alerts"
#: recipient prefix of a canon route (`route:<app>/<route>`): its own rate
#: bucket, its own ledger identity, never an avatar
ROUTE_PREFIX = "route:"

TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: a deduplication key: printable, no newline, bounded
KEY_MAX = 200
TEXT_MAX = 2000


class AppAlertRefused(ValueError):
    """A raise or resolve refused before any write (`code`, readable reason)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AlertType:
    name: str
    urgent: bool = False

    def to_dict(self) -> dict:
        return {"urgent": self.urgent}


@dataclass(frozen=True)
class AlertPolicy:
    """The `alerts:` block of one Application card."""
    types: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"types": {name: kind.to_dict() for name, kind in self.types.items()},
                "routes": {name: list(names) for name, names in self.routes.items()}}


def parse_alerts(raw) -> tuple:
    """`(AlertPolicy | None, [(code, message)])` for an `alerts:` block.

    No block: `(None, [])`. Every defect is named, so `canon check` refuses
    the card without blocking the agents (as `incident:`)."""
    if raw is None:
        return None, []
    defects: list = []
    if not isinstance(raw, dict):
        return None, [("application-alerts-invalid",
                       "Application card: `alerts` must be a mapping (types, routes)")]
    unknown = sorted(set(raw) - {"types", "routes"})
    if unknown:
        defects.append(("application-alerts-invalid",
                        "Application card: unknown key(s) in `alerts`: %s"
                        % ", ".join(map(str, unknown))))
    types: dict = {}
    raw_types = raw.get("types") or {}
    if not isinstance(raw_types, dict):
        defects.append(("application-alerts-type-invalid",
                        "Application card: `alerts.types` must be a mapping of type names"))
        raw_types = {}
    for name, spec in raw_types.items():
        name = str(name)
        if not TYPE_RE.fullmatch(name):
            defects.append(("application-alerts-type-invalid",
                            "Application card: alert type %r is not a lowercase name "
                            "(letters, digits, `_`; 64 characters at most)" % name))
            continue
        spec = {} if spec is None else spec
        if not isinstance(spec, dict) or set(spec) - {"urgent"}:
            defects.append(("application-alerts-type-invalid",
                            "Application card: alert type %s takes only `urgent`" % name))
            continue
        urgent = spec.get("urgent", False)
        if not isinstance(urgent, bool):
            defects.append(("application-alerts-type-invalid",
                            "Application card: `alerts.types.%s.urgent` must be true "
                            "or false" % name))
            continue
        types[name] = AlertType(name=name, urgent=urgent)
    routes: dict = {}
    raw_routes = raw.get("routes") or {}
    if not isinstance(raw_routes, dict):
        defects.append(("application-alerts-route-invalid",
                        "Application card: `alerts.routes` must be a mapping "
                        "{route: [channels]}"))
        raw_routes = {}
    for name, channels in raw_routes.items():
        name = str(name)
        if not ROUTE_RE.fullmatch(name):
            defects.append(("application-alerts-route-invalid",
                            "Application card: route %r is not a NAME" % name))
            continue
        if (not isinstance(channels, list) or not channels
                or not all(isinstance(c, str) and CHANNEL_RE.fullmatch(c)
                           for c in channels)):
            defects.append(("application-alerts-route-invalid",
                            "Application card: `alerts.routes.%s` must be a non-empty "
                            "list of channel names" % name))
            continue
        routes[name] = tuple(channels)
    return AlertPolicy(types=types, routes=routes), defects


# -- canon --------------------------------------------------------------------

def policies(cfg) -> dict:
    """`{application id: AlertPolicy}` of the configured canons, the default
    canon first (the first card of an id wins). An invalid card declares
    nothing: its alerts are refused until `canon check` is green."""
    from . import canon as canon_mod
    out: dict = {}
    for canon in canon_mod.load_configured(cfg) or ():
        if canon is None or not getattr(canon, "readable", True):
            continue
        for application in getattr(canon, "applications", ()) or ():
            policy = getattr(application, "alerts", None)
            if policy is None or not application.valid:
                continue
            out.setdefault(application.id, policy)
    return out


def route_id(app: str, route: str) -> str:
    return "%s%s/%s" % (ROUTE_PREFIX, app, route)


def is_route(recipient) -> bool:
    return isinstance(recipient, str) and recipient.startswith(ROUTE_PREFIX)


def route_channels(cfg) -> dict:
    """`{route:<app>/<route>: (channel names)}` of the configured canons."""
    return {route_id(app, name): names
            for app, policy in policies(cfg).items()
            for name, names in policy.routes.items()}


# -- levée et résolution ------------------------------------------------------

def _check_key(key) -> str:
    text = str(key or "").strip()
    if not text or len(text) > KEY_MAX or not text.isprintable():
        raise AppAlertRefused("bad_key", "deduplication key: 1 to %d printable characters, "
                              "on one line" % KEY_MAX)
    return text


def _check_ident(app, kind) -> tuple:
    from .config import NAME_RE
    app = str(app or "").strip()
    if not NAME_RE.fullmatch(app):
        raise AppAlertRefused("bad_app", "application: an Application card id expected")
    kind = str(kind or "").strip()
    if not TYPE_RE.fullmatch(kind):
        raise AppAlertRefused("bad_type", "alert type: a lowercase name expected")
    return app, kind


def _native(db) -> None:
    from . import storage
    if storage.is_remote(db):
        raise AppAlertRefused("native_only",
                              "application alerts need a native database connection")


def raise_alert(db, cfg, *, app: str, type: str, key: str, text: str,
                to: str | None = None, route: str | None = None,
                urgent: bool | None = None, actor: str | None = None,
                policy: AlertPolicy | None = None) -> dict:
    """Raise (or keep) the alert `(app, type, key)`; return its row.

    `state` is `raised` for a new episode, `open` when the same alert was
    already open (nothing changes: the condition is pushed once). The
    recipient is exactly one of `to` (`human:<id>`) and `route` (a route of
    the card). `urgent` defaults to the type's declaration; only an urgent
    alert wakes the human's avatar at once. `policy` replaces the canon read
    (tests, callers that already hold it)."""
    app, kind = _check_ident(app, type)
    key = _check_key(key)
    body = " ".join(str(text or "").split())
    if not body:
        raise AppAlertRefused("bad_text", "alert text: required")
    body = body[:TEXT_MAX]
    if policy is None:
        policy = policies(cfg).get(app)
    if policy is None:
        raise AppAlertRefused("unknown_app",
                              "application %s: no valid Application card declares "
                              "`alerts:` in the configured canons" % app)
    declared = policy.types.get(kind)
    if declared is None:
        raise AppAlertRefused("unknown_type",
                              "alert type %s is not declared by application %s "
                              "(`alerts.types` of its card)" % (kind, app))
    if (to is None) == (route is None):
        raise AppAlertRefused("bad_recipient",
                              "one recipient: a human (`--to human:<id>`) or a route "
                              "of the card (`--route`)")
    if to is not None:
        to = str(to).strip()
        if not HUMAN_RE.fullmatch(to):
            raise AppAlertRefused("bad_recipient", "recipient: `human:<id>` expected")
    else:
        route = str(route).strip()
        if route not in policy.routes:
            raise AppAlertRefused("unknown_route",
                                  "route %s is not declared by application %s "
                                  "(`alerts.routes` of its card)" % (route, app))
    urgent = declared.urgent if urgent is None else bool(urgent)
    _native(db)
    # One statement, atomic: a resolved row is reopened as a new episode; an
    # open row is left as is (no update), so a repeated raise neither
    # re-pushes nor moves `raised_at`, the episode identity of the notifier.
    rows = db.query(
        "insert into app_alerts (app, type, key, recipient, route, urgent, text,"
        " raised_by)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (app, type, key) do update set"
        " recipient = excluded.recipient, route = excluded.route,"
        " urgent = excluded.urgent, text = excluded.text,"
        " raised_by = excluded.raised_by, raised_at = clock_timestamp(),"
        " resolved_at = null, resolved_by = null"
        " where app_alerts.resolved_at is not null"
        " returning app",
        (app, kind, key, to, route, urgent, body, actor))
    row = get(db, app, kind, key) or {"app": app, "type": kind, "key": key}
    row["state"] = "raised" if rows else "open"
    return row


def resolve_alert(db, *, app: str, type: str, key: str,
                  actor: str | None = None) -> dict:
    """Close the open alert `(app, type, key)` once.

    `state` is `resolved` when this call closed it, `not_open` when no open
    alert has this key (already resolved, or never raised): nothing changes.
    No canon read: an alert whose type was since removed can still close."""
    app, kind = _check_ident(app, type)
    key = _check_key(key)
    _native(db)
    rows = db.query(
        "update app_alerts set resolved_at = clock_timestamp(), resolved_by = %s"
        " where app = %s and type = %s and key = %s and resolved_at is null"
        " returning app", (actor, app, kind, key))
    row = get(db, app, kind, key) or {"app": app, "type": kind, "key": key}
    row["state"] = "resolved" if rows else "not_open"
    return row


_COLUMNS = ("select app, type, key, recipient, route, urgent, text, raised_by,"
            " extract(epoch from raised_at)::float8 as raised_ts,"
            " extract(epoch from resolved_at)::float8 as resolved_ts, resolved_by"
            " from app_alerts")


def get(db, app: str, kind: str, key: str) -> dict | None:
    rows = db.query(_COLUMNS + " where app = %s and type = %s and key = %s",
                    (app, kind, key))
    return dict(rows[0]) if rows else None


def listing(db, *, app: str | None = None, open_only: bool = True,
            limit: int = 500) -> list:
    where, params = [], []
    if open_only:
        where.append("resolved_at is null")
    if app:
        where.append("app = %s")
        params.append(app)
    sql = _COLUMNS + (" where " + " and ".join(where) if where else "")
    sql += " order by raised_at, app, type, key limit %s"
    params.append(int(limit))
    return [dict(row) for row in db.query(sql, tuple(params))]


def alerts(db) -> list:
    """The open application alerts, in the shape of `exploitation.alerts`.

    `since` is the episode start (`raised_at`): it enters the alert key, so a
    raise after a resolution is a new alert and the `notified` ledger keeps
    one notification per episode. The canon is not read here: a type removed
    from the card after its raise still resolves normally."""
    from . import exploitation
    out = []
    for row in listing(db, open_only=True):
        out.append({
            "schema": exploitation.ALERT_SCHEMA, "type": ALERT_TYPE, "agent": None,
            "since": round(float(row["raised_ts"]), 3), "value": None,
            "threshold": None, "detail": row["text"], "app": row["app"],
            "app_type": row["type"], "app_key": row["key"],
            "responsible": row.get("recipient"), "route": row.get("route"),
            "urgent": bool(row.get("urgent"))})
    return out


# -- commande -----------------------------------------------------------------

#: the `ameesh notify` subcommands handled here
COMMANDS = ("raise", "resolve", "list")


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="ameesh notify",
        description="#8344: an application raises, resolves and lists its own "
                    "alerts; the `alerts` pass of `ameesh notify` pushes them.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--app", required=True,
                       help="the application: id of its Application card")
        p.add_argument("--type", required=True, dest="kind",
                       help="an alert type declared by the card (`alerts.types`)")
        p.add_argument("--key", required=True,
                       help="deduplication key: one open alert per (app, type, key)")
        p.add_argument("--actor", default=None,
                       help="who raises or resolves (default: AGENT_MAIL_NAME)")
        p.add_argument("--json", action="store_true", help="the row as JSON")

    p = sub.add_parser("raise", help="raise an alert; an open one is left as is")
    common(p)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", metavar="human:ID", help="the human to alert "
                        "(their avatar first, #7634)")
    target.add_argument("--route", help="a team route declared by the card "
                        "(`alerts.routes`)")
    p.add_argument("--severity", choices=("normal", "urgent"), default=None,
                   help="default: the type's declaration; only `urgent` wakes "
                        "the avatar at once")
    p.add_argument("--text", required=True,
                   help="what the human reads; `-` reads standard input")
    p = sub.add_parser("resolve", help="resolve the open alert once")
    common(p)
    p = sub.add_parser("list", help="the application alerts")
    p.add_argument("--app", default=None)
    p.add_argument("--all", action="store_true", help="resolved ones too")
    p.add_argument("--json", action="store_true")
    return parser


def _line(row: dict) -> str:
    import time as _time
    who = row.get("recipient") or ("route %s" % row.get("route"))
    when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(row["raised_ts"]))
    state = ("resolved %s" % _time.strftime("%Y-%m-%d %H:%M",
                                            _time.localtime(row["resolved_ts"]))
             if row.get("resolved_ts") is not None else "open")
    return "%s/%s %s → %s%s · raised %s · %s" % (
        row["app"], row["type"], row["key"], who,
        " (urgent)" if row.get("urgent") else "", when, state)


def main(argv: list) -> int:
    import json
    import os
    import sys
    from . import config as config_mod, db as db_mod
    args = build_parser().parse_args(argv)
    cfg = config_mod.load()
    actor = getattr(args, "actor", None) or os.environ.get("AGENT_MAIL_NAME") or None
    try:
        db = db_mod.open_db(cfg)
    except (db_mod.Unavailable, db_mod.DbError) as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    try:
        if args.command == "list":
            rows = listing(db, app=args.app, open_only=not args.all)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False))
            else:
                for row in rows:
                    print(_line(row))
                if not rows:
                    print("no application alert")
            return 0
        if args.command == "raise":
            text = sys.stdin.read() if args.text == "-" else args.text
            urgent = None if args.severity is None else args.severity == "urgent"
            row = raise_alert(db, cfg, app=args.app, type=args.kind, key=args.key,
                              text=text, to=args.to, route=args.route,
                              urgent=urgent, actor=actor)
        else:
            row = resolve_alert(db, app=args.app, type=args.kind, key=args.key,
                                actor=actor)
    except AppAlertRefused as exc:
        if getattr(args, "json", False):
            print(json.dumps({"refused": exc.code, "reason": str(exc)}))
        print("ameesh notify %s: refused (%s): %s" % (args.command, exc.code, exc),
              file=sys.stderr)
        return 2
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
    if args.json:
        print(json.dumps(row, ensure_ascii=False))
    else:
        verb = {"raised": "raised", "open": "already open: nothing changes",
                "resolved": "resolved", "not_open": "not open: nothing changes"}
        print("%s/%s %s: %s" % (args.app, args.kind, args.key, verb[row["state"]]))
    return 0
