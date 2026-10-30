# SPDX-License-Identifier: AGPL-3.0-only
"""L56: executor data access without schema administration."""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import subprocess
import uuid
import urllib.parse
import unittest
from unittest import mock

from ameesh import migrations

from ameesh import db as db_mod, decisions, mail, registry, work, modules
from ameesh.modules.storage import ModuleStore
from tests.test_l317_project_modules import federation

from .support import REPO, PgTestCase, attendu
from .test_cluster import TEST_DSN, _dsn_for

ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-executor.sql")

#: `ameesh_canon_sync` est le nom fixe que le déclencheur de politique de la
#: migration 0363 refuse de remplacer, et aucune migration ne lui accorde de
#: droit dans les schémas voisins : il reste le nom de production, créé et
#: détruit sous ce verrou. Les deux groupes d'écriture, eux, reçoivent EXECUTE
#: dans chaque schéma migré tant qu'ils existent (migration 0427) : un nom
#: fixe y laisse une ACL qui empêche `DROP ROLE` (h-32 v2, #3842, --parallel).
#: Leurs noms de test sont donc suffixés par processus, et le script du contrat
#: est exécuté sur une copie où seuls ces deux noms sont remplacés.
MODULE_WRITERS_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('ameesh-role-module-writers', 0))"


def _database_admin(cfg):
    """Connexion d'administration sans `statement_timeout` (#4822).

    Un CREATE/DROP DATABASE déclenche un checkpoint immédiat ; sur un banc
    chargé il dépasse le délai d'instruction de la suite (30 s). Un DROP
    annulé laisse alors la base invalide (`datconnlimit = -2`) et l'autovacuum
    du banc s'arrête (#4680). Le test ne supprime que sa propre base : la
    connexion qui porte le geste ne doit avoir aucune borne d'instruction,
    y compris un délai hérité du rôle, de la base, de `PGOPTIONS` ou du DSN
    (`db.execute_without_statement_timeout`).
    """
    return db_mod.connect(dataclasses.replace(cfg, statement_timeout_ms=0))


def _create_database(cfg, database: str) -> None:
    admin = _database_admin(cfg)
    try:
        db_mod.execute_without_statement_timeout(
            admin, "CREATE DATABASE %s TEMPLATE template0"
            % db_mod.quote_ident(database))
    finally:
        admin.close()


def _drop_database(cfg, database: str) -> None:
    admin = _database_admin(cfg)
    try:
        db_mod.execute_without_statement_timeout(
            admin, "DROP DATABASE IF EXISTS %s WITH (FORCE)"
            % db_mod.quote_ident(database))
    finally:
        admin.close()


class RoleExecutorTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Database ACL tests must not change PUBLIC privileges in the shared bench.
        cls.admin_db = cls.db
        cls.database = "t_executor_db_" + uuid.uuid4().hex[:8]
        # Le cleanup est posé AVANT la création : un CREATE annulé peut lui
        # aussi laisser une base invalide, que ce geste retire (#4822).
        cls.addClassCleanup(cls.drop_database)
        _create_database(cls.cfg, cls.database)
        parts = urllib.parse.urlsplit(TEST_DSN)
        if not parts.scheme.startswith("postgres"):
            raise AssertionError("Executor tests require a PostgreSQL URL DSN")
        cls.owner_dsn = urllib.parse.urlunsplit(parts._replace(path="/" + cls.database))
        cls.admin_db.execute("DROP SCHEMA %s CASCADE" % db_mod.quote_ident(cls.schema))
        cls.cfg = dataclasses.replace(cls.cfg, dsn=cls.owner_dsn)
        cls.db = db_mod.connect(cls.cfg)
        migrations.migrate(cls.db, log=None)
        cls.db.execute("REVOKE TEMP ON DATABASE %s FROM PUBLIC" % db_mod.quote_ident(cls.database))
        suffix = uuid.uuid4().hex[:8]
        cls.role = "t_executor_%s" % suffix
        cls.login = "t_executor_host_%s" % suffix
        cls.password = secrets.token_hex(16)
        try:
            for _ in range(2):          # Verify idempotence
                proc = cls.run_script(cls.role)
                if proc.returncode != 0:
                    raise AssertionError("role-executor.sql : %s" % proc.stderr)
            cls.db.execute("CREATE ROLE %s LOGIN PASSWORD '%s' IN ROLE %s" % (
                db_mod.quote_ident(cls.login), cls.password, db_mod.quote_ident(cls.role)))
            cls.db.execute("ALTER ROLE %s SET search_path = %s" % (
                db_mod.quote_ident(cls.login), db_mod.quote_ident(cls.schema)))
        except Exception:
            cls.drop_roles()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.drop_roles()
        finally:
            super().tearDownClass()

    @classmethod
    def drop_database(cls):
        if cls.db is not cls.admin_db:
            cls.db.close()
        try:
            _drop_database(cls.admin_db.cfg, cls.database)
        finally:
            cls.admin_db.close()

    @classmethod
    def host_dsn(cls):
        parts = urllib.parse.urlsplit(_dsn_for(cls.login, cls.password))
        return urllib.parse.urlunsplit(parts._replace(path="/" + cls.database))

    @classmethod
    def drop_roles(cls) -> None:
        for name in (cls.login, cls.role):
            if cls.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)):
                cls.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(name))
                cls.db.execute("DROP ROLE %s" % db_mod.quote_ident(name))

    @classmethod
    def run_script(cls, role: str, *, schema=None, applying_role=None) -> subprocess.CompletedProcess:
        options = "-c search_path=%s" % (schema or cls.schema)
        if applying_role:
            options += " -c role=%s" % applying_role
        env = dict(os.environ, PGOPTIONS=options)
        return subprocess.run(["psql", cls.owner_dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                               "-v", "role=%s" % role, "-f", ROLE_SQL],
                              capture_output=True, text=True, timeout=60, env=env)

    def as_host(self, sql: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("PGPASSWORD", None)
        env.pop("PGOPTIONS", None)
        return subprocess.run(["psql", self.host_dsn(), "-X", "-At",
                               "-v", "ON_ERROR_STOP=1", "-c", sql],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_drop_database_does_not_inherit_the_statement_timeout(self):
        """#4822 : un DROP annulé par le délai de la suite laissait la base
        invalide (`datconnlimit = -2`). Avec 1 ms, le geste serait annulé ;
        la connexion d'administration le remplace par 0 et la base part."""
        database = "t_executor_db_" + uuid.uuid4().hex[:8]
        _create_database(self.admin_db.cfg, database)
        self.addCleanup(_drop_database, self.admin_db.cfg, database)
        _drop_database(dataclasses.replace(self.admin_db.cfg,
                                           statement_timeout_ms=1), database)
        self.assertEqual(self.db.query(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)), [])

    def test_host_reads_and_writes_data(self):
        cfg = dataclasses.replace(self.cfg, dsn=self.host_dsn())
        db = db_mod.connect(cfg)
        try:
            registry.upsert(db, "verif-a", harness="claude", host="pc", mode="execute")
            ident = mail.send(db, "coord", "verif-a", "review the migration")
            self.assertTrue(ident > 0)
            self.assertEqual(db.query("SELECT name FROM agent_registry WHERE name = 'verif-a'"),
                             [{"name": "verif-a"}])
        finally:
            db.close()

    def test_host_reads_a_real_cluster_identity(self):
        """Codex r6: the event stream's position carries the cluster; a host
        login, read-only like `ameesh serve`, must get a real one, not
        `none` (every resume a reset). Revoked from PUBLIC (hardened
        server): the script warns, and the superuser's grant restores it."""
        from ameesh import storage

        def cluster():
            cfg = dataclasses.replace(self.cfg, dsn=self.host_dsn(), read_only=True)
            db = db_mod.connect(cfg)
            try:
                return storage.of(db).events.bounds()["cluster"]
            finally:
                db.close()

        own = storage.of(self.db).events.bounds()["cluster"]
        # #928 : la position porte aussi l'identité propre du journal (j + 16 hexa)
        self.assertRegex(own, "^[0-9a-f]{1,16}t[0-9a-f]{8}(j[0-9a-f]{16})?$")
        self.assertEqual(cluster(), own)
        functions = ("pg_control_system(), pg_control_checkpoint(),"
                     " pg_current_wal_lsn(), pg_walfile_name(pg_lsn)")
        # pg_catalog is per database: this test's own database only
        self.addCleanup(self.db.execute,
                        "GRANT EXECUTE ON FUNCTION %s TO PUBLIC" % functions)
        self.addCleanup(self.db.execute, "REVOKE EXECUTE ON FUNCTION %s FROM %s" % (
            functions, db_mod.quote_ident(self.role)))
        self.db.execute("REVOKE EXECUTE ON FUNCTION pg_control_system() FROM PUBLIC")
        self.assertIsNone(cluster())
        proc = self.run_script(self.role)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("may not execute pg_control_system()", proc.stderr,
                      attendu(proc.stderr))
        self.db.execute("GRANT EXECUTE ON FUNCTION %s TO %s" % (
            functions, db_mod.quote_ident(self.role)))
        self.assertEqual(cluster(), own)
        proc = self.run_script(self.role)
        self.assertNotIn("may not execute", proc.stderr)

    def test_executor_cannot_forge_execution_proof_or_rewrite_policy(self):
        import json
        ModuleStore(self.db).replace("", modules.declarations(federation()))
        lot = work.add(self.db, title="Release", type="release", app="acme")["id"]
        delivery = ModuleStore(self.db).record(lot, "it", "delivery", "a" * 64,
                                               {"artifact": "/artifact", "sha256": "a" * 64},
                                               actor="test")
        payload = db_mod.sql_literal(json.dumps({"artifact": "/artifact", "sha256": "a" * 64,
                                                 "exit_code": 0, "command": ["true"], "output": "invented"}))
        sql = ("INSERT INTO work_module_milestones(work_item_id,module,kind,subject,binding_id,payload,actor) "
               "VALUES (%s,'it','proof','%s',%s,%s::jsonb,'fake')" %
               (lot, "a" * 64, delivery["id"], payload))
        result = self.as_host(sql)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("isolated writer role", result.stderr, attendu(result.stderr))
        for sql in ("UPDATE project_modules SET rules = '[]'::jsonb",
                    "DELETE FROM project_modules",
                    "INSERT INTO project_modules SELECT 'other',module,canon,types,schemas,rules FROM project_modules"):
            result = self.as_host(sql)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("permission denied", result.stderr, attendu(result.stderr))
        # Even an accidental grant cannot bypass the canonical writer trigger.
        self.db.execute("GRANT UPDATE ON project_modules TO %s" % db_mod.quote_ident(self.role))
        try:
            result = self.as_host("UPDATE project_modules SET rules = '[]'::jsonb")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("isolated canon sync role", result.stderr, attendu(result.stderr))
        finally:
            self.db.execute("REVOKE UPDATE ON project_modules FROM %s" % db_mod.quote_ident(self.role))
        # The trusted policies are unchanged and direct storage still refuses closure.
        # Reach the module-policy gate after an independently valid Git proof.
        with mock.patch.object(work, "_declared_merge_commit", return_value="abcdef1234"), \
                self.assertRaises(work.WorkError):
            work.merged(self.db, lot, sha="abcdef1234")

    def test_executor_appends_but_cannot_rewrite_the_signed_supersession_log(self):
        """#4995: SELECT and INSERT (the delegated decision cycle appends one
        event), never UPDATE nor DELETE; the append-only trigger refuses even
        an accidental rewrite grant."""
        q = db_mod.quote_ident
        table = "%s.decision_supersessions" % q(self.schema)
        for privilege in ("SELECT", "INSERT"):
            self.assertTrue(self.db.query(
                "SELECT has_table_privilege(%s, %s, %s) AS p",
                (self.role, table, privilege))[0]["p"], privilege)
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "TRIGGER"):
            self.assertFalse(self.db.query(
                "SELECT has_table_privilege(%s, %s, %s) AS p",
                (self.role, table, privilege))[0]["p"], privilege)
        decision_payload = json.dumps({"decision": {"state": "answered"}})
        rows = self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload) VALUES"
            " ('a', 'human:x', 'old authority', 'request', %s::jsonb),"
            " ('a', 'human:x', 'replacement', 'request', %s::jsonb),"
            " ('a', 'human:x', 'delegated old', 'request', %s::jsonb),"
            " ('a', 'human:x', 'delegated replacement', 'request', %s::jsonb)"
            " RETURNING id",
            (decision_payload, decision_payload, decision_payload, decision_payload))
        old_id, new_id, delegated_old, delegated_new = sorted(
            int(row["id"]) for row in rows)
        self.db.execute(
            "INSERT INTO decision_supersessions"
            " (supersedes, superseded_by, person, occurred_at, proof)"
            " VALUES (%s, %s, 'alice', now(), '{\"digest\": \"d\"}'::jsonb)",
            (old_id, new_id))
        self.db.execute(
            "UPDATE agent_mailbox SET payload = jsonb_set(payload,"
            " '{decision,superseded_by}', to_jsonb(%s::bigint)) WHERE id = %s",
            (new_id, old_id))
        # The delegated answer runs under the executor role: it appends the
        # event and sets the marker after it, but can neither clear nor move a
        # marker once set, nor rewrite the log.
        result = self.as_host(
            "INSERT INTO decision_supersessions"
            " (supersedes, superseded_by, person, occurred_at, proof)"
            " VALUES (%d, %d, 'alice', now(), '{\"digest\": \"d\"}'::jsonb)"
            % (delegated_old, delegated_new))
        self.assertEqual(result.returncode, 0, attendu(result.stderr))
        result = self.as_host(
            "UPDATE agent_mailbox SET payload = jsonb_set(payload,"
            " '{decision,superseded_by}', to_jsonb(%s::bigint)) WHERE id = %d"
            % (delegated_new, delegated_old))
        self.assertEqual(result.returncode, 0, attendu(result.stderr))
        for sql in ("UPDATE agent_mailbox SET payload = payload #-"
                    " '{decision,superseded_by}' WHERE id = %d" % delegated_old,
                    "UPDATE agent_mailbox SET payload = jsonb_set(payload,"
                    " '{decision,superseded_by}', to_jsonb(%s::bigint)) WHERE id = %d"
                    % (old_id, delegated_old)):
            result = self.as_host(sql)
            self.assertNotEqual(result.returncode, 0, sql)
            self.assertIn("decision supersession marker is append-only", result.stderr,
                          attendu(result.stderr))
        for sql in ("UPDATE decision_supersessions SET person = 'x'",
                    "DELETE FROM decision_supersessions",
                    "TRUNCATE decision_supersessions"):
            result = self.as_host(sql)
            self.assertNotEqual(result.returncode, 0, sql)
            self.assertIn("permission denied", result.stderr, attendu(result.stderr))
        # Even an accidental grant cannot bypass the append-only trigger.
        self.db.execute("GRANT UPDATE, DELETE ON decision_supersessions TO %s" % q(self.role))
        try:
            for sql in ("UPDATE decision_supersessions SET person = 'x'",
                        "DELETE FROM decision_supersessions"):
                result = self.as_host(sql)
                self.assertNotEqual(result.returncode, 0, sql)
                self.assertIn("append-only", result.stderr, attendu(result.stderr))
        finally:
            self.db.execute("REVOKE UPDATE, DELETE ON decision_supersessions FROM %s"
                            % q(self.role))
        self.assertEqual(sorted(
            (int(row["supersedes"]), int(row["superseded_by"]))
            for row in self.db.query(
                "SELECT supersedes, superseded_by FROM decision_supersessions")),
            [(old_id, new_id), (delegated_old, delegated_new)])
        self.assertEqual(sorted(
            int(row["marker"]) for row in self.db.query(
                "SELECT payload->'decision'->>'superseded_by' AS marker"
                " FROM agent_mailbox WHERE id IN (%s, %s)", (old_id, delegated_old))),
            [new_id, delegated_new])

    def test_event_log_grant_reaches_an_isolated_writer(self):
        """R2 auditeur P2-2: migration 0396's triggers write `ameesh_events`
        with the writer's rights. An isolated writer granted table by table
        before the migration (role-module-writers.sql, no ALTER DEFAULT
        PRIVILEGES) gets 'permission denied for table ameesh_events' and can
        no longer journal; the migration hands it the log's INSERT right."""
        q = db_mod.quote_ident
        role = "t_events_writer_" + uuid.uuid4().hex[:8]
        self.db.execute("CREATE ROLE %s" % q(role))
        self.addCleanup(self.db.execute, "DROP ROLE %s" % q(role))
        self.addCleanup(self.db.execute, "DROP OWNED BY %s" % q(role))
        self.db.execute("GRANT USAGE ON SCHEMA %s TO %s" % (q(self.schema), q(role)))
        self.db.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %s"
                        " TO %s" % (q(self.schema), q(role)))
        self.db.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %s TO %s"
                        % (q(self.schema), q(role)))
        # the grant a protected deployment took before migration 0396 existed
        self.db.execute("REVOKE ALL ON ameesh_events FROM %s" % q(role))
        self.db.execute("REVOKE ALL ON SEQUENCE ameesh_events_id_seq FROM %s" % q(role))
        lot = work.add(self.db, title="journal", app="demo")["id"]
        insert = ("INSERT INTO work_item_events (work_item_id, state, actor)"
                  " VALUES (%d, 'build', 'writer')" % lot)
        with self.assertRaises(db_mod.DbError) as refused, self.db.transaction() as tx:
            tx.execute("SET LOCAL ROLE %s" % q(role))
            tx.execute(insert)
        self.assertIn("permission denied for table ameesh_events", str(refused.exception))
        self.db.execute("SELECT ameesh_events_grant_writers()")
        before = self.db.query("SELECT count(*) AS n FROM ameesh_events"
                               " WHERE object = 'lot:%d'" % lot)[0]["n"]
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ROLE %s" % q(role))
            tx.execute(insert)
        self.assertEqual(
            self.db.query("SELECT count(*) AS n FROM ameesh_events"
                          " WHERE object = 'lot:%d'" % lot)[0]["n"], before + 1)

    def test_module_writer_role_contract_and_executor_separation(self):
        q = db_mod.quote_ident
        # 0427 accorde EXECUTE à ces deux noms dans chaque schéma migré tant
        # qu'ils existent : les suffixer par processus empêche un worker
        # parallèle de laisser une ACL sur un rôle qu'on s'apprête à détruire.
        suffix = uuid.uuid4().hex[:8]
        canon = "ameesh_canon_sync"
        proof = "ameesh_it_proof_%s" % suffix
        qa = "ameesh_it_qa_%s" % suffix
        names = (canon, proof, qa)
        # Le nom de canon sync est figé par le déclencheur de 0363 : le verrou
        # sérialise sa création/destruction entre les runs du banc.
        with self.admin_db.transaction() as lock_db:
            lock_db.execute(MODULE_WRITERS_LOCK)
            placeholders = ", ".join(["%s"] * len(names))
            existing = {r["rolname"] for r in self.db.query(
                "SELECT rolname FROM pg_roles WHERE rolname IN (%s)" % placeholders, names)}
            script = os.path.join(REPO, "deploy", "sql", "role-module-writers.sql")
            with open(script, encoding="utf-8") as source_file:
                source = source_file.read()
            for fixed, scoped in (("ameesh_it_proof", proof), ("ameesh_it_qa", qa)):
                self.assertIn(fixed, source)
                source = source.replace(fixed, scoped)
            scoped_script = os.path.join(self.tmp, "role-module-writers-%s.sql" % suffix)
            with open(scoped_script, "w", encoding="utf-8") as target:
                target.write(source)

            def run_writers():
                return subprocess.run(
                    ["psql", self.owner_dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                     "-f", scoped_script], capture_output=True, text=True, timeout=60,
                    env=dict(os.environ, PGOPTIONS="-c search_path=%s" % self.schema))

            try:
                for _ in range(2):
                    proc = run_writers()
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                # #2274: the review catch-up activation bound stays owner-only;
                # granting canon sync DML on it would move the review horizon.
                self.assertFalse(self.db.query(
                    "SELECT has_table_privilege(%s, %s,"
                    " 'INSERT,UPDATE,DELETE') AS p",
                    (canon, "%s.review_catch_up" % q(self.schema)))[0]["p"])
                # #2481: a full work row projects its reserved migration slots;
                # both isolated writers read the lot through that projection.
                for writer_role in (proof, qa):
                    with self.subTest(writer_role=writer_role):
                        self.assertTrue(self.db.query(
                            "SELECT has_table_privilege(%s, %s, 'SELECT') AS p",
                            (writer_role, "%s.work_migration_reservations" % q(self.schema),))[0]["p"])
                # #4975: additive migration also upgrades already-installed
                # service groups; the role script must preserve read-only ACLs.
                with open(os.path.join(migrations.MIGRATIONS_DIR,
                          '0524_schema_write_guard.sql'), encoding='utf-8') as marker_file:
                    marker_sql = marker_file.read()
                for fixed, scoped in (("ameesh_it_proof", proof), ("ameesh_it_qa", qa)):
                    marker_sql = marker_sql.replace(fixed, scoped)
                self.db.execute('DROP TABLE schema_non_additive_migrations')
                self.db.script(marker_sql)
                for _ in range(2):
                    for writer_role in names:
                        row = self.db.query(
                            "SELECT has_table_privilege(%s, 'schema_non_additive_migrations', 'SELECT') AS reads, "
                            "has_table_privilege(%s, 'schema_non_additive_migrations', 'INSERT,UPDATE,DELETE') AS writes",
                            (writer_role, writer_role))[0]
                        self.assertTrue(row['reads'], writer_role)
                        self.assertFalse(row['writes'], writer_role)
                    proc = run_writers()
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                self.db.execute('GRANT UPDATE ON schema_non_additive_migrations TO PUBLIC')
                try:
                    proc = run_writers()
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn('must not change the schema compatibility inventory', proc.stderr)
                finally:
                    self.db.execute('REVOKE UPDATE ON schema_non_additive_migrations FROM PUBLIC')
                # This class has a private database: never mutate shared bench ACLs.
                for grantee in (*names, "PUBLIC"):
                    with self.subTest(temp_grantee=grantee):
                        target = "PUBLIC" if grantee == "PUBLIC" else q(grantee)
                        self.db.execute("GRANT TEMP ON DATABASE %s TO %s" %
                                        (q(self.database), target))
                        try:
                            proc = run_writers()
                            self.assertNotEqual(proc.returncode, 0)
                            self.assertIn("must not create temporary objects", proc.stderr, attendu(proc.stderr))
                        finally:
                            self.db.execute("REVOKE TEMP ON DATABASE %s FROM %s" %
                                            (q(self.database), target))
                # #1194 (N1): the groups are NOLOGIN, so a member login holding
                # TEMP re-opens the shadowing path; the audit walks the direct and
                # inherited memberships, not just the group.
                member = "t_writer_member_" + uuid.uuid4().hex[:8]
                inherited = "t_writer_parent_" + uuid.uuid4().hex[:8]
                other = "t_writer_other_" + uuid.uuid4().hex[:8]
                self.db.execute("CREATE ROLE %s LOGIN" % q(member))
                self.db.execute("CREATE ROLE %s NOLOGIN" % q(inherited))
                self.db.execute("CREATE ROLE %s NOLOGIN" % q(other))
                try:
                    # TEMP on an unrelated role must not make the script fail.
                    self.db.execute("GRANT TEMP ON DATABASE %s TO %s" %
                                    (q(self.database), q(other)))
                    proc = run_writers()
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    for group in names:
                        with self.subTest(direct_writer_group=group):
                            self.db.execute("GRANT %s TO %s" % (q(group), q(member)))
                            self.db.execute("GRANT TEMP ON DATABASE %s TO %s" %
                                            (q(self.database), q(member)))
                            try:
                                proc = run_writers()
                                self.assertNotEqual(proc.returncode, 0)
                                self.assertIn("has a member with TEMP", proc.stderr, attendu(proc.stderr))
                            finally:
                                self.db.execute("REVOKE TEMP ON DATABASE %s FROM %s" %
                                                (q(self.database), q(member)))
                                self.db.execute("REVOKE %s FROM %s" % (q(group), q(member)))
                    self.db.execute("GRANT %s TO %s" % (q(qa), q(inherited)))
                    self.db.execute("GRANT %s TO %s" % (q(inherited), q(member)))
                    for grantee in (inherited, member):
                        with self.subTest(temp_member=grantee):
                            self.db.execute("GRANT TEMP ON DATABASE %s TO %s" %
                                            (q(self.database), q(grantee)))
                            try:
                                proc = run_writers()
                                self.assertNotEqual(proc.returncode, 0)
                                self.assertIn("has a member with TEMP", proc.stderr, attendu(proc.stderr))
                            finally:
                                self.db.execute("REVOKE TEMP ON DATABASE %s FROM %s" %
                                                (q(self.database), q(grantee)))
                    # A member without TEMP leaves the contract intact.
                    proc = run_writers()
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    # An additional parent can confer TEMP through inheritance or
                    # only after SET ROLE; both paths allow creating a shadow table.
                    self.db.execute("GRANT %s TO %s" % (q(other), q(member)))
                    for inherit in (True, False):
                        with self.subTest(temp_parent_inherit=inherit):
                            self.db.execute("GRANT %s TO %s WITH INHERIT %s" %
                                            (q(other), q(member), "TRUE" if inherit else "FALSE"))
                            proc = run_writers()
                            self.assertNotEqual(proc.returncode, 0)
                            self.assertIn("has a member with TEMP", proc.stderr, attendu(proc.stderr))
                    self.db.execute("REVOKE %s FROM %s" % (q(other), q(member)))
                    proc = run_writers()
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                finally:
                    self.db.execute("DROP OWNED BY %s, %s, %s" % (q(member), q(inherited), q(other)))
                    self.db.execute("DROP ROLE %s, %s, %s" % (q(member), q(inherited), q(other)))
                self.assertEqual(self.run_script(self.role).returncode, 0)
                for name in names:
                    self.assertNotEqual(self.as_host("SET ROLE %s" % q(name)).returncode, 0)
                with self.db.transaction() as tx:
                    tx.execute("SET LOCAL ROLE %s" % q(canon))
                    ModuleStore(tx).replace("", modules.declarations(federation()))
                # Neither executor nor proof/QA writer is a policy writer.
                for writer in (proof, qa):
                    with self.assertRaises(db_mod.DbError), self.db.transaction() as tx:
                        tx.execute("SET LOCAL ROLE %s" % writer)
                        ModuleStore(tx).replace("", [])
            finally:
                for name in names:
                    self.db.execute("DROP OWNED BY %s" % q(name))
                    if name not in existing:
                        self.db.execute("DROP ROLE %s" % q(name))

    def test_isolated_writer_records_observed_execution_without_policy_rights(self):
        import sys
        from pathlib import Path
        from ameesh.modules import it
        q = db_mod.quote_ident
        writer = "t_proof_writer_" + uuid.uuid4().hex[:8]
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(writer))
        try:
            self.db.execute("GRANT USAGE ON SCHEMA %s TO %s" % (q(self.schema), q(writer)))
            # #2481: the full work row projects its reserved migration slots.
            self.db.execute("GRANT SELECT ON work_items, project_modules, "
                            "work_module_milestones, work_migration_reservations TO %s"
                            % q(writer))
            self.db.execute("GRANT UPDATE(id) ON work_items TO %s" % q(writer))
            self.db.execute("GRANT INSERT ON work_module_milestones TO %s" % q(writer))
            self.db.execute("GRANT USAGE ON SEQUENCE work_module_milestones_id_seq TO %s" % q(writer))
            entries = modules.declarations(federation())
            entries[0]["schemas"]["proof"]["writer_role"] = writer
            ModuleStore(self.db).replace("", entries)
            artifact = Path(self.tmp, "artifact.py")
            artifact.write_text("print('observed execution')")
            lot = work.add(self.db, title="Release", type="release", app="acme")["id"]
            it.declare(self.db, lot, artifact, actor="test")
            with self.db.transaction() as tx:
                tx.execute("SET LOCAL ROLE %s" % q(writer))
                proof = it.prove(tx, lot, artifact, actor="producer", runner=sys.executable)
            self.assertEqual(proof["payload"]["output"], "observed execution")
            self.assertFalse(self.db.query("SELECT has_table_privilege(%s, 'project_modules', 'UPDATE') AS allowed",
                                           (writer,))[0]["allowed"])
            with mock.patch.object(work, "_declared_merge_commit", return_value="abcdef1234"):
                self.assertEqual(work.merged(self.db, lot, sha="abcdef1234")["result"], "merged")
        finally:
            self.db.execute("DROP OWNED BY %s" % q(writer))
            self.db.execute("DROP ROLE %s" % q(writer))

    def test_controller_audit_checks_maintain_only_from_postgresql_17(self):
        """MAINTAIN n'existe qu'à partir de PG17 : l'audit ne l'interroge pas avant."""
        import tempfile
        from pathlib import Path
        q = db_mod.quote_ident
        controller = 't_release_maintain_' + uuid.uuid4().hex[:8]
        sql = Path(REPO,'deploy/sql/role-release-controller.sql')
        text = sql.read_text()
        self.assertIn("current_setting('server_version_num')",text)
        env = dict(os.environ, PGOPTIONS='-c search_path='+self.schema)
        def apply(path):
            return subprocess.run(['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                '-v','role='+controller,'-f',str(path)], env=env, text=True,
                capture_output=True, timeout=60)
        try:
            # Avant PG17 (ou sur le banc 17) l'audit passe : le nom MAINTAIN
            # n'est alors jamais demandé à has_table_privilege.
            result = apply(sql)
            self.assertEqual(result.returncode,0,result.stderr)
            version = int(self.db.query("SELECT current_setting('server_version_num') AS v")[0]['v'])
            if version < 170000:
                return
            self.db.execute('GRANT MAINTAIN ON host_release_fences TO %s' % q(controller))
            # PG17 : MAINTAIN est un droit réel, l'audit le refuse.
            result = apply(sql)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('unrelated write privilege',result.stderr,attendu(result.stderr))
            # Même audit avec la version forcée avant 17 : la liste ne porte
            # plus MAINTAIN, donc l'audit va au bout au lieu d'échouer sur un
            # nom de droit inconnu.
            with tempfile.TemporaryDirectory() as directory:
                older = Path(directory)/'role-release-controller.sql'
                older.write_text(text.replace("current_setting('server_version_num')::int",
                                              '160000'))
                result = apply(older)
                self.assertEqual(result.returncode,0,result.stderr)
        finally:
            if self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s',(controller,)):
                self.db.execute('DROP OWNED BY %s' % q(controller))
                self.db.execute('DROP ROLE %s' % q(controller))

    def test_inherited_policy_writes_make_executor_audit_fail(self):
        q = db_mod.quote_ident
        parent = "t_policy_parent_" + uuid.uuid4().hex[:8]
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(parent))
        try:
            self.db.execute("GRANT UPDATE ON project_modules TO %s" % q(parent))
            self.db.execute("GRANT %s TO %s" % (q(parent), q(self.role)))
            result = self.run_script(self.role)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("forbidden UPDATE on project_modules", result.stderr,
                          attendu(result.stderr))
        finally:
            self.db.execute("REVOKE %s FROM %s" % (q(parent), q(self.role)))
            self.db.execute("DROP OWNED BY %s" % q(parent))
            self.db.execute("DROP ROLE %s" % q(parent))

    def test_executor_reads_claim_fences_without_release_controller_writes(self):
        for relation in ('host_release_operations','host_release_fences','host_release_fence_receipts'):
            for privilege in ('INSERT','UPDATE','DELETE'):
                with self.subTest(relation=relation, privilege=privilege):
                    self.assertFalse(self.db.query(
                        'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                        (self.login, relation, privilege))[0]['allowed'])
        self.assertEqual(self.as_host("SELECT ameesh_host_claims_open('synthetic-host')").returncode, 0)

    def test_inherited_release_fence_writes_make_executor_audit_fail(self):
        q = db_mod.quote_ident
        parent = 't_release_parent_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE ROLE %s NOLOGIN' % q(parent))
        try:
            self.db.execute('GRANT DELETE ON host_release_fences TO %s' % q(parent))
            self.db.execute('GRANT %s TO %s' % (q(parent),q(self.role)))
            result = self.run_script(self.role)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('forbidden DELETE on host_release_fences', result.stderr,
                          attendu(result.stderr))
        finally:
            self.db.execute('REVOKE %s FROM %s' % (q(parent),q(self.role)))
            self.db.execute('DROP OWNED BY %s' % q(parent))
            self.db.execute('DROP ROLE %s' % q(parent))

    def test_release_fence_migration_revokes_creation_time_executor_defaults(self):
        import shutil
        from pathlib import Path
        import tempfile
        q = db_mod.quote_ident
        schema = 't_release_upgrade_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE SCHEMA %s' % q(schema))
        cfg = dataclasses.replace(self.cfg, schema=schema)
        db = db_mod.connect(cfg)
        try:
            db.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT,INSERT,UPDATE,DELETE,'
                       'TRUNCATE,REFERENCES,TRIGGER ON TABLES TO %s'
                       % (q(schema),q(self.role)))
            with tempfile.TemporaryDirectory() as directory:
                shutil.copy(Path(REPO,'src/ameesh/migrations/0417_host_release_fences.sql'), directory)
                migrations.migrate(db, directory)
            for relation in ('host_release_operations','host_release_fences','host_release_fence_receipts'):
                for privilege in ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'):
                    self.assertFalse(db.query('SELECT has_table_privilege(%s,%s,%s) AS allowed',
                        (self.role,schema+'.'+relation,privilege))[0]['allowed'])
                self.assertTrue(db.query('SELECT has_table_privilege(%s,%s,%s) AS allowed',
                    (self.role,schema+'.'+relation,'SELECT'))[0]['allowed'])
        finally:
            db.close()
            self.db.execute('DROP SCHEMA %s CASCADE' % q(schema))

    def test_schema_guard_migration_revokes_creation_time_executor_defaults(self):
        import shutil
        from pathlib import Path
        import tempfile
        q = db_mod.quote_ident
        schema = 't_guard_upgrade_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE SCHEMA %s' % q(schema))
        cfg = dataclasses.replace(self.cfg, schema=schema)
        db = db_mod.connect(cfg)
        try:
            db.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT,INSERT,UPDATE,DELETE,'
                       'TRUNCATE,REFERENCES,TRIGGER ON TABLES TO %s'
                       % (q(schema),q(self.role)))
            db.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT UPDATE ON TABLES TO PUBLIC'
                       % q(schema))
            with tempfile.TemporaryDirectory() as directory:
                shutil.copy(Path(REPO,'src/ameesh/migrations/0524_schema_write_guard.sql'), directory)
                migrations.migrate(db, directory)
            for relation in ('schema_non_additive_migrations',):
                for privilege in ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'):
                    self.assertFalse(db.query('SELECT has_table_privilege(%s,%s,%s) AS allowed',
                        (self.role,schema+'.'+relation,privilege))[0]['allowed'])
                self.assertTrue(db.query('SELECT has_table_privilege(%s,%s,%s) AS allowed',
                    (self.role,schema+'.'+relation,'SELECT'))[0]['allowed'])
            result = self.run_script(self.role)
            self.assertEqual(result.returncode, 0, result.stderr)
            for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
                self.assertEqual(self.db.query('SELECT has_table_privilege(%s,%s,%s) AS allowed',
                    (self.role,self.schema+'.schema_non_additive_migrations',privilege))[0]['allowed'],
                    privilege == 'SELECT')
        finally:
            db.close()
            self.db.execute('DROP SCHEMA %s CASCADE' % q(schema))

    def test_executor_reads_review_catch_up_without_writes(self):
        """#1448 N1 : la borne d'activation du rattrapage après fusion (0438)
        est lisible par l'exécuteur, mais aucun rôle exécuteur ne la déplace."""
        for privilege in ('INSERT', 'UPDATE', 'DELETE'):
            with self.subTest(privilege=privilege):
                self.assertFalse(self.db.query(
                    'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                    (self.login, 'review_catch_up', privilege))[0]['allowed'])
        self.assertTrue(self.db.query(
            'SELECT has_table_privilege(%s,%s,%s) AS allowed',
            (self.login, 'review_catch_up', 'SELECT'))[0]['allowed'])
        self.assertEqual(self.as_host('SELECT bound_at FROM review_catch_up').returncode, 0)
        refused = self.as_host('UPDATE review_catch_up SET bound_at = now()')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('permission denied', refused.stderr.lower(),
                      attendu(refused.stderr.lower()))

    def test_inherited_review_bound_writes_make_executor_audit_fail(self):
        """#1448 N1 : un droit d'écriture hérité sur la borne (rôle parent ou
        octroi direct) fait échouer le contrat de rôle, pas seulement le
        rôle nommé."""
        q = db_mod.quote_ident
        parent = 't_bound_parent_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE ROLE %s NOLOGIN' % q(parent))
        try:
            self.db.execute('GRANT UPDATE ON review_catch_up TO %s' % q(parent))
            self.db.execute('GRANT %s TO %s' % (q(parent), q(self.role)))
            result = self.run_script(self.role)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('forbidden UPDATE on review_catch_up', result.stderr,
                          attendu(result.stderr))
        finally:
            self.db.execute('REVOKE %s FROM %s' % (q(parent), q(self.role)))
            self.db.execute('DROP OWNED BY %s' % q(parent))
            self.db.execute('DROP ROLE %s' % q(parent))

    def test_column_level_review_bound_write_is_revoked_from_executor(self):
        """#1448 B1 (R Codex cx2) : un GRANT UPDATE (bound_at) vit dans
        pg_attribute.attacl, que REVOKE ... ON TABLE ne nettoie pas. Le script
        révoque le droit de colonne : l'audit reste vert et l'UPDATE échoue."""
        q = db_mod.quote_ident
        self.db.execute('GRANT UPDATE (bound_at) ON review_catch_up TO %s' % q(self.role))
        self.assertTrue(self.db.query(
            'SELECT has_column_privilege(%s,%s,%s,%s) AS allowed',
            (self.role, 'review_catch_up', 'bound_at', 'UPDATE'))[0]['allowed'])
        result = self.run_script(self.role)
        self.assertEqual(result.returncode, 0, attendu(result.stderr))
        self.assertFalse(self.db.query(
            'SELECT has_column_privilege(%s,%s,%s,%s) AS allowed',
            (self.role, 'review_catch_up', 'bound_at', 'UPDATE'))[0]['allowed'])
        refused = self.as_host('UPDATE review_catch_up SET bound_at = now()')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('permission denied', refused.stderr.lower(),
                      attendu(refused.stderr.lower()))

    def test_column_level_review_bound_write_through_parent_makes_audit_fail(self):
        """#1448 B1 : un droit de colonne porté par un parent accessible par
        SET ROLE (membre WITH INHERIT FALSE) doit faire échouer le contrat."""
        q = db_mod.quote_ident
        parent = 't_bound_col_parent_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE ROLE %s NOLOGIN' % q(parent))
        try:
            self.db.execute('GRANT UPDATE (bound_at) ON review_catch_up TO %s' % q(parent))
            self.db.execute('GRANT %s TO %s WITH INHERIT FALSE' % (q(parent), q(self.role)))
            self.assertFalse(self.db.query(
                'SELECT has_column_privilege(%s,%s,%s,%s) AS allowed',
                (self.role, 'review_catch_up', 'bound_at', 'UPDATE'))[0]['allowed'])
            result = self.run_script(self.role)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('forbidden UPDATE on a column of review_catch_up through %s' % parent,
                          result.stderr, attendu(result.stderr))
        finally:
            self.db.execute('REVOKE %s FROM %s' % (q(parent), q(self.role)))
            self.db.execute('DROP OWNED BY %s' % q(parent))
            self.db.execute('DROP ROLE %s' % q(parent))

    def test_set_role_parent_review_bound_write_makes_executor_audit_fail(self):
        """#1448 B2 : un parent porteur d'UPDATE accessible par SET ROLE
        (membre WITH INHERIT FALSE) échappe à has_table_privilege(r) ; le
        contrat doit le refuser, car SET ROLE le rend effectif."""
        q = db_mod.quote_ident
        parent = 't_bound_setrole_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE ROLE %s NOLOGIN' % q(parent))
        try:
            self.db.execute('GRANT UPDATE ON review_catch_up TO %s' % q(parent))
            self.db.execute('GRANT %s TO %s WITH INHERIT FALSE' % (q(parent), q(self.role)))
            self.assertFalse(self.db.query(
                'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                (self.role, 'review_catch_up', 'UPDATE'))[0]['allowed'])
            result = self.run_script(self.role)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('forbidden UPDATE on review_catch_up through %s' % parent,
                          result.stderr, attendu(result.stderr))
        finally:
            self.db.execute('REVOKE %s FROM %s' % (q(parent), q(self.role)))
            self.db.execute('DROP OWNED BY %s' % q(parent))
            self.db.execute('DROP ROLE %s' % q(parent))

    def test_review_catch_up_migration_revokes_creation_time_executor_defaults(self):
        """#1448 N1 : les droits par défaut présents quand 0438 crée la table
        (exécuteur, PUBLIC) ne survivent pas à la migration 0461 ; le
        propriétaire, lui, peut toujours déplacer la borne."""
        import shutil
        import tempfile
        from pathlib import Path
        q = db_mod.quote_ident
        schema = 't_bound_upgrade_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE SCHEMA %s' % q(schema))
        db = db_mod.connect(dataclasses.replace(self.cfg, schema=schema))
        try:
            db.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT,INSERT,UPDATE,DELETE,'
                       'TRUNCATE,REFERENCES,TRIGGER ON TABLES TO %s'
                       % (q(schema), q(self.role)))
            with tempfile.TemporaryDirectory() as directory:
                for name in ('0438_review_catch_up.sql', '0461_review_catch_up_acl.sql'):
                    shutil.copy(Path(REPO, 'src/ameesh/migrations', name), directory)
                migrations.migrate(db, directory)
            for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'):
                with self.subTest(privilege=privilege):
                    self.assertFalse(db.query(
                        'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                        (self.role, schema + '.review_catch_up', privilege))[0]['allowed'])
            self.assertTrue(db.query(
                'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                (self.role, schema + '.review_catch_up', 'SELECT'))[0]['allowed'])
            db.execute('UPDATE review_catch_up SET bound_at = bound_at')
            self.assertTrue(db.query('SELECT bound_at FROM review_catch_up')[0]['bound_at'])
        finally:
            db.close()
            self.db.execute('DROP SCHEMA %s CASCADE' % q(schema))

    def test_review_catch_up_migration_revokes_column_acl(self):
        """#1448 B1 : 0461 révoque aussi les droits de colonne laissés à
        l'exécuteur ou à PUBLIC (pg_attribute.attacl), que
        REVOKE ... ON TABLE ne touche pas."""
        import shutil
        import tempfile
        from pathlib import Path
        q = db_mod.quote_ident
        schema = 't_bound_col_upgrade_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE SCHEMA %s' % q(schema))
        db = db_mod.connect(dataclasses.replace(self.cfg, schema=schema))
        try:
            with tempfile.TemporaryDirectory() as first, \
                    tempfile.TemporaryDirectory() as second:
                shutil.copy(Path(REPO, 'src/ameesh/migrations', '0438_review_catch_up.sql'),
                            first)
                migrations.migrate(db, first)
                db.execute('GRANT UPDATE (bound_at) ON %s.review_catch_up TO %s'
                           % (q(schema), q(self.role)))
                db.execute('GRANT UPDATE (bound_at) ON %s.review_catch_up TO PUBLIC'
                           % q(schema))
                self.assertTrue(db.query(
                    'SELECT has_column_privilege(%s,%s,%s,%s) AS allowed',
                    (self.role, schema + '.review_catch_up', 'bound_at',
                     'UPDATE'))[0]['allowed'])
                shutil.copy(Path(REPO, 'src/ameesh/migrations',
                                 '0461_review_catch_up_acl.sql'), second)
                migrations.migrate(db, second)
            self.assertFalse(db.query(
                'SELECT has_column_privilege(%s,%s,%s,%s) AS allowed',
                (self.role, schema + '.review_catch_up', 'bound_at',
                 'UPDATE'))[0]['allowed'])
            db.execute('UPDATE %s.review_catch_up SET bound_at = bound_at' % q(schema))
        finally:
            db.close()
            self.db.execute('DROP SCHEMA %s CASCADE' % q(schema))

    def test_independent_release_role_writes_only_fences_and_locks_registry(self):
        from pathlib import Path
        q = db_mod.quote_ident
        controller = 't_release_controller_' + uuid.uuid4().hex[:8]
        sql = Path(REPO,'deploy/sql/role-release-controller.sql')
        env = dict(os.environ, PGOPTIONS='-c search_path='+self.schema)
        try:
            for _ in range(2):
                result = subprocess.run(['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                    '-v','role='+controller,'-f',str(sql)], env=env, text=True,capture_output=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stderr)
            # Upgrade an already-provisioned controller, then reapply its
            # contract. Process admission must run AS controller.
            marker_sql = Path(migrations.MIGRATIONS_DIR,
                              '0524_schema_write_guard.sql').read_text().replace(
                                  'ameesh_release_controller', controller)
            self.db.execute('DROP TABLE schema_non_additive_migrations')
            self.db.script(marker_sql)
            for _ in range(2):
                permissions = self.db.query(
                    "SELECT has_table_privilege(%s, 'schema_non_additive_migrations', 'SELECT') AS reads, "
                    "has_table_privilege(%s, 'schema_non_additive_migrations', 'INSERT,UPDATE,DELETE') AS writes",
                    (controller, controller))[0]
                self.assertTrue(permissions['reads'])
                self.assertFalse(permissions['writes'])
                result = subprocess.run(['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                    '-v','role='+controller,'-f',str(sql)], env=env, text=True,capture_output=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stderr)
            registry.upsert(self.db,'release-worker',harness='other',host='synthetic-host')
            self.assertTrue(self.db.query(
                'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                (controller, self.schema + '.decision_supersessions',
                 'SELECT'))[0]['allowed'])
            # #4995: the controller re-reads the signed replacement log at every
            # step; it must reach the same refusal under its own role, not a
            # permission error.
            decision_payload = json.dumps({"decision": {"state": "answered"}})
            rows = self.db.query(
                "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload)"
                " VALUES ('a', 'human:x', 'old', 'request', %s::jsonb),"
                " ('a', 'human:x', 'new', 'request', %s::jsonb) RETURNING id",
                (decision_payload, decision_payload))
            old_decision, new_decision = sorted(int(row["id"]) for row in rows)
            self.db.execute(
                "INSERT INTO decision_supersessions"
                " (supersedes, superseded_by, person, occurred_at, proof)"
                " VALUES (%s, %s, 'alice', now(), '{}'::jsonb)",
                (old_decision, new_decision))
            with self.db.transaction() as tx:
                tx.execute('SET LOCAL ROLE %s' % q(controller))
                migrations.require_compatible_writer(tx)
                tx.query('SELECT name FROM agent_registry WHERE name=%s FOR UPDATE',('release-worker',))
                tx.execute('INSERT INTO host_release_operations(operation,host,manifest_digest) VALUES (%s,%s,%s)',
                           ('controller-fixture','synthetic-host','a'*64))
                tx.execute('INSERT INTO host_release_fences(host,operation,probe) VALUES (%s,%s,%s)',
                           ('synthetic-host','controller-fixture','synthetic-probe'))
                for relation in ('agent_mailbox','mesh_approvals','mesh_consumed_nonces','turn_resources','spend_pending','decision_supersessions'):
                    tx.query('SELECT * FROM '+q(relation)+' LIMIT 0')
                # #4995: the real authority read, under the controller role: it
                # sees the logged event and refuses the old authority because
                # the replacement carries no signed answer (no permission error).
                view = decisions.get(tx, old_decision)
                with self.assertRaisesRegex(decisions.DecisionError,
                                            'not a signed replacement'):
                    decisions.require_current(tx, view)
                tx.query('SELECT * FROM mesh_approvals_status LIMIT 0')
                # SignedDecisionAuthority re-reads the owner key at every step.
                tx.query('SELECT name, holder, public_key, public_key_fingerprint, key_role,'
                         ' key_updated_at, key_revoked_at FROM mesh_keys LIMIT 0')
            with self.assertRaises(db_mod.DbError):
                with self.db.transaction() as tx:
                    tx.execute('SET LOCAL ROLE %s' % q(controller))
                    tx.execute("UPDATE agent_registry SET status='running' WHERE name='release-worker'")
            self.db.execute('DELETE FROM host_release_fences WHERE operation=%s',('controller-fixture',))
            self.db.execute('DELETE FROM host_release_operations WHERE operation=%s',('controller-fixture',))
            # A column-only grant must be detected, even without table UPDATE.
            self.db.execute('GRANT UPDATE(public_key) ON agent_registry TO %s' % q(controller))
            result = subprocess.run(['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                '-v','role='+controller,'-f',str(sql)], env=env,text=True,capture_output=True,timeout=60)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('unrelated column write privilege',result.stderr,
                          attendu(result.stderr))
        finally:
            if self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s',(controller,)):
                self.db.execute('DROP OWNED BY %s' % q(controller))
                self.db.execute('DROP ROLE %s' % q(controller))

    def test_controller_audit_refuses_an_enrollment_without_the_supersession_read(self):
        """#4995: the authority gate reads the signed replacement log; an
        enrollment that cannot read it must not pass the audit."""
        from pathlib import Path
        q = db_mod.quote_ident
        controller = 't_release_controller_' + uuid.uuid4().hex[:8]
        sql = Path(REPO,'deploy/sql/role-release-controller.sql')
        env = dict(os.environ, PGOPTIONS='-c search_path='+self.schema)
        command = ['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                   '-v','role='+controller,'-f',str(sql)]
        try:
            result = subprocess.run(command, env=env, text=True,
                                    capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            # The script grants the read itself; revoke it and remove that one
            # grant from a copy to exercise the audit's fail-closed check.
            self.db.execute('REVOKE SELECT ON decision_supersessions FROM %s'
                            % q(controller))
            source = sql.read_text(encoding='utf-8')
            self.assertIn(',%I.decision_supersessions TO %I', source)
            patched = source.replace(',%I.decision_supersessions TO %I', ' TO %I', 1)
            head, tail = patched.split("SELECT format('GRANT SELECT ON", 1)
            patched = (head + "SELECT format('GRANT SELECT ON"
                       + tail.replace(":'schema',", "", 1))
            without_read = Path(self.tmp) / 'role-release-controller-no-log.sql'
            without_read.write_text(patched, encoding='utf-8')
            result = subprocess.run(command[:-1] + [str(without_read)], env=env,
                                    text=True, capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lacks SELECT on decision_supersessions', result.stderr,
                          attendu(result.stderr))
        finally:
            if self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s',(controller,)):
                self.db.execute('DROP OWNED BY %s' % q(controller))
                self.db.execute('DROP ROLE %s' % q(controller))

    def test_sequence_privilege_makes_the_controller_audit_fail(self):
        """A sequence is outside relkind r/p, but its own privileges are writes:
        USAGE opens nextval/currval, UPDATE opens setval. The controller needs
        none of them, so each one fails the audit instead of passing silently."""
        from pathlib import Path
        q = db_mod.quote_ident
        controller = 't_release_controller_' + uuid.uuid4().hex[:8]
        sequence = 't_release_sequence_' + uuid.uuid4().hex[:8]
        sql = Path(REPO,'deploy/sql/role-release-controller.sql')
        env = dict(os.environ, PGOPTIONS='-c search_path='+self.schema)
        command = ['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                   '-v','role='+controller,'-f',str(sql)]
        self.db.execute('CREATE SEQUENCE %s.%s' % (q(self.schema),q(sequence)))
        try:
            result = subprocess.run(command, env=env,text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,attendu(result.stderr))
            for privilege in ('USAGE','SELECT','UPDATE'):
                self.db.execute('GRANT %s ON SEQUENCE %s.%s TO %s'
                                % (privilege,q(self.schema),q(sequence),q(controller)))
                try:
                    result = subprocess.run(command, env=env,text=True,capture_output=True,timeout=60)
                    self.assertNotEqual(result.returncode,0,privilege)
                    self.assertIn('unrelated sequence privilege',result.stderr,
                                  attendu(result.stderr))
                finally:
                    self.db.execute('REVOKE %s ON SEQUENCE %s.%s FROM %s'
                                    % (privilege,q(self.schema),q(sequence),q(controller)))
        finally:
            if self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s',(controller,)):
                self.db.execute('DROP OWNED BY %s' % q(controller))
                self.db.execute('DROP ROLE %s' % q(controller))
            self.db.execute('DROP SEQUENCE IF EXISTS %s.%s' % (q(self.schema),q(sequence)))

    def test_release_role_write_grant_on_a_view_makes_the_audit_fail(self):
        from pathlib import Path
        q = db_mod.quote_ident
        controller = 't_release_controller_' + uuid.uuid4().hex[:8]
        sql = Path(REPO,'deploy/sql/role-release-controller.sql')
        env = dict(os.environ, PGOPTIONS='-c search_path='+self.schema)
        view = 't_release_view_' + uuid.uuid4().hex[:8]
        try:
            result = subprocess.run(['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                '-v','role='+controller,'-f',str(sql)], env=env,text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr)
            # A view is a write path too: a grant on it must be audited like a
            # table, not silently ignored.
            self.db.execute('CREATE VIEW %s AS SELECT name FROM agent_registry' % q(view))
            self.db.execute('GRANT UPDATE ON %s TO %s' % (q(view),q(controller)))
            result = subprocess.run(['psql',self.owner_dsn,'-X','-q','-v','ON_ERROR_STOP=1',
                '-v','role='+controller,'-f',str(sql)], env=env,text=True,capture_output=True,timeout=60)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('unrelated write privilege',result.stderr,attendu(result.stderr))
        finally:
            self.db.execute('DROP VIEW IF EXISTS %s' % q(view))
            if self.db.query('SELECT 1 FROM pg_roles WHERE rolname=%s',(controller,)):
                self.db.execute('DROP OWNED BY %s' % q(controller))
                self.db.execute('DROP ROLE %s' % q(controller))

    def test_host_cannot_change_schema(self):
        for sql in ("CREATE TABLE intruder (x int)",
                    "CREATE SCHEMA intruder",
                    "CREATE TEMP TABLE intruder (x int)",
                    "ALTER TABLE agent_registry ADD COLUMN intruder int",
                    "DROP TABLE agent_mailbox",
                    "TRUNCATE agent_mailbox"):
            proc = self.as_host(sql)
            self.assertNotEqual(proc.returncode, 0, sql)
            error = proc.stderr.lower()
            self.assertTrue("permission denied" in error or "must be owner" in error,
                            (sql, attendu(proc.stderr)))

    def test_host_cannot_replace_a_key(self):
        """#326 r1: every agent shares this role; the owner's key is never
        replaced by it, the owner's gesture setting notwithstanding."""
        self.db.execute("INSERT INTO key_holders (name, human, public_key, key_role) "
                        "VALUES ('l56-owner', 'human:l56-owner', 'k0', 'owner') "
                        "ON CONFLICT (name) DO NOTHING")
        for sql in ("BEGIN; SET LOCAL ameesh.owner_gesture = 'on'; "
                    "UPDATE key_holders SET public_key = 'k1' WHERE name = 'l56-owner'; COMMIT",
                    "SET ameesh.owner_gesture = 'on'; "
                    "DELETE FROM key_holders WHERE name = 'l56-owner'",
                    "SET ameesh.owner_gesture = 'on'; INSERT INTO key_holders "
                    "(name, human, public_key, key_role) "
                    "VALUES ('l56-other', 'human:l56-other', 'k2', 'owner')",
                    "ALTER TABLE key_holders DISABLE TRIGGER key_holders_guard",
                    "DROP TRIGGER key_holders_guard ON key_holders",
                    "CREATE OR REPLACE FUNCTION key_holders_owner_gesture() RETURNS boolean "
                    "LANGUAGE sql AS 'select true'"):
            proc = self.as_host(sql)
            self.assertNotEqual(proc.returncode, 0, sql)
        self.assertEqual(self.db.query("SELECT name, public_key FROM key_holders "
                                       "WHERE name LIKE 'l56-%'"),
                         [{"name": "l56-owner", "public_key": "k0"}])
        # an agent of that name is refused; a revocation is not
        proc = self.as_host("INSERT INTO agent_registry (name) VALUES ('l56-owner')")
        self.assertIn("holds a human's key", proc.stderr, attendu(proc.stderr))
        proc = self.as_host("UPDATE key_holders SET key_revoked_at = now() "
                            "WHERE name = 'l56-owner'")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # #326 r2: nor an owner key on an agent; an agent key is free, and
        # its event is journaled under the executor's role
        self.db.execute("INSERT INTO agent_registry (name) VALUES ('l56-agent') "
                        "ON CONFLICT (name) DO NOTHING")
        for sql in ("SET ameesh.owner_gesture = 'on'; UPDATE agent_registry "
                    "SET public_key = 'k3', key_role = 'owner' WHERE name = 'l56-agent'",
                    "INSERT INTO agent_registry (name, public_key, key_role) "
                    "VALUES ('l56-agent2', 'k4', 'owner')",
                    "DELETE FROM key_events",
                    "UPDATE key_events SET at = now() - interval '30 days'",
                    "INSERT INTO key_events (name, holder, event) "
                    "VALUES ('x', 'human', 'revoked')",
                    # #326 r3: nor confirm a registration, nor a holder under
                    # an agent's name
                    "SET ameesh.owner_gesture = 'on'; INSERT INTO key_events "
                    "(name, holder, event) VALUES ('l56-owner', 'human', 'confirmed')",
                    "INSERT INTO key_holders (name, human, public_key, key_role) "
                    "VALUES ('l56-agent', 'human:l56-agent', 'k6', 'agent')",
                    # #326 r4: nor move the journal's ids
                    "SELECT setval('key_events_id_seq', 1000, true)"):
            proc = self.as_host(sql)
            self.assertNotEqual(proc.returncode, 0, sql)
        proc = self.as_host("UPDATE agent_registry SET public_key = 'k5', key_role = 'agent' "
                            "WHERE name = 'l56-agent'")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.db.query(
            "SELECT event, db_user FROM key_events WHERE name = 'l56-agent'"),
            [{"event": "registered", "db_user": self.login}])

    def test_host_cannot_take_an_owner_key_away_nor_rename_onto_a_holder(self):
        """#326 r4: the review's sequences, run as the executor."""
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            tx.execute("INSERT INTO agent_registry (name, public_key, public_key_fingerprint, "
                       "key_role) VALUES ('l56-orch', 'ko', 'fo', 'owner') "
                       "ON CONFLICT (name) DO NOTHING")
            tx.execute("INSERT INTO key_holders (name, human, public_key, "
                       "public_key_fingerprint, key_role) VALUES ('l56-alice', "
                       "'human:l56-alice', 'ka', 'fa', 'agent') ON CONFLICT (name) DO NOTHING")
        proc = self.as_host("UPDATE agent_registry SET key_revoked_at = clock_timestamp() "
                            "WHERE name = 'l56-orch'")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for sql in ("UPDATE agent_registry SET public_key = NULL, "
                    "public_key_fingerprint = NULL, key_role = 'agent' WHERE name = 'l56-orch'",
                    "DELETE FROM agent_registry WHERE name = 'l56-orch'",
                    "INSERT INTO agent_registry (name, public_key, public_key_fingerprint, "
                    "key_role) VALUES ('l56-intruder', 'K', 'F', 'agent'); "
                    "UPDATE agent_registry SET name = 'l56-alice' WHERE name = 'l56-intruder'"):
            proc = self.as_host(sql)
            self.assertNotEqual(proc.returncode, 0, sql)
        self.assertEqual(self.db.query("SELECT public_key, key_role FROM agent_registry "
                                       "WHERE name = 'l56-orch'"),
                         [{"public_key": "ko", "key_role": "owner"}])
        self.assertEqual(self.db.query("SELECT holder, public_key_fingerprint FROM mesh_keys "
                                       "WHERE name = 'l56-alice'"),
                         [{"holder": "human", "public_key_fingerprint": "fa"}])

    def test_host_checks_names_at_read_committed_only(self):
        """#326 r5: a name is checked against the other table under a lock
        on it, which a snapshot taken before the wait would not see."""
        proc = self.as_host("BEGIN ISOLATION LEVEL REPEATABLE READ; "
                            "INSERT INTO agent_registry (name) VALUES ('l56-rr'); COMMIT")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("read committed only", proc.stderr, attendu(proc.stderr))
        proc = self.as_host("INSERT INTO agent_registry (name) VALUES ('l56-rr')")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_public_schema_create_rolls_back(self):
        """PUBLIC schema CREATE makes the script roll back the new role."""
        q = db_mod.quote_ident
        self.db.execute("GRANT CREATE ON SCHEMA %s TO PUBLIC" % q(self.schema))
        new_role = "t_executor_new_role_%s" % uuid.uuid4().hex[:8]
        try:
            proc = self.run_script(new_role)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("ROLE CONTRACT FAILED", proc.stderr, attendu(proc.stderr))
            self.assertIn("CREATE or ownership on schema", proc.stderr, attendu(proc.stderr))
            self.assertFalse(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (new_role,)))
        finally:
            self.db.execute("REVOKE CREATE ON SCHEMA %s FROM PUBLIC" % q(self.schema))

    def test_database_privileges_roll_back(self):
        q = db_mod.quote_ident
        for privilege in ("CREATE", "TEMP"):
            for grant_to in ("PUBLIC", q(self.role)):
                with self.subTest(privilege=privilege, grant_to=grant_to):
                    self.db.execute("GRANT %s ON DATABASE %s TO %s" % (
                        privilege, q(self.database), grant_to))
                    try:
                        result = self.run_script(self.role)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(privilege + " on database", result.stderr,
                                      attendu(result.stderr))
                    finally:
                        self.db.execute("REVOKE %s ON DATABASE %s FROM %s" % (
                            privilege, q(self.database), grant_to))

    def test_noinherit_membership_cannot_bypass_database_audit(self):
        q = db_mod.quote_ident
        parent = "t_executor_parent_" + uuid.uuid4().hex[:8]
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(parent))
        try:
            self.db.execute("GRANT CREATE ON DATABASE %s TO %s" % (q(self.database), q(parent)))
            self.db.execute("ALTER ROLE %s NOINHERIT" % q(self.role))
            self.db.execute("GRANT %s TO %s" % (q(parent), q(self.role)))
            result = self.run_script(self.role)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CREATE on database", result.stderr, attendu(result.stderr))
        finally:
            self.db.execute("REVOKE %s FROM %s" % (q(parent), q(self.role)))
            self.db.execute("ALTER ROLE %s INHERIT" % q(self.role))
            self.db.execute("DROP OWNED BY %s" % q(parent))
            self.db.execute("DROP ROLE %s" % q(parent))

    def test_privileged_predefined_roles_are_rejected(self):
        q = db_mod.quote_ident
        for parent in ('pg_execute_server_program', 'pg_read_server_files', 'pg_write_server_files'):
            with self.subTest(parent=parent):
                self.db.execute("GRANT %s TO %s" % (q(parent), q(self.role)))
                try:
                    result = self.run_script(self.role)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('privileged predefined role ' + parent, result.stderr,
                                  attendu(result.stderr))
                finally:
                    self.db.execute("REVOKE %s FROM %s" % (q(parent), q(self.role)))

    def test_unsafe_table_and_default_privileges_are_rejected(self):
        q = db_mod.quote_ident
        parent = 't_acl_parent_' + uuid.uuid4().hex[:8]
        self.db.execute('CREATE ROLE %s NOLOGIN' % q(parent))
        self.db.execute('GRANT %s TO %s' % (q(parent), q(self.role)))
        self.db.execute('ALTER ROLE %s NOINHERIT' % q(self.role))
        try:
            for grantee in ('PUBLIC', q(self.role), q(parent)):
                for privilege in ('TRIGGER', 'TRUNCATE'):
                    with self.subTest(grantee=grantee, privilege=privilege):
                        grant = '%s ON TABLE agent_mailbox TO %s' % (privilege, grantee)
                        self.db.execute('GRANT ' + grant)
                        try:
                            result = self.run_script(self.role)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn(privilege + ' on table agent_mailbox', result.stderr,
                                          attendu(result.stderr))
                        finally:
                            self.db.execute('REVOKE ' + grant.replace(' TO ', ' FROM '))
                        for scope in ('', ' IN SCHEMA ' + q(self.schema)):
                            grant = '%s ON TABLES TO %s' % (privilege, grantee)
                            self.db.execute('ALTER DEFAULT PRIVILEGES%s GRANT %s' % (scope, grant))
                            try:
                                result = self.run_script(self.role)
                                self.assertNotEqual(result.returncode, 0)
                                self.assertIn('unsafe default ' + privilege, result.stderr,
                                              attendu(result.stderr))
                            finally:
                                self.db.execute('ALTER DEFAULT PRIVILEGES%s REVOKE %s' % (
                                    scope, grant.replace(' TO ', ' FROM ')))
        finally:
            self.db.execute('ALTER ROLE %s INHERIT' % q(self.role))
            self.db.execute('REVOKE %s FROM %s' % (q(parent), q(self.role)))
            self.db.execute('DROP ROLE %s' % q(parent))

    def test_database_owner_and_incomplete_grants(self):
        """Real migration creator with pg_database_owner schema ownership.

        Applying without grant options must reject any missing table/sequence
        rights instead of committing after GRANT warnings.
        """
        q = db_mod.quote_ident
        suffix = uuid.uuid4().hex[:8]
        creator, other, executor = ('t_creator_' + suffix, 't_other_' + suffix, 't_exec_' + suffix)
        schema = 't_owned_' + suffix
        old_owner = self.db.query('SELECT pg_get_userbyid(datdba) AS name FROM pg_database WHERE datname = %s',
                                  (self.database,))[0]['name']
        for role in (creator, other, executor):
            self.db.execute('CREATE ROLE %s NOLOGIN' % q(role))
        try:
            self.db.execute('ALTER DATABASE %s OWNER TO %s' % (q(self.database), q(creator)))
            self.db.execute('CREATE SCHEMA %s AUTHORIZATION pg_database_owner' % q(schema))
            # One session: the psql driver runs each bare execute() in its own process.
            with self.db.transaction() as tx:
                tx.execute('SET LOCAL ROLE %s' % q(creator))
                tx.execute('CREATE TABLE %s.agent_registry (id serial PRIMARY KEY)' % q(schema))
                tx.execute('CREATE TABLE %s.ci_runs (id int)' % q(schema))
                tx.execute('CREATE SEQUENCE %s.estimate_references_id_seq' % q(schema))
            result = self.run_script(executor, schema=schema, applying_role=creator)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = self.db.query('SELECT pg_get_userbyid(defaclrole) AS name FROM pg_default_acl '
                                 'WHERE defaclnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)',
                                 (schema,))
            self.assertTrue(rows)
            self.assertEqual({row['name'] for row in rows}, {creator})
            for kind, name, privileges in (
                ('TABLE', 'ci_runs', 'SELECT, INSERT, UPDATE, DELETE'),
                ('SEQUENCE', 'estimate_references_id_seq', 'USAGE, SELECT, UPDATE')):
                with self.subTest(kind=kind):
                    obj = '%s.%s' % (q(schema), q(name))
                    self.db.execute('ALTER %s %s OWNER TO %s' % (kind, obj, q(other)))
                    self.db.execute('REVOKE ALL ON %s %s FROM %s' % (kind, obj, q(executor)))
                    self.db.execute('GRANT %s ON %s %s TO %s' % (privileges, kind, obj, q(creator)))
                    result = self.run_script(executor, schema=schema, applying_role=creator)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('missing', result.stderr, attendu(result.stderr))
                    self.assertIn(name, result.stderr, attendu(result.stderr))
                    self.db.execute('ALTER %s %s OWNER TO %s' % (kind, obj, q(creator)))
                    result = self.run_script(executor, schema=schema, applying_role=creator)
                    self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            self.db.execute('ALTER DATABASE %s OWNER TO %s' % (q(self.database), q(old_owner)))
            self.db.execute('DROP SCHEMA IF EXISTS %s CASCADE' % q(schema))
            for role in (executor, other, creator):
                self.db.execute('DROP OWNED BY %s' % q(role))
                self.db.execute('DROP ROLE %s' % q(role))

    def test_execute_grant_skips_a_function_owned_by_another_role(self):
        """#4806 : sur un hôte où une extension (pg_stat_statements) laisse
        dans le schéma une fonction appartenant à un autre rôle que le
        migrationowner, `GRANT EXECUTE ON ALL FUNCTIONS` échouait en
        « permission denied for function » et annulait toute la phase rôle.
        Le droit se borne aux fonctions applicatives du propriétaire : l'objet
        étranger n'est ni un motif d'échec ni un droit accordé."""
        q = db_mod.quote_ident
        suffix = uuid.uuid4().hex[:8]
        creator, other, executor = ("t_creator_" + suffix, "t_other_" + suffix,
                                    "t_exec_" + suffix)
        schema = "t_owned_" + suffix
        foreign = "pg_stat_statements_reset_" + suffix
        helper = "app_helper_" + suffix
        old_owner = self.db.query(
            "SELECT pg_get_userbyid(datdba) AS name FROM pg_database WHERE datname = %s",
            (self.database,))[0]["name"]
        for role in (creator, other, executor):
            self.db.execute("CREATE ROLE %s NOLOGIN" % q(role))
        try:
            self.db.execute("ALTER DATABASE %s OWNER TO %s" % (q(self.database), q(creator)))
            self.db.execute("CREATE SCHEMA %s AUTHORIZATION pg_database_owner" % q(schema))
            with self.db.transaction() as tx:
                tx.execute("SET LOCAL ROLE %s" % q(creator))
                tx.execute("CREATE TABLE %s.agent_registry (id serial PRIMARY KEY)" % q(schema))
                tx.execute("CREATE FUNCTION %s.%s() RETURNS int LANGUAGE sql AS 'SELECT 1'"
                           % (q(schema), q(helper)))
            # L'objet étranger est créé par le superutilisateur puis possédé par
            # un autre rôle : le propriétaire du schéma ne peut pas l'accorder.
            self.db.execute("CREATE FUNCTION %s.%s() RETURNS boolean LANGUAGE sql"
                            " AS 'SELECT true'" % (q(schema), q(foreign)))
            self.db.execute("ALTER FUNCTION %s.%s() OWNER TO %s"
                            % (q(schema), q(foreign), q(other)))
            for name in (helper, foreign):
                self.db.execute("REVOKE ALL ON FUNCTION %s.%s() FROM PUBLIC, %s"
                                % (q(schema), q(name), q(executor)))
            result = self.run_script(executor, schema=schema, applying_role=creator)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.db.query(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS p",
                (executor, "%s.%s()" % (q(schema), q(helper))))[0]["p"],
                "la fonction applicative du propriétaire reste exécutable")
            self.assertFalse(self.db.query(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS p",
                (executor, "%s.%s()" % (q(schema), q(foreign))))[0]["p"],
                "la fonction d'un autre propriétaire n'est pas accordée")
        finally:
            self.db.execute("ALTER DATABASE %s OWNER TO %s" % (q(self.database), q(old_owner)))
            self.db.execute("DROP SCHEMA IF EXISTS %s CASCADE" % q(schema))
            for role in (executor, other, creator):
                self.db.execute("DROP OWNED BY %s" % q(role))
                self.db.execute("DROP ROLE %s" % q(role))

    def test_future_objects_receive_data_grants(self):
        self.db.execute("CREATE TABLE executor_future (id bigserial PRIMARY KEY, body text)")
        try:
            result = self.as_host("INSERT INTO executor_future(body) VALUES ('hello'); "
                                  "UPDATE executor_future SET body = 'updated'; "
                                  "SELECT body FROM executor_future; DELETE FROM executor_future")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("updated", result.stdout)
        finally:
            self.db.execute("DROP TABLE executor_future")


class ExecutorUpdateScriptTest(unittest.TestCase):
    def test_reapply_only_existing_role(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake = root / 'psql'
            fake.write_text('#!/bin/sh\n'
                            'printf "%s\\n" "$*" >> "$CALLS"\n'
                            'case " $* " in *" -At "*) printf "%s" "$EXISTS" ;; esac\n')
            fake.chmod(0o755)
            for exists, expected_calls in (("", 1), ("1", 2)):
                calls = root / 'calls'
                calls.write_text('')
                result = subprocess.run(
                    [os.path.join(REPO, 'deploy/mise-a-jour/role-executor.sh'),
                     'postgresql://owner@invalid/mesh'],
                    env=dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                             CALLS=str(calls), EXISTS=exists),
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls.read_text().splitlines()), expected_calls)
                self.assertNotIn('postgresql://', result.stdout + result.stderr)
                if exists:
                    self.assertIn('role-executor.sql', calls.read_text())
