# SPDX-License-Identifier: AGPL-3.0-only
"""#4377 — host_idle.snapshot ne lit que ce que sa vue projette.

Suite de #4375 et #4178 (profil du bureau #159196, 28/10) : `ameesh hosts` et
`doctor` lisaient encore le lot de session de chaque agent du mesh, un par un,
et `critical_names` rechargeait les canons déjà lus pour la vue.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import config, host_idle, host_lifecycle, storage, work
from tests.test_4150_host_idle import HostIdleTest, NOW


class SnapshotScopeTest(unittest.TestCase):
    reading = HostIdleTest.reading
    agent = HostIdleTest.agent

    def _store(self, agents):
        store = mock.MagicMock()
        store.operations.listing.return_value = agents
        store.work.items.return_value = []
        store.work.get.side_effect = lambda ident: dict(
            id=ident, state='build', assignee='x', updated_ts=7000)
        store.progress.lot_hold_events.return_value = []
        store.hosts.since.return_value = []
        return store

    def test_session_lots_outside_the_projected_hosts_are_not_read(self):
        store = self._store([
            self.agent(name='here', host='busy', session_work_item='41',
                       session_lot_state='build'),
            self.agent(name='far', host='elsewhere', session_work_item='42',
                       session_lot_state='build')])
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            out = host_idle.snapshot(cfg, 'db', [], {'busy': self.reading(turns_in_progress=2)},
                                     now=NOW)
        self.assertEqual(list(out), ['busy'])
        self.assertEqual([call.args[0] for call in store.work.get.call_args_list], [41])

    def test_complete_canons_only(self):
        manaty, other = NS(id='manaty'), NS(id='other')
        cfg = mock.Mock()
        with mock.patch('ameesh.canon.configured', return_value=('a', 'b')):
            self.assertEqual(host_idle._complete_canons(cfg, [manaty, other]), [manaty, other])
            # an unreadable canon (skipped by load_configured) or a duplicate id
            self.assertIsNone(host_idle._complete_canons(cfg, [manaty]))
            self.assertIsNone(host_idle._complete_canons(cfg, [manaty, NS(id='manaty')]))
        with mock.patch('ameesh.canon.configured', return_value=()):
            self.assertIsNone(host_idle._complete_canons(cfg, []))
        with mock.patch('ameesh.canon.configured', side_effect=OSError('config')):
            self.assertIsNone(host_idle._complete_canons(cfg, [manaty]))

    def test_snapshot_hands_its_complete_canons_to_critical_names(self):
        store = self._store([self.agent()])
        canons = [NS(id='manaty', hosts=[])]
        cfg = config.Config(resource_interval=60)
        for configured, expected in ((('one',), canons), (('one', 'two'), None)):
            with self.subTest(configured=configured), \
                    mock.patch.object(storage, 'of', return_value=store), \
                    mock.patch('ameesh.canon.configured', return_value=configured), \
                    mock.patch('ameesh.election.status',
                               return_value={'holder': None, 'candidates': []}), \
                    mock.patch('ameesh.host_lifecycle.critical_names',
                               return_value={'pilot'}) as critical:
                host_idle.snapshot(cfg, 'db', canons, {'vm': self.reading()}, now=NOW)
            self.assertEqual(critical.call_args.kwargs.get('canons'), expected)


class CriticalNamesCanonsTest(unittest.TestCase):
    def test_given_canons_are_not_read_again(self):
        cfg = config.Config()
        with mock.patch.object(work, 'configured_canons') as load, \
                mock.patch('ameesh.registry.overview', return_value=[]), \
                mock.patch('ameesh.sous_utilisation.critical_memory', return_value={}):
            host_lifecycle.critical_names(cfg, 'db', canons=[])
            load.assert_not_called()
            host_lifecycle.critical_names(cfg, 'db')
            load.assert_called_once_with(cfg)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
