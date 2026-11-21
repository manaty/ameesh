# SPDX-License-Identifier: AGPL-3.0-only
"""#7982 : le contrat du rôle superviseur est rejoué après les migrations.

Cause racine : `deploy/sql/role-superviseur.sql` n'était rejoué que par l'étape
`role` de `poste.sh` (mise à jour du poste owner). Une migration qui ajoute une
colonne au contrat (0602 : `agent_watches.last_fired_at`,
`.last_fired_reason`, `.last_attempt_at`) partait sur la base partagée avec le
paquet, et le rôle superviseur gardait l'ancien contrat jusqu'au retour du
poste. Témoins : le staging rejoue le contrat sous la connexion de `migrate`
(propriétaire des tables, sans CREATEROLE) juste après `migrate` ; un refus
arrête avant toute installation ; le rôle absent est sauté sans être créé ; un
droit retiré est nommé puis reposé ; l'audit annule le rejeu quand un droit
étranger subsiste.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
import uuid

from ameesh import droits_superviseur

from .role_acl import serialize_role_acl
from .support import REPO, TEST_DSN, PgTestCase

SQL = os.path.join(REPO, "deploy", "sql")
ROLE_SQL = os.path.join(SQL, "role-superviseur.sql")
REPLAY_SQL = os.path.join(SQL, "rejeu-superviseur.sql")
STAGING = os.path.join(REPO, "deploy", "mise-a-jour", "staging.sh")
POSTE = os.path.join(REPO, "deploy", "mise-a-jour", "poste.sh")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _sans_commentaires(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


class SequenceTest(unittest.TestCase):
    """La séquence appelle le rejeu au bon endroit et s'arrête sur un refus."""

    def test_staging_rejoue_le_contrat_apres_migrate(self):
        text = _read(STAGING)
        debut = text.index("migrer_depuis_vm() {")
        remote = text[text.index("<<'REMOTE'\n", debut):]
        remote = remote[:remote.index("\nREMOTE\n")]
        migrate = remote.index("-m ameesh migrate --additive-only")
        limites = remote.index("-m ameesh.droits_limites")
        superviseur = remote.index("-m ameesh.droits_superviseur")
        self.assertLess(migrate, limites)
        self.assertLess(limites, superviseur)
        self.assertIn('--sql-dir "$d/deploy/sql" || exit 6', remote)

    def test_staging_arrete_avant_installation_sur_refus_superviseur(self):
        text = _read(STAGING)
        refus = text.index('elif [ "$rc_migrer" = 6 ]')
        self.assertLess(refus, text.index("# --- sequence par hote"))
        bloc = text[refus:text.index('elif [ "$rc_migrer" != 0 ]', refus)]
        self.assertIn("contrat du role superviseur refuse", bloc)
        self.assertIn("contrat du superviseur en echec", bloc)
        self.assertIn("exit 1", bloc)

    def test_poste_rejoue_le_superviseur_apres_la_base_du_mesh(self):
        text = _read(POSTE)
        migrer = text[text.index("\nmigrer() {"):text.index("\nmigrer_base() {")]
        rejeu = migrer.index('-m ameesh.droits_superviseur --sql-dir "$cible/deploy/sql"')
        self.assertLess(migrer.index('migrer_base "$cible"'), rejeu)
        self.assertIn('AMEESH_CONFIG="$CONFIG"',
                      migrer[migrer.rindex("\n", 0, rejeu - 80):rejeu])
        # un refus arrête avant l'installation (ou, si l'ancien code ne sert
        # plus, après l'installation immédiate puis échec)
        garde = migrer.index('if { [ "$droits" = 1 ] || [ "$superviseur" = 1 ]; }')
        self.assertLess(garde, migrer.index('if [ "$incompatible" = 1 ]; then'))

    def test_le_rejeu_ne_cree_ni_ne_modifie_le_role(self):
        sql = _sans_commentaires(_read(REPLAY_SQL)).upper()
        for interdit in ("CREATE ROLE", "ALTER ROLE", "PASSWORD", "DROP ", "INSERT", "UPDATE "):
            self.assertNotIn(interdit, sql)
        # le mode rejeu est explicite dans le contrat commun
        contrat = _sans_commentaires(_read(os.path.join(SQL, "_contrat-superviseur.sql")))
        self.assertIn("ameesh.rejeu_superviseur", contrat)
        self.assertIn("CREATE ROLE", contrat)  # l'administration reste possible


