# SPDX-License-Identifier: AGPL-3.0-only
"""#5912: cost per host state from the cards, and a journal of the
reserved-capacity alerts (study #5900 §4, §8 C3)."""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import host_capacity_alerts, host_idle, notify

GP1_S = {'calcul_eur_h': 0.19074, 'disque_eur_h': 0.00735, 'ip_eur_h': 0.005,
         'source': 'Scaleway catalog fr-par-1, 05/11/2026'}


def canon(**cards):
    return NS(hosts=[NS(title=n, fiche=NS(data=d)) for n, d in cards.items()])


class CostStatesTest(unittest.TestCase):
    def test_parts_give_on_and_off(self):
        cost = host_idle.cost_states([{'tarif': GP1_S}])
        self.assertAlmostEqual(cost['on'], 0.20309)
        self.assertAlmostEqual(cost['off'], 0.01235)
        # the study's monthly figures: 148.26 on, 9.02 archived
        self.assertAlmostEqual(cost['on'] * 730, 148.26, places=2)
        self.assertAlmostEqual(cost['off'] * 730, 9.02, places=2)
        self.assertIsNone(cost['problem'])

    def test_legacy_price_is_the_running_cost_only(self):
        cost = host_idle.cost_states([{'tarif_eur_h': 0.2}])
        self.assertEqual((cost['on'], cost['off']), (0.2, None))
        self.assertIsNone(host_idle.cost_states([{}]))

    def test_partial_tarif_prices_nothing_and_says_why(self):
        cost = host_idle.cost_states([{'tarif': dict(GP1_S, ip_eur_h=None)}])
        self.assertIsNone(cost['on'])
        self.assertIn('ip_eur_h', cost['problem'])
        cost = host_idle.cost_states([{'tarif': dict(GP1_S, disque_eur_h=-1),
                                       'tarif_eur_h': 0.2}])
        self.assertEqual(cost['on'], 0.2)
        self.assertIn('disque_eur_h', cost['problem'])
        self.assertIn('mapping', host_idle.cost_states([{'tarif': 3}])['problem'])

    def test_capacities_and_line(self):
        caps, costs = host_idle.capacities(
            [canon(a={'capacity': 'baseline', 'tarif': GP1_S},
                   b={'capacity': 'baseline'}, c={'tarif': GP1_S})],
            ['a', 'b', 'c'])
        self.assertEqual(caps, dict(a='baseline', b='baseline', c='burst'))
        self.assertIsNone(costs['b'])
        out = host_idle.project({}, [], [], [], now=0, capacity='burst', cost=costs['c'])
        self.assertAlmostEqual(out['cost_off_eur_h'], 0.01235)
        text = host_idle.line(out)
        self.assertIn('cost on 0.20309 EUR/h (148.26 EUR/month)', text)
        self.assertIn('off 0.01235 EUR/h (9.02 EUR/month)', text)
        self.assertIn('calcul 0.19074, disque 0.00735, ip 0.005', text)
        self.assertIn('source: Scaleway', text)
        self.assertIn('cost unknown', host_idle.line(
            host_idle.project({}, [], [], [], now=0)))


def idle_alert(host, since=100.0):
    return {'type': 'host_idle_reserved', 'host': host, 'since': since,
            'footprint': {'capacity': 'burst', 'cost_eur_h': 0.2, 'cost_off_eur_h': 0.01},
            'detail': 'free text not journaled'}


class JournalTest(unittest.TestCase):
    def run_pass(self, state, current, now=1000.0, **kw):
        written = []
        host_capacity_alerts.journal(state, current, now,
                                     lambda *a: written.append(a), **kw)
        return written

    def test_raised_once_per_episode_then_resolved(self):
        state = {}
        events = self.run_pass(state, [idle_alert('vm'), {'type': 'idle_capacity'}])
        self.assertEqual([e[:2] for e in events],
                         [('alert.raised', 'alert:host_idle_reserved')])
        data = events[0][2]
        self.assertEqual((data['host'], data['cost_off_eur_h'], data['key']),
                         ('vm', 0.01, 'host_idle_reserved:vm'))
        self.assertNotIn('detail', data)
        # A2 moves `since` forward: the same episode, nothing new
        self.assertEqual(self.run_pass(state, [idle_alert('vm', since=160.0)]), [])
        events = self.run_pass(state, [], now=4600.0)
        self.assertEqual(events[0][0], 'alert.resolved')
        self.assertEqual(events[0][2]['raised_ts'], 1000.0)
        self.assertEqual(state, {})

    def test_oversized_baseline_and_types_already_journaled(self):
        alert = {'type': 'baseline_oversized', 'hosts': ['b', 'a', 'c'], 'since': 5.0,
                 'threshold': 2, 'burst_candidates': ['c']}
        state = {}
        data = self.run_pass(state, [alert])[0][2]
        self.assertEqual((data['key'], data['baseline_hosts'], data['burst_candidates']),
                         ('baseline_oversized:a,b,c', 2, ['c']))
        self.assertEqual(self.run_pass({}, [alert],
                                       journaled_elsewhere={'baseline_oversized'}), [])

    def test_failed_write_retries(self):
        state = {}

        def fail(*_a):
            raise RuntimeError('db')
        with self.assertRaises(RuntimeError):
            host_capacity_alerts.journal(state, [idle_alert('vm')], 1.0, fail)
        self.assertEqual(state, {})
        self.assertEqual(len(self.run_pass(state, [idle_alert('vm')])), 1)

    def test_notifier_state_keeps_the_journal(self):
        self.assertEqual(notify.empty_state()['capacity_journal'], {})
        doc = dict(notify.empty_state(), capacity_journal={'k': {'data': {}}})
        self.assertIn('k', notify._state_from_document(doc)['capacity_journal'])

    def test_notifier_pass_writes_events_and_survives_a_failure(self):
        logs = []
        fake = NS(dry_run=False, _delivery_open=lambda: True, state={},
                  ncfg=NS(types=['dead_runner']), log=logs.append)
        with mock.patch('ameesh.events.record') as record:
            notify.Notifier._journal_capacity(fake, object(), [idle_alert('vm')], 10.0)
        kind, obj = record.call_args.args[1:3]
        self.assertEqual((kind, obj), ('alert.raised', 'alert:host_idle_reserved'))
        self.assertEqual(record.call_args.kwargs['host'], 'vm')
        self.assertIn('host_idle_reserved:vm', fake.state['capacity_journal'])
        with mock.patch('ameesh.events.record', side_effect=RuntimeError):
            notify.Notifier._journal_capacity(fake, object(), [], 20.0)
        self.assertIn('not recorded', logs[-1])
        fake.dry_run = True
        with mock.patch('ameesh.events.record') as record:
            notify.Notifier._journal_capacity(fake, object(), [], 30.0)
        record.assert_not_called()


if __name__ == '__main__':
    unittest.main()
