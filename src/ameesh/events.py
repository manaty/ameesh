# SPDX-License-Identifier: AGPL-3.0-only
"""The mesh's event log (L85, migration 0396), source of `GET /api/v1/events`.

  ameesh events [--after ID] [--limit N] [--json]   the events after ID (default: the last 20)
  ameesh events prune [--days N] [--dry-run]        retention (default 365 days)

Embeddable UI study (docs/design/etudes/ui-integrable.md §2.1). Each change
the interface shows is one row of the append-only `ameesh_events` table,
written by a trigger in the SAME transaction as the change (psql and psycopg
alike); the insertion emits `pg_notify('ameesh_events', id)`. Types:

  sandbox.created      durable sandbox intent (no Job assumed yet)
  sandbox.bound        one Job UID bound to a turn
  sandbox.result       exactly-once accepted receipt
  sandbox.cancelled    cancellation/termination state changed
  sandbox.orphan       termination could not be observed
  sandbox.tranche_overdraft  a tranche settlement exceeds its admitted bound
  lot.updated          a lot's transition, note or assignment (work_item_events)
  turn.started         an agent goes `running`
  turn.ended           an agent leaves `running`
  turn.stages          the stage receipt of a mail turn: per-stage timestamps
                       and durations from the message to the harness's first
                       output (#3218, written by the runner, `turn_stages`)
  turn.context         the labelled context measures of a turn
                       (`ameesh-context/1`) next to its legacy counters and
                       the close/rotation values (#4870, observation only)
  agent.updated        another change of status, lease, mode or stop reason
  agent.moved          a manual move of a `releve: hote` agent to another
                       admitted host (#3299, written with the move)
  agent.retired        an agent taken out of the active registry, and
  agent.restored       brought back (#3460, written with the gesture)
  cost.recorded        a priced turn (turn_costs)
  thread.entry         an entry in a thread (thread_index)
  decision.created     an action proposed (actions)
  decision.closed      an action leaves `proposed`
  action.updated       another change of an action's state
  receipt.attached     a receipt attached to an action
  approval.recorded    a signed decision of ameesh-approve (mesh_approvals)
  alert.raised         an alert raised, seen by `ameesh notify` (no table: written
  alert.resolved       by the notifier, outside any transaction of the change)
  pilot.gesture        an elected pilot's gesture (merge, queue, assign,
                       packet, decision, or the drill's no-op canary), in the
                       transaction of its lease check
  staging.attested     a complete staging sequence, its active hosts and its
                       commit (no table: written by `actif.py`, #3981)
  staging.sequence     bounded progress snapshot and fixed deadline, written
                       before staging mutations and at each step (#4880)
  staging.request      queued commit and elected execution attempt; survives
                       the launching turn, reused by candidate takeover (#4880)
  git.lock_removed     an orphan git lock removed from a radar clone: path, age,
                       size (no table: written by `resolutions`, #5179)
  packet.pause         a pause with a deadline (alert or automatic end) of the hourly packet tick, or its end
                       (no table: written by `paquet-horaire.py pause|resume`,
                       read by its tick and `role timers --check`, #4361)

`data` holds identifiers and states, never free text: the client rereads the
object through the API. One bounded exception: `packet.pause` carries the
pause's `reason` and announcement `source` (normalized, 200 characters at most,
`packet_pause`), the
only place the tick and `role timers --check` read it from (#4361).

**Delivery order.** A `bigserial` is taken at insertion, not at commit: the
event 10 of a long transaction can become visible after the event 11. A
reader therefore follows a **position** (tx, id): it reads only the events
whose transaction `tx` is below the snapshot's xmin (over for good: committed
or rolled back, no event can come before them any more), by (tx, id), after
its position. Each event has its own position (`format_position`,
"tx-id-journal"), the SSE `id`: resuming after it gives exactly the events
that follow, none twice, none lost. An open transaction (any one, on the
base) holds the delivery back until it ends; it never loses an event.
`tx` only orders within ONE Postgres cluster and timeline, and within ONE
journal: the position carries the cluster's `system_identifier`, its timeline
and a random identifier of the log itself (`ameesh_events_cluster()`,
"<hex>t<timeline>j<journal>", migration 0396/0448), so a position of another
cluster, timeline or mesh — logical restore, point-in-time recovery,
promotion, another schema of the same base — is a reset, never a silent skip.

The retention deletes by `created_at`, compared on the DATABASE's clock
(`now()`), the same clock as the table's own guard, and raises, in the same
statement, a **watermark**: the greatest position it deleted
(`ameesh_events_pruned`). A position below it is a reset, at connection and
for the whole life of the stream; it waits for a locked row rather than skip
it.

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
TYPES = ("lot.updated", "turn.started", "turn.ended", "turn.stages",
         "turn.context", "agent.updated",
         "agent.moved",
         "cost.recorded", "thread.entry", "decision.created", "decision.closed",
         "action.updated", "receipt.attached", "approval.recorded", "alert.raised",
         "alert.resolved", "pilot.gesture", "staging.attested", "staging.sequence", "staging.request", "git.lock_removed",
         "packet.pause")
RETENTION_ENV = "AMEESH_EVENTS_RETENTION_DAYS"
RETENTION_DAYS = 365
#: the table refuses to delete an event younger than this (migration 0396)
MIN_RETENTION_DAYS = 30
PRUNE_BATCH = 10000
#: bound of one pass of the runner's retention (the rest at the next pass)
PRUNE_MAX_BATCHES = 10
PRUNE_MAX_S = 5.0
_TYPE_RE = re.compile(r"^[a-z]+\.[a-z_]+$")
#: "<tx>-<id>-<identity>": the identity is "<sysid>t<timeline>j<journal>"
#: (0396/0448); the journal part is optional, so a position of the code before
#: 0448 is parsed and then compared — it names another journal, hence a reset,
#: never a 400 that would leave a browser retrying forever.
_POSITION_RE = re.compile(
    r"^([0-9]{1,20})-([0-9]{1,19})-"
    r"([0-9a-f]{1,16}t[0-9a-f]{8}(?:j[0-9a-f]{16})?|none)$")
#: the journal's identity when its functions are refused: no resume trusted
UNKNOWN_CLUSTER = "none"
_XID8_MAX = 2 ** 64 - 1
_BIGINT_MAX = 2 ** 63 - 1


def format_position(position: tuple, cluster: str) -> str:
    """A reader's position (tx, id) in `cluster` (`bounds()["cluster"]`, the
    journal's identity) as an SSE id: "tx-id-cluster"."""
    return "%d-%d-%s" % (int(position[0]), int(position[1]), cluster)


