# SPDX-License-Identifier: AGPL-3.0-only
"""#6104 : le silence d'une unité portée par une PR de paquet sans porteur est
borné — CI rouge, ou PR ouverte au-delà de la cible de l'étape."""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import forge_packets, packet_candidates

from . import test_6088_forge_packets as t6088
from .test_4572_slo_candidate_cause import _SloBase


class CiStateTest(unittest.TestCase):
    def test_no_check_is_none(self):
        self.assertEqual(forge_packets.ci_state([]), "none")
        self.assertEqual(forge_packets.ci_state(None), "none")

    def test_one_red_check_wins(self):
        rollup = [{"status": "IN_PROGRESS", "conclusion": ""},
                  {"status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertEqual(forge_packets.ci_state(rollup), "red")

    def test_cancelled_run_is_not_red(self):
        rollup = [{"status": "COMPLETED", "conclusion": "CANCELLED"},
                  {"status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertEqual(forge_packets.ci_state(rollup), "green")

    def test_status_context_error_is_red(self):
        self.assertEqual(forge_packets.ci_state([{"state": "ERROR"}]), "red")

    def test_unfinished_check_is_running(self):
        rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"},
                  {"status": "QUEUED", "conclusion": ""},
                  {"state": "SUCCESS"}]
        self.assertEqual(forge_packets.ci_state(rollup), "running")
        self.assertEqual(forge_packets.ci_state([{"state": "PENDING"}]), "running")

    def test_all_concluded_without_red_is_green(self):
        rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"},
                  {"status": "COMPLETED", "conclusion": "SKIPPED"},
                  {"state": "SUCCESS"}]
        self.assertEqual(forge_packets.ci_state(rollup), "green")


class StaleReasonTest(unittest.TestCase):
    def test_red_ci_no_longer_carries(self):
        why = forge_packets.stale_reason({"ci": "red", "created_ts": 90.0}, 3600, 100.0)
        self.assertEqual(why, "its CI is red")

    def test_open_beyond_target_no_longer_carries(self):
        why = forge_packets.stale_reason({"ci": "running", "created_ts": 0.0}, 3600, 3601.0)
        self.assertIn("beyond the step target (3600s)", why)

    def test_recent_running_pr_carries(self):
        self.assertEqual(
            forge_packets.stale_reason({"ci": "running", "created_ts": 0.0}, 3600, 3600.0), "")

    def test_unknown_age_or_target_only_red_lifts(self):
        self.assertEqual(forge_packets.stale_reason({"ci": "green"}, 3600, 1e9), "")
        self.assertEqual(
            forge_packets.stale_reason({"ci": "green", "created_ts": 0.0}, None, 1e9), "")


class VehicleFieldsTest(unittest.TestCase):
    # le dépôt de t6088, sans rejouer ses tests ici
    setUp = t6088.ForgePacketTest.setUp
    gh = t6088.ForgePacketTest.gh
    pr = t6088.ForgePacketTest.pr
    frozen_ms = t6088.ForgePacketTest.frozen_ms

    def test_vehicle_carries_age_and_ci(self):
        pr = dict(self.pr(), createdAt="2026-11-07T03:00:00Z",
                  statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
        found = forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(self.frozen),
                                      gh=self.gh([pr]), cache={})
        self.assertEqual(found["ci"], "red")
        self.assertEqual(found["created_ts"], 1794020400.0)


class SilencePassTest(_SloBase):
    READY = {"code": "", "detail": "", "owner": "", "gesture": "",
             "ready": True, "unknown": False}

    def alert_with(self, found):
        with mock.patch.object(forge_packets, "vehicle", return_value=found), \
                mock.patch.object(packet_candidates, "candidate_refusal",
                                  return_value=dict(self.READY)):
            return self.alert(now=self.GO + 6 * 3600.0)

    def found(self, **kw):
        return dict({"pr": 9, "branch": "integration/paquet-9-x", "head": "c" * 40,
                     "created_ts": self.GO + 6 * 3600.0 - 60.0, "ci": "running"}, **kw)

    def test_recent_running_packet_pr_stays_silent(self):
        self.assertEqual(self.alert_with(self.found()), [])

    def test_red_packet_pr_keeps_alert_and_names_it(self):
        alerts = self.alert_with(self.found(ci="red"))
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("open packet PR #9 (integration/paquet-9-x) no longer carries"
                      " the merge: its CI is red", alerts[0]["detail"])

    def test_packet_pr_open_beyond_target_keeps_alert(self):
        alerts = self.alert_with(self.found(created_ts=self.GO - 3 * 86400.0))
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("beyond the step target", alerts[0]["detail"])


if __name__ == "__main__":
    unittest.main()
