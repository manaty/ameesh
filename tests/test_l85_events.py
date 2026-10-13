# SPDX-License-Identifier: AGPL-3.0-only
"""L85: the event log (`ameesh_events`, migration 0396) and `GET /api/v1/events`.

Events written by triggers in the same transaction as the change, append-only
table, retention, NOTIFY with the id alone, the at-least-once cursor, and the
SSE stream (live, resume with Last-Event-ID, reset, heartbeat, limits).
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import os
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import db as db_mod
from ameesh import events, fil, notify, registry, runner, serve, storage, work
from ameesh import actions

from .support import PgTestCase
from .test_l84_serve import TOKEN, ServerMixin


def _rows(db, after=0):
    return storage.of(db).events.after(after, 1000)


def _types(rows):
    return [(row["type"], row["object"]) for row in rows]


class PositionTest(unittest.TestCase):
    def test_format_and_parse(self):
        self.assertEqual(events.format_position((70001, 12), "6f3at00000001"),
                         "70001-12-6f3at00000001")
        self.assertEqual(events.parse_position(" 70001-12-6f3at00000001 "),
                         ((70001, 12), "6f3at00000001"))
        self.assertEqual(events.parse_position("1-2-none"), ((1, 2), events.UNKNOWN_CLUSTER))
        for bad in ("12", "a-1", "1-", "-1", "70001-12", "1-2-x", "1-2-3-4", "1-2-",
                    "1-2-6f3a", "1-2-6f3at1", "1-2-%st00000001" % ("f" * 17), ""):
            self.assertIsNone(events.parse_position(bad), bad)

    def test_out_of_range_is_refused(self):
        """Codex P3 r2: the digit counts allow values beyond xid8 and bigint."""
        top = "%d-%d-at00000001" % (2 ** 64 - 1, 2 ** 63 - 1)
        self.assertEqual(events.parse_position(top),
                         ((2 ** 64 - 1, 2 ** 63 - 1), "at00000001"))
        for bad in ("%d-1-at00000001" % 2 ** 64, "1-%d-at00000001" % 2 ** 63,
                    "99999999999999999999-1-at00000001"):
            self.assertIsNone(events.parse_position(bad), bad)


class RetentionSettingTest(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(events.retention_days({}), 365)
        self.assertEqual(events.retention_days({events.RETENTION_ENV: "90"}), 90)
        for bad in ("29", "un an", "-1"):
            with self.assertRaises(ValueError):
                events.retention_days({events.RETENTION_ENV: bad})

    def test_record_refuses_a_malformed_type(self):
        with self.assertRaises(ValueError):
            events.record(None, "Lot Updated", "lot:1")

    def test_every_type_is_in_the_openapi_enum(self):
        enum = serve.SCHEMAS["ameesh-event/1"]["properties"]["type"]["enum"]
        self.assertEqual(enum, list(events.TYPES))


class ListenerErrorTest(unittest.TestCase):
    """R2 auditeur P2-3: a LISTEN the base refuses (hot standby) or that the
    driver does not translate makes the listener unavailable, it never kills
    the hub thread."""

    def driver(self, connect):
        driver = object.__new__(db_mod.PsycopgDriver)
        patcher = mock.patch.object(driver, "_connect", **connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        return driver

    def test_an_untranslated_error_becomes_a_db_error(self):
        conn = mock.Mock()
        conn.execute.side_effect = RuntimeError("cannot execute LISTEN during recovery")
        driver = self.driver({"return_value": conn})
        with self.assertRaises(db_mod.DbError):
            driver.listen([events.CHANNEL])
        # and the hub's constructor sees no listener: it polls
        self.assertIsNone(db_mod.listener(driver, [events.CHANNEL]))

    def test_the_missing_listen_support_keeps_its_message(self):
        driver = self.driver({"side_effect": AttributeError("psycopg 2")})
        with self.assertRaises(db_mod.Unavailable) as caught:
            driver.listen([events.CHANNEL])
        self.assertIn("psycopg without LISTEN support", str(caught.exception))


class RunnerPruneTest(unittest.TestCase):
    """The runner prunes at most once an hour, never in dry run."""

    def fake(self, **kw):
        fields = dict(db=object(), dry_run=False, _events_pruned_at=None,
                      _events_prune_error=None)
        fields.update(kw)
        return types.SimpleNamespace(**fields)

    def test_once_an_hour(self):
        fake = self.fake()
        with mock.patch.object(events, "prune", return_value=3) as prune, \
                mock.patch.object(runner, "log_async"):
            self.assertEqual(runner.Runner.prune_events_once(fake, now=100.0), 3)
            # bounded: the rest at the next hour
            self.assertEqual(prune.call_args.kwargs, {
                "max_batches": events.PRUNE_MAX_BATCHES, "max_s": events.PRUNE_MAX_S})
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=200.0))
            self.assertEqual(runner.Runner.prune_events_once(fake, now=100.0 + 3600), 3)
        self.assertEqual(prune.call_count, 2)

    def test_dry_run_and_errors(self):
        with mock.patch.object(events, "prune") as prune:
            self.assertIsNone(runner.Runner.prune_events_once(self.fake(dry_run=True)))
            # a mediated executor has no base: the mesh server prunes
            self.assertIsNone(runner.Runner.prune_events_once(self.fake(mediated=True)))
        prune.assert_not_called()
        fake = self.fake()
        with mock.patch.object(events, "prune", side_effect=db_mod.DbError("down")), \
                mock.patch.object(runner, "log_async") as said:
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=1.0))
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=1.0 + 3600))
        self.assertEqual(said.call_count, 1)   # said once

    def test_nothing_deleted_while_expired_events_remain_is_said(self):
        """Codex P2 r5: a blocked retention returned 0 "successfully"."""
        fake = self.fake()
        with mock.patch.object(events, "prune", return_value=0), \
                mock.patch.object(events, "expired", return_value=7), \
                mock.patch.object(runner, "log_async") as said:
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=1.0))
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=1.0 + 3600))
        self.assertEqual(said.call_count, 1)
        self.assertIn("7 expired event(s) left, none deleted", said.call_args.args[0])
        with mock.patch.object(events, "prune", return_value=0), \
                mock.patch.object(events, "expired", return_value=0), \
                mock.patch.object(runner, "log_async") as said:
            self.assertEqual(runner.Runner.prune_events_once(fake, now=1.0 + 7200), 0)
        said.assert_not_called()


class EventLogPgTest(PgTestCase):
    """The triggers, the table's guards, the retention, NOTIFY."""

    def last(self):
        return storage.of(self.db).events.bounds()["last"]

    def test_lot_events_carry_the_project(self):
        start = self.last()
        lot = work.add(self.db, title="lot", app="demo")
        work.move(self.db, lot["id"], "build")
        rows = _rows(self.db, start)
        self.assertEqual(_types(rows), [("lot.updated", "lot:%d" % lot["id"])] * 2)
        self.assertEqual([r["data"]["state"] for r in rows], ["intake", "build"])
        self.assertEqual({r["project"] for r in rows}, {"demo"})

    def test_turns_lease_and_unchanged_updates(self):
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host="h1")
        start = self.last()
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'a1'")
        self.db.execute("UPDATE agent_registry SET status = 'running', last_seen = now()"
                        " WHERE name = 'a1'")   # nothing changes: no event
        self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = 'a1'")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r1' WHERE name = 'a1'")
        self.db.execute("UPDATE agent_registry SET last_seen = now() WHERE name = 'a1'")
        rows = _rows(self.db, start)
        self.assertEqual([r["type"] for r in rows],
                         ["turn.started", "turn.ended", "agent.updated"])
        self.assertEqual(rows[1]["data"]["previous"], "running")
        self.assertTrue(rows[2]["data"]["leased"])
        self.assertEqual({r["project"] for r in rows}, {"demo"})

    def test_cost_thread_action_approval(self):
        registry.upsert(self.db, "a2", chantier="demo", harness="codex", host="h1")
        start = self.last()
        self.db.execute("INSERT INTO turn_costs (agent, harness, turn, usd)"
                        " VALUES ('a2', 'codex', 't1', 0.25)")
        fil.record(self.cfg, self.db, sender="a2", recipients=["tous"], text="secret text",
                   project="demo", lot="7")
        from ameesh.connectors.shell_noop import ShellNoopConnector
        noop = ShellNoopConnector(os.path.join(self.tmp, "noop"), timeout=10.0)
        action = actions.propose(self.db, noop, project="demo", operation="send",
                                 target="client:42", args={"n": 1}, proposed_by="agent:a2")
        actions.cancel(self.db, action["action_id"], by="human:o")
        self.db.execute(
            "INSERT INTO mesh_approvals (approver, action, artifact_hash, nonce,"
            " signed_payload, signature, signature_key, expires_at)"
            " VALUES ('human:o', 'merge', 'h', 'n', 'p', 's', 'k', now() + interval '1 h')")
        rows = _rows(self.db, start)
        kinds = [r["type"] for r in rows]
        for kind in ("cost.recorded", "thread.entry", "decision.created", "decision.closed",
                     "approval.recorded"):
            self.assertIn(kind, kinds)
        by = {r["type"]: r for r in rows}
        self.assertEqual(by["cost.recorded"]["project"], "demo")
        self.assertIn("thread:demo/7", [r["object"] for r in rows if r["type"] == "thread.entry"])
        self.assertEqual(by["decision.closed"]["data"]["state"], "cancelled")
        # never free text in the log
        self.assertNotIn("secret text", json.dumps([r["data"] for r in rows]))

    def test_same_transaction_a_rollback_leaves_no_event(self):
        lot = work.add(self.db, title="lot", app="demo")
        start = self.last()

        class Boom(Exception):
            pass
        with self.assertRaises(Boom):
            with self.db.transaction() as tx:
                tx.execute("INSERT INTO work_item_events (work_item_id, state)"
                           " VALUES (%s, 'qa')", (lot["id"],))
                raise Boom()
        self.assertEqual(_rows(self.db, start), [])

    def _empty(self):
        # a known table: the fixture writes events of its own
        self.db.execute("TRUNCATE ameesh_events, ameesh_events_pruned")

    def test_append_only_and_retention(self):
        self._empty()
        ancient = self.db.query(
            "INSERT INTO ameesh_events (type, object, created_at)"
            " VALUES ('alert.resolved', 'alert:x', now() - interval '400 days') RETURNING id")
        recent = self.db.query(
            "INSERT INTO ameesh_events (type, object, created_at)"
            " VALUES ('alert.resolved', 'alert:x', now() - interval '100 days') RETURNING id")
        old = storage.of(self.db).events.record("alert.raised", "alert:x", None, {})
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE ameesh_events SET type = 'x.y' WHERE id = %s", (old,))
        with self.assertRaises(db_mod.DbError):
            self.db.execute("DELETE FROM ameesh_events WHERE id = %s", (old,))
        self.assertEqual(events.prune(self.db, days=365), 1)
        ids = {r["id"] for r in _rows(self.db)}
        self.assertNotIn(ancient[0]["id"], ids)
        self.assertIn(recent[0]["id"], ids)
        self.assertEqual(events.prune(self.db, days=90, batch=1), 1)
        with self.assertRaises(ValueError):
            events.prune(self.db, days=10)

    def test_prune_is_bounded(self):
        self._empty()
        for _ in range(5):
            self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                            " VALUES ('alert.resolved', 'alert:b', now() - interval '400 days')")
        self.assertEqual(events.prune(self.db, days=365, batch=2, max_batches=2), 4)
        ticks = iter([0.0, 1.0, 10.0])
        self.assertEqual(events.prune(self.db, days=365, batch=1, max_s=5.0,
                                      clock=lambda: next(ticks)), 1)
        self.assertEqual(events.prune(self.db, days=365), 0)

    def test_prune_budget_bounds_each_deletion(self):
        """Codex P2 r2: the budget was checked only after a deletion returned.
        Each deletion now gets the rest of `max_s` as lock and statement timeout."""
        store = mock.Mock()
        store.prune.return_value = 1
        ticks = iter([0.0, 1.0, 4.5, 6.0])
        with mock.patch.object(storage, "of", return_value=types.SimpleNamespace(events=store)):
            self.assertEqual(events.prune(self.db, days=365, batch=1, max_s=5.0,
                                          clock=lambda: next(ticks)), 2)
        self.assertEqual([c.args[2] for c in store.prune.call_args_list], [4.0, 0.5])

    def _old_event(self, name, days=400):
        self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                        " VALUES ('alert.resolved', %s, now() - make_interval(days => %s))",
                        (name, days))

    def _kept(self):
        return [r["object"] for r in self.db.query(
            "select object from ameesh_events where object like 'alert:p%%'"
            " order by tx, id")]

    def test_prune_waits_for_a_locked_row_instead_of_leaving_a_gap(self):
        """Codex P2 r4: with SKIP LOCKED, a locked A survives while B goes;
        A, still the oldest, hid the gap: a resume from A lost B silently."""
        self._empty()
        for name in ("alert:pa", "alert:pb"):
            self._old_event(name)
        self._old_event("alert:pc", days=0)
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        try:
            with other.transaction() as tx:
                tx.query("select id from ameesh_events where object = 'alert:pa' for update")
                start = time.monotonic()
                with self.assertRaises(db_mod.DbError):      # lock_timeout: the budget
                    events.prune(self.db, days=365, max_s=1.0)
                self.assertLess(time.monotonic() - start, 5.0)
                self.assertEqual(self._kept(), ["alert:pa", "alert:pb", "alert:pc"])
                raise KeyError("the locker rolls back")
        except KeyError:
            pass
        self.assertEqual(events.prune(self.db, days=365), 2)
        self.assertEqual(self._kept(), ["alert:pc"])
        with other.transaction() as tx:
            tx.execute("lock table ameesh_events in access exclusive mode")
            start = time.monotonic()
            with self.assertRaises(db_mod.DbError):
                events.prune(self.db, days=365, max_s=1.0)
            self.assertLess(time.monotonic() - start, 5.0)

    def test_prune_is_not_stopped_by_a_kept_event_and_raises_the_watermark(self):
        """Codex P2 r5: a first row (in (tx, id) order) that never expires
        stopped a prefix retention for good. By `created_at`, and the
        watermark (the greatest position deleted) bounds what was lost."""
        self._empty()
        self._old_event("alert:pa")
        self._old_event("alert:pk", days=0)
        self._old_event("alert:pz")
        rows = {r["object"]: (int(r["tx"]), int(r["id"])) for r in self.db.query(
            "select object, tx::text as tx, id from ameesh_events")}
        store = storage.of(self.db).events
        self.assertEqual(store.bounds()["pruned"], (0, 0))
        self.assertEqual(events.prune(self.db, days=365), 2)
        self.assertEqual(self._kept(), ["alert:pk"])
        self.assertEqual(store.bounds()["pruned"], rows["alert:pz"])
        # never lowered
        self._old_event("alert:pb")
        self.db.execute("insert into ameesh_events_pruned (one, tx, id) values (true, '1', 1)"
                        " on conflict (one) do nothing")
        self.assertEqual(store.bounds()["pruned"], rows["alert:pz"])

    def test_created_at_is_finite_and_not_in_the_future(self):
        for value, why in (("'infinity'", "not finite"), ("'-infinity'", "not finite"),
                           ("now() + interval '2 days'", "in the future")):
            with self.assertRaisesRegex(db_mod.DbError, why, msg=value):
                self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                                " VALUES ('alert.resolved', 'alert:f', %s)" % value)
        self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                        " VALUES ('alert.resolved', 'alert:f', now() + interval '1 minute')")

    def test_rows_above_the_counter_say_restore_not_open_transaction(self):
        """Codex P3 r5: after a logical restore, rows of the old cluster sit
        above the counter; `delayed` named an open transaction."""
        store = storage.of(self.db).events
        position = store.bounds()["head"]
        self.addCleanup(self._empty)
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, tx) VALUES ('alert.raised', 'alert:r',"
            " ((pg_current_xact_id()::text::bigint + 1000000)::text)::xid8)")
        self.assertEqual(store.readable(position, 100), ([], "restore"))

    def test_a_long_project_name_never_refuses_the_thread_write(self):
        project = "p" * 301
        start = self.last()
        self.db.execute("INSERT INTO thread_index (project, location) VALUES (%s, 'x')",
                        (project,))
        rows = _rows(self.db, start)
        self.assertEqual(len(rows[0]["object"]), 300)
        self.assertEqual(rows[0]["project"], project)

    def test_commit_order_an_open_transaction_holds_later_events_back(self):
        """Codex P1 r1: A takes id 11 and stays open, B takes 12 and commits;
        the reader must neither give 12 and skip 11, nor lose 11."""
        store = storage.of(self.db).events
        position = store.bounds()["head"]
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with other.transaction() as tx_a:
            a = storage.of(tx_a).events.record("alert.raised", "alert:a", None, {})
            b = events.record(self.db, "alert.raised", "alert:b")
            self.assertGreater(b, a)
            self.assertEqual(store.readable(position, 100), ([], "xmin"))   # held back by A
        rows, waiting = store.readable(position, 100)
        self.assertEqual([r["object"] for r in rows], ["alert:a", "alert:b"])
        self.assertFalse(waiting)
        self.assertFalse(store.readable((int(rows[-1]["tx"]), int(rows[-1]["id"])), 100)[1])
        # a rolled-back transaction leaves nothing to wait for
        after = (int(rows[-1]["tx"]), int(rows[-1]["id"]))
        try:
            with other.transaction() as tx_c:
                storage.of(tx_c).events.record("alert.raised", "alert:c", None, {})
                raise KeyError("rollback")
        except KeyError:
            pass
        events.record(self.db, "alert.raised", "alert:d")
        self.assertEqual([r["object"] for r in store.readable(after, 100)[0]], ["alert:d"])

    def test_readable_and_waiting_share_one_snapshot(self):
        """Codex P2 r2: A holds B back, then ends WITHOUT writing an event
        between two reads on two snapshots: `[]` then `waiting` False, and B
        undelivered with no notification to come. One statement, one snapshot."""
        store = storage.of(self.db).events
        position = store.bounds()["head"]
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with other.transaction() as tx_a:
            tx_a.query("select pg_current_xact_id()")      # A: an xid, no event
            events.record(self.db, "alert.raised", "alert:b")
            with mock.patch.object(self.db, "query", wraps=self.db.query) as query:
                self.assertEqual(store.readable(position, 100), ([], "xmin"))
            self.assertEqual(query.call_count, 1)
        rows, waiting = store.readable(position, 100)        # A over, no event of its own
        self.assertEqual(([r["object"] for r in rows], waiting), (["alert:b"], None))

    def test_a_limited_read_orders_the_position_as_a_number(self):
        """R2 auditeur P1: the CTE selects `tx::text as tx`, and a bare
        `order by tx` reads that output column: the sort was textual, so a
        read smaller than the backlog across a power of ten returned the
        recent first and the cursor skipped the older events for good."""
        self._empty()
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, tx) VALUES"
            " ('alert.raised', 'alert:nine', '9'::xid8),"
            " ('alert.raised', 'alert:ten', '10'::xid8),"
            " ('alert.raised', 'alert:eleven', '11'::xid8)")
        store = storage.of(self.db).events
        self.assertEqual([r["object"] for r in store.readable((0, 0), 1)[0]],
                         ["alert:nine"])
        rows, waiting = store.readable((0, 0), 10)
        self.assertEqual([r["object"] for r in rows],
                         ["alert:nine", "alert:ten", "alert:eleven"])
        self.assertIsNone(waiting)
        # the cursor follows the same numeric order, batch after batch
        first = store.readable((0, 0), 1)[0][0]
        following, _ = store.readable((int(first["tx"]), int(first["id"])), 1)
        self.assertEqual([r["object"] for r in following], ["alert:ten"])

    def test_notify_carries_the_id_alone(self):
        listener = db_mod.listener(self.db, [events.CHANNEL])
        if listener is None:
            self.skipTest("driver without LISTEN")
        try:
            new = events.record(self.db, "alert.raised", "alert:x", agent="nobody")
            deadline = time.monotonic() + 5
            got = None
            while time.monotonic() < deadline:
                got = listener.wait(0.5)
                if got and got.get("channel") == events.CHANNEL:
                    break
            self.assertEqual(got, {"channel": events.CHANNEL, "payload": str(new)})
        finally:
            listener.close()

    def test_listen_works_on_a_read_only_session(self):
        ro = db_mod.connect(dataclasses.replace(self.cfg, read_only=True))
        try:
            listener = db_mod.listener(ro, [events.CHANNEL])
            self.assertIsNotNone(listener)
            listener.close()
        finally:
            ro.close()

    def test_notifier_records_raised_and_resolved(self):
        start = self.last()
        ncfg = notify.parse_config({"types": ["stale_agent"]})
        notifier = notify.Notifier(self.cfg, ncfg, sender=notify.DrySender(),
                                   log=lambda text: None, clock=lambda: 1000.0)
        alert = {"type": "stale_agent", "agent": "a9", "since": 1.0, "detail": "d"}
        notifier.run_pass(self.db, current=[alert])
        notifier.run_pass(self.db, current=[alert])   # same episode: nothing new
        notifier.run_pass(self.db, current=[])
        rows = [r for r in _rows(self.db, start) if r["type"].startswith("alert.")]
        self.assertEqual([r["type"] for r in rows], ["alert.raised", "alert.resolved"])
        self.assertEqual(rows[0]["data"]["alert"], "stale_agent")
        self.assertEqual(rows[0]["data"]["agent"], "a9")
        self.assertEqual(rows[0]["data"]["key"], notify.key_of(alert))

    def test_cli_lists_and_prunes(self):
        events.record(self.db, "alert.raised", "alert:cli")
        out = self.mesh("events", "--json", "--limit", "5")
        self.assertEqual(out.returncode, 0, out.stderr)
        listed = json.loads(out.stdout)
        self.assertEqual(listed["schema"], "ameesh-events/1")
        self.assertEqual(listed["events"][-1]["object"], "alert:cli")
        out = self.mesh("events", "prune", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("older than 365 days", out.stdout)
        self.assertEqual(self.mesh("events", "prune", "--days", "5").returncode, 2)


class StreamPgTest(ServerMixin, PgTestCase):
    """`GET /api/v1/events` against a migrated schema."""

    def setUp(self):
        super().setUp()
        self.ro = dataclasses.replace(self.cfg, read_only=True)
        self.read = serve.db_reader(self.cfg)

    def serve(self, *, hub=True, listen=True, **kwargs):
        self.hub = serve.Hub(self.ro, self.read, poll_s=0.2, listen=listen).start() \
            if hub else None
        ctx = serve.Context(cfg=self.ro, human="human:owner")
        self.start(ctx, self.read, hub=self.hub, **kwargs)
        if self.hub is not None and listen:
            deadline = time.monotonic() + 10
            while not self.hub.listening and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.hub.listening)

    def open_stream(self, headers=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("GET", serve.EVENTS_PATH, skip_host=True)
        conn.putheader("Host", "127.0.0.1:%d" % self.port)
        if auth:
            conn.putheader("Authorization", "Bearer %s" % TOKEN)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        self.addCleanup(conn.close)
        return resp

    def read_messages(self, resp, count, timeout=8.0, kind=None):
        """`count` messages (of `kind` only, if given): dicts of their fields."""
        found, buf = [], b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                fields = {}
                for line in block.decode("utf-8").split("\n"):
                    name, _, value = line.partition(":")
                    fields[name or "comment"] = value[1:] if value.startswith(" ") else value
                if kind is None or fields.get("event") == kind:
                    found.append(fields)
                if len(found) >= count:
                    return found
            buf += resp.fp.read1(65536)
        self.fail("%d message(s) expected, got %r" % (count, found))

    def test_live_events_then_resume(self):
        self.serve()
        resp = self.open_stream()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        lot = work.add(self.db, title="lot", app="demo")
        work.move(self.db, lot["id"], "build")
        got = self.read_messages(resp, 2, kind="lot.updated")
        data = [json.loads(m["data"]) for m in got]
        self.assertEqual([d["data"]["state"] for d in data], ["intake", "build"])
        self.assertEqual(data[0]["object"], "lot:%d" % lot["id"])
        # codex P1 r1: each message carries ITS position, never a later one
        self.assertNotEqual(got[0]["id"], got[1]["id"])
        cluster = storage.of(self.db).events.bounds()["cluster"]
        self.assertTrue(got[0]["id"].endswith("-%d-%s" % (data[0]["id"], cluster)))
        # resume after the first: exactly the second, then nothing again
        resp2 = self.open_stream({"Last-Event-ID": got[0]["id"]})
        again = self.read_messages(resp2, 1, kind="lot.updated")
        self.assertEqual(json.loads(again[0]["data"])["id"], data[1]["id"])
        self.assertEqual(again[0]["id"], got[1]["id"])

    def test_an_event_held_by_an_open_transaction_comes_once_it_ends(self):
        self.serve()
        resp = self.open_stream()
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with other.transaction() as tx_a:
            storage.of(tx_a).events.record("alert.raised", "alert:first", None, {})
            events.record(self.db, "alert.raised", "alert:second")
            time.sleep(0.5)
        got = self.read_messages(resp, 2, kind="alert.raised")
        self.assertEqual([json.loads(m["data"])["object"] for m in got],
                         ["alert:first", "alert:second"])

    def test_delayed_is_observable_while_a_transaction_holds_events(self):
        with mock.patch.object(serve, "HEARTBEAT_S", 0.5):
            self.serve()
            resp = self.open_stream()
            other = db_mod.connect(self.cfg)
            self.addCleanup(other.close)
            with other.transaction() as tx_a:
                storage.of(tx_a).events.record("alert.raised", "alert:held", None, {})
                events.record(self.db, "alert.raised", "alert:behind")
                got = self.read_messages(resp, 1, kind="delayed", timeout=5)
                self.assertNotIn("id", got[0])
                self.assertEqual(json.loads(got[0]["data"])["reason"],
                                 "open transaction (xmin)")
            got = self.read_messages(resp, 2, kind="alert.raised")
            self.assertEqual([json.loads(m["data"])["object"] for m in got],
                             ["alert:held", "alert:behind"])

    def test_restore_warning_is_logged_at_once(self):
        """Codex P3 r6: rows above the counter hold the stream for long; the
        warning comes at the first read, not after a heartbeat of silence,
        and names both tables to empty."""
        self.addCleanup(self.db.execute, "TRUNCATE ameesh_events, ameesh_events_pruned")
        with mock.patch.object(serve, "HEARTBEAT_S", 60.0), \
                self.assertLogs("ameesh.serve", "WARNING") as logs:
            self.serve()
            resp = self.open_stream()
            self.read_messages(resp, 1, timeout=5)              # retry
            self.db.execute(
                "INSERT INTO ameesh_events (type, object, tx) VALUES ('alert.raised',"
                " 'alert:r', ((pg_current_xact_id()::text::bigint + 1000000)::text)::xid8)")
            events.record(self.db, "alert.raised", "alert:wake")
            deadline = time.monotonic() + 5
            while not logs.records and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("TRUNCATE ameesh_events, ameesh_events_pruned", logs.output[0])

    def test_without_last_event_id_only_new_events(self):
        events.record(self.db, "alert.raised", "alert:before")
        self.serve()
        resp = self.open_stream()
        events.record(self.db, "alert.raised", "alert:after")
        got = self.read_messages(resp, 1, kind="alert.raised")
        self.assertEqual(json.loads(got[0]["data"])["object"], "alert:after")

    def test_polling_without_listen(self):
        self.serve(listen=False)
        resp = self.open_stream()
        events.record(self.db, "alert.raised", "alert:polled")
        got = self.read_messages(resp, 1, kind="alert.raised")
        self.assertEqual(json.loads(got[0]["data"])["object"], "alert:polled")

    def test_hub_survives_an_untranslated_listen_error(self):
        """R2 auditeur P2-3: an error the driver did not translate (a hot
        standby refusing LISTEN, a cut between the connection and LISTEN)
        must not kill the hub thread: it falls back to polling, and the
        streams keep receiving."""
        events.record(self.db, "alert.raised", "alert:before-hub")
        hub = serve.Hub(self.ro, self.read, poll_s=0.1, listen=True)
        with mock.patch.object(db_mod, "listener", side_effect=RuntimeError("refused")):
            hub.start()
            self.addCleanup(hub.stop)
            deadline = time.monotonic() + 5
            while hub.generation < 2 and time.monotonic() < deadline:
                time.sleep(0.05)          # the refusal's wake, then a poll
            self.assertTrue(hub._thread.is_alive())
            self.assertFalse(hub.listening)
            before = hub.generation
            events.record(self.db, "alert.raised", "alert:after-hub")
            deadline = time.monotonic() + 5
            while hub.generation == before and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertGreater(hub.generation, before)

    def test_reset_when_events_were_lost(self):
        self.serve()
        events.record(self.db, "alert.raised", "alert:x")
        bounds = storage.of(self.db).events.bounds()
        top = bounds["top"]
        cluster = bounds["cluster"]
        resp = self.open_stream({"Last-Event-ID": events.format_position(
            (top[0] + 1000, top[1]), cluster)})
        got = self.read_messages(resp, 1, kind="reset")
        self.assertEqual(json.loads(got[0]["data"])["reason"], "unknown position")
        self.assertEqual(got[0]["id"], events.format_position(bounds["head"], cluster))
        # a position below the retention's watermark: events may be lost
        before = bounds["head"]
        self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                        " VALUES ('alert.resolved', 'alert:gone', now() - interval '400 days')")
        events.record(self.db, "alert.raised", "alert:kept")
        self.assertGreaterEqual(events.prune(self.db, days=365), 1)
        pruned = storage.of(self.db).events.bounds()["pruned"]
        self.assertGreater(pruned, before)
        resp = self.open_stream({"Last-Event-ID": events.format_position(before, cluster)})
        got = self.read_messages(resp, 1, kind="reset")
        self.assertEqual(json.loads(got[0]["data"])["reason"], "events lost (retention)")
        # at the watermark: nothing lost, no reset, the kept event follows
        resp = self.open_stream({"Last-Event-ID": events.format_position(pruned, cluster)})
        got = self.read_messages(resp, 1, kind="alert.raised")
        self.assertEqual(json.loads(got[0]["data"])["object"], "alert:kept")

    def test_reset_for_a_position_of_another_cluster(self):
        """Codex P2 r3: after a logical restore, `tx` starts again lower. A
        position 70001-12 of the old cluster passes the bounds of the new
        one, and an event at (1000, 13) sorts below it: lost for good.
        The cluster in the position makes it a reset."""
        self.serve()
        events.record(self.db, "alert.raised", "alert:old")
        bounds = storage.of(self.db).events.bounds()
        self.assertRegex(bounds["cluster"], "^[0-9a-f]{1,16}t[0-9a-f]{8}$")
        sysid, timeline = bounds["cluster"].split("t")
        head = bounds["head"]
        # another cluster; then the same cluster on another timeline (codex
        # P2 r5: a point-in-time recovery keeps the system identifier)
        for other in ("0t" + timeline if sysid != "0" else "1t" + timeline,
                      "%st%08x" % (sysid, int(timeline, 16) + 1)):
            resp = self.open_stream({"Last-Event-ID": events.format_position(
                (head[0] - 1, head[1] - 1), other)})          # within the bounds
            got = self.read_messages(resp, 1, kind="reset")
            self.assertEqual(json.loads(got[0]["data"])["reason"],
                             "another cluster or timeline", other)
            self.assertEqual(got[0]["id"], events.format_position(head, bounds["cluster"]))
        events.record(self.db, "alert.raised", "alert:new")
        got = self.read_messages(resp, 1, kind="alert.raised")
        self.assertEqual(json.loads(got[0]["data"])["object"], "alert:new")

    def test_an_unknown_cluster_resets_every_resume(self):
        """The identity functions refused to the role: no resume trusted."""
        bounds = storage.of(self.db).events.bounds
        real = type(storage.of(self.db).events).bounds

        def refused(store):
            return dict(real(store), cluster=None)
        with mock.patch.object(type(storage.of(self.db).events), "bounds", refused):
            self.serve()
            head = bounds()["head"]
            resp = self.open_stream({"Last-Event-ID": events.format_position(
                head, events.UNKNOWN_CLUSTER)})
            got = self.read_messages(resp, 1, kind="reset")
            self.assertEqual(json.loads(got[0]["data"])["reason"],
                             "another cluster or timeline")
            self.assertTrue(got[0]["id"].endswith("-none"))

    def test_heartbeat(self):
        with mock.patch.object(serve, "HEARTBEAT_S", 0.3):
            self.serve()
            resp = self.open_stream()
            got = self.read_messages(resp, 1, timeout=5)
            self.assertIn("retry", got[0])
            got = self.read_messages(resp, 1, timeout=5)
            self.assertEqual(got[0], {"comment": "heartbeat"})

    def test_refusals(self):
        self.serve(max_streams=1)
        self.assertEqual(self.open_stream(auth=False).status, 401)
        for value in ("abc", "70001-12"):     # the cluster is required
            bad = self.open_stream({"Last-Event-ID": value})
            self.assertEqual(bad.status, 400, value)
            bad.read()
        first = self.open_stream()
        self.assertEqual(first.status, 200)
        second = self.open_stream()
        self.assertEqual(second.status, 503)
        self.assertEqual(second.getheader("Retry-After"), "10")
        self.assertEqual(json.loads(second.read())["error"], "too_many_streams")
        # the other routes stay reachable
        self.assertEqual(self.get_json("/api/v1/me")[0], 200)

    def test_without_hub_503(self):
        self.serve(hub=False)
        resp = self.open_stream()
        self.assertEqual(resp.status, 503)

    def test_hub_stops_with_the_server(self):
        self.serve()
        resp = self.open_stream()
        self.read_messages(resp, 1, timeout=5)   # retry:
        self._stop()
        self.assertTrue(self.hub.stopped)


if __name__ == "__main__":
    unittest.main()
