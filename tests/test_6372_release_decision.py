# SPDX-License-Identifier: AGPL-3.0-only
"""Real PostgreSQL read boundary and closed LOGIN reader contract."""
import os
from pathlib import Path
import subprocess
import uuid
from tests.support import PgTestCase, TEST_DSN
from tests.release_decision import decision_owner, install_decision_gate
from tests.role_acl import serialize_role_acl

ROOT = Path(__file__).resolve().parents[1]


class ReleaseDecisionGateTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        serialize_role_acl(cls)
        install_decision_gate(cls)

    def sql(self, sql, schema=None):
        return subprocess.run(['psql','-X','-w',TEST_DSN,'-q','-v','ON_ERROR_STOP=1'],
            input=sql,env=dict(os.environ,PGOPTIONS='-c search_path='+(schema or self.schema)),
            capture_output=True,text=True,timeout=30)

    def test_additive_alone_preserves_old_and_new_decision_paths(self):
        # Restore the closed gate in finally for the other tests.
        role = self.controller_role
        script = ("\\set role " + role + "\n"
                  + "\\i " + str(ROOT/'deploy/sql/release-decision-additive.sql') + "\n")
        # The standalone entry commits; run it twice to prove reinstallation.
        try:
            granted = self.sql('GRANT SELECT ON agent_mailbox TO "%s";' % role)
            self.assertEqual(granted.returncode, 0, granted.stderr)
            for _ in range(2):
                result = self.sql(script)
                self.assertEqual(result.returncode, 0, result.stderr)
            message_id = self.db.query("INSERT INTO agent_mailbox(sender,recipient,body,kind,payload)"
                " VALUES('fixture','human:fixture','secret','request',"
                " '{\"decision\":{\"needs_signature\":true,\"question\":\"same decision\"}}') RETURNING id")[0]['id']
            result = self.sql('SET ROLE "%s"; DO $check$ BEGIN '
                'IF NOT EXISTS (SELECT FROM agent_mailbox m '
                'JOIN release_controller_decision(m.id) d USING (id) '
                'WHERE m.id=%d AND d.sender=m.sender AND d.recipient=m.recipient '
                "AND d.payload->'decision'=m.payload->'decision' "
                'AND d.created_ts=extract(epoch FROM m.created_at)::float8) '
                "THEN RAISE EXCEPTION 'old/new decision mismatch'; END IF; END $check$;"
                % (role, message_id))
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            result = self.sql("\\set role " + role + "\n"
                + "\\i " + str(ROOT/'deploy/sql/release-decision-cutover.sql') + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_signature_request_and_minimal_columns(self):
        ids = []
        for kind, payload in [('request','{"decision":{"needs_signature":true},"private":"hidden"}'),
                              ('request','{"decision":{"needs_signature":false}}'),
                              ('notify','{"decision":{"needs_signature":true}}'),
                              ('request','{}')]:
            ids.append(self.db.query("INSERT INTO agent_mailbox(sender,recipient,body,kind,payload)"
                " VALUES('fixture','human:fixture','secret',%s,%s::jsonb) RETURNING id",(kind,payload))[0]['id'])
        row = self.db.query('SELECT * FROM release_controller_decision(%s)',(ids[0],))[0]
        self.assertNotIn('body',row)
        self.assertNotIn('meta',row)
        self.assertEqual(row['payload'],{'decision':{'needs_signature':True}})
        for message_id in ids[1:]+[0,-1]:
            self.assertEqual(self.db.query('SELECT * FROM release_controller_decision(%s)',(message_id,)),[])
        role = self.controller_role
        result = self.sql('SET ROLE "%s"; SELECT * FROM release_controller_decision(%d);' % (role,ids[0]))
        self.assertEqual(result.returncode,0,result.stderr)
        for expression in ['*','body','meta','payload','id']:
            result = self.sql('SET ROLE "%s"; SELECT %s FROM agent_mailbox;' % (role,expression))
            self.assertNotEqual(result.returncode,0)
            self.assertIn('permission denied',result.stderr)

    def test_exact_question_enumeration_under_controller(self):
        from ameesh import decisions
        ids = []
        for question, signed, state, recipient in [
            ('exact', True, 'answered', 'human:fixture'),
            ('other', True, 'answered', 'human:fixture'),
            ('exact', False, 'answered', 'human:fixture'),
            ('exact', True, 'pending', 'human:fixture'),
            ('exact', True, 'answered', 'agent:fixture'),
        ]:
            import json
            payload = json.dumps(dict(decision=dict(question=question, needs_signature=signed, state=state)))
            ids.append(self.db.query("INSERT INTO agent_mailbox(sender,recipient,body,kind,payload)"
                " VALUES('fixture',%s,'secret','request',%s::jsonb) RETURNING id",
                (recipient, payload))[0]['id'])
        with self.db.transaction() as tx:
            tx.execute('SET LOCAL ROLE "'+self.controller_role+'"')
            self.assertEqual(decisions.ids_for_controller(tx, 'exact'), [ids[0]])
            self.assertEqual(decisions.ids_for_controller(tx, "exact' OR true --"), [])

    def test_endpoint_contract_rejects_public_and_foreign_execute(self):
        script = ("SELECT set_config('ameesh.release_role','%s',false);\n" % self.controller_role
                  + '\\i '+str(ROOT/'deploy/sql/_contrat-release-decision.sql')+'\n')
        self.assertEqual(self.sql(script).returncode,0)
        login = 't_endpoint_' + uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE "'+login+'" LOGIN')
        try:
            for signature in ['release_controller_decision(bigint)', 'release_controller_decision_ids(text)']:
                for grantee in ['PUBLIC','"'+login+'"']:
                    self.db.execute('GRANT EXECUTE ON FUNCTION '+signature+' TO '+grantee)
                    try:
                        self.assertNotEqual(self.sql(script).returncode,0)
                    finally:
                        self.db.execute('REVOKE EXECUTE ON FUNCTION '+signature+' FROM '+grantee)
        finally:
            self.db.execute('DROP OWNED BY "'+login+'" CASCADE')
            self.db.execute('DROP ROLE "'+login+'"')

    def test_closed_reader_contract_rejects_table_column_and_set_role(self):
        login = 't_privacy_' + uuid.uuid4().hex[:10]
        parent = login+'_parent'
        self.db.execute('CREATE ROLE "%s" LOGIN NOINHERIT' % login)
        self.db.execute('CREATE ROLE "%s" NOLOGIN' % parent)
        contract = '\\i '+str(ROOT/'deploy/sql/_contrat-mailbox-readers.sql')+'\n'
        try:
            self.assertEqual(self.sql(contract).returncode,0)
            for grant,revoke in [
                ('GRANT SELECT ON agent_mailbox TO "%s"'%login,'REVOKE SELECT ON agent_mailbox FROM "%s"'%login),
                ('GRANT SELECT(meta) ON agent_mailbox TO "%s"'%login,'REVOKE SELECT(meta) ON agent_mailbox FROM "%s"'%login),
                ('GRANT SELECT(payload) ON agent_mailbox TO "%s"; GRANT "%s" TO "%s"'%(parent,parent,login),
                 'REVOKE "%s" FROM "%s"; REVOKE SELECT(payload) ON agent_mailbox FROM "%s"'%(parent,login,parent))]:
                self.db.execute(grant)
                self.assertNotEqual(self.sql(contract).returncode,0)
                self.db.execute(revoke)
            self.assertEqual(self.sql(contract).returncode,0)
        finally:
            self.db.execute('DROP OWNED BY "%s", "%s" CASCADE'%(login,parent))
            self.db.execute('DROP ROLE "%s", "%s"'%(login,parent))

    def test_reader_contract_admits_executor_members_by_contract(self):
        group = 't_exec_' + uuid.uuid4().hex[:10]
        member = 't_exec_login_' + uuid.uuid4().hex[:10]
        outsider = 't_exec_out_' + uuid.uuid4().hex[:10]
        contract = ('\\set mailbox_executor '+group+'\n'
                    + '\\i '+str(ROOT/'deploy/sql/_contrat-mailbox-readers.sql')+'\n')
        self.db.execute('CREATE ROLE "%s" NOLOGIN' % group)
        self.db.execute('CREATE ROLE "%s" LOGIN IN ROLE "%s"' % (member, group))
        self.db.execute('CREATE ROLE "%s" LOGIN' % outsider)
        self.db.execute('GRANT SELECT ON agent_mailbox TO "%s"' % group)
        try:
            self.assertEqual(self.sql(contract).returncode, 0)
            # The same direct grant outside the documented group is refused.
            self.db.execute('GRANT SELECT ON agent_mailbox TO "%s"' % outsider)
            refused = self.sql(contract)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(outsider, refused.stderr)
        finally:
            self.db.execute('DROP OWNED BY "%s", "%s", "%s" CASCADE' % (member, outsider, group))
            self.db.execute('DROP ROLE "%s", "%s"' % (outsider, member))
            self.db.execute('DROP ROLE "%s"' % group)

    def test_reader_contract_admits_the_relation_owner_not_a_direct_grant(self):
        schema = 't_own_' + uuid.uuid4().hex[:10]
        owner = 't_own_login_' + uuid.uuid4().hex[:10]
        intruder = 't_own_out_' + uuid.uuid4().hex[:10]
        contract = '\\i '+str(ROOT/'deploy/sql/_contrat-mailbox-readers.sql')+'\n'
        self.db.execute('CREATE SCHEMA "%s"' % schema)
        self.db.execute('CREATE ROLE "%s" LOGIN' % owner)
        self.db.execute('CREATE ROLE "%s" LOGIN' % intruder)
        self.db.execute('CREATE TABLE "%s".agent_mailbox(id bigint)' % schema)
        self.db.execute('ALTER TABLE "%s".agent_mailbox OWNER TO "%s"' % (schema, owner))
        try:
            self.assertEqual(self.sql(contract, schema=schema).returncode, 0)
            self.db.execute('GRANT SELECT ON "%s".agent_mailbox TO "%s"' % (schema, intruder))
            refused = self.sql(contract, schema=schema)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(intruder, refused.stderr)
            self.db.execute('REVOKE SELECT ON "%s".agent_mailbox FROM "%s"' % (schema, intruder))
            self.assertEqual(self.sql(contract, schema=schema).returncode, 0)
        finally:
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
            self.db.execute('DROP OWNED BY "%s", "%s" CASCADE' % (owner, intruder))
            self.db.execute('DROP ROLE IF EXISTS "%s", "%s"' % (owner, intruder))

    def test_reader_contract_admits_only_the_enrolled_backup_marker(self):
        backup = 't_backup_' + uuid.uuid4().hex[:10]
        operation = 'op_' + uuid.uuid4().hex[:10]
        contract = '\\i '+str(ROOT/'deploy/sql/_contrat-mailbox-readers.sql')+'\n'
        self.db.execute('CREATE ROLE "%s" LOGIN' % backup)
        self.db.execute('GRANT SELECT ON agent_mailbox TO "%s"' % backup)
        try:
            self.db.execute("COMMENT ON ROLE \"%s\" IS 'ameesh-release-identities:%s:backup'"
                            % (backup, operation))
            self.assertEqual(self.sql(contract).returncode, 0)
            # The enrollment marker names the kind: another enrolled identity or
            # a bare SELECT is not admitted.
            self.db.execute("COMMENT ON ROLE \"%s\" IS 'ameesh-release-identities:%s:controller'"
                            % (backup, operation))
            refused = self.sql(contract)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(backup, refused.stderr)
            self.db.execute('COMMENT ON ROLE "%s" IS NULL' % backup)
            self.assertNotEqual(self.sql(contract).returncode, 0)
            self.db.execute("COMMENT ON ROLE \"%s\" IS 'ameesh-release-identities:%s:backup'"
                            % (backup, operation))
            self.assertEqual(self.sql(contract).returncode, 0)
        finally:
            self.db.execute('DROP OWNED BY "%s" CASCADE' % backup)
            self.db.execute('DROP ROLE "%s"' % backup)

    def test_legacy_decision_owner_is_renamed_out_of_the_controller_prefix(self):
        schema = 't_legacy_' + uuid.uuid4().hex[:10]
        role = 't_legacy_ctl_' + uuid.uuid4().hex[:10]
        legacy = role + '_decision_reader'
        owner = decision_owner(role)
        install = ("BEGIN; SELECT set_config('ameesh.release_role','%s',true);\n" % role
                   + '\\i '+str(ROOT/'deploy/sql/_release-decision.sql')+'\nCOMMIT;\n')
        contract = ("BEGIN; SELECT set_config('ameesh.release_role','%s',true);\n" % role
                    + '\\i '+str(ROOT/'deploy/sql/_contrat-release-decision.sql')+'\nCOMMIT;\n')
        self.db.execute('CREATE SCHEMA "%s"' % schema)
        self.db.execute('CREATE TABLE "%s".agent_mailbox(id bigint, kind text, sender text, '
                        'recipient text, payload jsonb, host text, created_at timestamptz, '
                        'delivered_at timestamptz)' % schema)
        self.db.execute('CREATE TABLE "%s".decision_supersessions(id bigint)' % schema)
        self.db.execute('CREATE ROLE "%s" NOLOGIN' % role)
        try:
            self.assertEqual(self.sql(install, schema=schema).returncode, 0)
            found = self.db.query(
                "SELECT r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
                "WHERE p.oid=to_regprocedure(%s)",
                (schema+'.release_controller_decision(bigint)',))[0]['rolname']
            self.assertEqual(found, owner)
            self.assertEqual(self.sql(contract, schema=schema).returncode, 0)
            # Simulate an installation enrolled by the legacy head: the same
            # definer under the controller-prefixed name, plus migration 0527's
            # stray grant once that prefix treated it as a controller.
            self.db.execute('ALTER ROLE "%s" RENAME TO "%s"' % (owner, legacy))
            self.db.execute('COMMENT ON ROLE "%s" IS NULL' % legacy)
            self.db.execute('GRANT SELECT ON "%s".decision_supersessions TO "%s"' % (schema, legacy))
            refused = self.sql(contract, schema=schema)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(legacy, refused.stderr)
            self.assertEqual(self.sql(install, schema=schema).returncode, 0)
            self.assertFalse(self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s', (legacy,)))
            marker = self.db.query(
                "SELECT shobj_description(oid,'pg_authid') AS marker FROM pg_roles WHERE rolname=%s",
                (owner,))[0]['marker']
            self.assertEqual(marker, 'ameesh-decision-owner:'+role)
            self.assertFalse(self.db.query(
                'SELECT has_table_privilege(%s,%s,%s) AS reads',
                (owner, schema+'.decision_supersessions', 'SELECT'))[0]['reads'])
            self.assertEqual(self.sql(contract, schema=schema).returncode, 0)
        finally:
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
            for name in (owner, legacy, role):
                if self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s', (name,)):
                    self.db.execute('DROP OWNED BY "%s" CASCADE' % name)
                    self.db.execute('DROP ROLE "%s"' % name)
