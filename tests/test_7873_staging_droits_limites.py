# SPDX-License-Identifier: AGPL-3.0-only
"""#7873 : le staging rejoue les droits des connexions limitées, puis l'audit.

Cause racine de #7870 : SELECT de bureau_goal sur module_schemas était dans
`_bureau-droits.sql` depuis le 18/11, mais rien ne le rejouait sur le hub.
Témoins : la séquence (simulée) lance le rejeu après `migrate`, sous la même
connexion, et s'arrête avant toute installation sur un refus ; le rejeu
rétablit un droit retiré, est idempotent, saute un rôle absent sans le créer,
et l'audit nomme le droit en défaut en annulant le rejeu.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid

from ameesh import droits_limites

from .role_acl import serialize_role_acl
from .support import REPO, TEST_DSN, PgTestCase

SQL = os.path.join(REPO, "deploy", "sql")
STAGING = os.path.join(REPO, "deploy", "mise-a-jour", "staging.sh")
POSTE = os.path.join(REPO, "deploy", "mise-a-jour", "poste.sh")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _remote_migrations() -> str:
    """Le script distant de `migrer_depuis_vm`, tel que staging.sh l'envoie."""
    text = _read(STAGING)
    start = text.index("migrer_depuis_vm() {")
    body = text[text.index("<<'REMOTE'\n", start) + len("<<'REMOTE'\n"):]
    return body[:body.index("\nREMOTE\n")] + "\n"


