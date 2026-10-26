# SPDX-License-Identifier: AGPL-3.0-only
"""#3632: a merged vehicle delivers its frozen members without sync-merges.

At 00:34Z on 2026-10-26, three level-3 `review GO → merge` alerts fired on
#2309, #2882 and #3067 while their vehicle #3514 had been merged since
00:30:47Z. `sync-merges` had not closed the members yet, and
`slo_delivery.merge_observed` only knew the Git probe: it needed a readable
clone, a declared target (#2309 has no `branch_target`) and a fresh
`origin/<target>`. The packet composition table already records the member's
effective freeze and its carrier's merge; reading it is enough.

#3634 (basket of #3632, N1): `record_packet` replaces the whole membership on
every record. A vehicle recomposed without a new record keeps rows of units it
no longer carries (#3584 left h-25); the database proof must only accept the
composition of the head the carrier actually froze and merged.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import mail, notify, slo_delivery, storage, work

from .test_789_slo_alerts import _Base


class VehicleMergedTest(_Base):
    """A member of a merged vehicle is delivered even while it is still `qa`."""

    #: the head recorded by `record_packet` (and frozen by the carrier).
    HEAD = "b" * 40

    def setUp(self):
        super().setUp()
        self.freeze = "d" * 40
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - interval '3 hours', %s, 'dev1')",
            (self.lot["id"], self.freeze))
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', now() - interval '2 hours', %s, 'rel', 'ok')",
            (self.lot["id"], self.freeze))
        # The member is composed in an `integration/**` vehicle. No repository,
        # no declared target: the Git probe cannot run (the #2309 shape).
        self.vehicle = self.packet()
        storage.of(self.db).work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-test",
            base_sha="a" * 40, head_sha=self.HEAD,
            units=[{"work_item_id": int(self.lot["id"]), "unit_sha": "c" * 40,
                    "source_sha": self.freeze, "frozen_sha": self.freeze}],
            note="packet composition recorded", actor="orch")
        self.notifier = notify.Notifier(
            self.cfg, notify.parse_config({"default": []}),
            sender=object(), log=lambda text: None)
        self.now = time.time()

    # -- fixtures ---------------------------------------------------------
    def freeze_vehicle(self, head=None):
        """The carrier freezes the head it will merge (the recorded one by
        default)."""
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - interval '1 hour', %s, 'orch')",
            (int(self.vehicle["id"]), self.HEAD if head is None else head))

    def merge_vehicle(self, *, frozen_head=None, freeze=True):
        if freeze:
            self.freeze_vehicle(frozen_head)
        for state in ("build", "qa", "merged"):
            work.move(self.db, int(self.vehicle["id"]), state)

    def member(self):
        return work.get(self.db, self.lot["id"])

    def verdict_merge(self, alerts=None):
        return [alert for alert in (self.slo() if alerts is None else alerts)
                if alert["step"] == "verdict_merge"]

    def deliver(self, alerts):
        with mock.patch.object(mail, "send", return_value=1) as send, \
                mock.patch.object(notify.fil, "record", return_value="thread") as thread:
            self.notifier._brief_agents(self.db, alerts, None, self.now)
        return send, thread

    # -- witness ----------------------------------------------------------
    def test_merged_vehicle_suppresses_the_member_alert_without_sync_merges(self):
        self.assertEqual([alert["level"] for alert in self.verdict_merge()], [3])
        self.merge_vehicle()
        # No sync-merges: the member is still open in qa.
        self.assertEqual(self.member()["state"], "qa")
        self.assertEqual(self.verdict_merge(), [])
        self.assertTrue(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_stale_snapshot_sends_no_mail_once_the_vehicle_is_merged(self):
        snapshot = self.slo()
        self.assertEqual([alert["step"] for alert in snapshot], ["verdict_merge"])
        self.merge_vehicle()
        send, thread = self.deliver(snapshot)
        send.assert_not_called()
        thread.assert_not_called()
        self.assertEqual(self.notifier.state["briefs"], {})

    # -- negative witnesses ----------------------------------------------
    def test_open_vehicle_keeps_the_alert(self):
        self.assertEqual([alert["level"] for alert in self.verdict_merge()], [3])
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_new_freeze_after_composition_is_not_delivered(self):
        self.merge_vehicle()
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now(), %s, 'dev1')",
            (self.lot["id"], "e" * 40))
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_freeze_recorded_after_composition_is_not_delivered(self):
        # `record` re-reads each unit's effective freeze (a verdict-origin
        # freeze included); a re-run after the merge can record a freeze the
        # composed head does not contain.
        self.merge_vehicle()
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', now(), %s, 'rel', 'ok')",
            (self.lot["id"], "e" * 40))
        self.db.execute(
            "UPDATE work_packet_members SET frozen_sha = %s"
            " WHERE work_item_id = %s", ("e" * 40, self.lot["id"]))
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_new_observed_head_after_composition_is_not_delivered(self):
        self.merge_vehicle()
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        ("e" * 40, self.lot["id"]))
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    # -- #3634: a composition must be the one the carrier merged ------------
    def test_a_recomposition_not_recorded_keeps_the_removed_members_alert(self):
        # #3584 left h-25 after the last record: the carrier was recomposed,
        # re-frozen and merged at another head, while the row still names the
        # old one. The removed unit is not delivered and its alert stays.
        self.merge_vehicle(frozen_head="e" * 40)
        self.assertEqual(self.member()["state"], "qa")
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))
        self.assertEqual([alert["level"] for alert in self.verdict_merge()], [3])

    def test_a_newer_freeze_without_commit_is_not_the_recorded_head(self):
        # The latest freeze is authoritative even when it names no commit: an
        # older matching head must not stand in for it.
        self.freeze_vehicle()
        self.freeze_vehicle("")
        for state in ("build", "qa", "merged"):
            work.move(self.db, int(self.vehicle["id"]), state)
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_vehicle_merged_without_a_frozen_head_is_not_database_evidence(self):
        self.merge_vehicle(freeze=False)
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_merged_milestone_naming_the_composed_head_delivers(self):
        # An ancestor/fast-forward closure records the exact branch head as the
        # `merged` milestone: that milestone carries the composition.
        self.merge_vehicle(freeze=False)
        self.db.execute(
            "UPDATE work_item_milestones SET sha = %s"
            " WHERE work_item_id = %s AND kind = 'merged'",
            (self.HEAD, int(self.vehicle["id"])))
        self.assertTrue(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_closed_vehicle_is_not_delivery_evidence(self):
        self.merge_vehicle()
        self.db.execute("UPDATE work_items SET state = 'closed',"
                        " close_reason = 'abandoned' WHERE id = %s",
                        (self.vehicle["id"],))
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))

    def test_a_merged_vehicle_without_member_membership_is_not_delivered(self):
        self.db.execute("DELETE FROM work_packet_members WHERE work_item_id = %s",
                        (self.lot["id"],))
        self.merge_vehicle()
        self.assertFalse(slo_delivery.merge_observed(self.cfg, self.db, self.member()))
