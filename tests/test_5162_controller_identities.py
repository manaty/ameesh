# SPDX-License-Identifier: AGPL-3.0-only
"""Disposable real clusters: least privileges, recovery and credential isolation."""
import importlib.util
import json
import os
import re
from pathlib import Path
import socket
import time
import struct
import threading
import shutil
import stat
import sys
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


class PrivateInputTest(unittest.TestCase):
    def test_rejects_service_options_nested_uri_and_multiple_endpoints(self):
        for dsn in ("service=ambient", "host=example port=5432 dbname=db user=owner options='-c role=admin'",
                    "host=example port=5432 dbname='postgresql://other@elsewhere/db' user=owner",
                    "host=one,two port=5432 dbname=db user=owner",
                    "host=example hostaddr=127.0.0.1,127.0.0.2 port=5432 dbname=db user=owner"):
            with self.subTest(case=dsn.split()[0]):
                with self.assertRaises(tool.IdentityError):
                    tool.connection(dsn)

    def test_private_socket_without_port_and_tcp_override_refusal(self):
        owner = 'host=/run/ameesh-release-restore dbname=postgres user=ameesh-restore'
        params = tool.connection(tool.login_dsn(owner, 'restore_login', 'test-secret'))
        self.assertEqual(params['host'], tool.RESTORE_SOCKET)
        self.assertNotIn('port', params)
        self.assertEqual(params['require_auth'], 'scram-sha-256')
        with self.assertRaisesRegex(tool.IdentityError, 'TCP hostaddr'):
            tool.connection(owner + ' hostaddr=127.0.0.1')
        with self.assertRaisesRegex(tool.IdentityError, 'explicit endpoint'):
            tool.connection('host=example dbname=postgres user=admin')

    def test_cli_masks_exceptions_and_ignores_ambient_credentials(self):
        secret = uuid.uuid4().hex
        env = dict(os.environ, PGPASSWORD=secret, PGHOST='must-not-be-used.invalid')
        result = subprocess.run([sys.executable, '-I', '-B', str(REPO / 'deploy/postgres/enroler-identites.py'),
                                 'invalid-operation'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(secret not in result.stdout + result.stderr, 'ambient credential escaped')
        self.assertEqual(result.stdout, '')
        self.assertEqual(json.loads(result.stderr),
                         {'error': 'identity_enrollment_refused; private source and intents retained',
                          'reason': 'isolated root enroll|audit|leave required'})

    def test_fake_server_cannot_downgrade_or_collect_a_password(self):
        for method in (3, 5, 0):  # cleartext password, MD5, trust
            captured = []
            secret = uuid.uuid4().hex
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                listener.settimeout(3)
                port = listener.getsockname()[1]
                def fake():
                    with listener.accept()[0] as client:
                        client.settimeout(3)
                        length = struct.unpack('!I', client.recv(4))[0]
                        startup = b''
                        while len(startup) < length - 4:
                            startup += client.recv(length - 4 - len(startup))
                        extra = b'abcd' if method == 5 else b''
                        client.sendall(b'R' + struct.pack('!II', 8 + len(extra), method) + extra
                                       + (b'Z' + struct.pack('!I', 5) + b'I' if method == 0 else b''))
                        try:
                            captured.append(client.recv(4096))
                        except (TimeoutError, ConnectionResetError):
                            captured.append(b'')
                thread = threading.Thread(target=fake, daemon=True)
                thread.start()
                owner = make_conninfo(host='127.0.0.1', port=port, dbname='postgres', user='admin', sslmode='disable')
                dsn = tool.login_dsn(owner, 'restore_login', secret)
                with self.assertRaises(psycopg.OperationalError):
                    psycopg.connect(dsn, connect_timeout=2)
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive(), 'fake server did not finish')
                self.assertEqual(len(captured), 1, 'fake server did not observe client closure')
                self.assertTrue(all(not packet.startswith(b'p') and secret.encode() not in packet
                                    for packet in captured), 'password material sent to a fake server')


class RealIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binaries = Path('/usr/lib/postgresql/16/bin')
        local = (binaries / 'initdb').exists() and os.geteuid() != 0
        if not local and not shutil.which('docker'):
            raise unittest.SkipTest('isolated PostgreSQL cluster tools required')
        cls.temporary = tempfile.TemporaryDirectory(prefix='i5162-', dir=os.environ.get('TMPDIR'))
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.dsns = []
        cls.container_ids = []
        cls.socket_dsns = []
        for index in range(2):
            root = cls.root / str(index)
            root.mkdir(mode=0o700)
            password = root / 'ownerpass'
            password.write_text(uuid.uuid4().hex)
            password.chmod(0o600)
            if not local:
                envfile = root / 'container.env'
                envfile.write_text('POSTGRES_USER=test_admin\nPOSTGRES_PASSWORD=' + password.read_text()
                                   + '\nPOSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256\n')
                envfile.chmod(0o600)
                sockets = root / 's'
                sockets.mkdir(mode=0o700)
                sockets.chmod(0o777)  # private fixture parent, writable only inside the mount
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                result = subprocess.run(['docker', 'run', '-d', '--env-file', str(envfile),
                                         '--network', 'host', '-v', str(sockets) + ':/socket',
                                         'postgres:16', '-c', 'listen_addresses=127.0.0.1',
                                         '-c', 'port=' + str(port), '-c', 'unix_socket_directories=/var/run/postgresql,/socket'],
                                        check=True, capture_output=True, text=True)
                container = result.stdout.strip()
                cls.container_ids.append(container)
                cls.addClassCleanup(subprocess.run, ['docker', 'rm', '-f', '-v', container], capture_output=True)
                dsn = make_conninfo(host='127.0.0.1', port=port, dbname='postgres',
                                   user='test_admin', password=password.read_text(), connect_timeout=2)
                deadline = time.monotonic() + 45
                while True:
                    try:
                        with psycopg.connect(dsn):
                            break
                    except psycopg.OperationalError:
                        if time.monotonic() > deadline:
                            logs = subprocess.run(['docker', 'logs', container], capture_output=True)
                            (root / 'server-private.log').write_bytes(logs.stdout + logs.stderr)
                            raise RuntimeError('disposable PostgreSQL cluster unavailable') from None
                        time.sleep(0.2)
                cls.dsns.append(dsn)
                cls.socket_dsns.append(make_conninfo(dsn, host=str(sockets)))
                continue
            subprocess.run([str(binaries / 'initdb'), '-D', str(root / 'data'), '-U', 'test_admin',
                            '--auth-local=scram-sha-256', '--auth-host=scram-sha-256', '--pwfile', str(password)],
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
            cls.socket_dsns.append(make_conninfo(cls.dsns[-1], host=str(root)))

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
        socket_patch = mock.patch.object(tool, 'RESTORE_SOCKET', tool.connection(self.socket_dsns[1])['host'])
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        self.value = dict(version=1, operation='op_' + self.suffix, schema='mesh',
                          source_owner_dsn=self.source, restore_owner_dsn=self.socket_dsns[1],
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
        with psycopg.connect(source['backup_dsn'], autocommit=True) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mesh.payload').fetchone()[0], 2)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('CREATE DATABASE probe_forbidden')
        self.assertEqual(tool.audit(self.value, self.directory)['outcome'], 'audit_ok')
        for container in self.container_ids:
            result = subprocess.run(['docker', 'logs', container], capture_output=True)
            logs = result.stdout + result.stderr
            for password in source['passwords'].values():
                self.assertTrue(password.encode() not in logs, 'plaintext credential in server output')

    def test_public_extension_reads_only_do_not_relax_other_audits(self):
        with psycopg.connect(self.source) as conn:
            conn.execute('CREATE EXTENSION pg_stat_statements')
        self.enroll()
        self.assertEqual(tool.audit(self.value, self.directory)['outcome'], 'audit_ok')
        with psycopg.connect(self.source) as conn:
            role = sql.Identifier(self.value['controller_role'])
            for statement in (
                sql.SQL('GRANT SELECT ON public.pg_stat_statements TO {}').format(role),
                sql.SQL('GRANT SELECT ON mesh.payload TO PUBLIC'),
                sql.SQL('GRANT UPDATE ON public.pg_stat_statements TO PUBLIC'),
            ):
                conn.execute('SAVEPOINT extra_privilege')
                conn.execute(statement)
                with self.assertRaises(tool.IdentityError):
                    tool.audit_data(conn, self.value['controller_role'], 'mesh',
                                    parent=self.value['group_role'])
                conn.execute('ROLLBACK TO SAVEPOINT extra_privilege')

    def test_unlisted_extension_public_select_is_refused(self):
        with psycopg.connect(self.source) as conn:
            conn.execute('CREATE EXTENSION pg_stat_statements')
            conn.execute('CREATE EXTENSION hstore')
            conn.execute('ALTER EXTENSION hstore ADD TABLE mesh.payload')
            conn.execute('GRANT SELECT ON mesh.payload TO PUBLIC')
        with self.assertRaisesRegex(tool.IdentityError, 'controller read privilege'):
            self.enroll()

    def test_real_dump_and_restore_only_with_restore_identity(self):
        majors = []
        for dsn in self.dsns:
            with psycopg.connect(dsn) as conn:
                majors.append(conn.info.server_version // 10000)
        # Prefer the server's own toolchain over the system pg_wrapper default.
        binaries = Path('/usr/lib/postgresql') / str(majors[0]) / 'bin'
        clients = []
        client_majors = []
        for name in ('pg_dump', 'pg_restore'):
            candidate = binaries / name
            client = str(candidate) if candidate.is_file() else shutil.which(name)
            if client is None:
                self.skipTest(f'{name} unavailable for PostgreSQL servers {majors}')
            result = subprocess.run([client, '--version'], capture_output=True, text=True, check=True)
            match = re.search(r'\(PostgreSQL\) (\d+)\.', result.stdout)
            if match is None:
                self.skipTest(f'{name} version unreadable: {result.stdout.strip()}')
            clients.append(client)
            client_majors.append(int(match.group(1)))
        dump_major, restore_major = client_majors
        if not (majors[0] <= dump_major <= majors[1] and restore_major >= dump_major):
            self.skipTest(f'incompatible PostgreSQL versions: source={majors[0]}, '
                          f'target={majors[1]}, pg_dump={dump_major}, pg_restore={restore_major}')
        self.enroll()
        source = self.source_doc()
        # Feed connection parameters through protected pgpass, not argv passwords.
        helper_spec = importlib.util.spec_from_file_location('privatebackup5162', REPO / 'deploy/mise-a-jour/sauvegarder-prive.py')
        backup = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(backup)
        archive = self.directory / 'dump'
        with backup.private_connection(tool.connection(source['backup_dsn']), self.directory) as (dsn, env):
            result = subprocess.run([clients[0], '-Fc', '--no-password', '--dbname', dsn, '-f', str(archive)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        database = 'probe_' + self.suffix
        with psycopg.connect(source['restore_dsn'], autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(database)))
        params = tool.connection(source['restore_dsn'])
        params['dbname'] = database
        with backup.private_connection(params, self.directory) as (dsn, env):
            result = subprocess.run([clients[1], '--exit-on-error', '--no-owner', '--no-privileges',
                                     '--no-password', '--dbname', dsn, str(archive)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        with psycopg.connect(**params) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mesh.payload').fetchone()[0], 2)
        # The backup identity cannot authenticate on the independent target cluster.
        wrong = dict(params, user=self.value['backup_role'], password=source['passwords']['backup'])
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(**wrong)

    def test_restore_login_uses_private_socket_and_requires_scram(self):
        self.enroll()
        source = self.source_doc()
        parameters = tool.connection(source['restore_dsn'])
        self.assertEqual(parameters['host'], tool.RESTORE_SOCKET)
        self.assertEqual(parameters['require_auth'], 'scram-sha-256')
        self.assertEqual(parameters['port'], tool.connection(self.socket_dsns[1])['port'])
        with psycopg.connect(source['restore_dsn'], autocommit=True) as conn:
            self.assertEqual(conn.execute('SELECT current_user').fetchone()[0], self.value['restore_role'])
            conn.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier('probe_' + self.suffix)))

    def test_refuses_same_cluster_even_different_database(self):
        self.value['restore_owner_dsn'] = self.socket_dsns[0]
        with mock.patch.object(tool, 'RESTORE_SOCKET', tool.connection(self.socket_dsns[0])['host']):
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
