# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only reserved-capacity warnings: fresh evidence, agents and quiet holds."""
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, config, exploitation, host_capacity_alerts as subject,
                    host_idle, notify, sous_utilisation, storage)

from .support import PgTestCase

NOW = 10000


class CapacityAlertsTest(unittest.TestCase):
    def footprint(self, **extra):
        return dict(dict(state='idle', capacity='burst', idle_since=7000,
                         reserved_cpu=8, reserved_mem_bytes=16000,
                         used_cpu=0.1, used_mem_bytes=1000, active_turns=0, leases=0,
                         runs=0, runs_source='aucune interface de déclaration',
                         cost_eur_h=0.08, unknown=[]), **extra)

    def alerts(self, footprints=None, **kw):
        return subject.project(footprints or {'burst': self.footprint()},
                               [dict(name='worker', host='burst')], now=NOW,
                               orchestrators=['pilot'], **kw)

    def test_idle_burst_names_footprint_price_holders_and_action(self):
        alert, = self.alerts()
        self.assertEqual(alert['type'], 'host_idle_reserved')
        self.assertEqual(alert['host'], 'burst')
        self.assertEqual(alert['agents'], ['worker'])
        self.assertEqual(alert['footprint']['reserved_cpu'], 8)
        for text in ('0.08 EUR/h', 'worker', 'lot leases 0', 'declared runs 0',
                     'aucune interface', 'ameesh host drain burst', '--hold'):
            self.assertIn(text, alert['detail'])
        self.assertEqual(alert['orchestrators'], ['pilot'])

    def test_baseline_held_draining_busy_stale_and_unknown_are_quiet(self):
        for extra in ({'capacity': 'baseline'}, {'state': 'busy'},
                      {'state': 'draining'}, {'state': 'idle-drained'},
                      {'unknown': ['stale host reading']}, {'idle_since': None},
                      {'idle_since': NOW - 1799}, {'idle_since': NOW + 1}):
            with self.subTest(extra=extra):
                self.assertEqual(self.alerts({'burst': self.footprint(**extra)}), [])
        self.assertEqual(self.alerts(held_hosts={'burst'}), [])
        self.assertEqual(self.alerts(draining_hosts={'burst'}), [])

    def test_three_idle_together_name_excess_and_keep_critical_guards(self):
        fps = {'leader': self.footprint(capacity='baseline', baseline_guard=True),
               'reserve': self.footprint(capacity='baseline', baseline_guard=True),
               'extra': self.footprint(capacity='baseline', idle_since=8000)}
        alert, = self.alerts(fps)
        self.assertEqual(alert['type'], 'baseline_oversized')
        self.assertEqual(alert['since'], 8000)
        self.assertEqual(alert['burst_candidates'], ['extra'])
        self.assertEqual(alert['value'], 3)
        self.assertEqual(alert['threshold'], 2)
        self.assertIn('Regroup', alert['detail'])
        self.assertIn('0.08 EUR/h', alert['detail'])
        self.assertEqual(self.alerts(fps, baseline_hosts=3), [])
        self.assertEqual(self.alerts(fps, held_hosts={'extra'}), [])
        self.assertEqual(self.alerts(fps, draining_hosts={'extra'}), [])
        fps['extra']['idle_since'] = NOW - 1700
        self.assertEqual(self.alerts(fps), [])

    def test_keys_stay_stable_when_bounded_idle_window_advances(self):
        alert, = self.alerts()
        moved = dict(alert, since=alert['since'] + 60)
        self.assertEqual(notify.brief_key_of(alert), notify.brief_key_of(moved))
        self.assertNotEqual(notify.key_of(alert), notify.key_of(dict(alert, host='other')))
        group = dict(type='baseline_oversized', hosts=['b', 'a'], since=1)
        self.assertEqual(notify.key_of(group), notify.key_of(dict(group, hosts=['a', 'b'], since=9)))
        self.assertNotEqual(notify.key_of(group), notify.key_of(dict(group, hosts=['a', 'c'])))

    def test_registered_and_briefed_to_pilot_and_auditor_without_owner_push(self):
        listing = [dict(name=n, host='baseline', lease_live=True, status='idle')
                   for n in ('pilot', 'audit', 'worker')]
        for kind in subject.TYPES:
            self.assertIn(kind, exploitation.ALERT_TYPES)
            self.assertIn(kind, sous_utilisation.UNDERUSE_TYPES)
            self.assertIn(kind, alert_briefs.BRIEFED_TYPES)
            self.assertNotIn(kind, notify.DEFAULT_TYPES)
            alert = dict(type=kind, detail='capacity warning', orchestrators=['pilot'])
            briefs = alert_briefs.briefs(alert, listing, auditor_names=['audit'])
            self.assertEqual(set(briefs), {'pilot', 'audit'})
            self.assertTrue(all('capacity warning' in text for text in briefs.values()))

    def test_integration_reads_lifecycle_and_never_drains_or_pushes(self):
        store = mock.MagicMock()
        cfg = config.Config()
        reading = dict(sampled_ts=NOW, turns_in_progress=0, cgroup={'limits_readable': True})
        store.host_lifecycle.listing.return_value = []
        listing = [dict(name='worker', host='burst'), dict(name='busy', host='other')]
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch.object(host_idle, 'snapshot', return_value={'burst': self.footprint()}) as snapshot:
            out = subject.alerts(cfg, 'db', listing, NOW, canons=[],
                                 readings={'burst': reading})
            self.assertEqual([a['type'] for a in out], ['host_idle_reserved'])
            # only the agents of the candidate hosts are read (alert pass cost)
            self.assertEqual(snapshot.call_args.kwargs['listing'],
                             [dict(name='worker', host='burst')])
            snapshot.reset_mock()
            for mode in ('draining', 'returning', 'lost', 'idle-drained'):
                store.host_lifecycle.listing.return_value = [dict(host='burst', mode=mode)]
                self.assertEqual(subject.alerts(cfg, 'db', listing, NOW, canons=[],
                                                readings={'burst': reading}), [])
            snapshot.assert_not_called()
        store.host_lifecycle.drain.assert_not_called()
        store.host_lifecycle.undrain.assert_not_called()
        store.mailbox.send.assert_not_called()

    def test_stale_or_busy_measurements_do_not_read_work_sources(self):
        cfg = config.Config()
        for reading in (dict(sampled_ts=NOW - 181, turns_in_progress=0,
                             cgroup={'limits_readable': True}),
                        dict(sampled_ts=NOW, turns_in_progress=1,
                             cgroup={'limits_readable': True}),
                        dict(sampled_ts=NOW, turns_in_progress=0, cgroup={})):
            with mock.patch.object(storage, 'of') as of, mock.patch.object(host_idle, 'snapshot') as snap:
                self.assertEqual(subject.alerts(cfg, 'db', [], NOW, canons=[],
                                                readings={'burst': reading}), [])
                of.assert_not_called()
                snap.assert_not_called()


