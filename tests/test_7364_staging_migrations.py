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

    def test_mandatory_column_check_accepting_the_default_is_additive(self):
        # #7531 : 0582 de #7520 portait un CHECK inline utile ; le classeur le
        # rangeait en destructif (mandatory column). Le CHECK n'est lu que s'il
        # ne peut dépendre que de la colonne ajoutée et s'il accepte le défaut
        # que la version N-1 produit en omettant la colonne.
        for suffix in (
                "NOT NULL DEFAULT 0 CHECK (y >= 0)",
                "NOT NULL DEFAULT 0 CHECK (y = 0)",
                "NOT NULL DEFAULT 0 CHECK (y IN (0, 1))",
                "NOT NULL DEFAULT 0 CHECK (y IS NOT NULL)",
                "NOT NULL DEFAULT 0 CHECK (y >= 0 AND y <= 10)",
                "DEFAULT 0 NOT NULL CHECK (y >= 0)",
                "NOT NULL DEFAULT -1 CHECK (y < 0)",
                # SQL NULL: un CHECK passe sur NULL, pas sur FALSE.
                "NOT NULL DEFAULT 0 CHECK (y > 0 OR NULL)"):
            with self.subTest(suffix=suffix):
                self.assertEqual(sm.classify("ALTER TABLE x ADD COLUMN y int " + suffix).kind, 'additive')
        sql = ("ALTER TABLE credit_cycles ADD COLUMN IF NOT EXISTS billing_lag_seconds "
               "bigint NOT NULL DEFAULT 0 CHECK (billing_lag_seconds >= 0);")
        self.assertEqual(sm.classify(sql).kind, 'additive')
        # La porte ne demande ni déclaration ni accord signé pour un additif.
        self.assertEqual(sm.check('0582.sql', sql.encode(), sm.load_policy(),
                                  read_file=lambda p: b'', signed_decision=lambda *a: False).kind,
                         'additive')
        # Le CHECK est évalué sur la vraie valeur : deux littéraux distincts ne
        # se confondent pas derrière le jeton opaque du lecteur.
        for sql, kind in (
                ("ALTER TABLE x ADD COLUMN y text NOT NULL DEFAULT 'a' CHECK (y IN ('a', 'b'))", 'additive'),
                ("ALTER TABLE x ADD COLUMN y text NOT NULL DEFAULT 'a' CHECK (y = 'b')", 'destructive'),
                ("ALTER TABLE x ADD COLUMN y text NOT NULL DEFAULT 'it''s' CHECK (y = 'it''s')", 'additive'),
                ("ALTER TABLE x ADD COLUMN y boolean NOT NULL DEFAULT false CHECK (y = false)", 'additive'),
                ("ALTER TABLE x ADD COLUMN y boolean NOT NULL DEFAULT false CHECK (y)", 'destructive')):
            with self.subTest(sql=sql):
                self.assertEqual(sm.classify(sql).kind, kind)

    def test_mandatory_column_check_rejecting_or_unprovable_stays_destructive(self):
        for suffix in (
                "NOT NULL DEFAULT 0 CHECK (y > 0)",
                "NOT NULL DEFAULT 0 CHECK (y <> 0)",
                "NOT NULL DEFAULT 0 CHECK (y = 1)",
                "NOT NULL DEFAULT 0 CHECK (y IS NULL)",
                "NOT NULL DEFAULT 0 CHECK (false)",
                "NOT NULL DEFAULT 0 CHECK (y > 0 AND NULL)",
                "NOT NULL DEFAULT 0 CHECK (y >= other_column)",
                "NOT NULL DEFAULT 0 CHECK (length(y::text) > 0)",
                "NOT NULL DEFAULT 0 CHECK (y BETWEEN 0 AND 1)",
                "NOT NULL DEFAULT 0 CHECK (y >= 0) CHECK (y <= 10)",
                "NOT NULL DEFAULT now() CHECK (y >= 0)"):
            with self.subTest(suffix=suffix):
                self.assertEqual(sm.classify("ALTER TABLE x ADD COLUMN y int " + suffix).kind, 'destructive')

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
        self.assertEqual(sm.classify("ALTER TABLE protected ADD COLUMN y int NOT NULL DEFAULT 1 CHECK (y >= 0)",
                                     policy).kind, 'destructive')

    def test_actual_migrations_and_new_trigger(self):
        root = Path(__file__).resolve().parents[1]
        for name, kind in (("0572_conversation_moves.sql", 'additive'),
                           ("0575_shared_host_tenants.sql", 'additive'),
                           ("0576_tenant_account_profiles.sql", 'additive')):
            with self.subTest(name=name):
                # 0575 replaces functions created by that same migration; the
                # frozen-tree history proof is exercised end to end below.
                prover = ((lambda kind, name, signature="": True)
                          if name.startswith("0575") else None)
                self.assertEqual(sm.classify((root/'src/ameesh/migrations'/name).read_text(),
                                             proves_new=prover).kind, kind)
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
        policy['declarations']['migration.sql'] = {'checksum':hashlib.sha256(data).hexdigest(), 'lot':1, 'restore':'cleanup_restore.sql',
            'rejects_previous_writes':False, 'previous_writes_justification':'Only cleans up deleted rows; function reviewed'}
        self.assertEqual(sm.check('migration.sql', data, policy, read_file=lambda p:b'DROP TRIGGER cleanup ON conversations;', signed_decision=lambda *a:False).kind, 'relaxation')

    def test_multiple_nullable_columns_in_one_alter_table_are_additive(self):
        # #7655: 0565 and 0568 add several nullable columns in one statement.
        sql = ("ALTER TABLE work_items "
               "ADD COLUMN IF NOT EXISTS external_session text, "
               "ADD COLUMN IF NOT EXISTS external_claim_session text, "
               "ADD COLUMN IF NOT EXISTS external_claimed_at timestamptz;")
        self.assertEqual(sm.classify(sql).kind, 'additive')
        self.assertEqual(sm.check('0565.sql', sql.encode(), sm.load_policy(),
                                  read_file=lambda p:b'', signed_decision=lambda *a:False).kind,
                         'additive')
        # Nullable columns with a constant default are additive too, and a
        # mandatory column with a proven constant default keeps its #7531
        # shortcut inside the same statement.
        sql = ("ALTER TABLE t ADD COLUMN a text, "
               "ADD COLUMN b integer NOT NULL DEFAULT 0, "
               "ADD COLUMN c text DEFAULT 'x' CHECK (c <> '');")
        self.assertEqual(sm.classify(sql).kind, 'additive')
        self.assertEqual(sm.classify("ALTER TABLE t ADD COLUMN y text DEFAULT 'x';").kind,
                         'additive')
        # A default cast to a custom type or domain, and a non-constant
        # default, stay unproven (as for a mandatory column).
        self.assertEqual(sm.classify(
            "ALTER TABLE c.t ADD COLUMN y custom_domain DEFAULT 'a';").kind, 'unknown')
        self.assertEqual(sm.classify(
            "ALTER TABLE t ADD COLUMN y timestamptz DEFAULT now();").kind, 'unknown')
        # One unsafe action keeps the whole statement in the worst class.
        for sql in (
                "ALTER TABLE t ADD COLUMN a text, ADD COLUMN b int NOT NULL",
                "ALTER TABLE t ADD COLUMN a text, DROP COLUMN z",
                "ALTER TABLE t ADD COLUMN a text, "
                "ADD COLUMN b int NOT NULL DEFAULT 1 CHECK (b > 1)"):
            with self.subTest(sql=sql):
                self.assertEqual(sm.classify(sql).kind, 'destructive')

    def test_constraint_not_tightened_marker_is_additive(self):
        # #7655: 0570, 0578 and 0579 replace a CHECK by a wider one; 0565 adds
        # several CHECKs on new columns. The L151 marker is the reviewed
        # declaration that the constraint does not tighten.
        marked = """-- ameesh: constraint not tightened
        ALTER TABLE account_switches DROP CONSTRAINT IF EXISTS account_switches_kind_check;
        ALTER TABLE account_switches ADD CONSTRAINT account_switches_kind_check
            CHECK (kind IN ('bascule', 'retour', 'agent'));"""
        self.assertEqual(sm.classify(marked).kind, 'additive')
        self.assertEqual(sm.check('0578.sql', marked.encode(), sm.load_policy(),
                                  read_file=lambda p:b'', signed_decision=lambda *a:False).kind,
                         'additive')
        marked = """-- ameesh: constraint not tightened
        ALTER TABLE work_items
            ADD CONSTRAINT a_ck CHECK (a IS NULL OR btrim(a) <> ''),
            ADD CONSTRAINT b_ck CHECK ((b IS NULL) = (c IS NULL));"""
        self.assertEqual(sm.classify(marked).kind, 'additive')
        # Without the marker, the old classes are kept.
        unmarked = marked.replace('-- ameesh: constraint not tightened\n', '')
        self.assertEqual(sm.classify(unmarked).kind, 'destructive')
        # A drop without a same-name re-add stays a removal, even marked.
        alone = """-- ameesh: constraint not tightened
        ALTER TABLE t DROP CONSTRAINT IF EXISTS c;"""
        self.assertEqual(sm.classify(alone).kind, 'relaxation')
        # The marker never covers UNIQUE, PRIMARY KEY or FOREIGN KEY.
        unique = """-- ameesh: constraint not tightened
        ALTER TABLE t ADD CONSTRAINT u UNIQUE (a);"""
        self.assertEqual(sm.classify(unique).kind, 'destructive')
        # A project destructive exception still wins over the marker.
        policy = sm.load_policy(json.dumps({'schema':'ameesh-staging-migrations/1',
            'exceptions':{'destructive':[{'pattern':r'alter table protected .*', 'reason':'protected table'}]}}))
        self.assertEqual(sm.classify(
            "-- ameesh: constraint not tightened\nALTER TABLE protected ADD CONSTRAINT ck CHECK (a > 0);",
            policy).kind, 'destructive')

    def test_trigger_marker_and_new_table_are_additive(self):
        # #7655: 0567 declares its triggers compatible with previous code;
        # 0572 creates its trigger on a table created by the same migration.
        marked = """-- ameesh: trigger compatible with previous code
        CREATE TRIGGER zz_guard BEFORE INSERT ON work_items
            FOR EACH ROW EXECUTE FUNCTION guard();"""
        self.assertEqual(sm.classify(marked).kind, 'additive')
        self.assertEqual(sm.check('0567.sql', marked.encode(), sm.load_policy(),
                                  read_file=lambda p:b'', signed_decision=lambda *a:False).kind,
                         'additive')
        new_table = """CREATE TABLE conversation_moves (id int);
        CREATE TRIGGER spelling BEFORE INSERT ON conversation_moves
            FOR EACH ROW EXECUTE FUNCTION f();"""
        self.assertEqual(sm.classify(new_table).kind, 'additive')
        self.assertEqual(sm.check('0572.sql', new_table.encode(), sm.load_policy(),
                                  read_file=lambda p:b'', signed_decision=lambda *a:False).kind,
                         'additive')
        # On an existing table, without the marker, the trigger is still a
        # relaxation owing its declaration and rejects_previous_writes.
        unmarked = ("CREATE TRIGGER guard BEFORE INSERT ON work_items "
                    "FOR EACH ROW EXECUTE FUNCTION guard();")
        self.assertEqual(sm.classify(unmarked).kind, 'relaxation')
        with self.assertRaises(sm.Refused):
            sm.check('migration.sql', unmarked.encode(), sm.load_policy(),
                     read_file=lambda p:b'-- restore', signed_decision=lambda *a:False)
        # A DROP is only idempotent when the same trigger is recreated.
        replaced = """-- ameesh: trigger compatible with previous code
        DROP TRIGGER IF EXISTS zz_guard ON work_items;
        CREATE TRIGGER zz_guard BEFORE INSERT ON work_items
            FOR EACH ROW EXECUTE FUNCTION guard();"""
        self.assertEqual(sm.classify(replaced).kind, 'additive')
        for sql in (
                """-- ameesh: trigger compatible with previous code
                DROP TRIGGER IF EXISTS zz_guard ON work_items;""",
                """-- ameesh: trigger compatible with previous code
                DROP TRIGGER IF EXISTS zz_guard ON work_items;
                CREATE TRIGGER other BEFORE INSERT ON work_items
                    FOR EACH ROW EXECUTE FUNCTION guard();""",
                """-- ameesh: trigger compatible with previous code
                DROP TRIGGER zz_guard ON work_items;
                CREATE TRIGGER zz_guard BEFORE INSERT ON work_items
                    FOR EACH ROW EXECUTE FUNCTION guard();"""):
            with self.subTest(sql=sql):
                self.assertEqual(sm.classify(sql).kind, 'destructive')
        # The marker covers the trigger, never another destructive statement.
        self.assertEqual(sm.classify(
            "-- ameesh: trigger compatible with previous code\nDROP TABLE x;").kind,
            'destructive')
        # Quoted identifiers are one neutral token in the lexer: never pair a
        # quoted drop with a quoted create, or prove a quoted table new.
        quoted = """-- ameesh: trigger compatible with previous code
        DROP TRIGGER IF EXISTS "old" ON t;
        CREATE TRIGGER "new" BEFORE INSERT ON t
            FOR EACH ROW EXECUTE FUNCTION guard();"""
        self.assertEqual(sm.classify(quoted).kind, 'destructive')
        self.assertEqual(sm.classify(
            '-- ameesh: constraint not tightened\n'
            'ALTER TABLE t DROP CONSTRAINT IF EXISTS "old";\n'
            'ALTER TABLE t ADD CONSTRAINT "new" CHECK (a > 0);').kind,
            'destructive')
        # A conditional branch keeps the worst class: the drop and its
        # re-creation may not execute together.
        conditional = """-- ameesh: trigger compatible with previous code
        DO $$ BEGIN DROP TRIGGER IF EXISTS g ON t;
        CREATE TRIGGER g BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f(); END $$;"""
        self.assertEqual(sm.classify(conditional).kind, 'destructive')

    def test_create_or_replace_routine_proven_new(self):
        # #7655: 0567 and 0575 introduce their functions with CREATE OR
        # REPLACE, but no earlier migration defines them.
        sql = ("CREATE OR REPLACE FUNCTION ameesh_host_shared(h text) "
               "RETURNS boolean LANGUAGE sql AS $$ SELECT true $$;")
        self.assertEqual(sm.classify(sql).kind, 'relaxation')
        self.assertEqual(sm.classify(sql, proves_new=lambda kind, name, signature="": True).kind,
                         'additive')
        self.assertEqual(sm.check('0575.sql', sql.encode(), sm.load_policy(),
                                  read_file=lambda p:b'', signed_decision=lambda *a:False,
                                  proves_new=lambda kind, name, signature="": True).kind,
                         'additive')
        # A name present in an earlier migration keeps the relaxation class.
        prover = lambda kind, name, signature="": name == "another_function"
        self.assertEqual(sm.classify(sql, proves_new=prover).kind, 'relaxation')
        self.assertEqual(sm.classify(
            "CREATE OR REPLACE PROCEDURE p() LANGUAGE sql AS $$ SELECT 1 $$;",
            proves_new=lambda kind, name, signature="": kind == "procedure").kind,
            'additive')
        # Qualified or quoted names are not proven by the frozen-tree search.
        self.assertEqual(sm.classify(
            "CREATE OR REPLACE FUNCTION public.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
            proves_new=lambda kind, name, signature="": True).kind, 'relaxation')

    def test_trigger_previous_writes_declaration_and_owner_proof(self):
        for timing in ('BEFORE', 'AFTER'):
            for form in ('TRIGGER', 'CONSTRAINT TRIGGER', 'OR REPLACE TRIGGER'):
                with self.subTest(timing=timing, form=form):
                    data = (f'CREATE {form} guard {timing} INSERT ON x '
                            'FOR EACH ROW EXECUTE FUNCTION guard_write();').encode()
                    policy = sm.load_policy()
                    entry = {'checksum':hashlib.sha256(data).hexdigest(), 'lot':17,
                             'restore':'guard_restore.sql'}
                    policy['declarations']['trigger.sql'] = entry
                    def check(verifier=lambda *args:False, restore=b'DROP TRIGGER guard ON x;'):
                        return sm.check('trigger.sql', data, policy,
                                        read_file=lambda p:restore, signed_decision=verifier)
                    with self.assertRaisesRegex(sm.Refused, 'boolean rejects_previous_writes'):
                        check()
                    for value in (None, 'false', 0, 1):
                        entry['rejects_previous_writes'] = value
                        entry['previous_writes_justification'] = 'Reviewed function'
                        with self.assertRaisesRegex(sm.Refused, 'boolean rejects_previous_writes'):
                            check()
                    entry['rejects_previous_writes'] = False
                    for justification in (None, '', '  ', True):
                        entry['previous_writes_justification'] = justification
                        with self.assertRaisesRegex(sm.Refused, 'justification'):
                            check()
                    entry['previous_writes_justification'] = 'Function only records writes'
                    self.assertEqual(check().kind, 'relaxation')
                    with self.assertRaisesRegex(sm.Refused, 'restore'):
                        check(restore=b'')
                    entry['rejects_previous_writes'] = True
                    with self.assertRaisesRegex(sm.Refused, 'signed owner agreement'):
                        check()
                    entry.update(decision=42, approval_option='approve')
                    with self.assertRaisesRegex(sm.Refused, 'signed owner agreement'):
                        check()
                    calls = []
                    self.assertEqual(check(lambda *args:calls.append(args) or True).kind, 'relaxation')
                    self.assertEqual(calls, [(42,17,'approve')])

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

    def test_routine_proof_reads_only_earlier_frozen_migrations(self):
        # #7655: a CREATE OR REPLACE FUNCTION is additive only when the name
        # is absent from every migration of a lower version in the frozen
        # tree; a replacement of an object created earlier stays relaxation.
        base = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
        replace = "CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 2 $$;\n"
        new = "CREATE OR REPLACE FUNCTION g() RETURNS int LANGUAGE sql AS $$ SELECT 3 $$;\n"
        self.put('src/ameesh/migrations/0100_base.sql', base)
        self.put('src/ameesh/migrations/0200_replace.sql', replace)
        self.put('src/ameesh/migrations/0300_new.sql', new)
        self.put('rollback/0200_restore.sql', '-- reviewed restore\n')
        self.policy({'src/ameesh/migrations/0200_replace.sql': {
            'checksum': hashlib.sha256(replace.encode()).hexdigest(), 'lot': 17,
            'restore': 'rollback/0200_restore.sql'}})
        sha = self.commit()
        report = sm.inventory(
            sm.GitTree(str(self.repo), sha),
            ['src/ameesh/migrations/0200_replace.sql',
             'src/ameesh/migrations/0300_new.sql'],
            signed_decision=lambda *args: False)
        self.assertTrue(report['allowed'], report)
        by_path = {row['path']: row for row in report['files']}
        self.assertEqual(by_path['src/ameesh/migrations/0200_replace.sql']['classification'],
                         'relaxation')
        self.assertEqual(by_path['src/ameesh/migrations/0300_new.sql']['classification'],
                         'additive')

    def test_twenty_pending_migrations_pass_at_the_frozen_head(self):
        # #7655 witness: the exact 0565..0584 inventory of the SHA where the
        # fix is taken passes the gate, through the same CLI the applicator
        # uses, with no signed owner decision.
        root = Path(__file__).resolve().parents[1]
        try:
            sha = subprocess.check_output(
                ['git', '-C', str(root), 'rev-parse', 'HEAD'],
                stderr=subprocess.DEVNULL).decode().strip()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('no frozen Git checkout available')
        paths = ['src/ameesh/migrations/' + path.name for path in
                 sorted((root/'src/ameesh/migrations').glob('05*.sql'))
                 if '0565' <= path.name[:4] <= '0584']
        self.assertEqual(len(paths), 20)
        def no_decision(*args):
            self.fail('no signed owner decision expected: %r' % (args,))
        report = sm.inventory(sm.GitTree(str(root), sha), paths,
                              signed_decision=no_decision)
        self.assertTrue(report['allowed'],
                        [row for row in report['files'] if not row.get('allowed')])
        kinds = [row['classification'] for row in report['files']]
        self.assertEqual(kinds.count('additive'), 18)
        self.assertEqual(kinds.count('relaxation'), 2)  # 0583/0584, declared
        pending = self.repo / 'pending-0565-0584.txt'
        pending.write_text('\n'.join(paths) + '\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sm.main(['--repo', str(root), '--sha', sha,
                                      '--project', 'ameesh',
                                      '--files-from', str(pending)]), 0)

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

    def test_frozen_trigger_declaration_controls_owner_gate(self):
        sql = """CREATE FUNCTION guard_write() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'write refused'; END $$;
        DO $$ BEGIN IF to_regclass('x') IS NOT NULL THEN
        CREATE TRIGGER guard BEFORE INSERT ON x FOR EACH ROW
        EXECUTE FUNCTION guard_write(); END IF; END $$;"""
        self.put('sql/trigger.sql', sql)
        self.put('rollback/trigger_restore.sql', 'DROP TRIGGER guard ON x;')
        entry = {'checksum':hashlib.sha256(sql.encode()).hexdigest(), 'lot':17,
                 'restore':'rollback/trigger_restore.sql',
                 'previous_writes_justification':'Function rejects writes incompatible with the new invariant'}
        self.policy({'sql/trigger.sql':entry})
        self.assertFalse(self.run_inventory(['sql/trigger.sql'])['allowed'])
        entry['rejects_previous_writes'] = True
        self.policy({'sql/trigger.sql':entry})
        self.assertFalse(self.run_inventory(['sql/trigger.sql'])['allowed'])
        entry.update(decision=42, approval_option='approve')
        self.policy({'sql/trigger.sql':entry})
        calls = []
        self.assertTrue(self.run_inventory(['sql/trigger.sql'],
            lambda *args:calls.append(args) or True)['allowed'])
        self.assertEqual(calls, [(42,17,'approve')])

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
            current.assert_called_once_with(mock.ANY, view, public_only=False)
            proof.assert_called_once_with(mock.ANY, view, public_only=False)
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
