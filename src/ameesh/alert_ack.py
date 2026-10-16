# SPDX-License-Identifier: AGPL-3.0-only
"""L87c: acknowledge an alert, without removing it from operations views.

The API supplies authenticated actor/principal. The CLI acts as its local
human. Only the routed recipient (or their authenticated deputy) can ack.
Notify invalidates acknowledgements when its complete snapshot observes
resolution or a severity change, retaining the row as an audit trail.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import storage

HUMAN_RE = re.compile(r"human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")
KEY_RE = re.compile(r"[0-9a-f]{64}\Z")


class AckRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def key_of(alert: dict) -> str:
    """URL-safe representation of the existing alert/dedupe identity."""
    from . import notify
    return hashlib.sha256(notify.key_of(alert).encode("utf-8")).hexdigest()


def severity(alert: dict) -> str:
    from . import notify
    return json.dumps([alert.get("severity"), alert.get("level"),
                       bool(alert.get("critical")),
                       alert.get("type") in notify.URGENT_TYPES or bool(alert.get("urgent"))],
                      ensure_ascii=False, separators=(",", ":"))


def current(cfg, db, **thresholds) -> list[dict]:
    from . import db as db_mod, exploitation
    if not thresholds:
        thresholds = exploitation.thresholds(exploitation.build_parser().parse_args(["alerts"]))
    return db_mod.batched(db, lambda db: exploitation.alerts(cfg, db, **thresholds))


def acknowledge(cfg, db, key: str, *, actor: str, principal: str | None = None,
                **thresholds) -> dict:
    from . import notify
    principal = actor if principal is None else principal
    if not all(isinstance(h, str) and HUMAN_RE.fullmatch(h) for h in (actor, principal)):
        raise AckRefused("bad_actor", "a human actor and principal are required")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise AckRefused("bad_key", "alert key: 64 lowercase hexadecimal characters required")
    if storage.is_remote(db):
        raise AckRefused("native_only", "alert acknowledgement needs a native operator connection")
    alert = next((a for a in current(cfg, db, **thresholds) if key_of(a) == key), None)
    if alert is None:
        raise AckRefused("not_found", "no current alert with this key")
    # The local console pass must work without the mesh database (#1179),
    # so it cannot honour a persistent acknowledgement or observe its end.
    if alert.get("type") in notify.LOCAL_TYPES:
        raise AckRefused("local_alert", "local pool alerts cannot be acknowledged")
    recipient, _ = notify.Router(cfg, db, notify.parse_config(cfg.notify)).resolve(alert)
    if principal != recipient:
        raise AckRefused("forbidden", "only the alert's human recipient can acknowledge it")
    return storage.of(db).alert_acknowledgements.acknowledge(
        key, severity(alert), actor, principal, observation_host=alert.get("observation_host"))


def matches(rows: list[dict], alert: dict, human: str | None) -> dict | None:
    key, level = key_of(alert), severity(alert)
    return next((r for r in rows if r["alert_key"] == key and r["principal"] == human
                 and r["severity"] == level), None)


def annotate(cfg, db, alerts: list[dict], *, human: str | None = None) -> list[dict]:
    """Read only: add the usable key and acknowledgement, never filter alerts."""
    from . import notify
    rows = storage.of(db).alert_acknowledgements.live()
    router = notify.Router(cfg, db, notify.parse_config(cfg.notify))
    out = []
    for alert in alerts:
        key = key_of(alert)
        candidates = [r for r in rows if r["alert_key"] == key
                      and r["severity"] == severity(alert)]
        ack = None
        if candidates:
            recipient, _ = router.resolve(alert)
            if human in (None, recipient):
                ack = matches(candidates, alert, recipient)
        out.append(dict(alert, key=key, acknowledged=ack is not None,
                        acknowledgement=ack))
    return out


def reconcile(db, current: list[dict], *, observed_ts: float, dry_run: bool = False,
              observation_host: str | None = None) -> list[dict]:
    """Invalidate old observations; return the pre-invalidation rows too, so
    a just-resolved acknowledged alert does not send a resolution message."""
    domain = storage.of(db).alert_acknowledgements
    rows = domain.live()
    levels = {key_of(a): severity(a) for a in current}
    if not dry_run:
        for row in rows:
            if row["observation_host"] is not None and row["observation_host"] != observation_host:
                continue
            level = levels.get(row["alert_key"])
            if level != row["severity"]:
                domain.invalidate(row["id"], reason="resolved" if level is None
                                  else "severity_changed", observed_ts=observed_ts)
    return rows


def cmd_cli(cfg, args) -> int:
    import sys
    from . import db as db_mod, exploitation, identity, serve
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            binding = identity.resolve_binding(cfg, db)
            if binding.name or binding.ok:
                raise AckRefused("bad_actor", "an agent session cannot act as the local human")
            human = serve.owner(cfg, args.human)
            out = acknowledge(cfg, db, args.key, actor=human, principal=human,
                              **exploitation.thresholds(args))
        except (AckRefused, ValueError) as exc:
            print("ameesh alerts ack: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("alert %s acknowledged by %s for %s%s" % (
                args.key, out["actor"], out["principal"],
                "" if out["changed"] else " (unchanged)"))
        return 0
    finally:
        db.close()
