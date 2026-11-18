# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §4.6) : la frontière du module `dev`.

Deux garanties, sans base :

* le noyau ne nomme **aucune** table du module `dev` dans une position de
  table SQL (``FROM``, ``JOIN``, ``UPDATE``, ``INSERT INTO``, ``DELETE FROM``,
  ``TRUNCATE``, ``ALTER TABLE``), hors docstrings ; une exception doit être
  datée, motivée, et disparaît quand le site est corrigé (liste décroissante) ;
* le résolveur ``relation``/``qualify`` rend le nom du schéma noyau avant P2
  et le schéma ``<noyau>__dev`` lu dans ``module_schemas`` après P2, sans
  toucher aux littéraux SQL ni aux commentaires.

Un témoin sur les pilotes réels couvre la lecture du registre sur une base
d'avant P1a : ``to_regclass`` sonde la relation sans condamner la transaction
de l'appelant (relecture cx2 #226204).
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
import re
import unittest

from ameesh import db as db_mod
from ameesh.modules.dev.tables import TABLES

from . import support

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"
DEV = SOURCE / "modules" / "dev"

#: Un nom de table en position de table, éventuellement qualifié par un schéma.
_TABLE_POSITION = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?"
    r"|ALTER\s+TABLE)\s+(?:ONLY\s+)?(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?\.)?"
    r"(?P<table>%s)\b" % "|".join(sorted((re.escape(name) for name in TABLES),
                                        key=len, reverse=True)))

#: Un ``to_regclass('…')`` littéral : le nom ne passe pas par `relation` et
#: `qualify` ne le réécrit pas (B1 #7650).
_TO_REGCLASS_LITERAL = re.compile(r"to_regclass\(\s*'((?:[^']|'')*)'", re.IGNORECASE)

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


def scan_to_regclass_literals() -> dict[tuple[str, str], list[int]]:
    """Les ``to_regclass('…')`` littéraux qui nomment une table `dev`.

    `qualify` ne réécrit pas les littéraux : un tel nom reste sur le
    `search_path` noyau après P2 et la lecture rend une absence silencieuse.
    """
    names = set(TABLES)
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in _string_constants(tree):
            for match in _TO_REGCLASS_LITERAL.finditer(node.value):
                literal = match.group(1).replace("''", "'")
                table = literal.rsplit(".", 1)[-1].strip().strip('"')
                if table in names:
                    key = (str(path.relative_to(SOURCE)), table)
                    found.setdefault(key, []).append(node.lineno)
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

    def test_no_to_regclass_literal_names_a_dev_table(self):
        """B1 (#7650) : ces noms passent par `relation`, jamais en littéral."""
        found = scan_to_regclass_literals()
        self.assertFalse(found,
                         "to_regclass littéral sur une table `dev` : %s"
                         % sorted(found))

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
                if "to_regclass" in sql:
                    return [{"present": self.schema_name is not None}]
                return [{"schema_name": self.schema_name}]

        before = _Db()
        self.assertEqual(schema.relation(before, "ci_runs"), '"public"."ci_runs"')
        # avant P1a, le registre n'est même pas lu : la transaction survit
        self.assertEqual(len(before.seen), 1)
        self.assertIn("to_regclass", before.seen[0][0])
        after = _Db("t_x__dev")
        # le nom physique suit la ligne du registre, sans second changement
        self.assertEqual(schema.relation(after, "ci_runs"), '"t_x__dev"."ci_runs"')
        self.assertEqual(schema.relation(after, "work_landings"),
                         '"t_x__dev"."work_landings"')

    def test_relation_keeps_a_valid_core_schema_and_never_uses_public(self):
        """B2 (#7650) : la forme admise est celle de ``config.SCHEMA_RE``."""
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "Mesh$One"

        class _Db:
            cfg = _Cfg()

            def query(self, sql, params=()):
                if "to_regclass" in sql:
                    return [{"present": False}]
                raise AssertionError("registre absent : aucune lecture")

        self.assertEqual(schema.relation(_Db(), "ci_runs"),
                         '"Mesh$One"."ci_runs"')

    def test_relation_fails_closed_when_the_registry_is_unreadable(self):
        """Un registre présent mais illisible ne simule pas un repli."""
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def query(self, sql, params=()):
                if "to_regclass" in sql:
                    return [{"present": True}]
                raise RuntimeError("registre illisible")

        with self.assertRaises(RuntimeError):
            schema.relation(_Db(), "ci_runs")

    def test_slots_available_reads_the_presence_column(self):
        """N (#7650) : une ligne revient toujours, sa colonne fait foi."""
        from ameesh.modules.dev.storage.work import DevWork

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def __init__(self, present):
                self.present = present

            def query(self, sql, params=()):
                if "module_schemas" in params[0]:
                    return [{"present": False}]
                return [{"present": self.present}]

        self.assertFalse(DevWork(_Db(False)).slots_available())
        self.assertTrue(DevWork(_Db(True)).slots_available())

    def test_qualify_rewrites_tables_but_not_literals_or_comments(self):
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def query(self, sql, params=()):
                if "to_regclass" in sql:
                    return [{"present": True}]
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


class MissingRegistryDbTest(support.PgTestCase):
    """cx2 #226204 : avant P1a, sonder le registre ne condamne pas la transaction.

    Un ``SELECT … module_schemas`` sur une base qui n'a pas encore la
    migration 0591 lève et invalide la transaction (psql ferme même son
    processus sous ``ON_ERROR_STOP``). Le résolveur doit sonder la relation
    avec ``to_regclass`` avant de la lire, sur les deux pilotes réels.
    """

    def test_relation_before_p1a_leaves_the_transaction_usable(self):
        from ameesh.modules.dev.storage import schema

        name = self.schema + "_noreg"
        self.create_schema(name)
        self.addCleanup(self.db.execute,
                        'DROP SCHEMA IF EXISTS "%s" CASCADE' % name)
        tried = []
        for driver in ("psql", "psycopg"):
            try:
                conn = db_mod.connect(
                    dataclasses.replace(self.cfg, schema=name),
                    driver=driver, writer=False)
            except db_mod.Unavailable:
                continue
            tried.append(driver)
            self.addCleanup(conn.close)
            table = "probe_%s" % driver
            with conn.transaction() as tx:
                self.assertEqual(schema.relation(tx, "work_items"),
                                 '"%s"."work_items"' % name)
                tx.execute('CREATE TABLE "%s".%s (id int)' % (name, table))
                tx.execute('INSERT INTO "%s".%s VALUES (1)' % (name, table))
            self.assertEqual(
                conn.query('SELECT count(*) AS n FROM "%s".%s' % (name, table))[0]["n"],
                1, driver)
        self.assertTrue(tried, "aucun pilote Postgres réel disponible")


if __name__ == "__main__":
    unittest.main()
