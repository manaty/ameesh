# SPDX-License-Identifier: AGPL-3.0-only
"""L50: one orphan notification per agent, reason and responsible human."""
import dataclasses
import unittest
from unittest.mock import patch

from ameesh import config, notify


def orphan(lot, agent="coordinator", reason="no_turn", **extra):
    return dict(type="orphan_lot", agent=agent, reason=reason, lot=lot,
                title="Work %s" % lot, since=100 + lot, host="host-%s" % lot, **extra)


class GroupOrphanLotsTest(unittest.TestCase):
    def test_grouping_keeps_humans_reasons_and_agents_separate(self):
        alerts = [orphan(i) for i in range(1, 8)] + [
            orphan(9), orphan(3, agent="other"), orphan(4, reason="stopped"),
            dict(type="idle_capacity", agent="worker")]
        original = [dict(alert) for alert in alerts]
        resolve = lambda alert: ("human:b" if alert.get("lot") == 9 else "human:a", "lot")
        result = notify.group_orphan_lots(alerts, resolve)
        groups = [alert for alert in result if alert["type"] == "orphan_lot"]
        self.assertEqual(sorted((a["agent"], a["reason"], a["responsible"], a["value"])
                                for a in groups),
                         [("coordinator", "no_turn", "human:a", 7),
                          ("coordinator", "no_turn", "human:b", 1),
                          ("coordinator", "stopped", "human:a", 1),
                          ("other", "no_turn", "human:a", 1)])
        self.assertEqual(len({notify.key_of(a) for a in groups}), 4)
        group = next(a for a in groups if a["value"] == 7)
        self.assertNotIn("lot", group)
        self.assertNotIn("since", group)
        self.assertNotIn("host", group)
        self.assertIn('7 orphan lot(s) (no_turn): #1 "Work 1"', group["detail"])
        self.assertIn("and 2 more", group["detail"])
        self.assertIs(result[0], alerts[-1])
        self.assertEqual(alerts, original)

    def test_one_raise_until_all_lots_resolve_and_restart_keeps_state(self):
        cfg = dataclasses.replace(config.Config(), canon=None, github=None)
        ncfg = notify.parse_config({"default_human": "human:owner", "default": ["desktop"]})
        sender = notify.DrySender()
        notifier = notify.Notifier(cfg, ncfg, sender=sender, clock=lambda: 1000,
                                   log=lambda message: None)
        with patch.object(notify.Router, "resolve", return_value=("human:owner", "lot")), \
             patch.object(notify.Notifier, "_brief_orchestrators"), \
             patch.object(notify.Notifier, "_brief_agents"):
            records = notifier.run_pass(None, [orphan(i) for i in (3, 9, 52)])
            self.assertEqual([(r["event"], r["type"]) for r in records], [("raised", "orphan_lot")])
            self.assertIn("3 orphan lot(s)", records[0]["text"])
            self.assertEqual(notifier.run_pass(None, [orphan(i) for i in (3, 9, 52, 54)]), [])
            notifier = notify.Notifier(cfg, ncfg, state=notifier.state, sender=sender,
                                       clock=lambda: 1060, log=lambda message: None)
            self.assertEqual(notifier.run_pass(None, [orphan(i) for i in (9, 54)]), [])
            self.assertEqual(len(sender.sent), 1)
            records = notifier.run_pass(None, [])
            self.assertEqual([(r["event"], r["type"]) for r in records], [("resolved", "orphan_lot")])
            self.assertEqual(len(sender.sent), 2)

    def test_two_responsible_humans_receive_separate_notifications(self):
        cfg = dataclasses.replace(config.Config(), canon=None, github=None)
        ncfg = notify.parse_config({"default": ["desktop"]})
        sender = notify.DrySender()
        notifier = notify.Notifier(cfg, ncfg, sender=sender, clock=lambda: 1000,
                                   log=lambda message: None)
        with patch.object(notify.Router, "resolve", side_effect=lambda a: (
                a.get("responsible") or ("human:a" if a["lot"] == 1 else "human:b"), "lot")), \
             patch.object(notify.Notifier, "_brief_orchestrators"), \
             patch.object(notify.Notifier, "_brief_agents"):
            records = notifier.run_pass(None, [orphan(1), orphan(2)])
            self.assertEqual({r["human"] for r in records}, {"human:a", "human:b"})
            self.assertEqual(len(sender.sent), 2)
