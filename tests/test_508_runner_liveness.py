# SPDX-License-Identifier: AGPL-3.0-only
"""#508 : une preuve du runner vivant, sans bail, bornée et placée."""
from __future__ import annotations

import dataclasses
import threading
from unittest import mock

from tests.support import PgTestCase
from ameesh import config, registry
from ameesh.runner import Runner


class RunnerLivenessTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, 'alpha', host=self.cfg.host)
        registry.upsert(self.db, 'beta', host=self.cfg.host)
        registry.upsert(self.db, 'other', host='elsewhere')
        registry.upsert(self.db, 'external', host=self.cfg.host, mode='externe')
        registry.upsert(self.db, 'stopped', host=self.cfg.host, status='stopped')
        # Le test fait passer sweep dans son chemin de porte fermée, sans
        # lancer les fils et harnais d'un Runner réel.
        self.runner = r = Runner.__new__(Runner)
        r.db, r.cfg = self.db, self.cfg
        r.host, r.runner_id = self.cfg.host, 'host:123'
        r.agents_filter, r.poll, r.dry_run = ['alpha'], 5.0, False
        r.stop, r.power_hold, r.draining = (threading.Event() for _ in range(3))
        r._gate_settled = threading.Event()  # aucune admission possible
        r.lock, r.workers = threading.Lock(), {}
        r.expire_delegations_once = lambda: []

    def test_closed_gate_reports_without_lease_and_preserves_state(self):
        before = registry.get(self.db, 'alpha')
        db_before = self.db.query('SELECT extract(epoch from clock_timestamp())::float8 AS ts')[0]['ts']
        self.runner.sweep()
        after = registry.get(self.db, 'alpha')
        self.assertGreaterEqual(after['runner_seen_ts'], db_before)
        self.assertEqual(after['runner_seen_host'], self.cfg.host)
        self.assertEqual(after['runner_seen_owner'], 'host:123')
        for key in ['lease_owner', 'lease_epoch', 'status', 'last_seen_ts', 'host']:
            self.assertEqual(after[key], before[key], key)
        for name in ['beta', 'other', 'external', 'stopped']:
            self.assertIsNone(registry.get(self.db, name)['runner_seen_ts'], name)
        row = next(row for row in registry.overview(self.db) if row['name'] == 'alpha')
        self.assertEqual(row['runner_seen_ts'], after['runner_seen_ts'])
        self.assertEqual(row['runner_seen_host'], self.cfg.host)
        self.assertEqual(row['runner_seen_owner'], 'host:123')

    def test_frequent_wakes_do_not_write_until_interval(self):
        with mock.patch('ameesh.runner.time.monotonic', return_value=100.0), \
             mock.patch('ameesh.runner.registry.runner_seen') as write:
            self.runner.sweep()
            self.runner.sweep()
            write.assert_called_once_with(self.db, self.cfg.host, 'host:123', ['alpha'])
            with mock.patch('ameesh.runner.time.monotonic', return_value=159.9):
                self.runner.sweep()
            self.assertEqual(write.call_count, 1)
            with mock.patch('ameesh.runner.time.monotonic', return_value=160.0):
                self.runner.sweep()
            self.assertEqual(write.call_count, 2)

    def test_stopped_runner_and_dry_run_do_not_advance(self):
        self.runner.sweep()
        before = registry.get(self.db, 'alpha')['runner_seen_ts']
        self.runner._runner_seen_next = 0
        self.runner.stop.set()
        self.runner.report_liveness()
        self.assertEqual(registry.get(self.db, 'alpha')['runner_seen_ts'], before)
        self.runner.stop.clear()
        self.runner.dry_run = True
        self.runner.report_liveness()
        self.assertEqual(registry.get(self.db, 'alpha')['runner_seen_ts'], before)

    def test_bulk_write_filters_placement_mode_and_stopped(self):
        with mock.patch.object(self.db, 'execute', wraps=self.db.execute) as write:
            registry.runner_seen(self.db, self.cfg.host, 'host:123', None)
        self.assertEqual(write.call_count, 1, 'une instruction pour les deux personas')
        sql = ' '.join(write.call_args.args[0].split())
        self.assertIn('ORDER BY name FOR UPDATE) s', sql)
        self.assertIn('WHERE r.name = s.name', sql)
        for name in ['alpha', 'beta']:
            self.assertIsNotNone(registry.get(self.db, name)['runner_seen_ts'])
        for name in ['other', 'external', 'stopped']:
            self.assertIsNone(registry.get(self.db, name)['runner_seen_ts'])
        registry.upsert(self.db, 'beta', host='elsewhere')
        before = registry.get(self.db, 'beta')['runner_seen_ts']
        registry.runner_seen(self.db, self.cfg.host, 'host:123', ['beta'])
        self.assertEqual(registry.get(self.db, 'beta')['runner_seen_ts'], before)

    def test_empty_filter_writes_nothing(self):
        registry.runner_seen(self.db, self.cfg.host, 'host:123', [])
        self.assertIsNone(registry.get(self.db, 'alpha')['runner_seen_ts'])

    def test_cadence_config_has_a_sixty_second_floor(self):
        with mock.patch.dict('os.environ', {'AMEESH_RUNNER_LIVENESS_INTERVAL': '1'}):
            self.assertEqual(config.load().runner_liveness_interval, 60)
        self.runner.cfg = dataclasses.replace(self.cfg, runner_liveness_interval=120)
        with mock.patch('ameesh.runner.time.monotonic', return_value=100), \
             mock.patch('ameesh.runner.registry.runner_seen'):
            self.runner.report_liveness()
        self.assertEqual(self.runner._runner_seen_next, 220)

    def test_draining_runner_still_reports(self):
        self.runner.draining.set()
        self.runner.sweep()
        self.assertIsNotNone(registry.get(self.db, 'alpha')['runner_seen_ts'])