def parse_position(text: str) -> tuple | None:
    """The ((tx, id), cluster) of a `Last-Event-ID`, None if it is not one.
    `cluster` is the journal's whole identity, journal identifier included."""
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


def prune(db, *, days: int | None = None,
          batch: int = PRUNE_BATCH, max_batches: int | None = None,
          max_s: float | None = None, clock=time.monotonic) -> int:
    """Deletes the events older than `days` (default `retention_days()`), by
    batches, `max_batches` batches and `max_s` seconds at most (None: until
    done); the number deleted. The cutoff follows the DATABASE's clock
    (`now()`), like the table's own guard: two hosts whose clocks differ prune
    the same events, and a host ahead of the base never has its whole deletion
    refused. A deletion that would outlast `max_s` (a lock, a slow statement)
    fails with DbError: the batches before it stay done."""
    days = retention_days() if days is None else int(days)
    if days < MIN_RETENTION_DAYS:
        raise ValueError("retention: at least %d days" % MIN_RETENTION_DAYS)
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
        n = store.prune(days, batch, timeout)
        total += n
        batches += 1
        if n < batch or (max_batches is not None and batches >= max_batches):
            return total


def expired(db, *, days: int | None = None) -> int:
    """The number of events past the retention (what `prune` would delete),
    on the database's clock."""
    days = retention_days() if days is None else int(days)
    return storage.of(db).events.count_before(days)


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
            print("ameesh events: %d event(s) older than %d days (dry run)"
                  % (storage.of(db).events.count_before(days), days))
            return 0
        print("ameesh events: %d event(s) older than %d days deleted"
              % (prune(db, days=days), days))
        return 0
    finally:
        db.close()


__all__ = ["CHANNEL", "TYPES", "UNKNOWN_CLUSTER", "expired", "format_position", "main",
           "parse_position", "prune", "record",
           "retention_days"]
