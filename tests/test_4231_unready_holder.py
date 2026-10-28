# SPDX-License-Identifier: AGPL-3.0-only
"""An unready elected holder yields to a ready standby, with epoch fencing."""
import os
import time
from types import SimpleNamespace
from unittest import mock

from ameesh import election, registry, role, runner
from .test_l165_ep2_election_score import _RoleCase


class RenewalReadinessTest(_RoleCase):
    def setUp(self):
        super().setUp()
        self.role()
        self.ready_pair()
        self.standing("pilote", "banc", 99)
        self.standing("pilote", "atelier", 10)
        self.holder = self.hold("pilote", epoch=7)

    def test_unready_holder_yields_and_old_epoch_canary_is_refused(self):
        worker, tick = self.worker()
        self.canon("banc", "unreadable")
        successor = election.unready_successor(self.db, self.holder)
        self.assertEqual(successor["host"], "atelier")
        self.assertIsNotNone(registry.renew(self.db, "pilote", "runner-test", 7, 300))
        self.assertIsNone(worker._election_handover())
        tick.monotonic.return_value = 160
        self.assertIsNone(worker._election_handover())
        tick.monotonic.return_value = 220
        due = worker._election_handover()
        self.assertEqual(due["challenger"], "atelier")
        self.assertEqual(due["seconds"], 120)
        self.assertLess(due["seconds"] + 60 + 60 + 5, 300)
        # The normal between-turn handover records the standby and releases.
        self.assertTrue(registry.release(self.db, "pilote", "runner-test", 7))
        lease = registry.claim(self.db, "pilote", "runner-new", 300, host="atelier",
                               election_brief="standby takes over unready holder")
        self.assertIsNotNone(lease)
        self.assertGreater(lease["lease_epoch"], 7)
        before = self.db.query(
            "SELECT count(*) AS n FROM ameesh_events WHERE type = 'pilot.gesture'"
            " AND object = 'agent:pilote'")
        with mock.patch.dict(os.environ, {"AMEESH_ROLE": "pilote",
                "AMEESH_LEASE_EPOCH": "7", "AMEESH_LEASE_OWNER": "runner-test"}):
            code, report = role.canary(self.cfg, self.db)
        self.assertEqual(code, 1, report)
        self.assertFalse(report["ok"])
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM ameesh_events WHERE type = 'pilot.gesture'"
            " AND object = 'agent:pilote'"),
            before)

    def worker(self):
        obj = runner.Runner(self.cfg, self.db, once=True)
        self.addCleanup(obj.stop.set)
        worker = runner.AgentWorker(obj, self.holder, self.holder)
        self.addCleanup(worker.watchdog_stop.set)
        tick = SimpleNamespace(monotonic=mock.Mock(return_value=100))
        worker.clock = tick
        return worker, tick

    def test_isolated_bad_sample_recovers_without_handover(self):
        worker, tick = self.worker()
        self.standing("pilote", "banc", 99, ready=False, diagnostic="critical host pressure")
        self.assertIsNone(worker._election_handover())
        self.standing("pilote", "banc", 99)
        tick.monotonic.return_value = 160
        self.assertIsNone(worker._election_handover())
        self.assertIsNone(worker._unready_since)
        self.standing("pilote", "banc", 99, ready=False, diagnostic="critical host pressure")
        tick.monotonic.return_value = 220
        self.assertIsNone(worker._election_handover())
        self.assertEqual(worker._unready_since, 220)

    def test_unknown_holder_does_not_trigger_health_handover(self):
        self.db.execute("DELETE FROM role_standings WHERE host = 'banc'")
        self.assertIsNone(election.unready_successor(self.db, self.holder))
        self.assertIsNotNone(registry.renew(self.db, "pilote", "runner-test", 7, 300))

    def test_unready_holder_keeps_fenced_gestures_until_planned_release(self):
        self.canon("banc", "unreadable")
        with mock.patch.dict(os.environ, {"AMEESH_ROLE": "pilote",
                "AMEESH_LEASE_EPOCH": "7", "AMEESH_LEASE_OWNER": "runner-test"}):
            code, report = role.canary(self.cfg, self.db)
        self.assertEqual(code, 0, report)
        self.assertEqual(registry.get(self.db, "pilote")["lease_epoch"], 7)

    def test_ready_holder_keeps_lease(self):
        self.assertIsNone(election.unready_successor(self.db, self.holder))
        self.assertIsNotNone(registry.renew(self.db, "pilote", "runner-test", 7, 300))

    def test_no_ready_replacement_does_not_abandon_role(self):
        self.canon("banc", "unreadable")
        self.canon("atelier", "unreadable")
        self.assertIsNone(election.unready_successor(self.db, self.holder))
        self.assertIsNotNone(registry.renew(self.db, "pilote", "runner-test", 7, 300))

    def test_stale_standby_cannot_displace_unready_holder(self):
        self.canon("banc", "unreadable")
        self.db.execute("UPDATE role_standings SET evaluated_at = now() - interval '1 hour'"
                        " WHERE host = 'atelier'")
        self.assertIsNone(election.unready_successor(self.db, self.holder))
        self.assertIsNotNone(registry.renew(self.db, "pilote", "runner-test", 7, 300))

    def test_critical_pressure_is_published_as_not_ready(self):
        election.publish_self(self.cfg, self.db, "banc", force=True,
                              pressure={"critical": True})
        state = election.candidacy(self.db, "pilote")
        mine = next(r for r in state if r["host"] == "banc")
        self.assertFalse(mine["ready"])
        self.assertIn("critical host pressure", mine["diagnostic"])
        self.assertEqual(election.unready_successor(self.db, self.holder)["host"],
                         "atelier")
