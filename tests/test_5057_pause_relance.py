# SPDX-License-Identifier: AGPL-3.0-only
"""#5057: an announcement pause past its deadline reminds the pilot until the
announcement; the SLO keeps the whole chain of renewed pauses before the
lookback, and an open announcement pause suspends a delay already accrued."""
from __future__ import annotations

import json
import time
from unittest import mock

from ameesh import mail, packet_pause
from .support import PgTestCase
from . import test_4361_paquet_pause as bench

NOW = bench.NOW
DAY = 86400.0


class ReminderTest(bench.Base):
    def test_reminder_every_period_until_announcement(self):
        self.pause(await_announcement=True)
        data = packet_pause.latest(object(), project="ameesh", target="main")
        until = float(data["until"])
        with mock.patch.object(mail, "send") as send:
            def keys(now):
                send.reset_mock()
                packet_pause.alert_due(object(), data, project="ameesh", target="main",
                                       recipients=("pilot", "bureau"), now=now)
                return [c.kwargs["idempotency_key"] for c in send.call_args_list]
            first = keys(until)
            self.assertEqual(first, keys(until + packet_pause.REMIND_S - 1))
            again = keys(until + packet_pause.REMIND_S)
            self.assertEqual(len(again), 2)
            self.assertFalse(set(again) & set(first))
            # 8 h 37 of silent freeze (0518): eight distinct reminders by then.
            late = {k for h in range(9) for k in keys(until + h * packet_pause.REMIND_S + 60)}
            self.assertEqual(len(late), 18)
            keys(until + 8 * 3600 + 37 * 60)
            body = send.call_args_list[0].args[3]
            self.assertIn("Relance 8", body)
            self.assertIn("8 h 37", body)
            self.assertIn("aucun geste", body)
        packet_pause.resume(object(), project="ameesh", target="main", by="pilot",
                            source="pilot, 31/10, #175083")
        data = packet_pause.latest(object(), project="ameesh", target="main")
        with mock.patch.object(mail, "send") as send:
            packet_pause.alert_due(object(), data, project="ameesh", target="main",
                                   recipients=("pilot",), now=until + 9 * 3600)
            send.assert_not_called()


class OpenPauseAfterClosedTest(bench.Base):
    def test_open_await_pause_wins_over_a_cursor_past_now(self):
        # A closed pause whose next slot lies after `now`, then a renewal to an
        # announcement pause within the same hour: nothing runs while it is open.
        now = NOW
        closed = {"start": now - 1800, "end": now - 600, "until": int(now - 600),
                  "open": False, "reason": "r", "by": "b", "target": "main"}
        waiting = {"start": now - 600, "end": now, "until": int(now - 60),
                   "open": True, "reason": "r", "by": "b", "target": "main"}
        accrued_start = now - 5 * 3600
        equivalent, crossed = packet_pause.suspended_start(
            accrued_start, [closed, waiting], lambda at: now + 1200, now)
        self.assertEqual(equivalent, now)
        self.assertIs(crossed, waiting)
        # The closed pause alone keeps its accrued time (unchanged behaviour).
        equivalent, crossed = packet_pause.suspended_start(
            accrued_start, [closed], lambda at: now + 1200, now)
        self.assertEqual(equivalent, now - (closed["start"] - accrued_start))
        self.assertIs(crossed, closed)


class ChainBeforeLookbackTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # #5057: the log is append-only and the class shares one schema; an
        # old tombstone breaks the pause chain earlier tests left behind.
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, project, data, created_at)"
            " VALUES ('packet.pause', 'paquet-horaire:ameesh:main', 'ameesh',"
            " '{\"state\": \"resumed\"}'::jsonb, now() - interval '500 days')")

    def at(self, state, age_days, *, until_age=None, await_=False,
           object="paquet-horaire:ameesh:main"):
        now = time.time()
        data = {"state": state, "reason": "window", "by": "pilot"}
        if until_age is not None:
            data["until"] = int(now - until_age * DAY)
        if await_:
            data["await_announcement"] = True
        self.db.execute(
            "INSERT INTO ameesh_events (type, object, project, data, created_at)"
            " VALUES ('packet.pause', %s, 'ameesh', %s::jsonb,"
            " now() - make_interval(secs => %s))",
            (object, json.dumps(data), age_days * DAY))

    def test_renewed_chain_keeps_its_true_start(self):
        # docs-01: pauses at -3 d / -2.5 d / -2.2 d, renewal -1.8 d, resume -1 d.
        # An earlier pause, expired before the next event, breaks the chain.
        self.at("paused", 5, until_age=4.9)
        self.at("paused", 3, until_age=2.4)
        self.at("paused", 2.5, until_age=2.1)
        self.at("paused", 2.2, until_age=0.5, await_=True)
        self.at("paused", 1.8, until_age=1.5, await_=True)
        self.at("resumed", 1)
        now = time.time()
        windows = packet_pause.windows(self.db, project="ameesh", now=now)
        self.assertEqual([round((now - w["start"]) / DAY, 2) for w in windows],
                         [3.0, 2.5, 2.2, 1.8])
        start = now - 3.5 * DAY
        equivalent, _ = packet_pause.suspended_start(start, windows, lambda at: None, now)
        # Counted: 0.5 d before the chain and 1 d after the resume, not 2.8 d.
        self.assertAlmostEqual((now - equivalent) / DAY, 1.5, delta=0.001)

    def test_last_state_alone_when_it_breaks(self):
        self.at("paused", 4, until_age=3.5)
        self.at("resumed", 3)
        self.at("paused", 2.8, until_age=2.6)
        windows = packet_pause.windows(self.db, project="ameesh", now=time.time())
        self.assertEqual(len(windows), 1)
        self.at("resumed", 2.5)
        self.assertEqual(packet_pause.windows(self.db, project="ameesh"), [])
