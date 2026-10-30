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
import uuid
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
        # #928/F5: the identity carries the journal's own random identifier
        journal = "6f3at00000001j0123456789abcdef"
        self.assertEqual(events.format_position((70001, 12), journal),
                         "70001-12-" + journal)
        self.assertEqual(events.parse_position("70001-12-" + journal),
                         ((70001, 12), journal))
        self.assertEqual(events.parse_position("1-2-none"), ((1, 2), events.UNKNOWN_CLUSTER))
        for bad in ("12", "a-1", "1-", "-1", "70001-12", "1-2-x", "1-2-3-4", "1-2-",
                    "1-2-6f3a", "1-2-6f3at1", "1-2-%st00000001" % ("f" * 17), "",
                    "1-2-6f3at00000001j0123",                 # journal too short
                    "1-2-6f3at00000001j0123456789abcdeg",     # not hexadecimal
                    "1-2-6f3at00000001x0123456789abcdef",     # wrong separator
                    "1-2-6f3at00000001j0123456789abcdef0"):   # journal too long
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

    def test_the_prune_runs_on_its_own_connection(self):
        """#928/F6: the retention's transaction (up to PRUNE_MAX_S, with lock
        and statement timeouts) must not hold the runner's shared connection,
        on whose transaction lock the lease renewals wait."""
        own, seen = mock.Mock(), []
        fake = self.fake(cfg=object())
        with mock.patch.object(runner.db_mod, "open_db", return_value=own) as opened, \
                mock.patch.object(events, "prune",
                                  side_effect=lambda db, **kw: seen.append(db) or 1), \
                mock.patch.object(runner, "log_async"):
            self.assertEqual(runner.Runner.prune_events_once(fake, now=1.0), 1)
        opened.assert_called_once_with(fake.cfg)
        self.assertEqual(seen, [own])          # never the runner's connection
        own.close.assert_called_once()

    def test_a_prune_connection_that_cannot_open_is_said_once(self):
        fake = self.fake(cfg=object())
        with mock.patch.object(runner.db_mod, "open_db",
                               side_effect=db_mod.Unavailable("down")), \
                mock.patch.object(events, "prune") as prune, \
                mock.patch.object(runner, "log_async") as said:
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=1.0))
            self.assertIsNone(runner.Runner.prune_events_once(fake, now=1.0 + 3600))
        prune.assert_not_called()
        self.assertEqual(said.call_count, 1)

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

    def position_after_existing(self):
        """Position after every row already present (#3868).

        `bounds()["head"]` is the last *deliverable* row: on the shared bench,
        a foreign transaction holds the cluster-wide xmin below rows this
        schema already committed, so the head lags. When that transaction
        ends, those rows surface after the position and the test reads a
        stranger's tail (`alert:pc` before `alert:b`). The schema is ours
        alone; `top` is the last existing row, so only the events created
        after it are read.
        """
        return storage.of(self.db).events.bounds()["top"]

    def delivered(self, store, position, expected, limit=100, timeout=10.0):
        """`readable()` until `expected` (objects, in order) is delivered.

        `pg_current_snapshot()`'s xmin is cluster-wide: on the shared bench,
        another agent's suite holding an xid below our events defers them —
        correct behaviour, announced as "xmin", never a skip. Wait (bounded)
        for that foreign transaction to end; past the bound, keep only what
        holds under it: a delivered prefix, in order, and the announced wait.
        """
        deadline = time.monotonic() + timeout
        unheld = 0
        while True:
            rows, waiting, _pruned = store.readable(position, limit)
            got = [r["object"] for r in rows]
            if waiting is None:
                self.assertEqual(got, expected)          # no skip, no loss
                return rows
            self.assertEqual(waiting, "xmin")
            self.assertEqual(got, expected[:len(got)])   # a prefix, in order
            if time.monotonic() >= deadline:
                # The prefix is accepted only while a transaction REALLY
                # holds the next event (cluster xmin at or below its tx): a
                # reader that keeps announcing "xmin" over nothing is stuck,
                # and that is the regression these tests exist for (R2 dev1).
                last = (int(rows[-1]["tx"]), int(rows[-1]["id"])) if rows else position
                held = self.db.query(
                    "select coalesce(pg_snapshot_xmin(pg_current_snapshot()) <= min(tx),"
                    " false)::int as held from ameesh_events"
                    " where (tx, id) > (%s::text::xid8, %s)",
                    (str(int(last[0])), int(last[1])))[0]["held"]
                if int(held):
                    return rows
                unheld += 1                              # the holder may just have ended
                self.assertLess(unheld, 3, "readable() announces xmin, nothing holds it")
            time.sleep(0.1)

    def test_lot_events_carry_the_project(self):
        start = self.last()
        # footprint=[] : sans analyse d'impact, dont la ligne de journal (#4818)
        # serait un événement de plus
        lot = work.add(self.db, title="lot", app="demo", footprint=[])
        work.move(self.db, lot["id"], "build")
        rows = _rows(self.db, start)
        self.assertEqual(_types(rows), [("lot.updated", "lot:%d" % lot["id"])] * 2)
        self.assertEqual([r["data"]["state"] for r in rows], ["intake", "build"])
        self.assertEqual({r["project"] for r in rows}, {"demo"})

    def test_recent_type_filters_the_window_and_the_object(self):
        """#3981 : la preuve partagee du staging se relit par type et objet,
        la plus recente d'abord, sans index dedie (borne `created_at`)."""
        store = storage.of(self.db).events
        first = store.record("staging.attested", "staging:first", "ameesh",
                             {"sha": "a" * 40})
        second = store.record("staging.attested", "staging:second", "ameesh",
                              {"sha": "b" * 40})
        store.record("lot.updated", "lot:1", "ameesh", {})
        rows = [row for row in store.recent_type("staging.attested", 1, 10,
                                                 object="staging:second")
                if row["id"] >= first]
        self.assertEqual([row["object"] for row in rows], ["staging:second"])
        self.assertEqual(rows[0]["data"]["sha"], "b" * 40)
        self.assertGreater(second, first)
        self.assertEqual(store.recent_type("staging.attested", 0, 10), [])

    def test_staging_attestation_round_trips_to_release_cutoff(self):
        """#3981 : la preuve compacte deposee par actif.py est exactement ce
        que release_cutoff relit sur le poste, sans ssh."""
        from ameesh import release_cutoff
        sha = "c" * 40
        data = {"schema": "ameesh-staging-actif/1", "sha": sha,
                "sha_court": sha[:9], "date": "2026-10-27T14:00:00Z",
                "hotes": {hote: {"actif": sha, "etapes": {"sante": "ok"}}
                          for hote in release_cutoff.STAGING_VMS},
                "reportes": {release_cutoff.STAGING_POSTE: {"raison": "poste absent"}},
                "lots": [3981], "rapporteurs": ["mesh-design"]}
        storage.of(self.db).events.record(
            "staging.attested", "staging:%s" % sha, "ameesh", data)
        self.assertEqual(release_cutoff.stored_attestations(self.db, sha), [data])
        self.assertIn(data, release_cutoff.stored_attestations(self.db))

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

    def test_retention_follows_the_database_clock(self):
        """#928/F6: the cutoff is the base's `now()`, the clock of the table's
        own guard, not the host's. A host 400 days ahead used to aim the
        whole deletion at recent events: the guard refused it and the
        retention failed in full, on one runner out of two."""
        self._empty()
        self._old_event("alert:pa")
        self._old_event("alert:pr", days=0)
        ahead = time.time() + 400 * 86400.0
        with mock.patch.object(events.time, "time", return_value=ahead):
            self.assertEqual(events.prune(self.db, days=365), 1)
        self.assertEqual(self._kept(), ["alert:pr"])
        self.assertEqual(events.expired(self.db, days=365), 0)

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

    def test_two_overlapping_prunes_both_progress(self):
        """#1711 (Codex N1 of #928): two pruners chose the same first batch;
        the second waited on the first's rows, then found them gone and
        deleted nothing — the runner said "expired event(s) left, none
        deleted" while the other progressed. Locked when chosen, the batch
        of the second skips the deleted rows and takes the next ones."""
        self._empty()
        for name in ("alert:pa", "alert:pb"):
            self._old_event(name)
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        probe = db_mod.connect(self.cfg)
        self.addCleanup(probe.close)
        result = {}

        def second():
            try:
                result["n"] = storage.of(self.db).events.prune(365, 1, 10.0)
            except Exception as exc:  # noqa: BLE001 - reported below
                result["error"] = exc

        with other.transaction() as tx:
            # the first pruner, its batch deleted and not yet committed
            tx.execute("delete from ameesh_events where object = 'alert:pa'")
            # the waiter is whoever this backend blocks (the psql driver
            # opens a backend per statement: its pid is not known ahead)
            first = int(tx.query("select pg_backend_pid() as p")[0]["p"])
            worker = threading.Thread(target=second, daemon=True)
            worker.start()
            deadline = time.monotonic() + 30.0
            while not probe.query("select 1 from pg_stat_activity"
                                  " where %s = any(pg_blocking_pids(pid))", (first,)):
                self.assertLess(time.monotonic(), deadline, "the second pruner never waited")
                time.sleep(0.05)
        worker.join(15.0)
        self.assertNotIn("error", result)
        self.assertEqual(result.get("n"), 1)
        self.assertEqual(self._kept(), [])

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
        position = self.position_after_existing()
        self.addCleanup(self._empty)
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, tx) VALUES ('alert.raised', 'alert:r',"
            " ((pg_current_xact_id()::text::bigint + 1000000)::text)::xid8)")
        self.assertEqual(store.readable(position, 100)[:2], ([], "restore"))

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
        position = self.position_after_existing()
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with other.transaction() as tx_a:
            a = storage.of(tx_a).events.record("alert.raised", "alert:a", None, {})
            b = events.record(self.db, "alert.raised", "alert:b")
            self.assertGreater(b, a)
            rows, waiting, _pruned = store.readable(position, 100)   # held back by A
            # A holds its own event back; a foreign transaction of the shared
            # bench (xmin) can only shorten that prefix, never skip 11 for 12.
            self.assertNotIn("alert:b", [r["object"] for r in rows])
            self.assertEqual(waiting, "xmin")
        self.delivered(store, position, ["alert:a", "alert:b"])
        b_tx = int(self.db.query("select tx::text as tx from ameesh_events where id = %s",
                                 (b,))[0]["tx"])
        after = (b_tx, b)
        self.assertFalse(store.readable(after, 100)[1])
        # a rolled-back transaction leaves nothing to wait for
        try:
            with other.transaction() as tx_c:
                storage.of(tx_c).events.record("alert.raised", "alert:c", None, {})
                raise KeyError("rollback")
        except KeyError:
            pass
        events.record(self.db, "alert.raised", "alert:d")
        self.delivered(store, after, ["alert:d"])

    def test_a_foreign_transaction_does_not_move_the_witness_position(self):
        """#3868: h-33 CI — another process of the part (`--parallel`, #3754)
        holds a transaction older than a row of this schema: `head`
        (deliverable) lags behind `top` (existing), and a witness starting at
        the head would read that row again once the foreign transaction ends."""
        store = storage.of(self.db).events
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with other.transaction() as tx:
            tx.query("select pg_current_xact_id()")     # the foreign xmin
            events.record(self.db, "alert.raised", "alert:held")
            bounds = store.bounds()
            self.assertNotEqual(bounds["head"], bounds["top"])
            self.assertEqual(self.position_after_existing(), bounds["top"])
            # nothing after the position: no event waits for the foreign xmin
            self.assertEqual(store.readable(self.position_after_existing(), 100)[:2],
                             ([], None))

    def test_readable_and_waiting_share_one_snapshot(self):
        """Codex P2 r2: A holds B back, then ends WITHOUT writing an event
        between two reads on two snapshots: `[]` then `waiting` False, and B
        undelivered with no notification to come. One statement, one snapshot."""
        store = storage.of(self.db).events
        position = self.position_after_existing()
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with other.transaction() as tx_a:
            tx_a.query("select pg_current_xact_id()")      # A: an xid, no event
            events.record(self.db, "alert.raised", "alert:b")
            with mock.patch.object(self.db, "query", wraps=self.db.query) as query:
                self.assertEqual(store.readable(position, 100)[:2], ([], "xmin"))
            self.assertEqual(query.call_count, 1)
        self.delivered(store, position, ["alert:b"])         # A over, no event of its own

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
        rows, waiting, _pruned = store.readable((0, 0), 10)
        self.assertEqual([r["object"] for r in rows],
                         ["alert:nine", "alert:ten", "alert:eleven"])
        self.assertIsNone(waiting)
        # the cursor follows the same numeric order, batch after batch
        first = store.readable((0, 0), 1)[0][0]
        following, _, _ = store.readable((int(first["tx"]), int(first["id"])), 1)
        self.assertEqual([r["object"] for r in following], ["alert:ten"])

    def notification(self, listener, payload, timeout=5.0):
        """The notification of `events.CHANNEL` carrying `payload`, patiently.

        `ameesh_events` is database-wide: on the shared bench another schema
        (or any other writer) notifies its own ids on the same channel
        (#4600). The payload is the id of the row this insert made, so any
        other message is a stranger's wake-up: ignore it and wait for ours,
        up to the bound, then fail cleanly.
        """
        deadline = time.monotonic() + timeout
        while True:
            got = listener.wait(max(0.0, min(0.5, deadline - time.monotonic())))
            if got == {"channel": events.CHANNEL, "payload": payload}:
                return got
            if got and got.get("event") == "down":
                self.fail("event listener went down while waiting for %r: %r"
                          % (payload, got))
            if time.monotonic() >= deadline:
                self.fail("no %s notification carrying %r within %gs (last: %r)"
                          % (events.CHANNEL, payload, timeout, got))

    def test_notify_carries_the_id_alone(self):
        listener = db_mod.listener(self.db, [events.CHANNEL])
        if listener is None:
            self.skipTest("driver without LISTEN")
        self.addCleanup(listener.close)
        new = events.record(self.db, "alert.raised", "alert:x", agent="nobody")
        got = self.notification(listener, str(new))
        self.assertEqual(got, {"channel": events.CHANNEL, "payload": str(new)})

    def test_a_foreign_notification_on_the_channel_is_ignored(self):
        """#4600: witness. The channel is database-wide, so a notification of
        another schema (or any other writer) can be delivered before ours; it
        must neither fail the wait nor stand in for the witness of this
        insert."""
        listener = db_mod.listener(self.db, [events.CHANNEL])
        if listener is None:
            self.skipTest("driver without LISTEN")
        self.addCleanup(listener.close)
        foreign = "foreign-%s" % uuid.uuid4().hex
        self.db.execute("SELECT pg_notify(%s, %s)", (events.CHANNEL, foreign))
        self.assertEqual(self.notification(listener, foreign),
                         {"channel": events.CHANNEL, "payload": foreign})
        new = events.record(self.db, "alert.raised", "alert:x", agent="nobody")
        self.assertEqual(self.notification(listener, str(new)),
                         {"channel": events.CHANNEL, "payload": str(new)})

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
            deadline = time.monotonic() + 30
            while not self.hub.listening and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.hub.listening)

    def open_stream(self, headers=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
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

    def read_messages(self, resp, count, timeout=30.0, kind=None):
        """`count` messages (of `kind` only, if given): dicts of their fields.

        #3832 : le témoin est l'arrivée du message, pas 8 s de mur ; sous
        --parallel, le serveur et le client peuvent attendre plusieurs
        secondes avant d'être ordonnancés (h-32 : aucun message en 8 s).
        """
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

    def wait_deliverable(self, timeout=60.0):
        """Wait (bounded) until every row of this schema is deliverable.

        #3868: another process of the part (`--parallel`, #3754) can hold a
        transaction older than this schema's rows: `head` then lags behind
        `top`, and a stream opened now starts before them and replays them
        legitimately once the foreign transaction ends. Wait for its end, or
        skip: what it holds is not this test's subject (#3832).
        """
        deadline = time.monotonic() + timeout
        while True:
            bounds = storage.of(self.db).events.bounds()
            if bounds["head"] == bounds["top"]:
                return bounds
            if time.monotonic() >= deadline:
                self.skipTest("a foreign transaction holds this schema's rows (xmin)")
            time.sleep(0.1)

    def assert_reset_at_head(self, frame, at_least, cluster):
        """#928: a reset frame names THIS journal and a position at or after
        `at_least`. The exact head at the instant of the reset cannot be
        compared to a snapshot taken by the test: an event held back by a
        foreign transaction of the shared bench (`xmin`) becomes readable
        when it ends, and the head only ever advances."""
        parsed = events.parse_position(frame["id"])
        self.assertIsNotNone(parsed, frame["id"])
        self.assertEqual(parsed[1], cluster)
        self.assertGreaterEqual(parsed[0], at_least)
        bounds = storage.of(self.db).events.bounds()
        self.assertLessEqual(parsed[0], max(bounds["top"], bounds["pruned"]))

    def test_live_events_then_resume(self):
        self.serve()
        resp = self.open_stream()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        # footprint=[] : sans analyse d'impact, dont la ligne de journal (#4818)
        # serait un événement de plus
        lot = work.add(self.db, title="lot", app="demo", footprint=[])
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
        before = events.record(self.db, "alert.raised", "alert:before")
        self.serve()
        # #928/#3868: a foreign transaction of the shared bench (xmin) can
        # hold the record back for a moment; opening the stream before it is
        # readable would replay it, legitimately, as a new event (never
        # skip). Wait for its end, bounded, before opening.
        bounds = self.wait_deliverable()
        self.assertEqual(bounds["head"][1], before)
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
        self.assert_reset_at_head(got[0], bounds["head"], cluster)
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

    def test_reset_after_pruning_the_whole_log_has_a_stable_cursor(self):
        self.db.execute("TRUNCATE ameesh_events, ameesh_events_pruned")
        self.db.execute("INSERT INTO ameesh_events (type, object, created_at)"
                        " VALUES ('alert.resolved', 'alert:gone',"
                        " now() - interval '400 days')")
        events.prune(self.db, days=365)
        bounds = storage.of(self.db).events.bounds()
        self.serve()
        resp = self.open_stream({"Last-Event-ID": events.format_position(
            (0, 0), bounds["cluster"])})
        reset = self.read_messages(resp, 1, kind="reset")[0]
        self.assertGreaterEqual(events.parse_position(reset["id"])[0], bounds["pruned"])

    def test_a_prune_during_the_stream_resets_the_open_reader(self):
        """#928/F6: the watermark was checked at connection only. A stream
        that was open when the retention passed its cursor kept reading,
        silently skipping the deleted events; it now gets the same `reset`
        frame as a resume below the watermark."""
        self.serve()
        resp = self.open_stream()
        self.read_messages(resp, 1, timeout=5)              # retry:
        before = storage.of(self.db).events.bounds()["head"]
        # the old event and its pruning commit together: the stream is woken
        # once, when the watermark already sits above its cursor
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO ameesh_events (type, object, created_at)"
                       " VALUES ('alert.resolved', 'alert:gone',"
                       " now() - interval '400 days')")
            self.assertGreaterEqual(events.prune(tx, days=365), 1)
        got = self.read_messages(resp, 1, kind="reset", timeout=5)
        self.assertEqual(json.loads(got[0]["data"])["reason"], "events lost (retention)")
        current = storage.of(self.db).events.bounds()
        self.assert_reset_at_head(got[0], before, current["cluster"])

    def test_reset_for_a_position_of_another_cluster_or_journal(self):
        """Codex P2 r3: after a logical restore, `tx` starts again lower. A
        position 70001-12 of the old cluster passes the bounds of the new
        one, and an event at (1000, 13) sorts below it: lost for good.
        The identity in the position makes it a reset.

        #928/F5 (witness W5): two meshes of the SAME cluster — two schemas,
        two bases — share the system identifier and the timeline but not
        their log; the journal's own identifier makes the other mesh's
        position a reset instead of a silent skip."""
        self.serve()
        events.record(self.db, "alert.raised", "alert:old")
        # #3868: a foreign transaction of the part (xmin) holds this schema's
        # rows back, so `head` lags behind `top`: the stream would reset
        # below `alert:old` and replay it after the reset. Wait, bounded, for
        # the head to reach our own top, then the stream starts after it.
        bounds = self.wait_deliverable()
        self.assertEqual(bounds["head"], bounds["top"])
        self.assertRegex(bounds["cluster"], "^[0-9a-f]{1,16}t[0-9a-f]{8}j[0-9a-f]{16}$")
        sysid, rest = bounds["cluster"].split("t")
        timeline, journal = rest.split("j")
        head = bounds["head"]
        # another cluster; the same cluster on another timeline (codex P2 r5:
        # a point-in-time recovery keeps the system identifier); and the same
        # cluster and timeline under another journal (another mesh of the base)
        for other in ("0t" + rest if sysid != "0" else "1t" + rest,
                      "%st%08xj%s" % (sysid, int(timeline, 16) + 1, journal),
                      "%st%sj%016x" % (sysid, timeline, int(journal, 16) + 1)):
            resp = self.open_stream({"Last-Event-ID": events.format_position(
                (head[0] - 1, max(0, head[1] - 1)), other)})   # within the bounds
            got = self.read_messages(resp, 1, kind="reset")
            self.assertEqual(json.loads(got[0]["data"])["reason"],
                             "another cluster or timeline", other)
            self.assert_reset_at_head(got[0], head, bounds["cluster"])
        events.record(self.db, "alert.raised", "alert:new")
        got = self.read_messages(resp, 1, kind="alert.raised")
        self.assertEqual(json.loads(got[0]["data"])["object"], "alert:new")

    def test_the_identity_carries_the_journal_of_this_log(self):
        """#928/F5: the identity names the journal itself (migration 0448),
        not only the cluster and timeline."""
        journal = self.db.query("select journal from ameesh_events_journal")[0]["journal"]
        self.assertRegex(journal, "^[0-9a-f]{16}$")
        self.assertTrue(storage.of(self.db).events.bounds()["cluster"].endswith(
            "j" + journal))
        # two journals of the same cluster draw different identifiers
        other = "t_journal_%s" % uuid.uuid4().hex[:8]
        self.db.execute('create schema "%s"' % other)
        self.addCleanup(self.db.execute, 'drop schema "%s" cascade' % other)
        self.db.execute('create table "%s".ameesh_events_journal'
                        " (like ameesh_events_journal including all)" % other)
        self.db.execute('insert into "%s".ameesh_events_journal (one) values (true)' % other)
        self.assertNotEqual(
            journal,
            self.db.query('select journal from "%s".ameesh_events_journal' % other)[0]["journal"])
        # and 0448 re-defines the identity function without 0396 being torn
        # down: the journal row is a singleton, never duplicated
        self.assertEqual(
            self.db.query("select count(*) as n from ameesh_events_journal")[0]["n"], 1)

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
