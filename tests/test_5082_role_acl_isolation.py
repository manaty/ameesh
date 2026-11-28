# SPDX-License-Identifier: AGPL-3.0-only
"""Le verrou des contrats couvre la classe et se libère au cleanup."""
import ast
import pathlib
import unittest

from .role_acl import serialize_role_acl
from .support import PgTestCase


def lock_holders(db, database) -> list[int]:
    """Porteurs du verrou dans `database` (le verrou est propre à une base)."""
    return [row["pid"] for row in db.query(
        "SELECT l.pid FROM pg_locks l JOIN pg_database d ON l.database = d.oid "
        "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
        "AND d.datname = %s "
        "AND (l.classid::bigint << 32 | l.objid::bigint) = "
        "hashtextextended('ameesh-test-role-public-acl', 0)", (database,))]


class RoleAclIsolationTest(PgTestCase):
    def test_lock_survives_statements_and_setup_failure(self):
        class Fixture:
            cfg = self.cfg
            cleanups = []

            @classmethod
            def addClassCleanup(cls, cleanup):
                cls.cleanups.append(cleanup)

        probe = self.connect()
        self.addCleanup(probe.close)
        try:
            with self.assertRaisesRegex(RuntimeError, "échec simulé"):
                guard = serialize_role_acl(Fixture)
                # Les scripts sont lancés sur d'autres connexions ; une requête
                # terminée sur la connexion de classe ne doit pas lever la garde.
                self.db.query("SELECT 1")
                self.assertIn(guard.pid, lock_holders(probe, guard.database))
                raise RuntimeError("échec simulé de setUpClass")
        finally:
            for cleanup in reversed(Fixture.cleanups):
                cleanup()
        # Pas de nouvel essai de verrou : une autre classe en file le prend
        # dès la libération, seule compte la session de cette garde.
        self.assertNotIn(guard.pid, lock_holders(probe, guard.database))


class RoleContractGuardCoverageTest(unittest.TestCase):
    """#8481 : la classe du contrat exécuteur prend la garde des ACL.

    `role-executor.sql` audite toutes les bases du cluster (`pg_database`) et
    la classe pose des `GRANT ... TO PUBLIC` temporaires. Quand le plan découpe
    une classe lourde entre les processus d'une part, deux instances de
    `RoleExecutorTest` intercalaient le GRANT de l'une avec l'audit de l'autre :
    le contrat voyait la base de l'autre instance et échouait (CI 46167715827,
    psql 9/12). `serialize_role_acl` est la garde commune des classes de
    contrat ; ce témoin fige son appel, avant toute mutation d'ACL.
    """

    def test_role_executor_takes_the_guard_before_any_acl(self):
        source = pathlib.Path(__file__).with_name("test_l56_role_executor.py")
        arbre = ast.parse(source.read_text(encoding="utf-8"))
        classe = next((noeud for noeud in arbre.body
                       if isinstance(noeud, ast.ClassDef)
                       and noeud.name == "RoleExecutorTest"), None)
        self.assertIsNotNone(classe, "RoleExecutorTest introuvable")
        setup = next((noeud for noeud in classe.body
                      if isinstance(noeud, ast.FunctionDef)
                      and noeud.name == "setUpClass"), None)
        self.assertIsNotNone(setup, "RoleExecutorTest.setUpClass introuvable")
        gardes = [noeud for noeud in ast.walk(setup)
                  if isinstance(noeud, ast.Call)
                  and isinstance(noeud.func, ast.Name)
                  and noeud.func.id == "serialize_role_acl"]
        self.assertEqual(len(gardes), 1,
                         "RoleExecutorTest.setUpClass doit appeler "
                         "serialize_role_acl(cls) exactement une fois")
        self.assertEqual(len(gardes[0].args), 1)
        self.assertEqual(ast.unparse(gardes[0].args[0]), "cls")

        def mutation(noeud: ast.Call) -> bool:
            if isinstance(noeud.func, ast.Name):
                return noeud.func.id == "_create_database"
            if isinstance(noeud.func, ast.Attribute):
                return noeud.func.attr in ("execute", "migrate", "run_script")
            return False

        mutations = [noeud.lineno for noeud in ast.walk(setup)
                     if isinstance(noeud, ast.Call) and mutation(noeud)]
        self.assertTrue(mutations, "aucune mutation d'ACL trouvée dans setUpClass")
        self.assertLess(gardes[0].lineno, min(mutations),
                        "la garde doit précéder la création de la base et les "
                        "GRANT/REVOKE de la classe")
