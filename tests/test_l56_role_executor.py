# SPDX-License-Identifier: AGPL-3.0-only
"""L56: executor data access without schema administration."""
from __future__ import annotations

import dataclasses
import os
import secrets
import subprocess
import uuid
import urllib.parse
import unittest

from ameesh import migrations

from ameesh import db as db_mod, mail, registry, work, modules
from ameesh.modules.storage import ModuleStore
from tests.test_l317_project_modules import federation

from .support import REPO, PgTestCase, attendu
from .test_cluster import TEST_DSN, _dsn_for

ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-executor.sql")


class RoleExecutorTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Database ACL tests must not change PUBLIC privileges in the shared bench.
        cls.admin_db = cls.db
        cls.database = "t_executor_db_" + uuid.uuid4().hex[:8]
        cls.admin_db.execute("CREATE DATABASE %s TEMPLATE template0" % db_mod.quote_ident(cls.database))
        cls.addClassCleanup(cls.drop_database)
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
            cls.admin_db.execute("DROP DATABASE %s" % db_mod.quote_ident(cls.database))
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
        # 0448 (F5) : l'identité nomme le journal lui-même — le cluster et sa
        # timeline ne suffisent pas (deux schémas du même cluster se confondent).
        self.assertRegex(own, "^[0-9a-f]{1,16}t[0-9a-f]{8}j[0-9a-f]{16}$")
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
        with self.assertRaises(work.WorkError):
            work.merged(self.db, lot, sha="abcdef1234")

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
        names = ("ameesh_canon_sync", "ameesh_it_proof", "ameesh_it_qa")
        placeholders = ", ".join(["%s"] * len(names))
        existing = {r["rolname"] for r in self.db.query(
            "SELECT rolname FROM pg_roles WHERE rolname IN (%s)" % placeholders, names)}
        script = os.path.join(REPO, "deploy", "sql", "role-module-writers.sql")

        def run_writers():
            return subprocess.run(
                ["psql", self.owner_dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                 "-f", script], capture_output=True, text=True, timeout=60,
                env=dict(os.environ, PGOPTIONS="-c search_path=%s" % self.schema))

        try:
            for _ in range(2):
                proc = run_writers()
                self.assertEqual(proc.returncode, 0, proc.stderr)
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
                self.db.execute("GRANT %s TO %s" % (q("ameesh_it_qa"), q(inherited)))
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
                tx.execute("SET LOCAL ROLE ameesh_canon_sync")
                ModuleStore(tx).replace("", modules.declarations(federation()))
            # Neither executor nor proof/QA writer is a policy writer.
            for writer in ("ameesh_it_proof", "ameesh_it_qa"):
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
            self.db.execute("GRANT SELECT ON work_items, project_modules, work_module_milestones TO %s" % q(writer))
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
            self.assertEqual(work.merged(self.db, lot, sha="abcdef1234")["result"], "merged")
        finally:
            self.db.execute("DROP OWNED BY %s" % q(writer))
            self.db.execute("DROP ROLE %s" % q(writer))

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
