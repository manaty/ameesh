# SPDX-License-Identifier: AGPL-3.0-only
"""#5045: deadline alerts without lifting a coordination window; sourced end."""
from __future__ import annotations

import json
import time
from unittest import mock

from ameesh import events, mail, packet_pause, storage
from .support import PgTestCase
from . import test_4361_paquet_pause as bench

NOW, ph = bench.NOW, bench.ph


class AnnouncementTest(bench.Base):
    def test_deadline_does_not_lift_and_sourced_announcement_does(self):
        self.pause(await_announcement=True)
        data = packet_pause.active(object(), project="ameesh", target="main", now=NOW + 3600)
        self.assertIsNotNone(data)
        with self.assertRaises(packet_pause.PauseError):
            packet_pause.resume(object(), project="ameesh", target="main", by="coordinator")
        # Neither a forgotten deadline nor an ordinary replacement bypasses it.
        with self.assertRaises(packet_pause.PauseError):
            self.pause()
        packet_pause.resume(object(), project="ameesh", target="main", by="coordinator",
                            source="coordinator, 2026-10-31, message #173000")
        self.assertIsNone(packet_pause.active(object(), project="ameesh", target="main",
                                              now=NOW + 3600))
        self.assertIn("#173000", self.events.rows[-1]["data"]["source"])

    def test_announcement_source_is_bounded(self):
        self.pause(await_announcement=True)
        with self.assertRaises(packet_pause.PauseError):
            packet_pause.resume(object(), project="ameesh", target="main", by="pilot",
                                source="x" * (packet_pause.REASON_MAX + 1))
        self.assertEqual(self.events.rows[-1]["data"]["state"], "paused")

    def test_announcement_mode_still_requires_a_finite_deadline(self):
        for until in (None, float("nan"), float("inf"), NOW):
            with self.subTest(until=until), self.assertRaises(packet_pause.PauseError):
                self.pause(await_announcement=True, until=until)
        self.assertEqual(self.events.rows, [])

    def test_alert_only_at_deadline_and_same_keys_after_lease_move(self):
        self.pause(await_announcement=True)
        data = packet_pause.latest(object(), project="ameesh", target="main")
        with mock.patch.object(mail, "send") as send:
            db = object()
            def check(now):
                packet_pause.alert_due(db, data, project="ameesh", target="main",
                                       recipients=("pilot", "bureau"), now=now)
            check(NOW + 3599)
            send.assert_not_called()
            check(NOW + 3600)
            first = send.call_args_list[:]
            check(NOW + 7200)
            self.assertEqual(send.call_args_list[2:], first)
            self.assertEqual(len(first), 2)
            self.assertEqual(first[0].args[1], "ameesh")
            self.assertIn("aucun geste", first[0].args[3])

    def test_expired_announcement_still_blocks_every_tick_action(self):
        self.pause(await_announcement=True)
        helper = bench.TickTest()
        helper.setUp()
        try:
            data = packet_pause.active(object(), project="ameesh", target="main", now=NOW + 7200)
            said = ph.run(helper.ctx(lambda: data))
            self.assertEqual(helper.calls, [])
            self.assertIn("annonce explicite", said)
        finally:
            helper.doCleanups()


class CommandTest(bench.CommandTest):
    def test_announcement_cli_and_source(self):
        with mock.patch.object(ph, "main_pause", return_value=0) as call:
            self.assertEqual(ph.main(["pause", "--until", "1h", "--reason", "CI window",
                                      "--await-announcement"]), 0)
            self.assertTrue(call.call_args.args[0].await_announcement)
            self.assertEqual(ph.main(["resume", "--source", "pilot, 31/10, #173000"]), 0)
            self.assertIn("#173000", call.call_args.args[0].source)
        rc, text, _ = self.run_cmd("pause", "--until", "1h", "--reason", "CI window",
                                   "--await_announcement", True)
        self.assertEqual(rc, 0, text)
        self.assertIn("échéance = alerte", text)
        rc, text, _ = self.run_cmd("resume")
        self.assertEqual(rc, 2, text)
        rc, text, _ = self.run_cmd("resume", "--source", "pilot, 31/10, #173000")
        self.assertEqual(rc, 0, text)
        self.assertIn("#173000", text)

    def test_tick_read_alerts_and_dry_run_only_reads(self):
        self.pause(await_announcement=True)
        db = mock.Mock()
        with mock.patch.object(ph, "_connect", return_value=db), \
                mock.patch.object(packet_pause, "alert_due") as alert:
            self.assertIsNotNone(ph.read_pause("ameesh", "main", pilot="pilot", now=NOW + 3600))
            self.assertEqual(alert.call_args.kwargs["recipients"], ("pilot", ph.BUREAU))
            alert.reset_mock()
            self.assertIsNotNone(ph.read_pause("ameesh", "main", notify=False, now=NOW + 3600))
            alert.assert_not_called()
        self.assertEqual(db.close.call_count, 2)


