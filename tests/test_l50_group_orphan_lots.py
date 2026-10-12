# SPDX-License-Identifier: AGPL-3.0-only
"""L50: one orphan notification per agent, reason and responsible human."""
import contextlib
import dataclasses
import json
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

    @contextlib.contextmanager
    def _notifier(self, state=None, sender=None):
        cfg = dataclasses.replace(config.Config(), canon=None, github=None)
        ncfg = notify.parse_config({"default_human": "human:owner", "default": ["desktop"]})
        notifier = notify.Notifier(cfg, ncfg, state=state, sender=sender or notify.DrySender(),
                                   clock=lambda: 1000, log=lambda message: None)
        with patch.object(notify.Router, "resolve", side_effect=lambda a: (
                a.get("responsible") or "human:owner", "lot")), \
             patch.object(notify.Notifier, "_brief_orchestrators"), \
             patch.object(notify.Notifier, "_brief_agents"):
            yield notifier

    def test_retry_uses_current_group_after_membership_changes(self):
        sender = notify.DrySender()
        with self._notifier(sender=sender) as notifier:
            with patch.object(sender, 'send', side_effect=notify.ChannelError('temporary')):
                records = notifier.run_pass(None, [orphan(1)])
                self.assertEqual(records[0]['delivery'], 'echec')
            # JSON roundtrip models a notifier restart with a pending delivery.
            notifier.state = json.loads(json.dumps(notifier.state))
            records = notifier.run_pass(None, [orphan(2)])
            self.assertEqual(len(records), 1)
            self.assertIn('#2 "Work 2"', records[0]['text'])
            self.assertNotIn('#1 "Work 1"', records[0]['text'])
            self.assertEqual(notifier.run_pass(None, [orphan(2), orphan(3)]), [])
            records = notifier.run_pass(None, [])
            self.assertIn('#3 "Work 3"', records[0]['text'])

    def test_upgrade_merges_legacy_deliveries_without_false_events(self):
        state = notify.empty_state()
        for lot, delivered in ((1, False), (2, True)):
            alert = orphan(lot)
            state['active'][notify.key_of(alert)] = {
                'alert': alert, 'raised_ts': 500 + lot, 'human': 'human:owner',
                'source': 'lot', 'status': 'envoi',
                'channels': {'desktop': {'ok': delivered, 'attempts': 1,
                                          'error': None if delivered else 'temporary'}}}
        with self._notifier(state=json.loads(json.dumps(state))) as notifier:
            self.assertEqual(notifier.run_pass(None, [orphan(1), orphan(2)]), [])
            self.assertEqual(len(notifier.state['active']), 1)
            entry = next(iter(notifier.state['active'].values()))
            self.assertEqual(entry['raised_ts'], 501)
            self.assertTrue(entry['channels']['desktop']['ok'])
            self.assertEqual(notifier.sender.sent, [])
            records = notifier.run_pass(None, [])
            self.assertEqual([r['event'] for r in records], ['resolved'])

    def test_upgrade_retries_legacy_failure_with_current_payload(self):
        alert = orphan(1)
        state = notify.empty_state()
        state['active'][notify.key_of(alert)] = {
            'alert': alert, 'raised_ts': 500, 'human': 'human:owner', 'source': 'lot',
            'status': 'envoi', 'channels': {'desktop': {'ok': False, 'attempts': 1, 'error': 'temporary'}}}
        with self._notifier(state=state) as notifier:
            records = notifier.run_pass(None, [orphan(2)])
            self.assertEqual([r['event'] for r in records], ['raised'])
            self.assertIn('#2 "Work 2"', records[0]['text'])
            self.assertNotIn('#1 "Work 1"', records[0]['text'])
            self.assertEqual(next(iter(state['active'].values()))['channels']['desktop']['attempts'], 2)

    def test_render_preserves_five_long_titles_and_more_suffix(self):
        alerts = [orphan(i) for i in range(1, 8)]
        for alert in alerts:
            alert['title'] = str(alert['lot']) * 60
        grouped = notify.group_orphan_lots(alerts, lambda a: ('human:owner', 'lot'))[0]
        message = notify.render(grouped, 'raised', 1000)
        for alert in alerts[:5]:
            self.assertIn('#%s "%s"' % (alert['lot'], alert['title']), message.body)
        self.assertTrue(message.body.endswith('and 2 more'))
        self.assertLessEqual(len(grouped['detail']), notify.GROUPED_DETAIL_LIMIT)

    def test_upgrade_keeps_changed_recipients_separate(self):
        alert = orphan(1)
        state = notify.empty_state()
        state['active'][notify.key_of(alert)] = {
            'alert': alert, 'raised_ts': 500, 'human': 'human:old', 'source': 'lot',
            'status': 'envoi', 'channels': {'desktop': {'ok': True, 'attempts': 1, 'error': None}}}
        with self._notifier(state=state) as notifier:
            records = notifier.run_pass(None, [orphan(1, responsible='human:new')])
            self.assertEqual([(r['event'], r['human']) for r in records],
                             [('resolved', 'human:old'), ('raised', 'human:new')])
