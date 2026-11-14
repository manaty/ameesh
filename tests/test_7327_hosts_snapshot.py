# SPDX-License-Identifier: AGPL-3.0-only
"""#7327 : `host_idle.snapshot` lit l'activité d'un hôte demandé, pas la flotte.

Deux preuves : la projection est identique entre la lecture complète par agent
(closed lots inclus) et la lecture agrégée réduite (`open` + `latest_ts`) ; et
un snapshot filtré sur un hôte ne lit que les agents de cet hôte, en une seule
requête agrégée.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import config, host_idle, storage

from . import test_4150_host_idle

NOW = test_4150_host_idle.NOW
DONE = host_idle.DONE


def reduce_activity(rows, *, limit=1000):
    """La lecture agrégée telle que `work.activity` la rend."""
    if len(rows) >= limit:
        open_rows = None
    else:
        open_rows = [{key: lot.get(key) for key in
                      ('id', 'state', 'assignee', 'planned_start', 'wake_ts',
                       'updated_ts')}
                     for lot in rows if lot.get('state') not in DONE]
    return {'latest_updated_ts': max(
        (row['updated_ts'] for row in rows if row.get('updated_ts') is not None),
        default=None), 'open': open_rows}


class SnapshotEquivalenceTest(unittest.TestCase):
    """La projection ne distingue pas la lecture agrégée de la lecture pleine."""

    reading = test_4150_host_idle.HostIdleTest.reading
    agent = test_4150_host_idle.HostIdleTest.agent

    def store(self, rows, *, legacy=False, holds=None):
        store = mock.MagicMock()
        store.operations.listing.return_value = [self.agent()]
        if legacy:
            # L'ancien chemin : toutes les lignes, closed comprises.
            store.work.activity.side_effect = lambda names, limit=1000: {
                name: {'latest_updated_ts': None, 'open': list(rows)}
                for name in names}
        else:
            store.work.activity.side_effect = lambda names, limit=1000: {
                name: reduce_activity(rows) for name in names}
        store.work.items.side_effect = AssertionError('per-agent read must not happen')
        store.progress.lot_hold_events.return_value = (
            [dict(id=1, work_item_id=42, state='build', actor='human:smichea',
                  note='hold: attente owner', created_ts=1000)] if holds else [])
        store.hosts.since.return_value = [self.reading(sampled_ts=t)
                                          for t in range(7600, NOW + 1, 60)]
        return store

    def snapshot(self, rows, *, legacy=False, holds=None):
        store = self.store(rows, legacy=legacy, holds=holds)
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()), \
                mock.patch('ameesh.planned_waits.pilot_holds',
                           return_value=holds or {}):
            return host_idle.snapshot(cfg, 'db', [], {'vm': self.reading()},
                                      now=NOW)['vm']

    def assert_same(self, rows, *, holds=None):
        full = self.snapshot(rows, legacy=True, holds=holds)
        reduced = self.snapshot(rows, holds=holds)
        self.assertEqual(reduced, full)
        return reduced

    def open_lot(self, lot_id, state, **extra):
        return dict(dict(id=lot_id, state=state, assignee='worker',
                         planned_start=None, wake_ts=None, updated_ts=7000),
                    **extra)

    def test_idle_evidence_is_identical_with_closed_lots_in_latest_ts(self):
        rows = [self.open_lot(42, 'build'),
                self.open_lot(43, 'closed', close_reason='abandoned')]
        out = self.assert_same(rows)
        self.assertEqual(out['state'], 'idle')
        self.assertEqual(out['idle_since'], 7600)

    def test_recent_close_still_resets_the_quiet_window(self):
        rows = [self.open_lot(43, 'closed', updated_ts=NOW - 60)]
        out = self.assert_same(rows)
        self.assertEqual(out['state'], 'busy')
        # La fermeture n'est pas dans `open` : seul `latest_ts` la porte.
        self.assertEqual(out['idle_since'], NOW - 60)

    def test_near_wake_and_future_wake_are_identical(self):
        for wake, expected in ((NOW - 1, 'busy'), (NOW + 60, 'busy'),
                               (NOW + 3600, 'idle')):
            with self.subTest(wake=wake):
                rows = [self.open_lot(44, 'blocked', wake_ts=wake)]
                self.assertEqual(self.assert_same(rows)['state'], expected)

    def test_held_session_lot_keeps_the_same_lease_count(self):
        rows = [self.open_lot(42, 'build',
                              assignee='worker', wake_ts=None)]
        store = self.store(rows, holds={42: {'since': 1000}})
        store.operations.listing.return_value = [
            self.agent(session_work_item='42', session_lot_state='build',
                       actionable_lots=1)]
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()), \
                mock.patch('ameesh.planned_waits.pilot_holds',
                           return_value={42: {'since': 1000}}):
            out = host_idle.snapshot(cfg, 'db', [], {'vm': self.reading()},
                                     now=NOW)['vm']
        self.assertEqual(out['leases'], 0)
        self.assertEqual(out['state'], 'idle')


class SnapshotScopeTest(unittest.TestCase):
    """Un hôte demandé ne paie ni les agents ni les lots des autres."""

    reading = test_4150_host_idle.HostIdleTest.reading
    agent = test_4150_host_idle.HostIdleTest.agent

    def store(self):
        store = mock.MagicMock()
        store.operations.listing.return_value = [
            self.agent(name='worker', host='vm'),
            self.agent(name='far1', host='old'),
            self.agent(name='far2', host='old')]
        store.work.activity.side_effect = lambda names, limit=1000: {
            name: {'latest_updated_ts': None, 'open': []} for name in names}
        store.work.items.side_effect = AssertionError('per-agent read must not happen')
        store.hosts.since.return_value = []
        return store

    def test_one_requested_host_reads_one_aggregate_for_its_agents(self):
        store = self.store()
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            out = host_idle.snapshot(
                cfg, 'db', [], {'vm': self.reading(), 'old': self.reading()},
                now=NOW, hosts=['vm'])
        self.assertEqual(list(out), ['vm'])
        store.work.activity.assert_called_once_with(['worker'], limit=1000)
        self.assertEqual([call.args[0] for call in store.hosts.since.call_args_list],
                         ['vm'])

    def test_without_a_filter_the_whole_projected_fleet_is_one_read(self):
        store = self.store()
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            out = host_idle.snapshot(
                cfg, 'db', [], {'vm': self.reading(), 'old': self.reading()},
                now=NOW)
        self.assertEqual(sorted(out), ['old', 'vm'])
        store.work.activity.assert_called_once_with(['worker', 'far1', 'far2'],
                                                    limit=1000)

    def test_capacity_and_guard_do_not_depend_on_the_filter(self):
        # `names` garde tous les hôtes connus : la capacité d'un hôte demandé
        # ne change pas quand la vue se restreint (même élection, même garde).
        store = self.store()
        canon = NS(hosts=[NS(title=h, fiche=NS(data={'capacity': 'baseline'}))
                           for h in ('vm', 'old')])
        role = dict(holder='vm', candidates=[dict(host='old', ready=True)])
        cfg = config.Config(resource_interval=60, baseline_hosts=3)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names',
                           return_value={'pilot'}), \
                mock.patch('ameesh.election.status', return_value=role):
            full = host_idle.snapshot(
                cfg, 'db', [canon], {'vm': self.reading(), 'old': self.reading()},
                now=NOW)
            one = host_idle.snapshot(
                cfg, 'db', [canon], {'vm': self.reading(), 'old': self.reading()},
                now=NOW, hosts=['vm'])
        self.assertEqual(one['vm'], full['vm'])
        self.assertEqual(one['vm']['capacity'], 'baseline')
        self.assertTrue(one['vm']['baseline_guard'])


class HostPartsFilterTest(unittest.TestCase):
    """`mesh_cli._host_parts` filtre l'hôte demandé AVANT le snapshot."""

    reading = test_4150_host_idle.HostIdleTest.reading

    def test_only_host_is_the_snapshot_scope(self):
        from ameesh import mesh_cli

        store = mock.MagicMock()
        store.host_lifecycle.listing.return_value = []
        store.housekeeping.last_measure.return_value = []
        cfg = config.Config(resource_interval=60)
        captured = {}

        def snapshot(cfg_, db_, canons_, readings, **kwargs):
            captured['readings'] = readings
            captured.update(kwargs)
            return {'vm': {'state': 'idle'}}

        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_holds.live', return_value={}), \
                mock.patch('ameesh.resources.current',
                           return_value=[dict(host='vm', **self.reading())]), \
                mock.patch('ameesh.resources.history', return_value=[]), \
                mock.patch('ameesh.host_idle.snapshot', side_effect=snapshot):
            parts = mesh_cli._host_parts(cfg, 'db', [], 'vm', 10)
        self.assertEqual([part['name'] for part in parts], ['vm'])
        self.assertEqual(captured['hosts'], ['vm'])
        self.assertEqual(list(captured['readings']), ['vm'])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
