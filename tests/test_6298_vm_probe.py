# SPDX-License-Identifier: AGPL-3.0-only
"""#6298: real SQL privileges, bounded deposit and additive migration drift."""
from pathlib import Path
import os
import dataclasses
import json
import secrets
import urllib.parse
from ameesh import db as db_mod
import subprocess
import uuid

from tests.support import PgTestCase, TEST_DSN
from tests.test_6276_vm_remote import remote, config, NONCE
from tests.role_acl import serialize_role_acl

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / 'deploy/sql'


def install_for_native_test(cls):
    """Install the real endpoint dependency; roles are local to this schema."""
    serialize_role_acl(cls)
    role = 't_vm_native_' + uuid.uuid4().hex[:10]
    writer = role + '_writer'
    result = subprocess.run(['psql', '-X', '-w', TEST_DSN, '-q',
        '-v', 'ON_ERROR_STOP=1', '-v', 'role=' + role,
        '-v', 'writer=' + writer, '-f', str(SQL / 'role-vm-probe.sql')],
        env=dict(os.environ, PGOPTIONS='-c search_path=' + cls.schema),
        capture_output=True, text=True, timeout=60)
    # Use a fresh administrative connection: class cleanups happen after the
    # PgTestCase connection has closed and its schema has been dropped.
    def cleanup():
        query = ''.join('DROP OWNED BY "' + r + '" CASCADE; DROP ROLE "' + r + '";'
                        for r in (role, writer))
        subprocess.run(['psql', '-X', '-w', TEST_DSN, '-q', '-c', query],
                       capture_output=True, text=True, timeout=30, check=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    cls.addClassCleanup(cleanup)


class VmProbeRoleTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        serialize_role_acl(cls)
        cls.role = 't_vm_probe_' + uuid.uuid4().hex[:10]
        cls.writer = cls.role + '_writer'
        cls.addClassCleanup(cls.drop_roles)
        result = cls.script('role-vm-probe.sql')
        if result.returncode:
            raise AssertionError(result.stderr)

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM turn_resources")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.drop_roles()
        finally:
            super().tearDownClass()

    @classmethod
    def drop_roles(cls):
        if cls.__dict__.get("_schema_dropped", False):
            return
        for role in (cls.role, cls.writer):
            if not cls.db.query("SELECT FROM pg_roles WHERE rolname=%s", (role,)):
                continue
            cls.db.execute('DROP OWNED BY "' + role + '" CASCADE')
            cls.db.execute('DROP ROLE "' + role + '"')

    @classmethod
    def script(cls, name):
        return subprocess.run(['psql', '-X', '-w', TEST_DSN, '-q',
            '-v', 'ON_ERROR_STOP=1', '-v', 'role=' + cls.role,
            '-v', 'writer=' + cls.writer, '-f', str(SQL / name)],
            env=dict(os.environ, PGOPTIONS='-c search_path=' + cls.schema),
            capture_output=True, text=True, timeout=60)

    def role_sql(self, sql):
        return subprocess.run(['psql', '-X', '-w', TEST_DSN, '-Atq',
            '-v', 'ON_ERROR_STOP=1'], input='SET ROLE "' + self.role + '";\n'
            + 'SET search_path="' + self.schema + '";\n' + sql,
            capture_output=True, text=True, timeout=20)

    def audit(self):
        result = self.script('_contrat-vm-probe.sql')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_idempotent_and_additive_migration_keeps_new_column_closed(self):
        self.assertEqual(self.script('role-vm-probe.sql').returncode, 0)
        self.db.execute('ALTER TABLE agent_registry ADD COLUMN vm_probe_private_test text')
        try:
            self.audit()
            self.assertNotEqual(self.role_sql('SELECT vm_probe_private_test FROM agent_registry;').returncode, 0)
        finally:
            self.db.execute('ALTER TABLE agent_registry DROP COLUMN vm_probe_private_test')

    def test_expected_facts_readable_and_free_mail_insert_refused(self):
        self.assertEqual(self.role_sql('SELECT host,mode FROM host_lifecycle;').returncode, 0)
        for sql in ('SELECT pending_prompt FROM agent_registry;',
                    'SELECT signature FROM agent_mailbox;',
                    "INSERT INTO agent_mailbox(sender,recipient,body) VALUES ('x','x','x');",
                    'UPDATE host_lifecycle SET claims_open=true;',
                    "SELECT nextval('agent_mailbox_id_seq');",
                    'CREATE TABLE forbidden(x int);'):
            with self.subTest(sql=sql):
                self.assertNotEqual(self.role_sql(sql).returncode, 0)

    def test_extra_direct_public_parent_and_default_rights_refused(self):
        mutations = [
            ('GRANT SELECT(name) ON agent_registry TO PUBLIC',
             'REVOKE SELECT(name) ON agent_registry FROM PUBLIC'),
            ('GRANT SELECT(pending_prompt) ON agent_registry TO "' + self.role + '"',
             'REVOKE SELECT(pending_prompt) ON agent_registry FROM "' + self.role + '"'),
            ('GRANT SELECT(pending_prompt) ON agent_registry TO PUBLIC',
             'REVOKE SELECT(pending_prompt) ON agent_registry FROM PUBLIC'),
            ('GRANT pg_read_all_data TO "' + self.role + '"',
             'REVOKE pg_read_all_data FROM "' + self.role + '"'),
            ('ALTER DEFAULT PRIVILEGES IN SCHEMA "' + self.schema + '" GRANT SELECT ON TABLES TO PUBLIC',
             'ALTER DEFAULT PRIVILEGES IN SCHEMA "' + self.schema + '" REVOKE SELECT ON TABLES FROM PUBLIC'),
            ('GRANT INSERT(body) ON agent_mailbox TO "' + self.role + '"',
             'REVOKE INSERT(body) ON agent_mailbox FROM "' + self.role + '"'),
        ]
        if int(self.db.query("SELECT current_setting('server_version_num') AS n")[0]['n']) >= 170000:
            mutations.append(('GRANT MAINTAIN ON agent_mailbox TO "' + self.role + '"',
                              'REVOKE MAINTAIN ON agent_mailbox FROM "' + self.role + '"'))
        for apply, undo in mutations:
            with self.subTest(apply=apply):
                self.db.execute(apply)
                try:
                    result = self.script('_contrat-vm-probe.sql')
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn('contract refused', result.stderr)
                    # Reinstallation must not conceal an unexpected grant.
                    self.assertNotEqual(self.script('role-vm-probe.sql').returncode, 0)
                finally:
                    self.db.execute(undo)
                self.audit()

    def test_diagnostics_usage_refused_even_without_table_rights(self):
        ns = self.schema + '_diagnostics'
        self.db.execute('CREATE SCHEMA "' + ns + '"')
        try:
            self.db.execute('GRANT USAGE ON SCHEMA "' + ns + '" TO PUBLIC')
            self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
        finally:
            self.db.execute('DROP SCHEMA "' + ns + '"')
        self.audit()

    def test_public_extension_view_is_inert_only_without_schema_usage(self):
        ns = self.schema + '_closed_extension'
        self.db.execute('CREATE SCHEMA "' + ns + '"')
        try:
            self.db.execute('CREATE VIEW "' + ns + '".extension_stats AS SELECT 1 AS calls')
            self.db.execute('GRANT SELECT ON "' + ns + '".extension_stats TO PUBLIC')
            self.audit()
            self.assertEqual(self.script('role-vm-probe.sql').returncode, 0)
            self.assertNotEqual(self.role_sql('SELECT * FROM "' + ns + '".extension_stats;').returncode, 0)
            # A latent direct grant remains a drift, even behind the wall.
            self.db.execute('GRANT SELECT(calls) ON "' + ns + '".extension_stats TO "' + self.role + '"')
            self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
            self.db.execute('REVOKE SELECT(calls) ON "' + ns + '".extension_stats FROM "' + self.role + '"')
            for grantee in ('PUBLIC', '"' + self.role + '"'):
                self.db.execute('GRANT USAGE ON SCHEMA "' + ns + '" TO ' + grantee)
                try:
                    self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
                    self.assertNotEqual(self.script('role-vm-probe.sql').returncode, 0)
                finally:
                    self.db.execute('REVOKE USAGE ON SCHEMA "' + ns + '" FROM ' + grantee)
            self.audit()
        finally:
            self.db.execute('DROP SCHEMA "' + ns + '" CASCADE')

    def test_definer_public_execution_is_refused(self):
        signature = 'vm_probe_send(text,text,bigint,bigint,text)'
        self.db.execute('GRANT EXECUTE ON FUNCTION ' + signature + ' TO PUBLIC')
        try:
            self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
        finally:
            self.db.execute('REVOKE EXECUTE ON FUNCTION ' + signature + ' FROM PUBLIC')
        self.audit()

    def admit(self):
        # Native journal shape used by the endpoint, no cloud or runners.
        self.db.execute("INSERT INTO agent_registry(name,host,mode) VALUES ('burst-probe','burst','execute')")
        self.db.execute("INSERT INTO host_lifecycle(host,mode,claims_open,generation,release_epoch,return_probe,actor) "
                        "VALUES ('burst','returning',false,1,2,'burst-probe','test')")
        self.db.execute("INSERT INTO vm_adapter_requests(host,generation,release_epoch,state,status,result) "
                        "VALUES ('burst',1,2,'busy','pending','probe admitted')")

    def send(self, host='burst', agent='burst-probe', generation=1, epoch=2, nonce=NONCE):
        return self.role_sql("BEGIN;\n"
            + "SELECT request_id FROM vm_probe_send('%s','%s',%d,%d,'%s');\nCOMMIT;\n"
            % (host, agent, generation, epoch, nonce))

    def test_bounded_message_replay_and_foreign_inputs(self):
        self.admit()
        result = self.send()
        self.assertEqual(result.returncode, 0, result.stderr)
        request = int(result.stdout.strip())
        row = self.db.query('SELECT * FROM agent_mailbox WHERE id=%s', (request,))[0]
        self.assertEqual((row['sender'], row['recipient'], row['kind']),
                         ('vm-adapter', 'burst-probe', 'request'))
        self.assertIn('VM-PROBE-ACK ' + NONCE, row['body'])
        self.assertEqual(row['payload']['vm_probe']['release_epoch'], 2)
        self.assertEqual(self.send().stdout, result.stdout)
        for kw in ({'host': 'other'}, {'agent': 'other'}, {'generation': 2},
                   {'epoch': 1}, {'nonce': 'bad'}, {'nonce': 'b' * 32}):
            with self.subTest(kw=kw):
                self.assertNotEqual(self.send(**kw).returncode, 0)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox WHERE sender='vm-adapter'")[0]['n'], 1)
        self.db.execute("UPDATE host_lifecycle SET claims_open=true WHERE host='burst'")
        self.assertNotEqual(self.send().returncode, 0)

    def test_send_refuses_read_only_transaction(self):
        self.admit()
        result = self.role_sql("BEGIN READ ONLY; SELECT * FROM vm_probe_send('burst','burst-probe',1,2,'" + NONCE + "'); COMMIT;")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.db.query("SELECT FROM agent_mailbox WHERE sender='vm-adapter'"))

    def test_temporary_shadowing_session_is_refused(self):
        self.admit()
        result = self.role_sql("CREATE TEMP TABLE agent_registry(name text);\n"
            "SELECT * FROM vm_probe_send('burst','burst-probe',1,2,'" + NONCE + "');")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('clean session', result.stderr)

    def receipt(self, request, host='burst', agent='burst-probe', generation=1, epoch=2, nonce=NONCE):
        # Inputs are fixed fixture literals, never external text.
        return self.role_sql("SELECT row_to_json(r) FROM vm_probe_receipt('%s','%s',%d,%d,'%s',%d) r;"
                             % (host, agent, generation, epoch, nonce, request))

    def test_mailbox_is_unreadable_and_old_column_grants_are_revoked_idempotently(self):
        for target in ('agent_mailbox', '(id,body,meta,payload) ON agent_mailbox'):
            grant = ('GRANT SELECT ON ' if target == 'agent_mailbox' else 'GRANT SELECT ') + target
            self.db.execute(grant + ' TO "' + self.role + '"')
            self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
            install = self.script('role-vm-probe.sql')
            self.assertEqual(install.returncode, 0, install.stderr)
            for col in ('id', 'body', 'meta', 'payload', 'sender', 'delivered_at'):
                with self.subTest(column=col):
                    self.assertNotEqual(self.role_sql('SELECT ' + col + ' FROM agent_mailbox').returncode, 0)
        self.audit()

    def test_other_login_cannot_gain_private_mail_even_through_set_role(self):
        other = self.role + '_other'
        group = self.role + '_group'
        self.db.execute('CREATE ROLE "' + other + '" LOGIN NOINHERIT')
        self.db.execute('CREATE ROLE "' + group + '" NOLOGIN')
        try:
            self.db.execute('GRANT "' + group + '" TO "' + other + '"')
            for grantee in (other, group):
                self.db.execute('GRANT SELECT(body) ON agent_mailbox TO "' + grantee + '"')
                try:
                    result = self.script('_contrat-vm-probe.sql')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('private mailbox readable', result.stderr)
                finally:
                    self.db.execute('REVOKE SELECT(body) ON agent_mailbox FROM "' + grantee + '"')
            self.audit()
        finally:
            self.db.execute('DROP OWNED BY "' + other + '","' + group + '" CASCADE')
            self.db.execute('DROP ROLE "' + other + '","' + group + '"')

    def test_receipt_matches_only_deposited_probe_and_never_returns_message_content(self):
        self.admit()
        sent = self.send()
        self.assertEqual(sent.returncode, 0, sent.stderr)
        request = int(sent.stdout.strip())
        proof = self.receipt(request)
        self.assertEqual(proof.returncode, 0, proof.stderr)
        self.assertEqual(json.loads(proof.stdout), dict(delivered=False, turn_id=None, acknowledged=False))
        # A row outside the bounded deposit, even addressed to this probe,
        # must remain invisible to the connection and the definer.
        row = self.db.query("INSERT INTO agent_mailbox(sender,recipient,body,host,payload) "
                            "VALUES ('human:owner','burst-probe','private-content','burst',"
                            "'{\"private\":\"secret-value\"}') RETURNING id")[0]
        self.assertEqual(self.receipt(row['id']).stdout.strip(), '')
        for kwargs in ({'host': 'other'}, {'agent': 'other'}, {'generation': 2},
                       {'epoch': 3}, {'nonce': 'b' * 32}, {'nonce': 'invalid'}):
            with self.subTest(kwargs=kwargs):
                foreign = self.receipt(request, **kwargs)
                self.assertEqual(foreign.returncode, 0, foreign.stderr)
                self.assertEqual(foreign.stdout.strip(), '')
        self.db.execute("UPDATE agent_mailbox SET meta='{\"retire\":true}' WHERE id=%s", (request,))
        self.assertEqual(self.receipt(request).stdout.strip(), '')

    def test_previous_writer_delivery_and_ack_need_no_new_function_or_columns(self):
        self.admit()
        sent = self.send()
        self.assertEqual(sent.returncode, 0, sent.stderr)
        request = int(sent.stdout.strip())
        # These are the N-1 writer's ordinary statements. No new schema
        # column or writer API is needed to satisfy the new receipt reader.
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status) "
                        "VALUES ('old-writer-turn','burst-probe','burst','running')")
        self.db.execute("UPDATE agent_mailbox SET delivered_at=clock_timestamp(),status='delivered',"
                        "meta='{\"consigne\":{\"turn\":\"old-writer-turn\"}}' WHERE id=%s", (request,))
        before = json.loads(self.receipt(request).stdout)
        self.assertEqual(before, dict(delivered=True, turn_id='old-writer-turn', acknowledged=False))
        for sender, recipient, host, body, payload in (
                ('burst-probe', 'burst-probe', 'other', 'VM-PROBE-ACK ' + NONCE, '{"ack":true}'),
                ('other', 'burst-probe', 'burst', 'VM-PROBE-ACK ' + NONCE, '{"ack":true}'),
                ('burst-probe', 'other', 'burst', 'VM-PROBE-ACK ' + NONCE, '{"ack":true}'),
                ('burst-probe', 'burst-probe', 'burst', 'VM-PROBE-ACK ' + 'b' * 32, '{"ack":true}'),
                ('burst-probe', 'burst-probe', 'burst', 'VM-PROBE-ACK ' + NONCE, '{}')):
            self.db.execute("INSERT INTO agent_mailbox(sender,recipient,body,host,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
                            (sender, recipient, body, host, payload))
        self.assertFalse(json.loads(self.receipt(request).stdout)['acknowledged'])
        self.db.execute("INSERT INTO agent_mailbox(sender,recipient,body,host,payload) VALUES "
                        "('burst-probe','burst-probe',%s,'burst','{\"ack\":true}')", ('VM-PROBE-ACK ' + NONCE,))
        self.db.execute("UPDATE turn_resources SET status='done',ended_at=clock_timestamp() "
                        "WHERE turn_id='old-writer-turn'")
        self.assertEqual(json.loads(self.receipt(request).stdout),
                         dict(delivered=True, turn_id='old-writer-turn', acknowledged=True))
        self.db.execute("UPDATE host_lifecycle SET generation=2 WHERE host='burst'")
        self.assertEqual(self.receipt(request).stdout.strip(), '')

    def test_receipt_ack_is_bounded_by_delivery_and_turn_end(self):
        self.admit()
        sent = self.send()
        self.assertEqual(sent.returncode, 0, sent.stderr)
        request = int(sent.stdout.strip())
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status,ended_at) "
                        "VALUES ('window-turn','burst-probe','burst','done',clock_timestamp()-interval '1 minute')")
        self.db.execute("UPDATE agent_mailbox SET delivered_at=clock_timestamp()-interval '2 minutes',"
                        "meta='{\"consigne\":{\"turn\":\"window-turn\"}}' WHERE id=%s", (request,))
        self.db.execute("INSERT INTO agent_mailbox(sender,recipient,body,host,payload,created_at) VALUES "
                        "('burst-probe','burst-probe',%s,'burst','{\"ack\":true}',clock_timestamp()-interval '3 minutes'),"
                        "('burst-probe','burst-probe',%s,'burst','{\"ack\":true}',clock_timestamp())",
                        ('VM-PROBE-ACK ' + NONCE, 'VM-PROBE-ACK ' + NONCE))
        self.assertFalse(json.loads(self.receipt(request).stdout)['acknowledged'])

    def test_receipt_definer_public_execution_and_body_drift_refused(self):
        signature = 'vm_probe_receipt(text,text,bigint,bigint,text,bigint)'
        definition = self.db.query('SELECT pg_get_functiondef(%s::regprocedure) AS definition', (signature,))[0]['definition']
        for grant, revoke in (('GRANT EXECUTE ON FUNCTION ' + signature + ' TO PUBLIC',
                               'REVOKE EXECUTE ON FUNCTION ' + signature + ' FROM PUBLIC'),):
            self.db.execute(grant)
            try:
                self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
            finally:
                self.db.execute(revoke)
        self.db.execute(definition.replace('RETURN QUERY', 'PERFORM 1; RETURN QUERY'))
        try:
            self.assertNotEqual(self.script('_contrat-vm-probe.sql').returncode, 0)
        finally:
            self.db.execute(definition)
        self.audit()

    def test_real_login_runs_native_receipt_and_reconciliation(self):
        self.admit()
        password = secrets.token_hex(24)
        self.db.execute('SET password_encryption=\'scram-sha-256\'')
        self.db.execute('ALTER ROLE "' + self.role + '" PASSWORD \'' + password + "'")
        parts = urllib.parse.urlsplit(TEST_DSN)
        address = parts.hostname + (':' + str(parts.port) if parts.port else '')
        dsn = urllib.parse.urlunsplit((parts.scheme,
            self.role + ':' + password + '@' + address, parts.path, parts.query, ''))
        limited = db_mod.connect(dataclasses.replace(self.cfg, dsn=dsn), writer=False)
        self.addCleanup(limited.close)
        with self.assertRaises(Exception):
            limited.execute('SET ROLE \"' + self.writer + '\"')
        native = remote.Native(limited, config(self.tmp))
        key = dict(host='burst', probe_agent='burst-probe', generation=1, release_epoch=2, nonce=NONCE)
        request_id, started = native.start(key)
        launch = dict(key, request_id=request_id, started_ts=started)
        self.assertEqual(native.receipt(launch)['verdict'], 'pending')
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status) "
                        "VALUES ('probe-turn','burst-probe','burst','running')")
        self.db.execute("UPDATE agent_mailbox SET delivered_at=clock_timestamp(),status='delivered',"
                        "meta='{\"consigne\":{\"turn\":\"probe-turn\"}}'::jsonb WHERE id=%s", (request_id,))
        self.db.execute("INSERT INTO agent_mailbox(sender,recipient,body,host,payload) VALUES "
                        "('burst-probe','burst-probe',%s,'burst','{\"ack\":true}'::jsonb)",
                        ('VM-PROBE-ACK ' + NONCE,))
        self.db.execute("UPDATE turn_resources SET status='done',ended_at=clock_timestamp() WHERE turn_id='probe-turn'")
        self.assertEqual(native.receipt(launch)['verdict'], 'ok')
        self.assertFalse(native.quiet(returning=True))
        self.db.execute("UPDATE agent_registry SET runner_seen_at=clock_timestamp(),runner_seen_host='burst',"
                        "runner_seen_owner='runner' WHERE name='burst-probe'")
        self.assertTrue(native.quiet(returning=True))
        self.db.execute("UPDATE host_lifecycle SET mode='draining' WHERE host='burst'")
        self.assertTrue(native.quiet(returning=False))
        self.audit()