class SequenceSimuleeTest(unittest.TestCase):
    """Le script distant joué pour de vrai, python et dépôt simulés."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        venv = os.path.join(self.home, ".local", "share", "ameesh", "venv", "bin")
        os.makedirs(venv)
        self.log = os.path.join(self.tmp, "appels")
        python = os.path.join(venv, "python3")
        with open(python, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$APPELS"\n'
                     'case "$*" in *ameesh.droits_limites*) exit "${DROITS_RC:-0}" ;; esac\n'
                     'case "$*" in *ameesh.droits_superviseur*) exit "${SUPERVISEUR_RC:-0}" ;; esac\n'
                     'case "$*" in *ameesh.droits_roles*) exit "${ROLES_RC:-0}" ;; esac\n')
        os.chmod(python, 0o755)
        self.depot = os.path.join(self.tmp, "depot")
        for sub in ("src/ameesh", "deploy/sql"):
            os.makedirs(os.path.join(self.depot, sub))
        for name in ("src/ameesh/__init__.py", "deploy/sql/rejeu-droits-limites.sql",
                     "deploy/sql/rejeu-superviseur.sql", "deploy/sql/rejeu-superviseur-contenus.sql",
                     "deploy/sql/rejeu-approve.sql", "deploy/sql/rejeu-module-writers.sql",
                     "deploy/sql/rejeu-pod-provisioner.sql"):
            with open(os.path.join(self.depot, name), "w", encoding="utf-8") as fh:
                fh.write("-- fixture\n")

        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", "-C", self.depot, "-c", "core.hooksPath=/dev/null", *args],
                text=True).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "fixture")
        git("remote", "add", "origin", self.depot)
        self.sha = git("rev-parse", "HEAD")

    def run_remote(self, droits_rc: int, superviseur_rc: int = 0,
                   roles_rc: int = 0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-s", "--", self.sha, self.depot, os.path.join(self.tmp, "snap")],
            input=_remote_migrations(), capture_output=True, text=True, timeout=60,
            env=dict(os.environ, HOME=self.home, APPELS=self.log, DROITS_RC=str(droits_rc),
                     SUPERVISEUR_RC=str(superviseur_rc), ROLES_RC=str(roles_rc)))

    def calls(self) -> list[str]:
        return _read(self.log).splitlines()

    def test_rejeu_apres_migrate_sur_l_instantane(self):
        proc = self.run_remote(0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        snap = os.path.join(self.tmp, "snap", "snap-" + self.sha[:8])
        self.assertEqual(calls[-4], "-m ameesh migrate --additive-only")
        self.assertEqual(calls[-3], "-m ameesh.droits_limites --sql-dir %s/deploy/sql" % snap)
        # #7982 : le contrat du superviseur est rejoué après les migrations, sur
        # le même instantané et sous la même connexion que le rejeu #7873.
        self.assertEqual(calls[-2], "-m ameesh.droits_superviseur --sql-dir %s/deploy/sql" % snap)
        # #7986 : les autres contrats rejouables suivent, même instantané.
        self.assertEqual(calls[-1], "-m ameesh.droits_roles --sql-dir %s/deploy/sql" % snap)
        # le SQL relu part avec l'instantané du commit déployé
        self.assertTrue(os.path.isfile(os.path.join(snap, "deploy", "sql", "rejeu-droits-limites.sql")))
        self.assertTrue(os.path.isfile(os.path.join(snap, "deploy", "sql", "rejeu-superviseur.sql")))
        for name in ("rejeu-superviseur-contenus.sql", "rejeu-approve.sql",
                     "rejeu-module-writers.sql", "rejeu-pod-provisioner.sql"):
            self.assertTrue(os.path.isfile(os.path.join(snap, "deploy", "sql", name)), name)

    def test_refus_du_contrat_code_5(self):
        proc = self.run_remote(1)
        self.assertEqual(proc.returncode, 5)
        self.assertIn("ameesh.droits_limites", self.calls()[-1])

    def test_refus_du_contrat_superviseur_code_6(self):
        proc = self.run_remote(0, superviseur_rc=1)
        self.assertEqual(proc.returncode, 6)
        self.assertIn("ameesh.droits_superviseur", self.calls()[-1])

    def test_refus_des_contrats_de_role_code_7(self):
        # les contrats précédents passent : seul droits_roles refuse
        proc = self.run_remote(0, roles_rc=1)
        self.assertEqual(proc.returncode, 7)
        self.assertIn("ameesh.droits_roles", self.calls()[-1])

    def test_staging_s_arrete_avant_toute_installation(self):
        text = _read(STAGING)
        refus = text.index('if [ "$rc_migrer" = 5 ]; then')
        self.assertLess(refus, text.index("# --- sequence par hote"))
        bloc = text[refus:text.index("elif", refus)]
        self.assertIn("droits ko", bloc)
        self.assertIn("exit 1", bloc)
        # #7986 : même arrêt avant installation sur refus des contrats de rôle
        refus7 = text.index('elif [ "$rc_migrer" = 7 ]; then')
        self.assertLess(refus7, text.index("# --- sequence par hote"))
        bloc7 = text[refus7:text.index("elif", refus7 + 1)]
        self.assertIn("droits ko", bloc7)
        self.assertIn("exit 1", bloc7)

    def test_poste_rejoue_apres_la_base_du_mesh(self):
        text = _read(POSTE)
        migrer = text[text.index("\nmigrer() {"):text.index("\nmigrer_base() {")]
        rejeu = migrer.index("-m ameesh.droits_limites --sql-dir \"$cible/deploy/sql\"")
        self.assertLess(migrer.index('migrer_base "$cible"'), rejeu)
        self.assertIn('AMEESH_CONFIG="$CONFIG"', migrer[migrer.rindex("\n", 0, rejeu - 80):rejeu])
        # #7986 : les contrats rejouables passent après le superviseur
        roles = migrer.index("-m ameesh.droits_roles --sql-dir \"$cible/deploy/sql\"")
        superviseur = migrer.index("-m ameesh.droits_superviseur --sql-dir \"$cible/deploy/sql\"")
        self.assertLess(rejeu, superviseur)
        self.assertLess(superviseur, roles)
        self.assertIn('AMEESH_CONFIG="$CONFIG"', migrer[migrer.rindex("\n", 0, roles - 80):roles])
        # staging (MIGRER_SANS_REDEMARRAGE=1) : refus avant le retour anticipé
        # (#7982 : la garde couvre aussi le contrat du superviseur ;
        # #7986 : et les autres contrats de rôle)
        garde = migrer.index('if { [ "$droits" = 1 ] || [ "$superviseur" = 1 ] || [ "$roles" = 1 ]; }')
        self.assertLess(garde, migrer.index('if [ "$incompatible" = 1 ]; then'))
        self.assertLess(roles, garde)

    def test_aucune_creation_de_role_ni_mot_de_passe(self):
        for name in ("rejeu-droits-limites.sql", "_bureau-poser-droits.sql"):
            sql = re.sub(r"--[^\n]*", "", _read(os.path.join(SQL, name))).upper()
            for interdit in ("CREATE ROLE", "ALTER ROLE", "PASSWORD", "DROP ", "INSERT", "UPDATE "):
                self.assertNotIn(interdit, sql, name)


class RejeuDroitsLimitesTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        serialize_role_acl(cls)
        cls.login = "t_bureau_%s" % uuid.uuid4().hex[:10]
        cls.addClassCleanup(cls.drop_roles, cls.login)
        proc = cls.psql("role-bureau.sql", role=cls.login, agent="bureau", host=cls.cfg.host)
        if proc.returncode:
            raise AssertionError(proc.stderr)

    @classmethod
    def drop_roles(cls, *roles: str) -> None:
        for role in roles:
            subprocess.run(["psql", "-X", "-w", TEST_DSN, "-q", "-c",
                            'DROP OWNED BY "%s" CASCADE; DROP ROLE IF EXISTS "%s";' % (role, role)],
                           capture_output=True, text=True, timeout=30)

    @classmethod
    def psql(cls, name: str, *, before: tuple = (), **variables) -> subprocess.CompletedProcess:
        args = ["psql", "-X", "-w", TEST_DSN, "-q", "-v", "ON_ERROR_STOP=1"]
        for key, value in variables.items():
            args += ["-v", "%s=%s" % (key, value)]
        for statement in before:
            args += ["-c", statement]
        return subprocess.run(args + ["-f", os.path.join(SQL, name)],
                              env=dict(os.environ, PGOPTIONS="-c search_path=" + cls.schema),
                              capture_output=True, text=True, timeout=120)

    def audit(self, **extra) -> subprocess.CompletedProcess:
        return self.psql("_contrat-bureau.sql", role=self.login, agent="bureau",
                         host=self.cfg.host, **extra)

    def acl(self) -> list:
        return self.db.query(
            "SELECT c.relname, c.relacl::text AS relacl, a.attname, a.attacl::text AS attacl"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0"
            " WHERE n.nspname = current_schema() ORDER BY 1, 3")

    def test_un_droit_retire_est_nomme_puis_rejoue(self):
        self.db.execute('REVOKE SELECT (module) ON module_schemas FROM "%s"' % self.login)
        proc = self.audit()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing SELECT on module_schemas.module", proc.stderr)
        code, verdict = droits_limites.replay(self.cfg, SQL, self.login)
        self.assertEqual(code, 0, verdict)
        self.assertIn("contrat bureau conforme", verdict)
        self.assertEqual(self.audit().returncode, 0)

    def test_rejeu_idempotent(self):
        self.assertEqual(droits_limites.replay(self.cfg, SQL, self.login)[0], 0)
        before = self.acl()
        self.assertEqual(droits_limites.replay(self.cfg, SQL, self.login)[0], 0)
        self.assertEqual(self.acl(), before)

    def test_role_absent_saute_sans_creation(self):
        absent = "t_absent_%s" % uuid.uuid4().hex[:10]
        code, verdict = droits_limites.replay(self.cfg, SQL, absent)
        self.assertEqual(code, 0, verdict)
        self.assertIn("absent, rejeu saute", verdict)
        self.assertEqual(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (absent,)), [])

    def test_role_non_inscrit_refuse(self):
        other = "t_autre_%s" % uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE "%s" NOLOGIN' % other)
        self.addCleanup(self.drop_roles, other)
        code, verdict = droits_limites.replay(self.cfg, SQL, other)
        self.assertEqual(code, 1)
        self.assertIn("is not a limited login", verdict)
        self.assertEqual(self.db.query(
            "SELECT 1 FROM information_schema.role_table_grants WHERE grantee = %s", (other,)), [])

    def test_audit_en_echec_nomme_le_droit_et_annule_le_rejeu(self):
        self.db.execute('REVOKE SELECT (module) ON module_schemas FROM "%s"' % self.login)
        self.db.execute("GRANT SELECT ON mesh_approvals TO PUBLIC")
        try:
            code, verdict = droits_limites.replay(self.cfg, SQL, self.login)
        finally:
            self.db.execute("REVOKE SELECT ON mesh_approvals FROM PUBLIC")
        self.assertEqual(code, 1)
        self.assertIn("unexpected SELECT on mesh_approvals", verdict)
        # une seule transaction : le droit retiré n'a pas été reposé
        self.assertIn("missing SELECT on module_schemas.module", self.audit().stderr)
        self.assertEqual(droits_limites.replay(self.cfg, SQL, self.login)[0], 0)

    def test_audit_sans_superutilisateur_saute_les_mots_de_passe(self):
        if not self.db.query("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")[0]["rolsuper"]:
            self.skipTest("SET ROLE vers un rôle ordinaire : superutilisateur requis")
        owner = "t_proprio_%s" % uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE "%s" NOLOGIN' % owner)
        self.addCleanup(self.drop_roles, owner)
        self.db.execute('GRANT USAGE ON SCHEMA "%s" TO "%s"' % (self.schema, owner))
        self.db.execute('GRANT SELECT ON limited_logins TO "%s"' % owner)
        proc = self.audit(before=('SET ROLE "%s"' % owner,))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password checks skipped", proc.stderr)

    def test_roles_lus_dans_limited_logins(self):
        # #7660 : aucun nom de rôle en dur, les inscriptions de la base
        self.assertIn(self.login, droits_limites.enrolled(self.cfg))

    def test_sql_sans_role_refuse(self):
        proc = self.psql("rejeu-droits-limites.sql")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("-v role=<connexion limitee> requis", proc.stderr)

    def test_entree_cli(self):
        with tempfile.TemporaryDirectory() as vide:
            code, verdict = droits_limites.replay(self.cfg, vide, self.login)
        self.assertEqual(code, 2)
        self.assertIn("introuvable", verdict)


if __name__ == "__main__":
    unittest.main()
