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
reader therefore follows a **position** (tx, id): it reads only the events
whose transaction `tx` is below the snapshot's xmin (over for good: committed
or rolled back, no event can come before them any more), by (tx, id), after
its position. Each event has its own position (`format_position`,
"tx-id-cluster"), the SSE `id`: resuming after it gives exactly the events
that follow, none twice, none lost. An open transaction (any one, on the
base) holds the delivery back until it ends; it never loses an event.
`tx` only orders within ONE Postgres cluster: the position carries the
cluster's `system_identifier` (hex), and a position of another cluster (a
logical restore, a rebuilt base) is a reset, never a silent skip.

The retention deletes a **prefix** in (tx, id) order (it stops at the first
event still kept, and waits for a locked row rather than skipping it): the
oldest event kept says exactly what was lost.

**Retention** (0033 §9): one year by default, `AMEESH_EVENTS_RETENTION_DAYS`
on the host that prunes (at least 30: the table refuses to delete younger
events). The runner prunes once an hour, `PRUNE_MAX_BATCHES` batches and
`PRUNE_MAX_S` seconds at most (the rest at the next hour); `ameesh events
prune` does it by hand, with no bound.
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
#: bound of one pass of the runner's retention (the rest at the next pass)
PRUNE_MAX_BATCHES = 10
PRUNE_MAX_S = 5.0
_TYPE_RE = re.compile(r"^[a-z]+\.[a-z_]+$")
_POSITION_RE = re.compile(r"^([0-9]{1,20})-([0-9]{1,19})-([0-9a-f]{1,16})$")
_XID8_MAX = 2 ** 64 - 1
_BIGINT_MAX = 2 ** 63 - 1


def format_position(position: tuple, cluster: str) -> str:
    """A reader's position (tx, id) in `cluster` (`bounds()["cluster"]`) as
    an SSE id: "tx-id-cluster"."""
    return "%d-%d-%s" % (int(position[0]), int(position[1]), cluster)


def parse_position(text: str) -> tuple | None:
    """The ((tx, id), cluster) of a `Last-Event-ID`, None if it is not one."""
    match = _POSITION_RE.match(text.strip())
    if not match:
        return None
    tx, last_id = int(match.group(1)), int(match.group(2))
    if tx > _XID8_MAX or last_id > _BIGINT_MAX:
        return None
    return (tx, last_id), match.group(3)


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
          batch: int = PRUNE_BATCH, max_batches: int | None = None,
          max_s: float | None = None, clock=time.monotonic) -> int:
    """Deletes the events older than `days` (default `retention_days()`), by
    batches, `max_batches` batches and `max_s` seconds at most (None: until
    done); the number deleted. A prefix in (tx, id) order: it stops at the
    first event still kept. A deletion that would outlast `max_s` (a lock, a
    slow statement) fails with DbError: the batches before it stay done."""
    days = retention_days() if days is None else int(days)
    if days < MIN_RETENTION_DAYS:
        raise ValueError("retention: at least %d days" % MIN_RETENTION_DAYS)
    before = (time.time() if now is None else now) - days * 86400.0
    store = storage.of(db).events
    total, batches, start = 0, 0, clock()
    while True:
        timeout = None
        if max_s is not None:
            # the rest of the budget bounds each deletion itself (lock and
            # statement timeout): a blocked query never outlasts `max_s`
            timeout = max_s - (clock() - start)
            if timeout <= 0:
                return total
        n = store.prune(before, batch, timeout)
        total += n
        batches += 1
        if n < batch or (max_batches is not None and batches >= max_batches):
            return total


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


__all__ = ["CHANNEL", "TYPES", "format_position", "main", "parse_position", "prune", "record",
           "retention_days"]
