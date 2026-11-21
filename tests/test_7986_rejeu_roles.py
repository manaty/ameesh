# SPDX-License-Identifier: AGPL-3.0-only
"""#7986 : les contrats de rôle rejouables sont rejoués après les migrations.

#7982 a découplé le seul rôle superviseur de la mise à jour du poste. Le même
trou attend les autres `deploy/sql/role-*.sql` dont le contrat est un ensemble
de GRANT : une migration qui ajoute une colonne ou une table au contrat laisse
le rôle sans le droit jusqu'à un geste d'administrateur. Ce témoin :

* vérifie que chaque `role-*.sql` est soit rejoué par un appelant déclaré
  (`droits_superviseur`, `droits_limites`, `droits_roles`), soit exempté avec
  sa raison ;
* rejoue sur un vrai Postgres les quatre scripts de `droits_roles`
  (superviseur-contenus, approve, module-writers, pod-provisioner) sous un
  propriétaire des tables sans CREATEROLE : un droit retiré est reposé, un
  rôle absent est sauté sans création, un audit en échec annule le rejeu ;
* vérifie que la séquence staging et `poste.sh migrer` appellent le rejeu après
  les migrations et s'arrêtent avant toute installation sur un refus.
"""
from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import unittest
import urllib.parse
import uuid

from ameesh import db as db_mod, droits_roles, migrations

from .support import REPO, TEST_DSN, PgTestCase

SQL = os.path.join(REPO, "deploy", "sql")
STAGING = os.path.join(REPO, "deploy", "mise-a-jour", "staging.sh")
POSTE = os.path.join(REPO, "deploy", "mise-a-jour", "poste.sh")

