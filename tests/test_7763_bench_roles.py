# SPDX-License-Identifier: AGPL-3.0-only
"""Cluster-wide test role cleanup requires a dead owner and no dependencies."""
import subprocess
import sys
import uuid

from ameesh import bench
from ameesh import db as db_mod
from tests.support import PgTestCase


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
