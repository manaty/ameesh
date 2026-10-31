# SPDX-License-Identifier: AGPL-3.0-only
"""#2340 : le rôle de connexion en lecture seule de l'auditeur.

Constat : `AUDITEUR_DSN` absent, rôle `auditeur_lecture` inexistant — la
consigne supposait une permission jamais posée, et une lecture SQL de
l'auditeur n'aurait pu passer que par `AGENT_MESH_DSN`, non restreint.

Ce module vérifie le script de mise en service (`deploy/sql/role-auditeur.sql`)
sur un Postgres réel : rôle LOGIN, membre des deux contrats de supervision,
lecture seule par défaut, aucun droit direct, refus d'une appartenance hors
contrat, de rôles de base absents, d'une propriété d'objet (B1 : le
propriétaire se redonne les droits retirés) et d'un réglage par base qui
supplanterait la lecture seule ; réparation d'une appartenance INHERIT FALSE
posée alors que le rôle était NOINHERIT (B2). L'application réelle reste un
geste du propriétaire (c'est une permission) : le script est la procédure, pas
le geste.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import unittest
import urllib.parse
import uuid

from ameesh import db as db_mod
from tests.support import REPO, TEST_DSN, PgTestCase
from tests.role_acl import serialize_role_acl

ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-superviseur.sql")
CONTENTS_SQL = os.path.join(REPO, "deploy", "sql", "role-superviseur-contenus.sql")
AUDITEUR_SQL = os.path.join(REPO, "deploy", "sql", "role-auditeur.sql")


def _dsn_for(user: str, password: str) -> str:
    """Le DSN du banc, avec un autre utilisateur (format URL seulement)."""
    parts = urllib.parse.urlsplit(TEST_DSN)
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
        raise unittest.SkipTest("AMEESH_TEST_DSN n'est pas une URL : test de connexion sauté")
    host = parts.hostname + (":%d" % parts.port if parts.port else "")
    netloc = "%s:%s@%s" % (urllib.parse.quote(user), urllib.parse.quote(password), host)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


class AuditeurLoginRoleTest(PgTestCase):
    """Le rôle de connexion de l'auditeur, ses deux contrats et sa lecture seule."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        serialize_role_acl(cls)
        suffix = uuid.uuid4().hex[:8]
        cls.etat = "t_aud_e_%s" % suffix
        cls.contenus = "t_aud_c_%s" % suffix
        cls.login = "t_aud_l_%s" % suffix
        cls.autre = "t_aud_x_%s" % suffix
        cls.password = secrets.token_hex(16)  # éphémère, détruit avec le rôle
        try:
            for _ in range(2):  # idempotent : la seconde passe ne change rien
                cls.apply_script(ROLE_SQL, role=cls.etat)
                cls.apply_script(CONTENTS_SQL, role=cls.contenus)
            cls.apply_script(AUDITEUR_SQL, role=cls.login, etat=cls.etat,
                             contenus=cls.contenus)
            cls.db.execute("ALTER ROLE %s PASSWORD '%s'"
                           % (db_mod.quote_ident(cls.login), cls.password))
            cls.db.execute("ALTER ROLE %s SET search_path = %s"
                           % (db_mod.quote_ident(cls.login),
                              db_mod.quote_ident(cls.schema)))
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
        for name in (cls.login, cls.autre, cls.contenus, cls.etat):
            exists = cls.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            if exists:
                cls.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(name))
                cls.db.execute("DROP ROLE %s" % db_mod.quote_ident(name))

    @classmethod
    def run_script(cls, path: str, **variables: str) -> subprocess.CompletedProcess:
        """Lance un script tel que documenté : sans `-1`, il ouvre sa transaction."""
        env = dict(os.environ, PGOPTIONS="-c search_path=%s" % cls.schema)
        args = ["psql", TEST_DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1"]
        for key, value in variables.items():
            args += ["-v", "%s=%s" % (key, value)]
        args += ["-f", path]
        return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)

    @classmethod
    def apply_script(cls, path: str, **variables: str) -> subprocess.CompletedProcess:
        proc = cls.run_script(path, **variables)
        if proc.returncode != 0:
            raise AssertionError("%s en échec : %s" % (os.path.basename(path), proc.stderr))
        return proc

    def as_role(self, name: str, password: str,
                sql: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("PGPASSWORD", None)
        env.pop("PGOPTIONS", None)
        return subprocess.run(
            ["psql", _dsn_for(name, password), "-X", "-At",
             "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=30, env=env)

    def as_auditeur(self, sql: str) -> subprocess.CompletedProcess:
        return self.as_role(self.login, self.password, sql)

    def memberships(self) -> dict[str, bool]:
        return {row["groupe"]: bool(row["admin"]) for row in self.db.query(
            "SELECT g.rolname AS groupe, m.admin_option AS admin "
            "FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s",
            (self.login,))}

    def role(self, name: str | None = None) -> dict:
        rows = self.db.query("SELECT * FROM pg_roles WHERE rolname = %s",
                             (name or self.login,))
        self.assertTrue(rows, "rôle %s absent" % (name or self.login))
        return rows[0]

    def test_role_login_lecture_seule_membre_des_deux_contrats(self):
        row = self.role()
        self.assertTrue(row["rolcanlogin"])
        self.assertTrue(row["rolinherit"])
        for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole",
                          "rolreplication", "rolbypassrls"):
            self.assertFalse(row[attribute], attribute)
        self.assertIn("default_transaction_read_only=on", row["rolconfig"] or [])
        # #3503 : un NOUVEAU rôle de connexion coupe aussi le JIT de Postgres
        self.assertIn("jit=off", row["rolconfig"] or [])
        self.assertEqual(self.memberships(), {self.etat: False, self.contenus: False})

    def test_idempotent(self):
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)
        self.assertTrue(self.role()["rolcanlogin"])
        self.assertEqual(self.memberships(), {self.etat: False, self.contenus: False})

    def test_le_login_lit_les_contenus_et_pas_la_signature(self):
        etat = self.as_auditeur("SELECT count(id) FROM agent_mailbox")
        self.assertEqual(etat.returncode, 0, etat.stderr)
        contenu = self.as_auditeur("SELECT count(body) FROM agent_mailbox")
        self.assertEqual(contenu.returncode, 0, contenu.stderr)
        signature = self.as_auditeur("SELECT count(signature) FROM agent_mailbox")
        self.assertNotEqual(signature.returncode, 0)
        self.assertIn("permission denied", signature.stderr)

    def test_usage_du_schema_relu(self):
        """N #2355 : sans USAGE effectif, les GRANT de colonne sont illusoires."""
        q = db_mod.quote_ident
        self.db.execute("REVOKE USAGE ON SCHEMA %s FROM %s, %s"
                        % (q(self.schema), q(self.etat), q(self.contenus)))
        try:
            # les deux contrats gardent la lecture de colonne : seul l'USAGE
            # manque, et la lecture réelle échouerait sur le schéma
            lit = self.db.query(
                "SELECT has_column_privilege(%s, %s, 'id', 'SELECT') AS lit",
                (self.login, "%s.agent_mailbox" % q(self.schema)))[0]["lit"]
            self.assertTrue(lit, "le cas N n'est pas reproduit : la colonne est déjà perdue")
            proc = self.run_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                                   contenus=self.contenus)
            self.assertNotEqual(proc.returncode, 0, "USAGE absent accepté à tort")
            self.assertIn("USAGE du schéma absent", proc.stderr)
        finally:
            self.apply_script(ROLE_SQL, role=self.etat)
            self.apply_script(CONTENTS_SQL, role=self.contenus)
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)

    def test_le_login_ne_peut_pas_creer_de_table(self):
        proc = self.as_auditeur("CREATE TEMP TABLE t(x int)")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("read-only", proc.stderr)

    def test_noinherit_remis_a_inherit(self):
        self.db.execute("ALTER ROLE %s NOINHERIT" % db_mod.quote_ident(self.login))
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)
        self.assertTrue(self.role()["rolinherit"])
        etat = self.as_auditeur("SELECT count(id) FROM agent_mailbox")
        self.assertEqual(etat.returncode, 0, etat.stderr)

    def test_noinherit_avant_les_grants_est_repare(self):
        """B2 (#2340) : des GRANT posés sous NOINHERIT gardent inherit_option=false."""
        version = int(self.db.query(
            "SELECT current_setting('server_version_num') AS v")[0]["v"])
        if version < 160000:
            self.skipTest("options d'appartenance des rôles : PostgreSQL 16+")
        q = db_mod.quote_ident
        role = "t_aud_b2_%s" % uuid.uuid4().hex[:8]
        password = secrets.token_hex(16)
        self.db.execute("CREATE ROLE %s LOGIN NOINHERIT" % q(role))
        try:
            self.db.execute("GRANT %s TO %s" % (q(self.etat), q(role)))
            self.db.execute("GRANT %s TO %s" % (q(self.contenus), q(role)))
            avant = self.db.query(
                "SELECT count(*) AS n FROM pg_auth_members "
                "WHERE member = %s::regrole AND NOT inherit_option", (role,))[0]["n"]
            self.assertEqual(avant, 2, "le cas B2 n'est pas reproduit")
            self.apply_script(AUDITEUR_SQL, role=role, etat=self.etat,
                              contenus=self.contenus)
            self.assertTrue(self.role(role)["rolinherit"])
            apres = self.db.query(
                "SELECT count(*) AS n FROM pg_auth_members "
                "WHERE member = %s::regrole AND inherit_option", (role,))[0]["n"]
            self.assertEqual(apres, 2)
            self.db.execute("ALTER ROLE %s PASSWORD '%s'" % (q(role), password))
            self.db.execute("ALTER ROLE %s SET search_path = %s"
                            % (q(role), q(self.schema)))
            etat = self.as_role(role, password, "SELECT count(id) FROM agent_mailbox")
            self.assertEqual(etat.returncode, 0, etat.stderr)
            contenu = self.as_role(role, password, "SELECT count(body) FROM agent_mailbox")
            self.assertEqual(contenu.returncode, 0, contenu.stderr)
        finally:
            self.db.execute("DROP OWNED BY %s" % q(role))
            self.db.execute("DROP ROLE %s" % q(role))

    def test_une_appartenance_hors_contrat_fait_refuser(self):
        q = db_mod.quote_ident
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(self.autre))
        try:
            self.db.execute("GRANT %s TO %s" % (q(self.autre), q(self.login)))
            proc = self.run_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                                   contenus=self.contenus)
            self.assertNotEqual(proc.returncode, 0, "appartenance acceptée à tort")
            self.assertIn("appartenance hors contrat", proc.stderr)
            self.assertIn(self.autre, self.memberships(),
                          "le script ne révoque pas un droit qu'il n'a pas posé")
        finally:
            self.db.execute("REVOKE %s FROM %s" % (q(self.autre), q(self.login)))
            self.db.execute("DROP ROLE %s" % q(self.autre))
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)

    def test_un_role_proprietaire_de_table_fait_refuser(self):
        """B1 (#2340) : le propriétaire se redonne les droits que le REVOKE retire."""
        q = db_mod.quote_ident
        table = "%s.probe_proprietaire" % q(self.schema)
        self.db.execute("DROP TABLE IF EXISTS %s" % table)
        self.db.execute("CREATE TABLE %s (id int, signature text)" % table)
        self.db.execute("ALTER TABLE %s OWNER TO %s" % (table, q(self.login)))
        try:
            proc = self.run_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                                   contenus=self.contenus)
            self.assertNotEqual(proc.returncode, 0, "propriété acceptée à tort")
            self.assertIn("propriété interdite", proc.stderr)
            self.assertIn("probe_proprietaire", proc.stderr)
        finally:
            self.db.execute("DROP TABLE IF EXISTS %s" % table)
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)

    def test_une_propriete_d_un_contrat_fait_refuser(self):
        """B1 (#2340) : le rôle peut SET ROLE vers un contrat, qui ne doit rien posséder."""
        q = db_mod.quote_ident
        table = "%s.probe_contrat" % q(self.schema)
        self.db.execute("DROP TABLE IF EXISTS %s" % table)
        self.db.execute("CREATE TABLE %s (id int)" % table)
        self.db.execute("ALTER TABLE %s OWNER TO %s" % (table, q(self.etat)))
        try:
            proc = self.run_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                                   contenus=self.contenus)
            self.assertNotEqual(proc.returncode, 0, "propriété d'un contrat acceptée à tort")
            self.assertIn("propriété interdite", proc.stderr)
            self.assertIn(self.etat, proc.stderr)
        finally:
            self.db.execute("DROP TABLE IF EXISTS %s" % table)
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)

    def test_un_reglage_par_base_ne_supplante_pas_la_lecture_seule(self):
        """B1 (#2340) : ALTER ROLE … IN DATABASE … SET l'emporte sur le rôle."""
        q = db_mod.quote_ident
        base = self.db.query("SELECT current_database() AS nom")[0]["nom"]
        self.db.execute("ALTER ROLE %s IN DATABASE %s "
                        "SET default_transaction_read_only = off"
                        % (q(self.login), q(base)))
        try:
            proc = self.run_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                                   contenus=self.contenus)
            self.assertNotEqual(proc.returncode, 0, "réglage par base accepté à tort")
            self.assertIn("réglage par base interdit", proc.stderr)
        finally:
            self.db.execute("ALTER ROLE %s IN DATABASE %s "
                            "RESET default_transaction_read_only"
                            % (q(self.login), q(base)))
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)

    def test_des_roles_de_base_absents_ne_creent_pas_le_login(self):
        neuf = "t_aud_n_%s" % uuid.uuid4().hex[:8]
        proc = self.run_script(AUDITEUR_SQL, role=neuf, etat=self.etat,
                               contenus="t_absent_%s" % uuid.uuid4().hex[:8])
        self.assertNotEqual(proc.returncode, 0, "rôles absents acceptés à tort")
        self.assertIn("rôles de base absents", proc.stderr)
        self.assertFalse(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (neuf,)),
                         "rôle créé malgré le refus")

    def test_admin_option_refusee(self):
        q = db_mod.quote_ident
        self.db.execute("GRANT %s TO %s WITH ADMIN OPTION" % (q(self.etat), q(self.login)))
        try:
            proc = self.run_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                                   contenus=self.contenus)
            self.assertNotEqual(proc.returncode, 0, "ADMIN OPTION acceptée à tort")
            self.assertIn("ADMIN OPTION", proc.stderr)
        finally:
            self.db.execute("REVOKE ADMIN OPTION FOR %s FROM %s"
                            % (q(self.etat), q(self.login)))
        self.apply_script(AUDITEUR_SQL, role=self.login, etat=self.etat,
                          contenus=self.contenus)


if __name__ == "__main__":
    unittest.main()
