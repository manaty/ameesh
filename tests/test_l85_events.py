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
        self.assertEqual(events.format_position((70001, 12)), "70001-12")
        self.assertEqual(events.parse_position(" 70001-12 "), (70001, 12))
        for bad in ("12", "a-1", "1-", "-1", "1-2-3", ""):
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

    def test_append_only_and_retention(self):
        old = storage.of(self.db).events.record("alert.raised", "alert:x", None, {})
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE ameesh_events SET type = 'x.y' WHERE id = %s", (old,))
        with self.assertRaises(db_mod.DbError):
            self.db.execute("DELETE FROM ameesh_events WHERE id = %s", (old,))
        ancient = self.db.query(
            "INSERT INTO ameesh_events (type, object, created_at)"
            " VALUES ('alert.resolved', 'alert:x', now() - interval '400 days') RETURNING id")
        recent = self.db.query(
            "INSERT INTO ameesh_events (type, object, created_at)"
            " VALUES ('alert.resolved', 'alert:x', now() - interval '100 days') RETURNING id")
        self.assertEqual(events.prune(self.db, days=365), 1)
        ids = {r["id"] for r in _rows(self.db)}
        self.assertNotIn(ancient[0]["id"], ids)
        self.assertIn(recent[0]["id"], ids)
        self.assertEqual(events.prune(self.db, days=90, batch=1), 1)
        with self.assertRaises(ValueError):
            events.prune(self.db, days=10)

    def test_prune_is_bounded(self):
        for _ in range(5):
            self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                            " VALUES ('alert.resolved', 'alert:b', now() - interval '400 days')")
        self.assertEqual(events.prune(self.db, days=365, batch=2, max_batches=2), 4)
        ticks = iter([0.0, 10.0])
        self.assertEqual(events.prune(self.db, days=365, batch=1, max_s=5.0,
                                      clock=lambda: next(ticks)), 1)
        self.assertEqual(events.prune(self.db, days=365), 0)

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
            self.assertEqual(store.readable(position, 100), [])   # held back by A
            self.assertTrue(store.waiting(position))
        rows = store.readable(position, 100)
        self.assertEqual([r["object"] for r in rows], ["alert:a", "alert:b"])
        self.assertFalse(store.waiting((int(rows[-1]["tx"]), int(rows[-1]["id"]))))
        # a rolled-back transaction leaves nothing to wait for
        after = (int(rows[-1]["tx"]), int(rows[-1]["id"]))
        try:
            with other.transaction() as tx_c:
                storage.of(tx_c).events.record("alert.raised", "alert:c", None, {})
                raise KeyError("rollback")
        except KeyError:
            pass
        events.record(self.db, "alert.raised", "alert:d")
        self.assertEqual([r["object"] for r in store.readable(after, 100)], ["alert:d"])

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
        self.assertTrue(got[0]["id"].endswith("-%d" % data[0]["id"]))
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

    def test_reset_when_events_were_lost(self):
        self.serve()
        events.record(self.db, "alert.raised", "alert:x")
        bounds = storage.of(self.db).events.bounds()
        top = bounds["top"]
        resp = self.open_stream({"Last-Event-ID": "%d-%d" % (top[0] + 1000, top[1])})
        got = self.read_messages(resp, 1, kind="reset")
        self.assertEqual(json.loads(got[0]["data"])["reason"], "unknown position")
        self.assertEqual(got[0]["id"], events.format_position(bounds["head"]))
        # a position older than the oldest kept event: lost to the retention
        oldest = bounds["oldest"]
        resp = self.open_stream({"Last-Event-ID": "%d-%d" % (oldest[0], oldest[1] - 1)})
        got = self.read_messages(resp, 1, kind="reset")
        self.assertEqual(json.loads(got[0]["data"])["reason"], "events lost (retention)")

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
        bad = self.open_stream({"Last-Event-ID": "abc"})
        self.assertEqual(bad.status, 400)
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
