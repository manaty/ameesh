# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import staging_migrations as sm

FIXTURES = Path(__file__).parent / 'fixtures' / 'staging_migrations'


class ClassificationTests(unittest.TestCase):
    def test_defaults_without_project_policy(self):
        for sql in ("CREATE TABLE x (id int);", "ALTER TABLE x ADD COLUMN y text;",
                    "DO $$ BEGIN CREATE TABLE IF NOT EXISTS x (id int); END $$;",
                    "-- DROP TABLE x;\nCREATE TABLE x (body text default 'delete from users');"):
            with self.subTest(sql=sql):
                self.assertEqual(sm.classify(sql).kind, 'additive')
        for sql in ("ALTER TABLE x DROP CONSTRAINT fk;",
                    "CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;"):
            self.assertEqual(sm.classify(sql).kind, 'relaxation')

    def test_mandatory_column_requires_only_a_constant_default(self):
        for default in ("'{}'", "'drop table x; --'", "true", "false", "0", "-1.25", "1e3", "ARRAY[]::text[]", "'{}'::text[]"):
            for suffix in (f"NOT NULL DEFAULT {default}", f"DEFAULT {default} NOT NULL"):
                with self.subTest(suffix=suffix):
                    self.assertEqual(sm.classify("ALTER TABLE x ADD COLUMN y text[] " + suffix).kind, 'additive')
        for suffix in ("NOT NULL", "NOT NULL DEFAULT NULL", "NOT NULL DEFAULT now()",
                       "NOT NULL DEFAULT 1 + 2", "NOT NULL DEFAULT '{}' || '{}'",
                       "NOT NULL DEFAULT fake_literal", "NOT NULL DEFAULT 'x'::custom_type", "NOT NULL DEFAULT 1 CHECK (false)",
                       "NOT NULL DEFAULT 1, DROP COLUMN z"):
            with self.subTest(suffix=suffix):
                self.assertEqual(sm.classify("ALTER TABLE x ADD COLUMN y text " + suffix).kind, 'destructive')

    def test_domain_column_default_stays_destructive(self):
        # PostgreSQL accepts ADD COLUMN on an empty table, but the domain CHECK
        # rejects a subsequent N-1 INSERT that omits y. No implicit domain cast
        # may use the additive shortcut, including arrays and quoted domains.
        for typename in ('c.d', 'c.d[]', 'custom_domain', '"d"', 'c."d"'):
            with self.subTest(typename=typename):
                sql = f"ALTER TABLE c.t ADD COLUMN y {typename} NOT NULL DEFAULT 'bad';"
                self.assertEqual(sm.classify(sql).kind, 'destructive')
                with self.assertRaises(sm.Refused):
                    sm.check('domain.sql', sql.encode(), sm.load_policy(),
                             read_file=lambda p:b'-- restore', signed_decision=lambda *a:False)
        self.assertEqual(sm.classify("ALTER TABLE c.t ADD COLUMN y pg_catalog.text NOT NULL DEFAULT 'bad'").kind, 'additive')

    def test_constant_column_does_not_override_project_destructive_rules(self):
        policy = sm.load_policy(json.dumps({'schema':'ameesh-staging-migrations/1',
            'exceptions':{'destructive':[{'pattern':r'alter table protected .*', 'reason':'protected table'}]}}))
        self.assertEqual(sm.classify("ALTER TABLE protected ADD COLUMN y int NOT NULL DEFAULT 1", policy).kind, 'destructive')

    def test_actual_migrations_and_new_trigger(self):
        root = Path(__file__).resolve().parents[1]
        for name, kind in (("0572_conversation_moves.sql", 'relaxation'),
                           ("0575_shared_host_tenants.sql", 'relaxation'),
                           ("0576_tenant_account_profiles.sql", 'additive')):
            with self.subTest(name=name):
                self.assertEqual(sm.classify((root/'src/ameesh/migrations'/name).read_text()).kind, kind)
        path = FIXTURES/'799_c1_residual_hub_fks.sql'
        meta = json.loads(path.with_suffix('.source.json').read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), meta['sha256'])
        self.assertEqual(sm.classify(path.read_text()).kind, 'unknown')
        trigger = "CREATE TRIGGER cleanup AFTER DELETE ON conversations FOR EACH ROW EXECUTE FUNCTION ops.cleanup();"
        self.assertEqual(sm.classify(trigger).kind, 'relaxation')
        data = trigger.encode()
        with self.assertRaises(sm.Refused):
            sm.check('migration.sql', data, sm.load_policy(), read_file=lambda p:b'-- restore', signed_decision=lambda *a:False)
        policy = sm.load_policy()
        policy['declarations']['migration.sql'] = {'checksum':hashlib.sha256(data).hexdigest(), 'lot':1, 'restore':'cleanup_restore.sql'}
        self.assertEqual(sm.check('migration.sql', data, policy, read_file=lambda p:b'DROP TRIGGER cleanup ON conversations;', signed_decision=lambda *a:False).kind, 'relaxation')

    def test_worst_instruction_in_all_static_branches(self):
        sql = """DO $$ BEGIN IF to_regnamespace('graph') IS NULL THEN
        CREATE TABLE x (id int); ELSE DROP TABLE x; END IF; END $$;"""
        self.assertEqual(sm.classify(sql).kind, 'destructive')
        for sql in ("REVOKE SELECT ON x FROM public;", "DELETE FROM x;", "UPDATE x SET id=1;",
                    "ALTER TYPE state ADD VALUE 'gone';", "ALTER TABLE x ALTER COLUMN y SET NOT NULL;",
                    "ALTER TABLE x ADD CONSTRAINT ck CHECK (id > 0);"):
            self.assertEqual(sm.classify(sql).kind, 'destructive')

    def test_unknown_is_never_hidden_by_a_known_statement(self):
        for sql in ("DO $$ BEGIN EXECUTE format('DROP TABLE %I', 'x'); END $$;",
                    "DO $$ BEGIN EXECUTE 'DROP TABLE ' || 'x'; END $$;",
                    "DO $$ BEGIN IF dangerous() THEN DROP TABLE x; END IF; END $$;",
                    "DO $$ BEGIN CREATE TABLE x (id int); $$;",
                    "CREATE TABLE x (id text default 'unterminated);",
                    "ALTER TABLE x DROP CONSTRAINT fk; VACUUM x;",
                    "ALTER TABLE x ADD COLUMN y int, DISABLE TRIGGER ALL;",
                    "DO $$ BEGIN PERFORM dangerous(); END $$;"):
            with self.subTest(sql=sql):
                self.assertEqual(sm.classify(sql).kind, 'unknown')

    def test_nested_comments_strings_and_routine_bodies(self):
        sql = """-- DO $$ DROP TABLE x $$;
        CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $body$
        BEGIN UPDATE x SET id = 2; END $body$;
        DO $block$ BEGIN /* comment */ CREATE TABLE x (body text default 'DROP TABLE y'); END $block$;"""
        self.assertEqual(sm.classify(sql).kind, 'additive')
        # A revoke of an existing overload is destructive even beside a new one.
        sql = "CREATE FUNCTION f(x int) RETURNS int LANGUAGE sql AS $$SELECT 1$$; REVOKE ALL ON FUNCTION f(text) FROM public;"
        self.assertEqual(sm.classify(sql).kind, 'destructive')
        sql = """DO $$ BEGIN IF to_regnamespace('graph') IS NULL THEN
        CREATE FUNCTION f(x int) RETURNS int LANGUAGE sql AS $fn$ SELECT 1 $fn$;
        END IF; END $$; REVOKE ALL ON FUNCTION f(int) FROM public;"""
        self.assertEqual(sm.classify(sql).kind, 'destructive')

    def test_policy_absence_malformed_policy_and_explicit_project_exception(self):
        self.assertEqual(sm.load_policy()['declarations'], {})
        for text in ('{}', 'not json', '{"schema":"ameesh-staging-migrations/1","exceptions":[]}'):
            with self.assertRaises(sm.Refused):
                sm.load_policy(text)
        policy = sm.load_policy(json.dumps({'schema':'ameesh-staging-migrations/1',
            'exceptions':{'additive':[{'pattern':'select 1','reason':'reviewed project probe'}]}}))
        self.assertEqual(sm.classify('SELECT 1;', policy).kind, 'additive')


class GitInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@localhost')
        self.put('base', 'base')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git','-C',str(self.repo),*args], stderr=subprocess.STDOUT).decode().strip()

    def put(self, path, text):
        target=self.repo/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text)

    def commit(self):
        self.git('add','.')
        self.git('commit','--allow-empty','-qm','fixture')
        return self.git('rev-parse','HEAD')

    def policy(self, declarations):
        self.put(sm.POLICY_PATH,json.dumps({'schema':'ameesh-staging-migrations/1','declarations':declarations}))

    def run_inventory(self, paths, verifier=lambda *args:False):
        tree=sm.GitTree(str(self.repo),self.commit())
        return sm.inventory(tree,paths,signed_decision=verifier)

    def test_additive_passes_without_policy_and_worktree_cannot_hide_frozen_sql(self):
        self.put('sql/1.sql','CREATE TABLE x (id int);')
        self.assertTrue(self.run_inventory(['sql/1.sql'])['allowed'])
        self.put('sql/1.sql','DROP TABLE x;')
        sha=self.commit()
        self.put('sql/1.sql','CREATE TABLE x (id int);')
        report=sm.inventory(sm.GitTree(str(self.repo),sha),['sql/1.sql'],signed_decision=lambda *args:False)
        self.assertFalse(report['allowed'])
        self.assertIn('destructive',report['files'][0]['reason'])

    def test_real_796_797_pass_only_when_declared_with_frozen_restore(self):
        meta=json.loads((FIXTURES/'source.json').read_text())
        declarations={}
        paths=[]
        for filename, source in meta['files'].items():
            data=(FIXTURES/filename).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),source['sha256'])
            self.assertEqual(sm.classify(data.decode()).kind,'relaxation')
            path=source['path']; paths.append(path)
            self.put(path,data.decode())
            restore='rollback/'+filename[:3]+'_restore.sql'
            self.put(restore,'-- reviewed restore fixture, presence checked here')
            declarations[path]={'checksum':source['sha256'],'lot':7266,'restore':restore}
        self.assertFalse(self.run_inventory(paths)['allowed'])
        self.policy(declarations)
        self.assertTrue(self.run_inventory(paths)['allowed'])
        declarations[paths[0]]['checksum']='0'*64
        self.policy(declarations)
        self.assertFalse(self.run_inventory(paths)['allowed'])

    def test_restore_must_be_regular_nonempty_and_in_frozen_tree(self):
        sql='ALTER TABLE x DROP CONSTRAINT fk;'
        self.put('sql/1.sql',sql)
        self.policy({'sql/1.sql':{'checksum':hashlib.sha256(sql.encode()).hexdigest(),
                                'lot':1,'restore':'rollback/1_restore.sql'}})
        self.assertFalse(self.run_inventory(['sql/1.sql'])['allowed'])
        self.put('rollback/1_restore.sql','')
        self.assertFalse(self.run_inventory(['sql/1.sql'])['allowed'])
        (self.repo/'rollback/1_restore.sql').unlink()
        (self.repo/'rollback/1_restore.sql').symlink_to('../base')
        self.assertFalse(self.run_inventory(['sql/1.sql'])['allowed'])

    def test_destructive_proof_binds_lot_and_approval_option(self):
        sql='DROP TABLE x;';self.put('sql/1.sql',sql)
        self.policy({'sql/1.sql':{'checksum':hashlib.sha256(sql.encode()).hexdigest(),
                     'lot':17,'decision':42,'approval_option':'approve'}})
        self.assertFalse(self.run_inventory(['sql/1.sql'])['allowed'])
        calls=[]
        self.assertTrue(self.run_inventory(['sql/1.sql'],lambda *args:calls.append(args) or True)['allowed'])
        self.assertEqual(calls,[(42,17,'approve')])

    def test_doubt_requires_signed_owner_agreement_not_a_restore_declaration(self):
        for data in (b"DO $$ BEGIN EXECUTE 'DROP TABLE ' || 'x'; END $$;",
                     b"DO $$ BEGIN CREATE TABLE x (id int); $$;", b"\xff"):
            with self.subTest(data=data):
                path = 'sql/unknown.sql'
                (self.repo / 'sql').mkdir(exist_ok=True)
                (self.repo / path).write_bytes(data)
                self.put('rollback/unknown_restore.sql', '-- restore')
                entry = {'checksum':hashlib.sha256(data).hexdigest(), 'lot':17,
                         'restore':'rollback/unknown_restore.sql'}
                self.policy({path:entry})
                report = self.run_inventory([path])
                self.assertFalse(report['allowed'])
                self.assertIn('unknown', report['files'][0]['reason'])
                entry.update(decision=42, approval_option='approve')
                self.policy({path:entry})
                self.assertFalse(self.run_inventory([path])['allowed'])
                calls = []
                report = self.run_inventory([path], lambda *args:calls.append(args) or True)
                self.assertTrue(report['allowed'])
                self.assertEqual(report['files'][0]['classification'], 'unknown')
                self.assertEqual(calls, [(42,17,'approve')])
                entry['checksum'] = '0' * 64
                self.policy({path:entry})
                self.assertFalse(self.run_inventory([path], lambda *args:True)['allowed'])

    def test_cli_explicit_inventory_exit_codes_and_no_credential_for_additive(self):
        self.put('sql/1.sql','CREATE TABLE x (id int);')
        self.put('sql/2.sql','DROP TABLE x;')
        sha=self.commit()
        args=['--repo',str(self.repo),'--sha',sha,'--project','test']
        from ameesh import config
        empty = self.repo / 'pending.txt'
        empty.write_text('')
        with mock.patch.object(config,'load',side_effect=AssertionError('no credential access')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sm.main(args+['--file','sql/1.sql']),0)
            self.assertEqual(sm.main(args+['--file','sql/2.sql']),1)
            self.assertEqual(sm.main(args),1)
            self.assertEqual(sm.main(args+['--file','../outside.sql']),1)
            self.assertEqual(sm.main(args+['--file','missing.sql']),1)
            self.assertEqual(sm.main(args+['--files-from',str(empty)]),0)
            from ameesh import main
            self.assertEqual(main._dispatch(['staging-migrations']+args+['--file','sql/1.sql']),0)

    def test_malformed_policy_refuses_even_additive_inventory(self):
        self.put('sql/1.sql', 'CREATE TABLE x (id int);')
        self.put(sm.POLICY_PATH, 'not JSON')
        sha = self.commit()
        with self.assertRaisesRegex(sm.Refused, 'policy unreadable'):
            sm.inventory(sm.GitTree(str(self.repo), sha), ['sql/1.sql'],
                         signed_decision=lambda *args: False)


class DecisionTests(unittest.TestCase):
    def test_only_current_signed_agreement_on_carrier_lot_and_project_passes(self):
        from ameesh import decisions
        view={'state':'answered','project':'project','lot':17,'options':[{'key':'approve'},{'key':'reject'}],
              'answer':{'option':'approve'}}
        verify=sm.decision_verifier(object(),'project')
        with mock.patch.object(decisions,'get',return_value=view), \
             mock.patch.object(decisions,'require_current') as current, \
             mock.patch.object(decisions,'verify_proof',return_value=(True,'verified')) as proof:
            self.assertTrue(verify(42,17,'approve'))
            self.assertFalse(verify(42,18,'approve'))
            self.assertFalse(verify(42,17,'reject'))
            view['project']='another'
            self.assertFalse(verify(42,17,'approve'))
            view['project']='project'
            proof.return_value=(False,'unsigned')
            self.assertFalse(verify(42,17,'approve'))
            proof.return_value=(True,'verified')
            current.side_effect=decisions.DecisionError('superseded')
            self.assertFalse(verify(42,17,'approve'))


if __name__ == '__main__':
    unittest.main()
