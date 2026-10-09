# SPDX-License-Identifier: AGPL-3.0-only
"""L56 (étude v2 D4) : rôle Postgres des exécuteurs — données oui, schéma non."""
from __future__ import annotations

import dataclasses
import os
import secrets
import subprocess
import uuid

from ameesh import db as db_mod, mail, registry

from .support import REPO, PgTestCase
from .test_cluster import TEST_DSN, _dsn_for

ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-executeur.sql")


class RoleExecuteurTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.role = "t_exe_%s" % suffix
        cls.login = "t_exe_hote_%s" % suffix
        cls.password = secrets.token_hex(16)
        try:
            for _ in range(2):          # idempotent
                proc = cls.run_script(cls.role)
                if proc.returncode != 0:
                    raise AssertionError("role-executeur.sql : %s" % proc.stderr)
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
    def drop_roles(cls) -> None:
        for name in (cls.login, cls.role):
            if cls.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)):
                cls.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(name))
                cls.db.execute("DROP ROLE %s" % db_mod.quote_ident(name))

    @classmethod
    def run_script(cls, role: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PGOPTIONS="-c search_path=%s" % cls.schema)
        return subprocess.run(["psql", TEST_DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                               "-v", "role=%s" % role, "-f", ROLE_SQL],
                              capture_output=True, text=True, timeout=60, env=env)

    def as_host(self, sql: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("PGPASSWORD", None)
        env.pop("PGOPTIONS", None)
        return subprocess.run(["psql", _dsn_for(self.login, self.password), "-X", "-At",
                               "-v", "ON_ERROR_STOP=1", "-c", sql],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_l_hote_lit_et_ecrit_les_donnees(self):
        cfg = dataclasses.replace(self.cfg, dsn=_dsn_for(self.login, self.password))
        db = db_mod.connect(cfg)
        try:
            registry.upsert(db, "verif-a", harness="claude", host="pc", mode="execute")
            ident = mail.send(db, "coord", "verif-a", "relis la migration")
            self.assertTrue(ident > 0)
            self.assertEqual(db.query("SELECT name FROM agent_registry WHERE name = 'verif-a'"),
                             [{"name": "verif-a"}])
        finally:
            db.close()

    def test_l_hote_ne_touche_pas_au_schema(self):
        for sql in ("CREATE TABLE intrus (x int)",
                    "ALTER TABLE agent_registry ADD COLUMN intrus int",
                    "DROP TABLE agent_mailbox",
                    "TRUNCATE agent_mailbox"):
            proc = self.as_host(sql)
            self.assertNotEqual(proc.returncode, 0, sql)
            erreur = proc.stderr.lower()
            self.assertTrue("permission denied" in erreur or "must be owner" in erreur,
                            (sql, proc.stderr))

    def test_refus_si_le_contrat_n_est_pas_tenu(self):
        """Un droit CREATE sur le schéma fait échouer le script, qui annule tout :
        un rôle neuf n'est pas créé."""
        q = db_mod.quote_ident
        self.db.execute("GRANT CREATE ON SCHEMA %s TO PUBLIC" % q(self.schema))
        neuf = "t_exe_neuf_%s" % uuid.uuid4().hex[:8]
        try:
            proc = self.run_script(neuf)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("NON TENU", proc.stderr)
            self.assertIn("CREATE sur le schéma", proc.stderr)
            self.assertFalse(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (neuf,)))
        finally:
            self.db.execute("REVOKE CREATE ON SCHEMA %s FROM PUBLIC" % q(self.schema))
