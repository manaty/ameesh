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
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
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

    def test_qualify_rewrites_tables_but_not_literals_or_comments(self):
        from ameesh.modules.dev.storage import schema

        class _Cfg:
            schema = "public"

        class _Db:
            cfg = _Cfg()

            def query(self, sql, params=()):
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


if __name__ == "__main__":
    unittest.main()
