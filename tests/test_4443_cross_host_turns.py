# SPDX-License-Identifier: AGPL-3.0-only
"""#4443: a claimed persona closes logical turns left on another host."""
import unittest

from ameesh import registry, storage
from .support import PgTestCase
from . import test_2341_host_unsampled as host_tests

HOST, OTHER, NOW, DAY = host_tests.HOST, host_tests.OTHER, host_tests.NOW, host_tests.DAY


class CrossHostTurnsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")
        registry.upsert(self.db, "alpha", harness="claude", host="new-host")
        self.turns = storage.of(self.db).turn_resources
        for turn, agent, host in (("old", "alpha", "old-host"),
                                  ("local", "alpha", "new-host"),
                                  ("known-orphan", "alpha", "old-host"),
                                  ("other-agent", "beta", "old-host")):
            self.turns.open_turn(turn, agent, host, pgid=42, label="test")
        self.turns.mark_orphan("known-orphan", ["container-kept"])

    def test_claim_settles_only_remote_running_rows(self):
        self.assertIsNotNone(registry.claim(self.db, "alpha", "new-runner", 60))
        self.assertEqual(self.turns.get("old")["status"], "done")
        self.assertIsNotNone(self.turns.get("old")["ended_ts"])
        self.assertEqual(self.turns.get("local")["status"], "running")
        self.assertEqual(self.turns.get("other-agent")["status"], "running")
        self.assertEqual(self.turns.get("known-orphan")["status"], "orphan")
        self.assertEqual(self.turns.get("known-orphan")["containers"], ["container-kept"])

    def test_refused_claim_does_not_settle(self):
        self.db.execute("UPDATE agent_registry SET lease_owner = 'live-runner',"
                        " lease_expires_at = now() + interval '1 hour' WHERE name = 'alpha'")
        self.assertIsNone(registry.claim(self.db, "alpha", "new-runner", 60))
        self.assertEqual(self.turns.get("old")["status"], "running")


class GroupedHostAlertsTest(unittest.TestCase):
    alerts = host_tests.HostUnsampledTest.alerts
    orphan = staticmethod(host_tests.HostUnsampledTest.orphan)
    def test_stale_host_groups_turns_and_fresh_host_keeps_individual_alert(self):
        found = self.alerts({HOST: {"sampled_ts": NOW - 2 * DAY},
                             OTHER: {"sampled_ts": NOW - 30}}, max_agents=3,
                            orphans=[self.orphan("old-1"), self.orphan("old-2"),
                                     self.orphan("fresh", OTHER)])
        grouped = [a for a in found if a["type"] == "host_unsampled"]
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["orphan_turns"], ["old-1", "old-2"])
        self.assertIn("2 unresolved turn resource(s)", grouped[0]["detail"])
        self.assertEqual([a["turn"] for a in found if a["type"] == "orphan_resource"],
                         ["fresh"])
