# SPDX-License-Identifier: AGPL-3.0-only
"""Disposable real clusters: least privileges, recovery and credential isolation."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import time
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('identities5162', REPO / 'deploy/postgres/enroler-identites.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class RealIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binaries = Path('/usr/lib/postgresql/16/bin')
        local = (binaries / 'initdb').exists() and os.geteuid() != 0
        if not local and not shutil.which('docker'):
            raise unittest.SkipTest('isolated PostgreSQL cluster tools required')
        cls.temporary = tempfile.TemporaryDirectory(prefix='identity5162-', dir=os.environ.get('TMPDIR'))
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.dsns = []
        cls.container_ids = []
        for index in range(2):
            root = cls.root / str(index)
            root.mkdir(mode=0o700)
            password = root / 'ownerpass'
            password.write_text(uuid.uuid4().hex)
            password.chmod(0o600)
            if not local:
                envfile = root / 'container.env'
                envfile.write_text('POSTGRES_USER=test_admin\nPOSTGRES_PASSWORD=' + password.read_text() + '\n')
                envfile.chmod(0o600)
                result = subprocess.run(['docker', 'run', '-d', '--env-file', str(envfile),
                                         '-p', '127.0.0.1::5432', 'postgres:16'], check=True, capture_output=True, text=True)
                container = result.stdout.strip()
                cls.container_ids.append(container)
                cls.addClassCleanup(subprocess.run, ['docker', 'rm', '-f', '-v', container], capture_output=True)
                ports = subprocess.check_output(['docker', 'inspect', '--format', '{{json .NetworkSettings.Ports}}', container], text=True)
                port = json.loads(ports)['5432/tcp'][0]['HostPort']
                dsn = make_conninfo(host='127.0.0.1', port=port, dbname='postgres',
                                   user='test_admin', password=password.read_text(), connect_timeout=2)
                deadline = time.monotonic() + 45
                while True:
                    try:
                        with psycopg.connect(dsn):
                            break
                    except psycopg.OperationalError:
                        if time.monotonic() > deadline:
                            raise RuntimeError('disposable PostgreSQL cluster unavailable') from None
                        time.sleep(0.2)
                cls.dsns.append(dsn)
                continue
            subprocess.run([str(binaries / 'initdb'), '-D', str(root / 'data'), '-U', 'test_admin',
                            '--auth-local=trust', '--auth-host=scram-sha-256', '--pwfile', str(password)],
                           check=True, capture_output=True)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            subprocess.run([str(binaries / 'pg_ctl'), '-D', str(root / 'data'), '-l', str(root / 'server.log'),
                            '-o', '-h 127.0.0.1 -k ' + str(root) + ' -p ' + str(port), '-w', 'start'],
                           check=True, capture_output=True)
            cls.addClassCleanup(subprocess.run, [str(binaries / 'pg_ctl'), '-D', str(root / 'data'),
                                                 '-m', 'immediate', '-w', 'stop'], capture_output=True)
            cls.dsns.append(make_conninfo(host='127.0.0.1', port=port, dbname='postgres',
                                          user='test_admin', password=password.read_text()))

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.directory = self.root / self.suffix
        self.directory.mkdir(mode=0o700)
        # The disposable cluster root is the trust boundary of this fixture;
        # production checks continue all the way to /. The host TMPDIR itself
        # may have group-writable ancestors, which tests must never chmod.
        def protected_fixture(path, directory=False):
            path = Path(path)
            for parent in (path, *path.parents):
                info = parent.lstat()
                tool.require(not parent.is_symlink() and info.st_uid == os.geteuid()
                             and not info.st_mode & 0o022, 'unsafe fixture source')
                if parent == self.root:
                    break
            info = path.lstat()
            tool.require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600), 'unsafe fixture mode')
            return path
        patcher = mock.patch.object(tool, 'protected', side_effect=protected_fixture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.database = 'live_' + self.suffix
        with psycopg.connect(self.dsns[0], autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.database)))
        self.addCleanup(self.cleanup)
        self.source = make_conninfo(self.dsns[0], dbname=self.database)
        with psycopg.connect(self.source) as conn:
            conn.execute(sql.SQL('REVOKE TEMP ON DATABASE {} FROM PUBLIC').format(sql.Identifier(self.database)))
            conn.execute('CREATE SCHEMA mesh')
            for table in ('agent_registry', 'agent_mailbox', 'mesh_approvals', 'mesh_consumed_nonces',
                          'turn_resources', 'spend_pending', 'host_release_operations', 'host_release_fences',
                          'host_release_fence_receipts', 'decision_supersessions', 'schema_non_additive_migrations'):
                conn.execute(sql.SQL('CREATE TABLE mesh.{} (id integer, updated_at timestamptz)').format(sql.Identifier(table)))
            for view in ('mesh_approvals_status', 'mesh_keys'):
                conn.execute(sql.SQL('CREATE VIEW mesh.{} AS SELECT id FROM mesh.mesh_approvals').format(sql.Identifier(view)))
            conn.execute("CREATE TABLE mesh.schema_migrations(version text, checksum text)")
            conn.execute("INSERT INTO mesh.schema_migrations VALUES ('1','synthetic')")
            conn.execute('CREATE TABLE mesh.payload (id serial, value text)')
            conn.execute("INSERT INTO mesh.payload(value) VALUES ('one'),('two')")
        self.value = dict(version=1, operation='op_' + self.suffix, schema='mesh',
                          source_owner_dsn=self.source, restore_owner_dsn=self.dsns[1],
                          controller_role='ctl_' + self.suffix, backup_role='bak_' + self.suffix,
                          restore_role='rst_' + self.suffix, group_role='grp_' + self.suffix)

    def cleanup(self):
        with psycopg.connect(self.dsns[0], autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.database)))
            for key in ('controller_role', 'backup_role', 'group_role'):
                conn.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(self.value[key])))
        with psycopg.connect(self.dsns[1], autocommit=True) as conn:
            for name, in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'probe_%'"):
                conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            conn.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(self.value['restore_role'])))

    def enroll(self):
        return tool.enroll(self.value, self.directory)

    def source_doc(self):
        return json.loads((self.directory / 'credentials.json').read_text())

    def test_real_identity_contract_idempotency_and_no_output_credentials(self):
        first = self.enroll()
        before = (self.directory / 'credentials.json').read_bytes()
        self.assertEqual(self.enroll(), first)
        self.assertEqual((self.directory / 'credentials.json').read_bytes(), before)
        self.assertEqual((self.directory / 'credentials.json').stat().st_mode & 0o777, 0o600)
        source = self.source_doc()
        for password in source['passwords'].values():
            self.assertTrue(password not in json.dumps(first), 'plaintext credential in public result')
        with psycopg.connect(source['database']['dsn']) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mesh.agent_registry').fetchone()[0], 0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM mesh.payload")
        with psycopg.connect(source['backup_dsn']) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mesh.payload').fetchone()[0], 2)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('CREATE DATABASE probe_forbidden')
        self.assertEqual(tool.audit(self.value, self.directory)['outcome'], 'audit_ok')
        for container in self.container_ids:
            result = subprocess.run(['docker', 'logs', container], capture_output=True)
            logs = result.stdout + result.stderr
            for password in source['passwords'].values():
                self.assertTrue(password.encode() not in logs, 'plaintext credential in server output')

    def test_real_dump_and_restore_only_with_restore_identity(self):
        self.enroll()
        source = self.source_doc()
        # Feed connection parameters through protected pgpass, not argv passwords.
        helper_spec = importlib.util.spec_from_file_location('privatebackup5162', REPO / 'deploy/mise-a-jour/sauvegarder-prive.py')
        backup = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(backup)
        archive = self.directory / 'dump'
        with backup.private_connection(tool.connection(source['backup_dsn']), self.directory) as (dsn, env):
            result = subprocess.run(['/usr/bin/pg_dump', '-Fc', '--no-password', '--dbname', dsn, '-f', str(archive)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        database = 'probe_' + self.suffix
        with psycopg.connect(source['restore_dsn'], autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(database)))
        params = tool.connection(source['restore_dsn'])
        params['dbname'] = database
        with backup.private_connection(params, self.directory) as (dsn, env):
            result = subprocess.run(['/usr/bin/pg_restore', '--exit-on-error', '--no-owner', '--no-privileges',
                                     '--no-password', '--dbname', dsn, str(archive)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        with psycopg.connect(**params) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mesh.payload').fetchone()[0], 2)
        # The backup identity cannot authenticate on the independent target cluster.
        wrong = dict(params, user=self.value['backup_role'], password=source['passwords']['backup'])
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(**wrong)

    def test_refuses_same_cluster_even_different_database(self):
        self.value['restore_owner_dsn'] = self.dsns[0]
        with self.assertRaisesRegex(tool.IdentityError, 'distinct PostgreSQL cluster'):
            self.enroll()
        self.assertFalse((self.directory / 'credentials.json').exists())

    def test_refuses_unowned_existing_login_without_modifying_it(self):
        with psycopg.connect(self.source) as conn:
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN').format(sql.Identifier(self.value['controller_role'])))
        with self.assertRaisesRegex(tool.IdentityError, 'already exists without source'):
            self.enroll()

    def test_refuses_extra_direct_table_column_membership_and_admin_rights(self):
        self.enroll()
        grants = [
            sql.SQL('GRANT UPDATE ON mesh.payload TO {}'),
            sql.SQL('GRANT UPDATE(value) ON mesh.payload TO {}'),
            sql.SQL('GRANT {} TO {}'),
            sql.SQL('ALTER ROLE {} CREATEROLE'),
        ]
        with psycopg.connect(self.source) as conn:
            role = sql.Identifier(self.value['backup_role'])
            for index, grant in enumerate(grants):
                conn.execute('SAVEPOINT probe')
                conn.execute(grant.format(sql.Identifier(self.value['group_role']), role) if index == 2 else grant.format(role))
                with self.assertRaises(tool.IdentityError):
                    tool.audit_data(conn, self.value['backup_role'], 'mesh')
                conn.execute('ROLLBACK TO SAVEPOINT probe')

    def test_refuses_public_write_security_definer_and_row_security(self):
        self.enroll()
        statements = ["GRANT INSERT ON mesh.payload TO PUBLIC",
                      "CREATE FUNCTION mesh.escalate() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
                      "ALTER TABLE mesh.payload ENABLE ROW LEVEL SECURITY"]
        with psycopg.connect(self.source) as conn:
            for statement in statements:
                conn.execute('SAVEPOINT probe')
                conn.execute(statement)
                with self.assertRaises(tool.IdentityError):
                    tool.audit_data(conn, self.value['backup_role'], 'mesh')
                conn.execute('ROLLBACK TO SAVEPOINT probe')

    def test_resume_after_lost_source_commit_uses_same_passwords(self):
        with mock.patch.object(tool, 'audit_restore', side_effect=tool.IdentityError('lost restore reply')):
            with self.assertRaises(tool.IdentityError):
                self.enroll()
        before = (self.directory / 'credentials.json').read_bytes()
        self.enroll()
        self.assertEqual((self.directory / 'credentials.json').read_bytes(), before)

    def test_leave_is_idempotent_keeps_shared_group_and_private_source(self):
        self.enroll()
        self.assertEqual(tool.leave(self.value, self.directory)['outcome'], 'retired')
        self.assertEqual(tool.leave(self.value, self.directory)['outcome'], 'retired')
        self.assertTrue((self.directory / 'credentials.json').exists())
        with psycopg.connect(self.source) as conn:
            self.assertTrue(conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (self.value['group_role'],)).fetchone())
        with self.assertRaisesRegex(tool.IdentityError, 'retired'):
            self.enroll()

    def test_private_source_changed_or_insecure_is_refused(self):
        self.enroll()
        self.value['operation'] += '_changed'
        with self.assertRaisesRegex(tool.IdentityError, 'input changed'):
            self.enroll()
        path = self.directory / 'credentials.json'
        path.chmod(0o644)
        with self.assertRaises(tool.IdentityError):
            tool.read(path)


if __name__ == '__main__':
    unittest.main()