class DurablePauseTest(PgTestCase):
    def old(self, state, *, object="paquet-horaire:ameesh:main", days=400):
        data = {"state": state, "until": int(time.time() - (days - 1) * 86400),
                "await_announcement": True, "reason": "window", "by": "pilot"}
        return int(self.db.query(
            "INSERT INTO ameesh_events (type, object, project, data, created_at)"
            " VALUES ('packet.pause', %s, 'ameesh', %s::jsonb,"
            " now() - make_interval(days => %s)) RETURNING id",
            (object, json.dumps(data), days))[0]["id"])

    def test_pause_survives_lookback_and_retention_then_resume_survives(self):
        first = self.old("paused")
        self.assertEqual(packet_pause.active(self.db, project="ameesh", target="main")["event"], first)
        events.prune(self.db, days=365)
        self.assertIsNotNone(packet_pause.active(self.db, project="ameesh", target="main"))
        self.assertEqual(events.expired(self.db, days=365), 0)
        # Equal age: id, not timestamp, defines the latest state. Keep its tombstone.
        last = self.old("resumed")
        events.prune(self.db, days=365, batch=1)
        self.assertIsNone(packet_pause.active(self.db, project="ameesh", target="main"))
        rows = storage.of(self.db).events.recent_type(
            "packet.pause", None, 10, object="paquet-horaire:ameesh:main")
        self.assertEqual([r["id"] for r in rows], [last])

    def test_slo_clock_waits_beyond_deadline_and_lookback_then_resumes(self):
        self.old("paused", days=4)
        now = time.time()
        windows = packet_pause.windows(self.db, project="ameesh", now=now)
        self.assertEqual(len(windows), 1)
        start = windows[0]["start"] + 60
        equivalent, crossed = packet_pause.suspended_start(
            start, windows, lambda at: None, now)
        self.assertEqual(equivalent, now)
        self.assertIsNotNone(crossed)
        # Closing a pause that started before the history cutoff still keeps
        # the whole suspension; time runs only after its sourced announcement.
        packet_pause.resume(self.db, project="ameesh", target="main", by="pilot",
                            source="pilot, 31/10, message #173232")
        later = now + 2400
        windows = packet_pause.windows(self.db, project="ameesh", now=later)
        self.assertEqual(len(windows), 1)
        equivalent, _ = packet_pause.suspended_start(
            start, windows, lambda at: None, later)
        self.assertAlmostEqual(equivalent, now, delta=2)

    def test_recent_chatter_and_other_project_do_not_evict_old_pause(self):
        self.old("paused", days=4)
        self.old("paused", object="paquet-horaire:other:main", days=4)
        store = storage.of(self.db).events
        for _ in range(4):
            store.record(packet_pause.EVENT, "paquet-horaire:ameesh:develop", "ameesh",
                         {"state": "resumed"})
        with mock.patch.object(packet_pause, "WINDOWS_LIMIT", 2):
            windows = packet_pause.windows(self.db, project="ameesh")
        self.assertEqual([w["target"] for w in windows], ["main"])

    def test_unrelated_older_and_superseded_events_are_pruned(self):
        self.old("paused")
        self.old("resumed", days=450)  # Deliberately older timestamp, newer state.
        self.db.execute("INSERT INTO ameesh_events (type, object, created_at) VALUES"
                        " ('alert.resolved', '5045:other', now() - interval '400 days')")
        events.prune(self.db, days=365, batch=1)
        self.assertIsNone(packet_pause.active(self.db, project="ameesh", target="main"))
        self.assertEqual(self.db.query("SELECT id FROM ameesh_events WHERE object = '5045:other'"), [])

    def test_deadline_mail_is_deduplicated_in_the_shared_mailbox(self):
        data = {"state": "paused", "await_announcement": True, "until": NOW,
                "event": 5045, "reason": "window"}
        with mock.patch("ameesh.fil.record"):
            for _ in range(2):
                packet_pause.alert_due(self.db, data, project="ameesh", target="main",
                                       recipients=("pilot", "bureau"), now=NOW)
        rows = self.db.query("SELECT recipient FROM agent_mailbox"
                             " WHERE payload->>'idempotency_key' LIKE 'packet-pause-5045-%%'")
        self.assertCountEqual([r["recipient"] for r in rows], ["pilot", "bureau"])
