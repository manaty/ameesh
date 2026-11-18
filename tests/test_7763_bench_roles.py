# SPDX-License-Identifier: AGPL-3.0-only
"""Cluster-wide test role cleanup requires a dead owner and no dependencies."""
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from ameesh import bench
from ameesh import db as db_mod
from tests.support import PgTestCase


class LocalDsnTest(unittest.TestCase):
    """#7802 : une adresse locale ne paie jamais une résolution DNS.

    `_bench_roles` et `_bench_satellites` passent par `_local_dsn` à chaque
    classe ; `socket.getfqdn()` peut bloquer des dizaines de secondes sous
    macOS, ce qui faisait dépasser le plafond des parts nocturnes.
    """

    def test_loopback_and_short_host_never_resolve(self):
        with mock.patch.object(bench.socket, "getfqdn",
                               side_effect=AssertionError("DNS lookup")), \
                mock.patch.object(bench.socket, "gethostname",
                                  return_value="bench.local"):
            for dsn in ("",
                        "postgresql://ameesh@127.0.0.1:5432/ameesh",
                        "postgresql://ameesh@localhost/ameesh",
                        "postgresql://ameesh@bench.local/ameesh",
                        "postgresql://ameesh@bench/ameesh"):
                self.assertTrue(bench._local_dsn(dsn), dsn)

    def test_fqdn_is_resolved_once_for_a_remote_shaped_name(self):
        with mock.patch.object(bench.socket, "gethostname",
                               return_value="bench.local"), \
                mock.patch.object(bench, "_local_fqdn",
                                  return_value="bench.example") as fqdn:
            self.assertTrue(bench._local_dsn(
                "postgresql://ameesh@bench.example/ameesh"))
            self.assertFalse(bench._local_dsn(
                "postgresql://ameesh@db.example/ameesh"))
            fqdn.assert_called()

    def test_fqdn_resolution_is_cached(self):
        ancien = bench._LOCAL_FQDN
        bench._LOCAL_FQDN = None
        try:
            with mock.patch.object(bench.socket, "gethostname",
                                   return_value="bench.local"), \
                    mock.patch.object(bench.socket, "getfqdn",
                                      return_value="bench.example") as fqdn:
                for _ in range(3):
                    self.assertTrue(bench._local_dsn(
                        "postgresql://ameesh@bench.example/ameesh"))
                self.assertEqual(fqdn.call_count, 1)
        finally:
            bench._LOCAL_FQDN = ancien


class TestBenchRoles(PgTestCase):
    def test_dead_live_unknown_and_dependency(self):
        names = ['t_vm_probe_' + uuid.uuid4().hex[:10] for _ in range(4)]
        dead, live, unknown, dependent = names
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=10)
        marker = bench.owner_marker(pid=child.pid)
        try:
            for name in names:
                self.db.execute('CREATE ROLE "%s"' % name)
            for name in (dead, dependent):
                self.db.execute('COMMENT ON ROLE "%s" IS \'%s\'' % (name, marker))
            self.db.execute('COMMENT ON ROLE "%s" IS \'%s\'' % (live, bench.owner_marker()))
            self.db.execute('GRANT USAGE ON SCHEMA "%s" TO "%s"' % (self.schema, dependent))
            result = bench.purge_test_roles(self.db, only=set(names))
            self.assertEqual(result.dropped, (dead,))
            self.assertIn((live, 'owner alive'), result.kept)
            self.assertIn((unknown, 'owner unknown'), result.kept)
            self.assertIn((dependent, 'shared dependencies remain'), result.kept)
        finally:
            self.db.execute('REVOKE USAGE ON SCHEMA "%s" FROM "%s"' % (self.schema, dependent))
            for name in names:
                self.db.execute('DROP ROLE IF EXISTS "%s"' % name)

    def test_creation_marks_both_roles(self):
        role = 't_vm_probe_' + uuid.uuid4().hex[:10]
        writer = role + '_writer'
        try:
            bench.create_test_roles(self.db, role, writer)
            rows = self.db.query("SELECT rolname, shobj_description(oid, 'pg_authid') AS comment "
                                 "FROM pg_roles WHERE rolname IN (%s, %s)", (role, writer))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(bench.owner_state(row['comment']) == bench.OWNER_ALIVE for row in rows))
        finally:
            for name in (role, writer):
                self.db.execute('DROP ROLE IF EXISTS "%s"' % name)

    def test_failed_second_creation_rolls_back_first(self):
        role = 't_vm_probe_' + uuid.uuid4().hex[:10]
        writer = role + '_writer'
        self.db.execute('CREATE ROLE "%s"' % writer)
        try:
            with self.assertRaises(db_mod.DbError):
                bench.create_test_roles(self.db, role, writer)
            rows = self.db.query('SELECT rolname FROM pg_roles WHERE rolname IN (%s, %s)',
                                 (role, writer))
            self.assertEqual([row['rolname'] for row in rows], [writer])
        finally:
            self.db.execute('DROP ROLE IF EXISTS "%s"' % writer)
