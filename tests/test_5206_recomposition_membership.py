# SPDX-License-Identifier: AGPL-3.0-only
"""#5206: a recomposition in progress keeps its members out of the SLO.

On 2026-11-01, `review GO → merge` breaches of levels 2 and 3 fired for #5151
at 00:03:53Z and 00:52:56Z although #5151 was a member of the open packet
#5196 the whole time (first unit re-added by both recompositions). The pilot
recomposed with `paquet-compose --record-package`, which records the packet
after each unit (00:03:17 → 00:04:53, 00:52:32 → 00:53:xx), each time on a head
that is not pushed yet. `recorded_packet_attachments` bound the membership to
the carrier head observed by the branch sweep (#4111): the previous head, so
every member was dropped and the packet's prose body proves nothing.

A composition recorded AFTER the observed head is the newer evidence, for a
bounded time (`RECORD_AHEAD_GRACE_S`). An undated, later or too old
observation keeps the #4111 rule.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import exploitation, planned_waits, storage, work

from .test_789_slo_alerts import _Base


class RecompositionTest(_Base):
    #: the head of the composition being recorded (not pushed yet)
    HEAD = "b" * 40
    #: the carrier head the branch sweep last observed (previous composition)
    OLD = "e" * 40

    def setUp(self):
        super().setUp()
        p80 = mock.patch.object(exploitation, "_ci_suite_p80", return_value=None)
        p80.start()
        self.addCleanup(p80.stop)
        self.freeze = "d" * 40
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")
        # same shape as test_3632: GO six hours ago, well past any level
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - interval '7 hours', %s, 'dev1')",
            (self.lot["id"], self.freeze))
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', now() - interval '6 hours', %s, 'rel', 'ok')",
            (self.lot["id"], self.freeze))
        self.vehicle = self.packet()
        storage.of(self.db).work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-test",
            base_sha="a" * 40, head_sha=self.HEAD,
            units=[{"work_item_id": int(self.lot["id"]), "unit_sha": "c" * 40,
                    "source_sha": self.freeze, "frozen_sha": self.freeze}],
            note="packet composition recorded", actor="orch")
        self.now = time.time()

    def observe(self, head, seconds_ago):
        """The branch sweep saw `head` on the carrier `seconds_ago` ago."""
        self.db.execute(
            "UPDATE work_items SET branch_head = %s,"
            " branch_head_at = now() - make_interval(secs => %s) WHERE id = %s",
            (head, float(seconds_ago), int(self.vehicle["id"])))

    def recorded(self, seconds_ago):
        self.db.execute(
            "UPDATE work_packet_members SET recorded_at = now() - make_interval(secs => %s)"
            " WHERE package_id = %s", (float(seconds_ago), int(self.vehicle["id"])))

    def verdict_merge(self):
        return [alert for alert in self.slo() if alert["step"] == "verdict_merge"]

    # -- witness ----------------------------------------------------------
    def test_a_recomposition_newer_than_the_observed_head_keeps_the_member(self):
        # 00:03:53Z: record of 00:03:17Z, carrier head swept before it
        self.recorded(36)
        self.observe(self.OLD, 600)
        self.assertEqual(self.verdict_merge(), [])

    def test_open_lots_activity_dates_the_observed_head(self):
        self.observe(self.OLD, 600)
        row = next(r for r in storage.of(self.db).operations.open_lots_activity(None)
                   if int(r["id"]) == int(self.vehicle["id"]))
        self.assertAlmostEqual(float(row["branch_head_ts"]), time.time() - 600, delta=60)

    # -- #4111 kept --------------------------------------------------------
    def test_a_head_observed_after_the_record_still_invalidates_it(self):
        # a later adaptation: the sweep saw another head after the record
        self.recorded(300)
        self.observe(self.OLD, 60)
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_an_undated_observed_head_still_invalidates_the_record(self):
        self.db.execute("UPDATE work_items SET branch_head = %s, branch_head_at = NULL"
                        " WHERE id = %s", (self.OLD, int(self.vehicle["id"])))
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_a_record_never_pushed_stops_counting_after_the_grace(self):
        # the composer recorded, then failed before pushing: bounded
        self.recorded(planned_waits.RECORD_AHEAD_GRACE_S + 300)
        self.observe(self.OLD, planned_waits.RECORD_AHEAD_GRACE_S + 900)
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_the_rule_without_storage(self):
        carrier = dict(id=90, state="qa", branch="integration/paquet-test",
                       branch_head=self.OLD, branch_head_ts=1000.0)
        unit = dict(id=1, state="validated", branch_head=self.freeze)
        store = mock.Mock()
        store.work.packet_members.return_value = [dict(
            package_id=90, work_item_id=1, head_sha=self.HEAD,
            source_sha=self.freeze, recorded_ts=1100.0)]
        store.progress.lot_milestones.return_value = []
        attach = planned_waits.recorded_packet_attachments
        self.assertEqual(attach(store, [carrier], [unit], now=1200.0), {90: {1}})
        self.assertEqual(attach(store, [carrier], [unit],
                                now=1100.0 + planned_waits.RECORD_AHEAD_GRACE_S + 1), {})
        self.assertEqual(attach(store, [dict(carrier, branch_head_ts=1150.0)], [unit],
                                now=1200.0), {})
