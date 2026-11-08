# SPDX-License-Identifier: AGPL-3.0-only
"""Real PostgreSQL read boundary and closed LOGIN reader contract."""
import os
from pathlib import Path
import subprocess
import uuid
from tests.support import PgTestCase, TEST_DSN
from tests.release_decision import install_decision_gate
from tests.role_acl import serialize_role_acl

ROOT = Path(__file__).resolve().parents[1]


class ReleaseDecisionGateTest(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        serialize_role_acl(cls)
        install_decision_gate(cls)

    def sql(self, sql):
        return subprocess.run(['psql','-X','-w',TEST_DSN,'-q','-v','ON_ERROR_STOP=1'],
            input=sql,env=dict(os.environ,PGOPTIONS='-c search_path='+self.schema),
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

    def test_endpoint_contract_rejects_public_and_foreign_execute(self):
        script = ("SELECT set_config('ameesh.release_role','%s',false);\n" % self.controller_role
                  + '\\i '+str(ROOT/'deploy/sql/_contrat-release-decision.sql')+'\n')
        self.assertEqual(self.sql(script).returncode,0)
        login = 't_endpoint_' + uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE "'+login+'" LOGIN')
        try:
            for grantee in ['PUBLIC','"'+login+'"']:
                self.db.execute('GRANT EXECUTE ON FUNCTION release_controller_decision(bigint) TO '+grantee)
                try:
                    self.assertNotEqual(self.sql(script).returncode,0)
                finally:
                    self.db.execute('REVOKE EXECUTE ON FUNCTION release_controller_decision(bigint) FROM '+grantee)
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