ROLE_SCRIPTS = {
    "role-approve.sql",
    "role-auditeur.sql",
    "role-bureau.sql",
    "role-executor.sql",
    "role-module-writers.sql",
    "role-pod-provisioner.sql",
    "role-release-controller.sql",
    "role-superviseur-contenus.sql",
    "role-superviseur.sql",
    "role-vm-probe.sql",
}
REPLAY_SCRIPTS = {item.role_script: item.script for item in droits_roles.REPLAYS}
NEW_SQL = {
    "rejeu-superviseur-contenus.sql",
    "rejeu-approve.sql",
    "rejeu-module-writers.sql",
    "rejeu-pod-provisioner.sql",
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def _sans_commentaires(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def _database_admin(cfg):
    """Une connexion sans borne pour CREATE/DROP DATABASE (#4822)."""
    return db_mod.connect(dataclasses.replace(cfg, statement_timeout_ms=0))


#: Refus d'environnement, pas un défaut du lot : le guest de prévalidation
#: n'accorde pas CREATE DATABASE (rôle du DSN sans CREATEDB, ou pooler). Le
#: contrat des groupes d'écriture exige de révoquer TEMP à PUBLIC, donc une
#: base dédiée ; sans elle le test est non concluant (sauté), il ne bloque pas
#: (§3), et il tourne sur le banc et en CI où CREATE DATABASE est accordé.
_CREATE_DATABASE_REFUSED = (
    "permission denied to create database",
    "CREATE DATABASE cannot run inside a transaction block",
    "cannot run inside a transaction block",
)


def _create_database(cfg, database: str) -> None:
    admin = _database_admin(cfg)
    try:
        db_mod.execute_without_statement_timeout(
            admin, "CREATE DATABASE %s TEMPLATE template0" % db_mod.quote_ident(database))
    except db_mod.DbError as exc:
        if not any(refus in str(exc) for refus in _CREATE_DATABASE_REFUSED):
            raise
        raise unittest.SkipTest(
            "banc sans CREATE DATABASE (rôle du DSN sans CREATEDB ou pooler) : "
            "RejeuRolesTest exige une base dédiée pour révoquer TEMP à PUBLIC ; "
            "détail : %s" % exc) from exc
    finally:
        admin.close()


def _drop_database(cfg, database: str) -> None:
    admin = _database_admin(cfg)
    try:
        db_mod.execute_without_statement_timeout(
            admin, "DROP DATABASE IF EXISTS %s WITH (FORCE)" % db_mod.quote_ident(database))
    finally:
        admin.close()


class ManifestTest(unittest.TestCase):
    """La liste déclarée couvre tous les scripts de rôle, sans trou muet."""

    def test_tous_les_scripts_de_role_sont_couverts(self):
        on_disk = {name for name in os.listdir(SQL)
                   if re.fullmatch(r"role-[a-z0-9-]+\.sql", name)}
        self.assertEqual(on_disk, ROLE_SCRIPTS)
        replayed = set(REPLAY_SCRIPTS) | set(droits_roles.REPLAYED_ELSEWHERE)
        covered = replayed | set(droits_roles.EXEMPTIONS)
        self.assertEqual(on_disk, covered)
        self.assertFalse(set(REPLAY_SCRIPTS) & set(droits_roles.EXEMPTIONS))
        self.assertFalse(set(REPLAY_SCRIPTS) & set(droits_roles.REPLAYED_ELSEWHERE))
        self.assertFalse(set(droits_roles.EXEMPTIONS) & set(droits_roles.REPLAYED_ELSEWHERE))
        for name, reason in droits_roles.EXEMPTIONS.items():
            self.assertGreaterEqual(len(reason), 60, name)

    def test_les_scripts_de_rejeu_ne_creent_ni_ne_modifient_un_role(self):
        for replay in NEW_SQL:
            path = os.path.join(SQL, replay)
            self.assertTrue(os.path.isfile(path), replay)
            sql = _sans_commentaires(_read(path)).upper()
            for interdit in ("CREATE ROLE", "ALTER ROLE", "PASSWORD", "DROP ROLE"):
                self.assertNotIn(interdit, sql, "%s : %s" % (replay, interdit))
            self.assertIn("ON_ERROR_STOP", sql)
            self.assertIn("REJEU SAUTE", sql)
            self.assertIn("ROLE", sql)

    def test_les_scripts_de_role_rejoues_existent(self):
        for role_script, replay in REPLAY_SCRIPTS.items():
            self.assertTrue(os.path.isfile(os.path.join(SQL, role_script)), role_script)
            self.assertTrue(os.path.isfile(os.path.join(SQL, replay)), replay)

    def test_module_refuse_un_script_absent(self):
        item = droits_roles.Replay("role-x.sql", "rejeu-x.sql")
        code, verdict = droits_roles.replay(None, os.path.join(REPO, "absent"), item)
        self.assertEqual(code, 2)
        self.assertIn("introuvable", verdict)


class SequenceTest(unittest.TestCase):
    """Le staging et poste.sh appellent le rejeu, puis s'arrêtent sur refus."""

    def test_staging_rejoue_les_contrats_apres_migrate(self):
        text = _read(STAGING)
        debut = text.index("migrer_depuis_vm() {")
        remote = text[text.index("<<'REMOTE'\n", debut):]
        remote = remote[:remote.index("\nREMOTE\n")]
        migrate = remote.index("-m ameesh migrate --additive-only")
        limites = remote.index("-m ameesh.droits_limites")
        superviseur = remote.index("-m ameesh.droits_superviseur")
        roles = remote.index("-m ameesh.droits_roles")
        self.assertLess(migrate, limites)
        self.assertLess(limites, superviseur)
        self.assertLess(superviseur, roles)
        self.assertIn('--sql-dir "$d/deploy/sql" || exit 7', remote)

    def test_staging_arrete_avant_installation_sur_refus_roles(self):
        text = _read(STAGING)
        refus = text.index('elif [ "$rc_migrer" = 7 ]')
        self.assertLess(refus, text.index("# --- sequence par hote"))
        bloc = text[refus:text.index('elif [ "$rc_migrer" != 0 ]', refus)]
        self.assertIn("contrats de rôle en echec", bloc)
        self.assertIn("exit 1", bloc)

    def test_poste_rejoue_les_contrats_apres_la_base_du_mesh(self):
        text = _read(POSTE)
        migrer = text[text.index("\nmigrer() {"):text.index("\nmigrer_base() {")]
        rejeu = migrer.index('-m ameesh.droits_roles --sql-dir "$cible/deploy/sql"')
        self.assertLess(migrer.index('migrer_base "$cible"'), rejeu)
        self.assertIn('AMEESH_CONFIG="$CONFIG"',
                      migrer[migrer.rindex("\n", 0, rejeu - 80):rejeu])
        garde = migrer.index('if { [ "$droits" = 1 ] || [ "$superviseur" = 1 ] || [ "$roles" = 1 ]; }')
        self.assertLess(garde, migrer.index('if [ "$incompatible" = 1 ]; then'))


class RejeuRolesTest(PgTestCase):
    """Les quatre rejeux sur un vrai Postgres, sous un propriétaire sans CREATEROLE."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.admin_db = cls.db
        # `ameesh_canon_sync` est un nom fixe de la grappe : le même verrou que
        # tests/test_l56_role_executor.py sérialise sa création et sa
        # destruction entre les workers du banc.
        cls.lock_db = db_mod.connect(cls.cfg)
        cls.lock_tx = cls.lock_db.transaction()
        cls.lock_tx.__enter__()
        cls.lock_tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('ameesh-role-module-writers', 0))")
        cls.addClassCleanup(cls._release_lock)
        cls.database = "t_rejeu_roles_" + uuid.uuid4().hex[:8]
        cls.addClassCleanup(cls._cleanup)
        _create_database(cls.cfg, cls.database)
        parts = urllib.parse.urlsplit(TEST_DSN)
        if not parts.scheme.startswith("postgres"):
            raise AssertionError("RejeuRolesTest requires a PostgreSQL URL DSN")
        cls.owner_dsn = urllib.parse.urlunsplit(parts._replace(path="/" + cls.database))
        cls.cfg = dataclasses.replace(cls.cfg, dsn=cls.owner_dsn)
        cls.db = db_mod.connect(cls.cfg)
        migrations.migrate(cls.db, log=None)
        # Le contrat des groupes d'écriture exige TEMP révoqué pour PUBLIC ;
        # le propriétaire des tables, lui, garde TEMP pour ses tables temporaires.
        cls.db.execute("REVOKE TEMP ON DATABASE %s FROM PUBLIC"
                       % db_mod.quote_ident(cls.database))
        suffix = uuid.uuid4().hex[:8]
        cls.owner = "t_rejeu_owner_" + suffix
        cls.db.execute('CREATE ROLE "%s" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                       'NOREPLICATION NOBYPASSRLS' % cls.owner)
        cls.db.execute('GRANT TEMP ON DATABASE %s TO "%s"'
                       % (db_mod.quote_ident(cls.database), cls.owner))
        # Le porteur de `migrate` en production : propriétaire du schéma et de
        # ses relations, ni superutilisateur ni CREATEROLE.
        cls.db.execute('ALTER SCHEMA "%s" OWNER TO "%s"' % (cls.schema, cls.owner))
        relations = cls.db.query(
            "SELECT c.relname, c.relkind FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relkind IN ('r','v','m','p','f','S')"
            " ORDER BY 1", (cls.schema,))
        for row in relations:
            commande = {"S": "SEQUENCE", "v": "VIEW", "m": "MATERIALIZED VIEW"}.get(
                row["relkind"], "TABLE")
            cls.db.execute('ALTER %s "%s"."%s" OWNER TO "%s"'
                           % (commande, cls.schema, row["relname"], cls.owner))
        # Le propriétaire des migrations possède les fonctions du schéma, sauf
        # les quelques fonctions à propriétaire dédié.
        routines = cls.db.query(
            "SELECT p.oid::regprocedure::text AS signature, p.prokind FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s",
            (cls.schema,))
        for row in routines:
            commande = {"f": "FUNCTION", "p": "PROCEDURE", "a": "AGGREGATE"}.get(
                row["prokind"], "ROUTINE")
            cls.db.execute('ALTER %s %s OWNER TO "%s"'
                           % (commande, row["signature"], cls.owner))
        # #7986 : sur le mesh, quatre fonctions restent possédées par des rôles
        # dédiés ; le rejeu par le propriétaire des migrations doit les sauter
        # au lieu d'échouer en bloc sur `GRANT ... ON ALL FUNCTIONS`. On
        # reproduit les deux cas, sans EXECUTE pour PUBLIC : une fonction du
        # propriétaire (le rejeu la repose) et une fonction étrangère (le rejeu
        # la saute sans échouer).
        cls.foreign_owner = "t_rejeu_foreign_" + uuid.uuid4().hex[:8]
        cls.db.execute('CREATE ROLE "%s" NOLOGIN' % cls.foreign_owner)
        for nom, proprietaire in (("t_rejeu_owned_fn", cls.owner),
                                  ("t_rejeu_foreign_fn", cls.foreign_owner)):
            cls.db.execute('CREATE FUNCTION "%s".%s() RETURNS int LANGUAGE sql '
                           'IMMUTABLE AS $$ SELECT 1 $$' % (cls.schema, nom))
            cls.db.execute('ALTER FUNCTION "%s".%s() OWNER TO "%s"'
                           % (cls.schema, nom, proprietaire))
            cls.db.execute('REVOKE ALL ON FUNCTION "%s".%s() FROM PUBLIC'
                           % (cls.schema, nom))
        # Installation administrative des rôles et de leurs contrats, comme le
        # bureau : les scripts créent les rôles, le rejeu ne les créera jamais.
        for script in ("role-superviseur.sql", "role-superviseur-contenus.sql",
                       "role-approve.sql", "role-module-writers.sql",
                       "role-pod-provisioner.sql"):
            proc = cls.psql(script)
            if proc.returncode:
                raise AssertionError("%s : %s" % (script, proc.stderr))

    @classmethod
    def _release_lock(cls) -> None:
        try:
            cls.lock_tx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — nettoyage
            pass
        try:
            cls.lock_db.close()
        except Exception:  # noqa: BLE001 — nettoyage
            pass

    @classmethod
    def _cleanup(cls) -> None:
        if cls.__dict__.get("_schema_dropped"):
            return
        cls._schema_dropped = True
        try:
            cls.db.close()
        except Exception:  # noqa: BLE001 — nettoyage
            pass
        try:
            _drop_database(cls.cfg, cls.database)
        except Exception:  # noqa: BLE001 — nettoyage
            pass
        roles = ["ameesh_superviseur", "ameesh_superviseur_contenus", "ameesh_approve",
                 "ameesh_canon_sync", "ameesh_it_proof", "ameesh_it_qa",
                 "ameesh_pod_provisioner", "ameesh_pod_admission"]
        for attribut in ("owner", "foreign_owner"):
            if cls.__dict__.get(attribut):
                roles.append(getattr(cls, attribut))
        for role in roles:
            for sql in ('DROP OWNED BY "%s" CASCADE' % role,
                        'DROP ROLE IF EXISTS "%s"' % role):
                try:
                    cls.admin_db.execute(sql)
                except Exception:  # noqa: BLE001 — nettoyage
                    pass
        try:
            cls.admin_db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % cls.schema)
        except Exception:  # noqa: BLE001 — nettoyage
            pass
        try:
            cls.admin_db.close()
        except Exception:  # noqa: BLE001 — nettoyage
            pass

    @classmethod
    def psql(cls, name: str, *, before: tuple = (), **variables) -> subprocess.CompletedProcess:
        args = ["psql", "-X", "-w", cls.owner_dsn, "-q", "-v", "ON_ERROR_STOP=1"]
        for key, value in variables.items():
            args += ["-v", "%s=%s" % (key, value)]
        for statement in before:
            args += ["-c", statement]
        return subprocess.run(
            args + ["-f", os.path.join(SQL, name)],
            env=dict(os.environ, PGOPTIONS="-c search_path=" + cls.schema),
            capture_output=True, text=True, timeout=120)

    @classmethod
    def replay(cls, name: str, **variables) -> subprocess.CompletedProcess:
        return cls.psql(name, before=('SET ROLE "%s"' % cls.owner,), **variables)

    def lit(self, role: str, colonne: str) -> bool:
        return bool(self.db.query(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT') AS ok",
            (role, '"%s".agent_mailbox' % self.schema, colonne))[0]["ok"])

    def table_priv(self, role: str, table: str, privilege: str) -> bool:
        return bool(self.db.query(
            "SELECT has_table_privilege(%s, %s, %s) AS ok",
            (role, '"%s".%s' % (self.schema, table), privilege))[0]["ok"])

    def function_priv(self, role: str, nom: str, privilege: str = "EXECUTE") -> bool:
        return bool(self.db.query(
            "SELECT has_function_privilege(%s, %s, %s) AS ok",
            (role, '"%s".%s()' % (self.schema, nom), privilege))[0]["ok"])

    def test_approve_rejoue_une_colonne(self):
        self.db.execute('REVOKE SELECT (credential_id) ON "%s".authenticators '
                        'FROM ameesh_approve' % self.schema)
        self.assertFalse(bool(self.db.query(
            "SELECT has_column_privilege('ameesh_approve', %s, 'credential_id', 'SELECT') AS ok",
            ('"%s".authenticators' % self.schema,))[0]["ok"]))
        proc = self.replay("rejeu-approve.sql")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("approve : ameesh_approve rejoue, contrat tenu", proc.stdout)
        self.assertTrue(bool(self.db.query(
            "SELECT has_column_privilege('ameesh_approve', %s, 'credential_id', 'SELECT') AS ok",
            ('"%s".authenticators' % self.schema,))[0]["ok"]))

    def test_superviseur_contenus_rejoue_une_colonne(self):
        self.db.execute('REVOKE SELECT (body) ON "%s".agent_mailbox '
                        'FROM ameesh_superviseur_contenus' % self.schema)
        self.assertFalse(self.lit("ameesh_superviseur_contenus", "body"))
        proc = self.replay("rejeu-superviseur-contenus.sql")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("superviseur-contenus : ameesh_superviseur_contenus rejoue, contrat tenu",
                      proc.stdout)
        self.assertTrue(self.lit("ameesh_superviseur_contenus", "body"))
        # idempotent
        self.assertEqual(self.replay("rejeu-superviseur-contenus.sql").returncode, 0)
        self.assertTrue(self.lit("ameesh_superviseur_contenus", "body"))

    def test_module_writers_rejoue_un_grant(self):
        # #7986 : EXECUTE sur les fonctions appartient au contrat ; le rejeu le
        # repose sur les fonctions du propriétaire et saute celle d'un
        # propriétaire dédié sans faire échouer le script.
        self.db.execute('REVOKE EXECUTE ON FUNCTION "%s".t_rejeu_owned_fn() '
                        'FROM ameesh_it_proof' % self.schema)
        self.assertFalse(self.function_priv("ameesh_it_proof", "t_rejeu_owned_fn"))
        self.db.execute('REVOKE SELECT ON "%s".work_items FROM ameesh_it_proof' % self.schema)
        self.assertFalse(self.table_priv("ameesh_it_proof", "work_items", "SELECT"))
        proc = self.replay("rejeu-module-writers.sql")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("module-writers : ameesh_canon_sync, ameesh_it_proof, ameesh_it_qa rejoues",
                      proc.stdout)
        self.assertTrue(self.table_priv("ameesh_it_proof", "work_items", "SELECT"))
        self.assertTrue(self.function_priv("ameesh_it_proof", "t_rejeu_owned_fn"))
        # la fonction étrangère garde le droit venu de l'installation
        # administrative ; le rejeu ne peut pas le reposer et n'échoue pas.
        self.assertTrue(self.function_priv("ameesh_it_proof", "t_rejeu_foreign_fn"))

    def test_pod_provisioner_rejoue_un_grant(self):
        self.db.execute('REVOKE SELECT ON "%s".pod_templates FROM ameesh_pod_provisioner'
                        % self.schema)
        self.assertFalse(self.table_priv("ameesh_pod_provisioner", "pod_templates", "SELECT"))
        proc = self.replay("rejeu-pod-provisioner.sql")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pod-provisioner : ameesh_pod_provisioner ameesh_pod_admission rejoues",
                      proc.stdout)
        self.assertTrue(self.table_priv("ameesh_pod_provisioner", "pod_templates", "SELECT"))

    def test_roles_absents_sont_sautes_sans_creation(self):
        absent = "t_absent_" + uuid.uuid4().hex[:8]
        for script in ("rejeu-superviseur-contenus.sql", "rejeu-approve.sql"):
            proc = self.replay(script, role=absent)
            self.assertEqual(proc.returncode, 0, (script, proc.stderr))
            self.assertIn("absent, rejeu saute", proc.stdout)
        self.assertEqual(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (absent,)), [])
        # module-writers et pod-provisioner : renommer les rôles le temps du
        # contrôle, sans les détruire (les droits les suivent par OID).
        renamed = []
        try:
            for role in ("ameesh_it_proof", "ameesh_pod_admission"):
                nouveau = role + "_abs"
                self.db.execute('ALTER ROLE "%s" RENAME TO "%s"' % (role, nouveau))
                renamed.append((role, nouveau))
            proc = self.replay("rejeu-module-writers.sql")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("roles absents", proc.stdout)
            proc = self.replay("rejeu-pod-provisioner.sql")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("roles absents", proc.stdout)
        finally:
            for role, nouveau in reversed(renamed):
                self.db.execute('ALTER ROLE "%s" RENAME TO "%s"' % (nouveau, role))

    def test_audit_de_contenus_refuse_et_annule_le_rejeu(self):
        self.db.execute('REVOKE SELECT (body) ON "%s".agent_mailbox '
                        'FROM ameesh_superviseur_contenus' % self.schema)
        self.db.execute('GRANT SELECT (signature) ON "%s".agent_mailbox TO PUBLIC' % self.schema)
        try:
            proc = self.replay("rejeu-superviseur-contenus.sql")
        finally:
            self.db.execute('REVOKE SELECT (signature) ON "%s".agent_mailbox FROM PUBLIC'
                            % self.schema)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lecture hors contrat : agent_mailbox.signature", proc.stderr)
        self.assertIn("NON TENU", proc.stderr)
        # une seule transaction : la colonne retirée n'a pas été reposée
        self.assertFalse(self.lit("ameesh_superviseur_contenus", "body"))
        self.assertEqual(self.replay("rejeu-superviseur-contenus.sql").returncode, 0)
        self.assertTrue(self.lit("ameesh_superviseur_contenus", "body"))

    def test_audit_module_writers_refuse_et_annule_le_rejeu(self):
        self.db.execute('GRANT INSERT ON "%s".project_modules TO PUBLIC' % self.schema)
        try:
            proc = self.replay("rejeu-module-writers.sql")
        finally:
            self.db.execute('REVOKE INSERT ON "%s".project_modules FROM PUBLIC' % self.schema)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("proof writer must not change module policies", proc.stderr)
        self.assertEqual(self.replay("rejeu-module-writers.sql").returncode, 0)

    def test_audit_pod_provisioner_refuse_et_annule_le_rejeu(self):
        self.db.execute('GRANT UPDATE (tenant) ON "%s".pod_requests TO PUBLIC' % self.schema)
        try:
            proc = self.replay("rejeu-pod-provisioner.sql")
        finally:
            self.db.execute('REVOKE UPDATE (tenant) ON "%s".pod_requests FROM PUBLIC'
                            % self.schema)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pod_requests UPDATE outside contract", proc.stderr)
        self.assertEqual(self.replay("rejeu-pod-provisioner.sql").returncode, 0)


if __name__ == "__main__":
    unittest.main()
