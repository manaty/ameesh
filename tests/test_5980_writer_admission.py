# SPDX-License-Identifier: AGPL-3.0-only
"""#5980 : admission partagée bornée, contrôles de passe toujours frais."""
from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import config, db as db_mod, migrations


class WriterAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.cache = mock.patch.object(migrations, '_WRITER_ADMISSIONS', {})
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.now = 100.0
        clock = mock.patch.object(migrations.time, 'monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.check = mock.patch.object(migrations, 'incompatible_migrations', return_value=[])
        self.inventory = self.check.start()
        self.addCleanup(self.check.stop)
        self.cfg = config.Config(dsn='postgresql://user@db/database', schema='public')
        self.db = SimpleNamespace(cfg=self.cfg)

    def admit(self, db=None, directory=migrations.MIGRATIONS_DIR):
        migrations.admit_writer_connection(db or self.db, directory)

    def test_success_is_shared_across_connections_and_expires_at_five_seconds(self):
        self.admit()
        self.now += migrations.WRITER_ADMISSION_TTL_S - 0.01
        self.admit(SimpleNamespace(cfg=dataclasses.replace(self.cfg)))
        self.assertEqual(self.inventory.call_count, 1)
        self.now = 100.0 + migrations.WRITER_ADMISSION_TTL_S
        self.admit()
        self.assertEqual(self.inventory.call_count, 2)

    def test_slow_inventory_time_is_included_in_the_bound(self):
        def slow(*args):
            self.now += migrations.WRITER_ADMISSION_TTL_S
            return []
        self.inventory.side_effect = slow
        self.admit()
        self.admit()
        self.assertEqual(self.inventory.call_count, 2)

    def test_tick_detects_new_incompatibility_and_invalidates_admission(self):
        process = SimpleNamespace(db=self.db)
        effects = []
        @migrations.writer_tick
        def tick(process):
            effects.append('write')
        self.admit()
        self.inventory.return_value = [dict(version=9999, name='future', checksum='unknown')]
        with self.assertRaisesRegex(migrations.IncompatibleSchema, '9999_future'):
            tick(process)
        self.assertEqual(effects, [])
        with self.assertRaises(migrations.IncompatibleSchema):
            self.admit()
        self.assertEqual(self.inventory.call_count, 3)

    def test_explicit_transport_failure_invalidates_success(self):
        self.admit()
        self.inventory.side_effect = db_mod.Unavailable('offline')
        with self.assertRaises(db_mod.Unavailable):
            migrations.require_compatible_writer(self.db)
        with self.assertRaises(db_mod.Unavailable):
            self.admit()
        self.assertEqual(self.inventory.call_count, 3)

    def test_successful_explicit_checks_are_never_skipped(self):
        self.admit()
        migrations.require_compatible_writer(self.db)
        migrations.require_compatible_writer(self.db)
        self.admit()
        self.assertEqual(self.inventory.call_count, 3)

    def test_outer_failure_revokes_a_nested_scoped_connection_success(self):
        def inventory(*args):
            if self.inventory.call_count == 1:
                # Une lecture ScopedDb ouvre sa propre connexion admise.
                self.admit()
                raise db_mod.Unavailable('outer read failed')
            return []
        self.inventory.side_effect = inventory
        with self.assertRaises(db_mod.Unavailable):
            migrations.require_compatible_writer(self.db)
        self.admit()
        self.assertEqual(self.inventory.call_count, 3)

    def test_dsn_schema_code_inventory_and_process_are_isolated(self):
        self.admit()
        for cfg in (dataclasses.replace(self.cfg, dsn='postgresql://other@db/database'),
                    dataclasses.replace(self.cfg, schema='other')):
            self.admit(SimpleNamespace(cfg=cfg))
        self.admit(directory='/another/code/inventory')
        with mock.patch.object(migrations.os, 'getpid', return_value=-1):
            self.admit()
        self.assertEqual(self.inventory.call_count, 5)

    def test_writer_false_and_read_only_connections_do_not_check(self):
        self.inventory.side_effect = AssertionError('read-only admission')
        db_mod._writer_connection(self.db, writer=False)
        db_mod._writer_connection(SimpleNamespace(cfg=dataclasses.replace(self.cfg, read_only=True)),
                                  writer=True)

    def test_failed_admission_closes_the_new_connection(self):
        self.inventory.return_value = [dict(version=9999, name='future', checksum='unknown')]
        self.db.close = mock.Mock()
        with self.assertRaises(migrations.IncompatibleSchema):
            db_mod._writer_connection(self.db, writer=True)
        self.db.close.assert_called_once()

    def test_mediated_requests_remain_admitted_by_the_server(self):
        remote = SimpleNamespace(cfg=dataclasses.replace(self.cfg, backend='mediated'))
        self.admit(remote)
        migrations.require_compatible_writer(remote)
        self.inventory.assert_not_called()
