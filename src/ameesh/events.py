# SPDX-License-Identifier: AGPL-3.0-only
"""The mesh's event log (L85, migration 0396), source of `GET /api/v1/events`.

  ameesh events [--after ID] [--limit N] [--json]   the events after ID (default: the last 20)
  ameesh events prune [--days N] [--dry-run]        retention (default 365 days)

Embeddable UI study (docs/design/etudes/ui-integrable.md §2.1). Each change
the interface shows is one row of the append-only `ameesh_events` table,
written by a trigger in the SAME transaction as the change (psql and psycopg
alike); the insertion emits `pg_notify('ameesh_events', id)`. Types:

  lot.updated          a lot's transition, note or assignment (work_item_events)
  turn.started         an agent goes `running`
  turn.ended           an agent leaves `running`
  agent.updated        another change of status, lease, mode or stop reason
  cost.recorded        a priced turn (turn_costs)
  thread.entry         an entry in a thread (thread_index)
  decision.created     an action proposed (actions)
  decision.closed      an action leaves `proposed`
  action.updated       another change of an action's state
  receipt.attached     a receipt attached to an action
  approval.recorded    a signed decision of ameesh-approve (mesh_approvals)
  alert.raised         an alert raised, seen by `ameesh notify` (no table: written
  alert.resolved       by the notifier, outside any transaction of the change)

`data` holds identifiers and states, never free text: the client rereads the
object through the API.

**Delivery order.** A `bigserial` is taken at insertion, not at commit: the
event 10 of a long transaction can become visible after the event 11. A
reader therefore keeps a `Cursor`: its `position` is the highest id below
which every event was delivered (or will never exist: a rolled-back
transaction leaves a hole); the events above it already delivered are
remembered, and a hole is skipped after `GAP_S` seconds. Delivery is at least
once: resuming from `position` may give an event again, `id` tells it apart.

**Retention** (0033 §9): one year by default, `AMEESH_EVENTS_RETENTION_DAYS`
on the host that prunes (at least 30: the table refuses to delete younger
events). The runner prunes once an hour; `ameesh events prune` does it by
hand.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from . import storage

CHANNEL = "ameesh_events"
TYPES = ("lot.updated", "turn.started", "turn.ended", "agent.updated", "cost.recorded",
         "thread.entry", "decision.created", "decision.closed", "action.updated",
         "receipt.attached", "approval.recorded", "alert.raised", "alert.resolved")
RETENTION_ENV = "AMEESH_EVENTS_RETENTION_DAYS"
RETENTION_DAYS = 365
#: the table refuses to delete an event younger than this (migration 0396)
MIN_RETENTION_DAYS = 30
PRUNE_BATCH = 10000
#: a hole in the ids is waited for this long before being skipped
GAP_S = 30.0
_TYPE_RE = re.compile(r"^[a-z]+\.[a-z_]+$")


def record(db, type: str, object: str, *, project: str | None = None, **data) -> int:
    """Appends an event that no trigger writes (an alert); its id."""
    if not _TYPE_RE.match(type):
        raise ValueError("event type refused: %r" % type)
    if not object or len(object) > 300:
        raise ValueError("event object refused")
    return storage.of(db).events.record(type, object, project, data)


def retention_days(env=None) -> int:
    """The retention of the log, in days: `AMEESH_EVENTS_RETENTION_DAYS`, else 365."""
    raw = (env if env is not None else os.environ).get(RETENTION_ENV, "").strip()
    if not raw:
        return RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("%s: a number of days expected" % RETENTION_ENV) from exc
    if days < MIN_RETENTION_DAYS:
        raise ValueError("%s: at least %d days" % (RETENTION_ENV, MIN_RETENTION_DAYS))
    return days


def prune(db, *, days: int | None = None, now: float | None = None,
          batch: int = PRUNE_BATCH) -> int:
    """Deletes the events older than `days` (default `retention_days()`), by
    batches; the number deleted."""
    days = retention_days() if days is None else int(days)
    if days < MIN_RETENTION_DAYS:
        raise ValueError("retention: at least %d days" % MIN_RETENTION_DAYS)
    before = (time.time() if now is None else now) - days * 86400.0
    store = storage.of(db).events
    total = 0
    while True:
        n = store.prune(before, batch)
        total += n
        if n < batch:
            return total


class Cursor:
    """A reader's place in the log, at least once (see the module's doc).

    `take(rows)` receives rows of id above `position`, by increasing id
    (already delivered ones included), and returns those not yet delivered."""

    def __init__(self, position: int, *, gap_s: float = GAP_S, clock=time.monotonic):
        self.position = int(position)
        self.gap_s = gap_s
        self.clock = clock
        self._seen: set[int] = set()
        self._stalled_at: float | None = None

    @property
    def stalled(self) -> bool:
        """A hole is being waited for."""
        return bool(self._seen)

    def take(self, rows) -> list:
        fresh = [row for row in rows
                 if int(row["id"]) > self.position and int(row["id"]) not in self._seen]
        self._seen.update(int(row["id"]) for row in fresh)
        self.advance()
        return fresh

    def advance(self) -> None:
        """Moves `position` over the delivered ids, and over a hole older than `gap_s`."""
        while True:
            while self.position + 1 in self._seen:
                self.position += 1
                self._seen.discard(self.position)
            if not self._seen:
                self._stalled_at = None
                return
            now = self.clock()
            if self._stalled_at is None:
                self._stalled_at = now
                return
            if now - self._stalled_at < self.gap_s:
                return
            # the hole is given up: a rolled-back transaction, or a very long one
            self.position = min(self._seen) - 1
            self._stalled_at = None


def _main_list(cfg, db, args) -> int:
    store = storage.of(db).events
    after = args.after
    if after is None:
        after = max(0, store.bounds()["last"] - args.limit)
    rows = store.after(after, args.limit)
    if args.json:
        print(json.dumps({"schema": "ameesh-events/1", "events": rows}, ensure_ascii=False,
                         sort_keys=True, default=str))
        return 0
    for row in rows:
        print("%s  %-18s %-24s %-12s %s" % (
            row["id"], row["type"], row["object"], row.get("project") or "-",
            json.dumps(row["data"], ensure_ascii=False, sort_keys=True, default=str)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh events", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--after", type=int, help="the events of id greater than this one")
    parser.add_argument("--limit", type=int, default=20, help="events at most (default 20)")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("prune", help="delete the events older than the retention")
    p.add_argument("--days", type=int, help="retention in days (default: %s, else %d)"
                   % (RETENTION_ENV, RETENTION_DAYS))
    p.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    from . import config as config_mod
    from . import db as db_mod
    args = build_parser().parse_args(argv)
    if args.limit < 1 or args.limit > 1000:
        print("ameesh events: --limit between 1 and 1000", file=sys.stderr)
        return 2
    cfg = config_mod.load()
    try:
        db = db_mod.open_db(cfg)
    except db_mod.DbError as exc:
        print("ameesh events: database unavailable: %s" % exc, file=sys.stderr)
        return 1
    try:
        if args.command != "prune":
            return _main_list(cfg, db, args)
        try:
            days = retention_days() if args.days is None else args.days
            if days < MIN_RETENTION_DAYS:
                raise ValueError("--days: at least %d" % MIN_RETENTION_DAYS)
        except ValueError as exc:
            print("ameesh events: %s" % exc, file=sys.stderr)
            return 2
        if args.dry_run:
            before = time.time() - days * 86400.0
            print("ameesh events: %d event(s) older than %d days (dry run)"
                  % (storage.of(db).events.count_before(before), days))
            return 0
        print("ameesh events: %d event(s) older than %d days deleted"
              % (prune(db, days=days), days))
        return 0
    finally:
        db.close()


__all__ = ["CHANNEL", "Cursor", "GAP_S", "TYPES", "main", "prune", "record",
           "retention_days"]