class RejeuSuperviseurTest(PgTestCase):
    """Le rejeu sur un vrai Postgres, sous un propriétaire sans CREATEROLE."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        serialize_role_acl(cls)
        suffix = uuid.uuid4().hex[:8]
        cls.role = "t_sup_%s" % suffix
        cls.owner = "t_supown_%s" % suffix
        cls.addClassCleanup(cls.drop_roles, cls.owner, cls.role)
        cls.db.execute(
            'CREATE ROLE "%s" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOREPLICATION NOBYPASSRLS' % cls.owner)
        # le rôle superviseur et son contrat, comme l'administrateur
        proc = cls.psql(ROLE_SQL, role=cls.role)
        if proc.returncode:
            raise AssertionError(proc.stderr)
        # le porteur de `migrate` en production : propriétaire du schéma et de
        # ses relations, mais ni superutilisateur ni CREATEROLE
        cls.db.execute('ALTER SCHEMA "%s" OWNER TO "%s"' % (cls.schema, cls.owner))
        relations = cls.db.query(
            "SELECT c.relname, c.relkind FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'p', 'f', 'S')"
            " ORDER BY 1", (cls.schema,))
        for row in relations:
            commande = "SEQUENCE" if row["relkind"] == "S" else "TABLE"
            cls.db.execute('ALTER %s "%s"."%s" OWNER TO "%s"'
                           % (commande, cls.schema, row["relname"], cls.owner))

    @classmethod
    def drop_roles(cls, *roles: str) -> None:
        for role in roles:
            subprocess.run(
                ["psql", "-X", "-w", TEST_DSN, "-q", "-c",
                 'DROP OWNED BY "%s" CASCADE; DROP ROLE IF EXISTS "%s";' % (role, role)],
                capture_output=True, text=True, timeout=60)

    @classmethod
    def psql(cls, name: str, *, before: tuple = (), **variables) -> subprocess.CompletedProcess:
        args = ["psql", "-X", "-w", TEST_DSN, "-q", "-v", "ON_ERROR_STOP=1"]
        for key, value in variables.items():
            args += ["-v", "%s=%s" % (key, value)]
        for statement in before:
            args += ["-c", statement]
        return subprocess.run(
            args + ["-f", name],
            env=dict(os.environ, PGOPTIONS="-c search_path=" + cls.schema),
            capture_output=True, text=True, timeout=120)

    def replay_sql(self, role: str | None = None, **extra) -> subprocess.CompletedProcess:
        return self.psql(REPLAY_SQL, before=('SET ROLE "%s"' % self.owner,),
                         role=role or self.role, **extra)

    def lit(self, colonne: str) -> bool:
        return bool(self.db.query(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT') AS ok",
            (self.role, '"%s".agent_watches' % self.schema, colonne))[0]["ok"])

    def columns(self) -> list:
        return self.db.query(
            "SELECT c.relname, a.attname, a.attacl::text AS attacl"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0"
            " WHERE n.nspname = %s ORDER BY 1, 2", (self.schema,))

    def test_rejeu_restaure_les_trois_colonnes_de_0602(self):
        self.db.execute(
            'REVOKE SELECT (last_fired_at, last_fired_reason, last_attempt_at)'
            ' ON "%s".agent_watches FROM "%s"' % (self.schema, self.role))
        for colonne in ("last_fired_at", "last_fired_reason", "last_attempt_at"):
            self.assertFalse(self.lit(colonne), colonne)
        proc = self.replay_sql()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rejoue, contrat tenu", proc.stdout)
        for colonne in ("last_fired_at", "last_fired_reason", "last_attempt_at"):
            self.assertTrue(self.lit(colonne), colonne)

    def test_rejeu_idempotent(self):
        self.assertEqual(self.replay_sql().returncode, 0)
        avant = self.columns()
        self.assertEqual(self.replay_sql().returncode, 0)
        self.assertEqual(self.columns(), avant)

    def test_role_absent_saute_sans_creation(self):
        absent = "t_supabs_%s" % uuid.uuid4().hex[:8]
        proc = self.replay_sql(role=absent)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("absent, rejeu saute", proc.stdout)
        self.assertEqual(self.db.query(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (absent,)), [])

    def test_audit_en_echec_nomme_le_droit_et_annule_le_rejeu(self):
        self.db.execute(
            'REVOKE SELECT (last_fired_at) ON "%s".agent_watches FROM "%s"'
            % (self.schema, self.role))
        self.db.execute('GRANT SELECT (signature) ON "%s".agent_mailbox TO PUBLIC' % self.schema)
        try:
            proc = self.replay_sql()
        finally:
            self.db.execute(
                'REVOKE SELECT (signature) ON "%s".agent_mailbox FROM PUBLIC' % self.schema)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lecture hors contrat : agent_mailbox.signature", proc.stderr)
        self.assertIn("NON TENU", proc.stderr)
        # une seule transaction : la colonne retirée n'a pas été reposée
        self.assertFalse(self.lit("last_fired_at"))
        self.assertEqual(self.replay_sql().returncode, 0)
        self.assertTrue(self.lit("last_fired_at"))

    def test_le_superviseur_lit_les_trois_colonnes_apres_rejeu(self):
        self.assertEqual(self.replay_sql().returncode, 0)
        # la même lecture que le superviseur : les trois colonnes nommées
        for colonne in ("last_fired_at", "last_fired_reason", "last_attempt_at"):
            self.assertTrue(self.lit(colonne), colonne)
        proc = self.psql(ROLE_SQL, role=self.role)
        # relancer l'installation administrative ne change rien (idempotent)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.replay_sql().returncode, 0)

    def test_module_refuse_un_script_absent(self):
        code, verdict = droits_superviseur.replay(self.cfg, os.path.join(REPO, "absent"), self.role)
        self.assertEqual(code, 2)
        self.assertIn("introuvable", verdict)


if __name__ == "__main__":
    unittest.main()
