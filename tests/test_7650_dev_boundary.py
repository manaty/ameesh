# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §4.6) : la frontière du module `dev`.

Garanties, sans base :

* le noyau ne nomme **aucune** table du module `dev` dans une position de
  table SQL (``FROM``, ``JOIN``, ``UPDATE``, ``INSERT INTO``, ``DELETE FROM``,
  ``TRUNCATE``, ``ALTER TABLE``), hors docstrings ; une exception doit être
  datée, motivée, et disparaît quand le site est corrigé (liste décroissante) ;
* aucune sonde ``to_regclass('…')`` ne nomme une table `dev` en littéral :
  ``qualify()`` laisse les littéraux intacts, donc après P2 la sonde lirait le
  search_path noyau et rendrait NULL sans erreur (revue B1 de #7650) ;
* le résolveur ``relation``/``qualify`` rend le nom du schéma noyau avant P2
  et le schéma ``<noyau>__dev`` lu dans ``module_schemas`` après P2, sans
  toucher aux littéraux SQL ni aux commentaires ;
* chaque module du paquet ``dev.storage`` s'importe seul dans un processus
  Python propre, sans préchargement d'``ameesh.storage`` (#7808).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from ameesh.modules.dev.tables import TABLES

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"
DEV = SOURCE / "modules" / "dev"

#: Un nom de table en position de table, éventuellement qualifié par un schéma.
_TABLE_POSITION = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?"
    r"|ALTER\s+TABLE)\s+(?:ONLY\s+)?(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?\.)?"
    r"(?P<table>%s)\b" % "|".join(sorted((re.escape(name) for name in TABLES),
                                        key=len, reverse=True)))

#: Exceptions datées : `(fichier, table)` → `(lot, date, raison)`. Une entrée
#: qui ne correspond plus à aucune occurrence fait échouer le test (la liste ne
#: peut que décroître).
EXCEPTIONS: dict[tuple[str, str], tuple[str, str, str]] = {}


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _string_constants(tree: ast.AST) -> list[ast.Constant]:
    docs = _docstrings(tree)
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docs]


def scan_source() -> dict[tuple[str, str], list[int]]:
    """Les occurrences de tables `dev` en position de table, par (fichier, table)."""
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        if DEV in path.parents:
            continue
        tree = ast.parse(path.read_text(), str(path))
        for node in _string_constants(tree):
            for match in _TABLE_POSITION.finditer(node.value):
                key = (str(path.relative_to(SOURCE)), match.group("table"))
                found.setdefault(key, []).append(node.lineno)
    return found


#: `to_regclass` sur un littéral nommant une table `dev` (revue B1 de #7650).
#: Le module lui-même est scruté : c'est là que la sonde doit passer par
#: ``relation(self.db, …)`` pour que ``qualify()`` la réécrive après P2.
_TO_REGCLASS_LITERAL = re.compile(
    r"\bto_regclass\s*\(\s*(['\"])(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?\.)?"
    r"(?P<table>%s)\1" % "|".join(sorted((re.escape(name) for name in TABLES),
                                        key=len, reverse=True)),
    re.IGNORECASE)


def scan_to_regclass_literals() -> dict[tuple[str, str], list[int]]:
    """Les `to_regclass('table dev')` littéraux, par (fichier, table)."""
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in _string_constants(tree):
            for match in _TO_REGCLASS_LITERAL.finditer(node.value):
                key = (str(path.relative_to(SOURCE)), match.group("table"))
                found.setdefault(key, []).append(node.lineno)
    return found


#: Les constructeurs SQL du module `dev` (#7650) : leur texte nomme des
#: tables `dev` et n'est qualifié que par ``ModuleDb``. Le noyau ne les
#: importe pas — il passerait par un port du module ; un appel sur sa
#: connexion brute casserait après P2 (revue B1 de #7760).
_DEV_SQL_BUILDERS = frozenset({
    "start_on_sql", "start_blocking", "unknown_policy_sql",
    "footprints_overlap_sql", "overlap_rows_sql", "overlap_exists_sql",
    "held_sql", "cap_rows_sql", "cap_reached_sql", "in_flight_sql",
    "occupying_sql",
})


def scan_dev_sql_builder_imports() -> list[tuple[str, int, str]]:
    """Les imports noyau d'un constructeur SQL du module `dev`."""
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        if DEV in path.parents:
            continue
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name in _DEV_SQL_BUILDERS:
                    found.append((str(path.relative_to(SOURCE)),
                                  node.lineno, alias.name))
    return found