class CapacityNotifierTest(PgTestCase):
    def test_agents_receive_once_but_configured_owner_channel_stays_silent(self):
        from ameesh import registry
        for name in ('orch', 'auditeur'):
            registry.upsert(self.db, name, harness='claude', host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name, "
                        "lease_expires_at = now() + interval '1 hour'")
        ncfg = notify.parse_config(dict(default_human='human:owner', default=['desktop']))
        sender = mock.Mock()
        notifier = notify.Notifier(self.cfg, ncfg, sender=sender, log=lambda text: None,
                                   clock=time.time)
        alerts = [dict(type=kind, host='burst', hosts=['a', 'b', 'c'],
                       since=time.time() - 1800, value=1800, threshold=1800,
                       detail='reserved idle capacity', orchestrators=['orch'])
                  for kind in subject.TYPES]
        notifier.run_pass(self.db, current=alerts)
        for alert in alerts:
            alert['since'] += 60  # rolling evidence does not wake the agents twice
        notifier.run_pass(self.db, current=alerts)
        sender.send.assert_not_called()
        self.assertEqual(notifier.state['active'], {})
        for name in ('orch', 'auditeur'):
            rows = self.db.query("SELECT body FROM agent_mailbox WHERE recipient = %s", (name,))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all('reserved idle capacity' in row['body'] for row in rows))
