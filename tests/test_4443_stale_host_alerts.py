# SPDX-License-Identifier: AGPL-3.0-only
"""#4443: stale host alerts group unresolved turns without changing resources."""
import unittest

from . import test_2341_host_unsampled as host_tests

HOST, OTHER, NOW, DAY = host_tests.HOST, host_tests.OTHER, host_tests.NOW, host_tests.DAY


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

    def test_host_return_restores_physical_resource_alerts(self):
        rows = [self.orphan("still-there")]
        stale = self.alerts({HOST: {"sampled_ts": NOW - 2 * DAY}},
                            max_agents=3, orphans=rows)
        self.assertEqual([a["type"] for a in stale], ["host_unsampled"])
        fresh = self.alerts({HOST: {"sampled_ts": NOW - 30}},
                            max_agents=3, orphans=rows)
        self.assertEqual([a["type"] for a in fresh], ["orphan_resource"])
        self.assertEqual(fresh[0]["turn"], "still-there")
        self.assertEqual(rows[0]["status"], "orphan")


class PublishOnlyHostTest(unittest.TestCase):
    """#5857: a fresh `publish@` reading proves no executor; its turns group."""
    alerts = host_tests.HostUnsampledTest.alerts
    orphan = staticmethod(host_tests.HostUnsampledTest.orphan)

    def test_publish_reading_groups_turns_in_one_host_alert(self):
        found = self.alerts(
            {HOST: {"sampled_ts": NOW - 30, "runner_id": "publish@%s" % HOST},
             OTHER: {"sampled_ts": NOW - 30, "runner_id": "runner-x@%s" % OTHER}},
            max_agents=3,
            orphans=[self.orphan("old-1"), self.orphan("old-2"),
                     self.orphan("fresh", OTHER)])
        grouped = [a for a in found if a["type"] == "host_no_executor"]
        self.assertEqual(len(grouped), 1)
        self.assertEqual((grouped[0]["host"], grouped[0]["orphan_turns"],
                          grouped[0]["value"]), (HOST, ["old-1", "old-2"], 2))
        self.assertIn("publish@%s" % HOST, grouped[0]["detail"])
        self.assertIn("2 unresolved turn resource(s)", grouped[0]["detail"])
        # the host with an executor keeps its individual alerts
        self.assertEqual([a["turn"] for a in found if a["type"] == "orphan_resource"],
                         ["fresh"])

    def test_one_episode_per_host_whatever_the_count(self):
        one = self.alerts({HOST: {"sampled_ts": NOW - 30, "runner_id": "publish@%s" % HOST}},
                          max_agents=3, orphans=[self.orphan("a")])
        two = self.alerts({HOST: {"sampled_ts": NOW - 30, "runner_id": "publish@%s" % HOST}},
                          max_agents=3, orphans=[self.orphan("a"), self.orphan("b")])
        key = host_tests.exploitation.alert_key
        self.assertEqual(key(one[0]), key(two[0]))

    def test_no_turn_no_alert_and_executor_reading_unchanged(self):
        quiet = self.alerts({HOST: {"sampled_ts": NOW - 30, "runner_id": "publish@%s" % HOST}},
                            max_agents=3, orphans=[])
        self.assertEqual([a["type"] for a in quiet], [])
        runner = self.alerts({HOST: {"sampled_ts": NOW - 30, "runner_id": "runner-a@%s" % HOST}},
                             max_agents=3, orphans=[self.orphan("t")])
        self.assertEqual([a["type"] for a in runner], ["orphan_resource"])

    def test_a_publish_reading_of_another_host_is_not_this_one(self):
        found = self.alerts({HOST: {"sampled_ts": NOW - 30, "runner_id": "publish@%s" % OTHER}},
                            max_agents=3, orphans=[self.orphan("t")])
        self.assertEqual([a["type"] for a in found], ["orphan_resource"])