class DevBoundaryTest(unittest.TestCase):

    def test_core_never_names_a_dev_table_in_sql(self):
        found = scan_source()
        actual = set(found)
        declared = set(EXCEPTIONS)
        self.assertFalse(actual - declared,
                         "tables du module `dev` nommées par le noyau : %s"
                         % sorted(actual - declared))
        # liste décroissante : une exception corrigée doit être retirée
        self.assertFalse(declared - actual,
                         "exceptions devenues inutiles : %s" % sorted(declared - actual))
        for key, (lot, declared_on, reason) in EXCEPTIONS.items():
            with self.subTest(key=key):
                self.assertRegex(lot, r"^#\d+$")
                self.assertRegex(declared_on, r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(reason)

    def test_core_never_imports_a_dev_sql_builder(self):
        found = scan_dev_sql_builder_imports()
        self.assertFalse(found,
                         "constructeurs SQL du module `dev` importés par le "
                         "noyau (exécutés sur la connexion brute, non "
                         "qualifiés après P2) : %s" % found)

    def test_no_to_regclass_literal_names_a_dev_table(self):
        found = scan_to_regclass_literals()
        self.assertFalse(found,
                         "to_regclass littéral sur une table `dev` : %s"
                         % sorted(found))

    def test_core_does_not_import_the_module_storage(self):
        """Seuls les alias datés et les déclarations de `dev` sont importés."""
        from .test_generic_core import COMPATIBILITY_ALIASES, DECLARED_IMPORTS
        allowed = set(COMPATIBILITY_ALIASES) | set(DECLARED_IMPORTS)
        offenders = []
        for path in sorted(SOURCE.rglob("*.py")):
            if DEV in path.parents:
                continue
            rel = str(path.relative_to(SOURCE))
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                # relatif (`from .modules.dev…`) ou absolu (`ameesh.modules.dev…`)
                module = node.module
                if node.level:
                    module = "ameesh." + module
                if module.startswith("ameesh.modules.dev") and rel not in allowed:
                    offenders.append((rel, node.lineno))
        self.assertFalse(offenders, "import direct du module `dev` : %s" % offenders)

    def test_relation_reads_the_registry_before_and_after_p2(self):
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def __init__(self, schema_name=None):
                self.schema_name = schema_name
                self.seen = []

            def query(self, sql, params=()):
                self.seen.append((sql, params))
                if "pg_catalog.pg_class" in sql:
                    present = self.schema_name is not None
                    return [{"present": present, "readable": present}]
                if self.schema_name is None:
                    return []
                return [{"schema_name": self.schema_name}]

        before = _Db()
        self.assertEqual(schema.relation(before, "ci_runs"), '"public"."ci_runs"')
        after = _Db("t_x__dev")
        # le nom physique suit la ligne du registre, sans second changement
        self.assertEqual(schema.relation(after, "ci_runs"), '"t_x__dev"."ci_runs"')
        self.assertEqual(schema.relation(after, "work_landings"),
                         '"t_x__dev"."work_landings"')

    def test_core_schema_is_taken_as_configured_not_fallen_back_to_public(self):
        """B2 de #7650 : un schéma noyau valide (majuscule, `$`) reste le sien."""
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "Mesh_$1"

        class _Db:
            cfg = _Cfg()

            def __init__(self):
                self.seen = []

            def query(self, sql, params=()):
                self.seen.append((sql, params))
                return []

        db = _Db()
        self.assertEqual(schema.relation(db, "ci_runs"), '"Mesh_$1"."ci_runs"')
        # la sonde de présence ne résout aucun nom par le search_path : le
        # schéma configuré est un paramètre, jamais retombé sur `public`.
        self.assertIn("pg_catalog.pg_class", db.seen[0][0])
        self.assertEqual(db.seen[0][1], ("Mesh_$1", "Mesh_$1"))

        class _EmptyCfg:
            schema = ""

        class _EmptyDb(_Db):
            cfg = _EmptyCfg()

        self.assertEqual(schema.relation(_EmptyDb(), "ci_runs"),
                         '"public"."ci_runs"')

    def test_absent_registry_is_never_queried(self):
        """Le registre absent retombe sur le noyau : aucun SELECT qui avarie."""
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def __init__(self):
                self.seen = []

            def query(self, sql, params=()):
                self.seen.append((sql, params))
                return [{"present": False, "readable": False}]

        db = _Db()
        self.assertEqual(schema.relation(db, "ci_runs"), '"public"."ci_runs"')
        self.assertEqual(len(db.seen), 1)
        self.assertIn("pg_catalog.pg_class", db.seen[0][0])
        self.assertNotIn("FROM \"public\".module_schemas", db.seen[0][0])

    def test_present_but_unreadable_registry_is_refused(self):
        """#43584 : présent sans droit de lecture ne devient pas « absent »."""
        from ameesh import db as db_mod
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def __init__(self):
                self.seen = []

            def query(self, sql, params=()):
                self.seen.append((sql, params))
                return [{"present": True, "readable": False}]

        db = _Db()
        with self.assertRaisesRegex(db_mod.DbError, "present but not readable"):
            schema.relation(db, "ci_runs")
        self.assertEqual(len(db.seen), 1)
        self.assertNotIn("FROM \"public\".module_schemas", db.seen[0][0])

    def test_qualify_rewrites_tables_but_not_literals_or_comments(self):
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def query(self, sql, params=()):
                if "pg_catalog.pg_class" in sql:
                    return [{"present": True, "readable": True}]
                return [{"schema_name": "t_x__dev"}]

        sql = ("SELECT ci_runs FROM ci_runs -- ci_runs\n"
               " WHERE note = 'ci_runs' AND id = %s")
        self.assertEqual(
            schema.qualify(_Db(), sql),
            'SELECT ci_runs FROM "t_x__dev"."ci_runs" -- ci_runs\n'
            " WHERE note = 'ci_runs' AND id = %s")
        # une colonne homonyme d'une table n'est jamais qualifiée
        self.assertEqual(
            schema.qualify(_Db(), "SELECT ci_runs FROM work_item_measures"),
            'SELECT ci_runs FROM "t_x__dev"."work_item_measures"')
        self.assertEqual(
            schema.qualify(_Db(), "INSERT INTO work_packet_members (package_id) VALUES (%s)"),
            'INSERT INTO "t_x__dev"."work_packet_members" (package_id) VALUES (%s)')

    def test_dev_declares_the_tables_it_owns(self):
        self.assertEqual(len(TABLES), 36)
        self.assertEqual(len(set(TABLES)), len(TABLES))
        # les déclarations d'agent ne nomment que des tables du module
        from ameesh.modules.dev.tables import KEPT, REWRITTEN
        for column in tuple(REWRITTEN) + tuple(KEPT):
            table = column.split(".", 1)[0]
            self.assertIn(table, TABLES, column)

    def test_each_dev_storage_module_imports_alone(self):
        """#7808 : chaque module se charge dans un processus propre, à froid.

        Sans préchargement d'``ameesh.storage`` : le cycle
        ``dev.storage`` → ``storage`` → ``storage.postgres`` (ses alias datés
        importent ``dev.storage``) → ``dev.storage`` faisait boucler l'import
        avant l'initialisation du stockage. Le processus vérifie aussi que
        l'export public ``PostgresStorage`` reste résolu malgré le chargement
        différé du pilote."""
        package = "ameesh.modules.dev.storage"
        directory = SOURCE / "modules" / "dev" / "storage"
        modules = sorted(path.stem for path in directory.glob("*.py")
                         if path.stem != "__init__")
        self.assertTrue(modules)
        script = (
            "import importlib\n"
            "import sys\n"
            "module = importlib.import_module(sys.argv[1])\n"
            "assert module.__name__ == sys.argv[1], module.__name__\n"
            "from ameesh.storage import PostgresStorage\n"
            "assert PostgresStorage.__name__ == 'PostgresStorage'\n"
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        for module in modules:
            with self.subTest(module=module):
                done = subprocess.run(
                    [sys.executable, "-c", script, package + "." + module],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(
                    done.returncode, 0,
                    "import isolé de %s.%s échoue :\n%s"
                    % (package, module, done.stderr[-2000:]))

    def test_apply_unknown_repository_needs_no_database(self):
        """B de #7808 : `_apply_unknown('repository')` construit son SQL avec
        une transaction factice, donc sans base ni pilote. Le noyau y est
        importé à l'usage (`_done_sql`) : un nom retiré du module (le
        `_DONE_SQL` du chargement) se voit ici au lieu d'un NameError dans la
        transaction réelle."""
        from ameesh.modules.dev.storage.work_footprints import WorkFootprints

        class _Tx:
            def __init__(self):
                self.queries = []
                self.executes = []

            def query(self, sql, params=()):
                self.queries.append((sql, params))
                return [{"id": 7, "state": "build"}]

            def execute(self, sql, params=()):
                self.executes.append((sql, params))

        tx = _Tx()
        marked = WorkFootprints._apply_unknown(
            tx, "acme", "repository", "agent")
        self.assertEqual(marked, [7])
        self.assertEqual(tx.queries[0][1], ("acme",))
        self.assertIn("w.state NOT IN ('delivered', 'delivered', 'closed')",
                      tx.queries[0][0])
        # une empreinte `.`, une analyse inconnue, une ligne de journal
        self.assertEqual(len(tx.executes), 3, tx.executes)
        self.assertIn("work_item_footprints", tx.executes[0][0])
        self.assertIn("work_item_impact_analyses", tx.executes[1][0])
        self.assertIn("work_item_events", tx.executes[2][0])


if __name__ == "__main__":
    unittest.main()
