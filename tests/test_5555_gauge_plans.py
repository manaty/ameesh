# SPDX-License-Identifier: AGPL-3.0-only
"""#5555 : garde de plan et garde statique pour les lectures de jauges.

#5466 (`gauge_latest_window`) a ajouté une lecture `DISTINCT ON` à côté du
seek par groupe de `_gauge_heads` (#3922) : sur le central, trier tout
l'historique coûtait 19 s par appel, 35 appels concurrents, 98 % de CPU. Les
témoins de #3922 et #5542 ne comparaient que les résultats, jamais le plan ;
les tests EXPLAIN existants (#3923/#3741/#3397) sautent sous le pilote psql :
la part psql ne les exerce jamais.

Deux gardes ici, complémentaires :

* un plan RÉEL des trois lectures de `quota_gauge_readings` (`gauge_latest`,
  `gauge_latest_window`, `latest_gauges`), à volume réaliste (52 800
  lectures, ≥ 50 000 comme au central) : aucun `Seq Scan` ni `Sort` des
  lignes de la table, et chaque lecture cherche par son index de seek
  (0493 pour la production, 0535 pour la fenêtre, 0493-source pour
  `by_source`) ;
* une garde statique qui refuse tout `DISTINCT ON` sur
  `quota_gauge_readings` hors de `_gauge_heads` : la requête de #5466 ne
  peut pas revenir à côté du seek sans faire rougir les tests.
"""
from __future__ import annotations

import ast
import json
import os
import re
import unittest

from ameesh import db as db_mod, storage

from .support import PgTestCase, SRC

TABLE = "quota_gauge_readings"
HEAD_SEEK = "_gauge_heads"
DISTINCT_ON = re.compile(r"DISTINCT\s+ON", re.IGNORECASE)


# -- plan des lectures ------------------------------------------------------

def _nodes(roots):
    """Tous les nœuds d'un plan `EXPLAIN (FORMAT JSON)`, profondeur d'abord."""
    for node in roots:
        yield node
        yield from _nodes(node.get("Plans", []))


def _only_gauge_readings(node) -> bool:
    """Vrai si le nœud ne consomme que des lignes de `quota_gauge_readings`.

    Un `Sort` dont l'entrée est le résultat du seek récursif (une petite
    `Recursive Union` / `CTE Scan`) n'est pas un tri de la table : il trie
    les quelques têtes déjà choisies. Seul un tri dont l'entrée descend
    exclusivement à des scans de la table compte.
    """
    relation = node.get("Relation Name")
    if relation is not None:
        return relation == TABLE
    if node.get("Node Type") in ("Recursive Union", "CTE Scan", "WorkTable Scan"):
        return False
    children = node.get("Plans", [])
    return bool(children) and all(_only_gauge_readings(child) for child in children)


_RESETS_LOW = re.compile(r"\bresets_at\s*>=")
_RESETS_HIGH = re.compile(r"\bresets_at\s*<=")


def _window_bounded(node) -> bool:
    """#5952 : un parcours d'index borné des DEUX côtés sur `resets_at`.

    Le départage dans une fenêtre gigotée trie les relevés d'une seule
    fenêtre (échéances entre la tête − tolérance et la tête), jamais
    l'historique : ce tri borné est admis. Une seule borne laisse le
    parcours descendre tout l'historique : refusé comme avant.

    #5985 : à volume égal, le planificateur choisit tantôt un `Index Scan`,
    tantôt un `Bitmap Heap Scan` pour cette fenêtre (coûts à la limite,
    échantillon d'ANALYZE variable) : les deux lisent la même fenêtre. Le
    bitmap est admis si chacun de ses `Bitmap Index Scan` est borné des deux
    côtés.
    """
    if node.get("Relation Name") != TABLE:
        return False
    kind = node.get("Node Type")
    if kind == "Index Scan":
        return _bounded_cond(node)
    if kind == "Bitmap Heap Scan":
        leaves = [leaf for leaf in _nodes(node.get("Plans", []))
                  if leaf.get("Node Type") == "Bitmap Index Scan"]
        return bool(leaves) and all(_bounded_cond(leaf) for leaf in leaves)
    return False


def _bounded_cond(node) -> bool:
    cond = node.get("Index Cond") or ""
    return bool(_RESETS_LOW.search(cond) and _RESETS_HIGH.search(cond))


def _table_plan_problems(roots) -> list[str]:
    """Les balayages complets et tris de la table que #5555 interdit."""
    problems = []
    for node in _nodes(roots):
        kind = node.get("Node Type")
        if kind in ("Seq Scan", "Parallel Seq Scan") \
                and node.get("Relation Name") == TABLE:
            problems.append("Seq Scan sur " + TABLE)
        if kind in ("Sort", "Incremental Sort") and any(
                _only_gauge_readings(child) and not _window_bounded(child)
                for child in node.get("Plans", [])):
            problems.append("Sort des lignes de " + TABLE)
    return problems


class GaugeReadPlansDbTest(PgTestCase):
    """#5555 : le plan des lectures de jauges reste un seek par index."""

    #: 4 harness × 5 comptes (NULL et vide compris) × 4 jauges × 660 relevés :
    #: le volume du central (#5466 y coûtait 19 s par appel).
    ROWS = 52_800
    SINCE_S = 86_400

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.db.execute("TRUNCATE quota_gauge_readings RESTART IDENTITY")
        # Répartition réaliste : Codex ne date pas ses relevés (produced_at
        # NULL, #5466), certains comptes sont NULL ou vides, certaines sources
        # aussi, et l'historique couvre ~3,2 jours — le filtre d'âge de
        # `latest_gauges` laisse donc une majorité de lectures périmées.
        cls.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s,
                 observed_at, produced_at, resets_at, source,
                 overage_capable, overage_in_use)
            SELECT h, a, g, 0.5, 3600,
                   now() - make_interval(secs => (g_n * 7 + j) * 60),
                   CASE WHEN h = 'codex' THEN NULL
                        ELSE now() - make_interval(secs => (g_n * 7 + j) * 60) END,
                   now() + make_interval(secs => 3600 - (g_n * 7 + j) * 60),
                   CASE WHEN j % 5 = 0 THEN NULL
                        ELSE 'ameesh-agents-' || (j % 4) END,
                   true, false
              FROM (SELECT h, a, g,
                           array_position(
                               ARRAY['five_hour','seven_day','10080min','300min'],
                               g) AS g_n
                      FROM unnest(ARRAY['codex','claude','deepseek','gemini']) h
                     CROSS JOIN unnest(ARRAY['primaire','compte-a','compte-b',
                                             NULL,'']) a
                     CROSS JOIN unnest(ARRAY['five_hour','seven_day',
                                             '10080min','300min']) g) base
             CROSS JOIN generate_series(1, 660) j
        """)
        cls.db.execute("ANALYZE quota_gauge_readings")
        cls.rows = int(cls.db.query(
            "SELECT count(*) AS n FROM quota_gauge_readings")[0]["n"])

    def setUp(self) -> None:
        super().setUp()
        #: connexion psycopg de secours quand le pilote actif est psql.
        self._plan_db = None

    def _reads(self):
        """(lecture, index attendu) des trois entrées de #5555."""
        ops = storage.of(self.db).operations
        return {
            "gauge_latest": (
                lambda: ops.gauge_latest(harness="codex", account="primaire"),
                "quota_gauge_readings_seek_idx"),
            "gauge_latest_window": (
                lambda: ops.gauge_latest_window(harness="codex", account="primaire"),
                "quota_gauge_readings_window_seek_idx"),
            "latest_gauges": (
                lambda: ops.latest_gauges(since_s=self.SINCE_S),
                "quota_gauge_readings_seek_idx"),
            "latest_gauges_by_source": (
                lambda: ops.latest_gauges(since_s=self.SINCE_S, by_source=True),
                "quota_gauge_readings_source_seek_idx"),
        }

    def _captured_sql(self, read):
        """La requête exacte d'une lecture, sans la rejouer à la main."""
        captured: dict = {}
        original = self.db.query

        def capture(sql, params=()):
            captured["sql"], captured["params"] = sql, tuple(params)
            return original(sql, params)

        self.db.query = capture
        try:
            rows = read()
        finally:
            self.db.query = original
        self.assertTrue(rows, "la lecture doit rendre des lignes")
        self.assertIn(TABLE, captured["sql"])
        return captured["sql"], captured["params"]

    def _plan(self, sql, params=()):
        """Plan de la requête exacte, sous psycopg.

        Le pilote psql enveloppe chaque lecture dans un agrégat JSON où
        `EXPLAIN` ne peut pas entrer ; quand psycopg est disponible (poste,
        part psycopg de la CI), le plan se lit sur une seconde connexion au
        même schéma. La part psql de la CI n'installe pas psycopg : le témoin
        y est sauté, jamais faux (#5555).
        """
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            if self._plan_db is None:
                try:
                    self._plan_db = db_mod.PsycopgDriver(self.cfg)
                except db_mod.Unavailable:
                    self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                                  "et psycopg est absent (#5555)")
                self.addCleanup(self._plan_db.close)
            driver = self._plan_db
        with driver.transaction() as tx:
            rows = tx.query("EXPLAIN (FORMAT JSON) " + sql, params)
        # le pilote peut rendre le JSON déjà décodé : on normalise.
        return [item["Plan"] for item in json.loads(json.dumps(rows))[0]["QUERY PLAN"]]

    def test_no_full_scan_and_no_sort_of_the_gauge_table(self):
        self.assertEqual(self.rows, self.ROWS)
        self.assertGreaterEqual(
            self.rows, 50_000,
            "le témoin de plan doit tourner au volume réaliste du central (#5555)")
        for name, (read, expected_index) in self._reads().items():
            with self.subTest(read=name):
                sql, params = self._captured_sql(read)
                roots = self._plan(sql, params)
                problems = _table_plan_problems(roots)
                self.assertEqual(problems, [], "%s : %s\n%s" % (
                    name, " ; ".join(problems), json.dumps(roots, indent=1)))
                # le seek doit rester un parcours ordonné de l'index : un
                # Bitmap Index Scan du même nom lirait, lui, tout l'historique.
                scans = [(node.get("Node Type"), node.get("Index Name"))
                         for node in _nodes(roots)]
                self.assertIn(("Index Scan", expected_index), scans,
                              "%s doit chercher par %s\n%s" % (
                                  name, expected_index, json.dumps(roots, indent=1)))


class GaugePlanCheckTest(unittest.TestCase):
    """Le contrôle de plan voit la forme qui a coûté 19 s par appel.

    Sans ce témoin, une erreur dans `_table_plan_problems` rendrait le test
    de plan muet au lieu de rouge.
    """

    def test_a_sort_over_the_table_is_refused(self):
        roots = [{
            "Node Type": "Unique",
            "Plans": [{
                "Node Type": "Sort",
                "Plans": [{"Node Type": "Bitmap Heap Scan",
                           "Relation Name": TABLE}],
            }],
        }]
        self.assertEqual(_table_plan_problems(roots),
                         ["Sort des lignes de " + TABLE])

    def test_a_seq_scan_is_refused(self):
        self.assertEqual(
            _table_plan_problems([{"Node Type": "Seq Scan",
                                   "Relation Name": TABLE}]),
            ["Seq Scan sur " + TABLE])

    def test_a_recursive_seek_and_its_final_sort_are_allowed(self):
        roots = [{
            "Node Type": "Sort",
            "Plans": [
                {"Node Type": "Recursive Union",
                 "Plans": [{"Node Type": "Index Scan",
                            "Index Name": "quota_gauge_readings_seek_idx",
                            "Relation Name": TABLE}]},
                {"Node Type": "CTE Scan", "CTE Name": "heads"},
            ],
        }]
        self.assertEqual(_table_plan_problems(roots), [])

    def test_a_sort_of_one_bounded_window_is_allowed(self):
        """#5952 : le départage dans la fenêtre trie une fenêtre, pas l'historique."""
        def sort_over(cond):
            return [{"Node Type": "Limit", "Plans": [{
                "Node Type": "Sort",
                "Plans": [{"Node Type": "Index Scan",
                           "Index Name": "quota_gauge_readings_window_seek_idx",
                           "Relation Name": TABLE, "Index Cond": cond}],
            }]}]
        both = ("((harness = h.harness) AND (gauge_key = h.gauge_key) AND "
                "(resets_at >= (h.resets_at - '00:05:00'::interval)) AND "
                "(resets_at <= h.resets_at))")
        self.assertEqual(_table_plan_problems(sort_over(both)), [])
        low_only = ("((harness = h.harness) AND "
                    "(resets_at >= (h.resets_at - '00:05:00'::interval)))")
        self.assertEqual(_table_plan_problems(sort_over(low_only)),
                         ["Sort des lignes de " + TABLE])
        self.assertEqual(_table_plan_problems(sort_over("(harness = 'codex'::text)")),
                         ["Sort des lignes de " + TABLE])

    def test_a_bitmap_over_one_bounded_window_is_allowed(self):
        """#5985 : la même fenêtre lue par bitmap (choix de la CI h-31)."""
        def sort_over(cond):
            return [{"Node Type": "Limit", "Plans": [{
                "Node Type": "Sort",
                "Plans": [{"Node Type": "Bitmap Heap Scan",
                           "Relation Name": TABLE, "Recheck Cond": cond,
                           "Plans": [{"Node Type": "Bitmap Index Scan",
                                      "Index Name":
                                          "quota_gauge_readings_window_seek_idx",
                                      "Index Cond": cond}]}],
            }]}]
        both = ("((harness = h.harness) AND (gauge_key = h.gauge_key) AND "
                "(resets_at >= (h.resets_at - '00:05:00'::interval)) AND "
                "(resets_at <= h.resets_at))")
        self.assertEqual(_table_plan_problems(sort_over(both)), [])
        low_only = ("((harness = h.harness) AND "
                    "(resets_at >= (h.resets_at - '00:05:00'::interval)))")
        self.assertEqual(_table_plan_problems(sort_over(low_only)),
                         ["Sort des lignes de " + TABLE])
        # un bitmap sans feuille d'index (forme inconnue) reste refusé
        bare = [{"Node Type": "Sort", "Plans": [{
            "Node Type": "Bitmap Heap Scan", "Relation Name": TABLE,
            "Recheck Cond": both}]}]
        self.assertEqual(_table_plan_problems(bare),
                         ["Sort des lignes de " + TABLE])


# -- garde statique ---------------------------------------------------------

def _string_units(node):
    """Les expressions de chaîne d'un nœud, recollées si elles sont concaténées.

    Rend des couples (texte, ligne). `a + "b" + "c"` est recollé en une seule
    unité : un SQL coupé sur plusieurs littéraux ne peut pas échapper à la
    garde. Une f-string garde ses parties littérales (le nom de table
    interpolé est retrouvé par les identifiants du même bloc).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [(node.value, node.lineno)]
    if isinstance(node, ast.JoinedStr):
        text = "".join(value.value if isinstance(value, ast.Constant) else "\x00"
                       for value in node.values)
        return [(text, node.lineno)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_units(node.left), _string_units(node.right)
        if len(left) == 1 and len(right) == 1:
            return [(left[0][0] + right[0][0], left[0][1])]
        return left + right
    units = []
    for child in ast.iter_child_nodes(node):
        units += _string_units(child)
    return units


def _scope_violations(scope, path: str) -> list[str]:
    """Les `DISTINCT ON` de ce bloc (module, classe, fonction) sur la table."""
    name = getattr(scope, "name", None)
    units = []
    mentions_table = False
    nested = []
    for index, child in enumerate(getattr(scope, "body", [])):
        if index == 0 and isinstance(child, ast.Expr) \
                and isinstance(child.value, ast.Constant) \
                and isinstance(child.value.value, str):
            continue  # docstring : elle cite #3922 sans être une requête
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested.append(child)
            continue
        units += _string_units(child)
        for part in ast.walk(child):
            if isinstance(part, ast.Name) and part.id == TABLE:
                mentions_table = True
            elif isinstance(part, ast.Attribute) and part.attr == TABLE:
                mentions_table = True
            elif isinstance(part, ast.Constant) and part.value == TABLE:
                mentions_table = True
    found = []
    if name != HEAD_SEEK and (mentions_table
                              or any(TABLE in text for text, _ in units)):
        for text, line in units:
            if DISTINCT_ON.search(text):
                found.append("%s:%d : DISTINCT ON sur %s hors de %s" % (
                    path, line, TABLE, HEAD_SEEK))
    for child in nested:
        found += _scope_violations(child, path)
    return found


def gauge_distinct_on_violations(source: str, path: str = "<source>") -> list[str]:
    """Les lectures `DISTINCT ON` de la table qui ne passent pas par le seek."""
    return _scope_violations(ast.parse(source, filename=path), path)


class GaugeDistinctOnGuardTest(unittest.TestCase):
    """#5555 : `DISTINCT ON` sur `quota_gauge_readings` hors de `_gauge_heads`.

    `_gauge_heads` est la seule lecture autorisée à porter l'historique : son
    seek récursif saute chaque groupe au lieu de le trier. Toute autre
    lecture `DISTINCT ON` de la table est refusée, quel que soit son module.
    """

    def test_no_distinct_on_over_the_gauge_table_outside_the_seek(self):
        violations = []
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    violations += gauge_distinct_on_violations(
                        fh.read(), os.path.relpath(path, os.path.dirname(SRC)))
        self.assertEqual(violations, [], "\n".join([
            "DISTINCT ON sur %s hors de %s (#5555) : cette lecture trie tout "
            "l'historique (19 s/appel, central à 98 %% CPU). Passer par le "
            "seek de %s (index 0493/0535)." % (TABLE, HEAD_SEEK, HEAD_SEEK),
        ] + violations))

    def test_the_guard_sees_the_regression_shape(self):
        """La requête de #5466, telle qu'elle était, est bien refusée."""
        source = '''
def gauge_latest_window(self, *, harness, account):
    return self.db.query(
        "SELECT DISTINCT ON (gauge_key) harness, gauge_key AS key, used"
        "  FROM quota_gauge_readings"
        " WHERE harness = %s AND coalesce(account, '') = %s"
        " ORDER BY gauge_key, resets_at DESC NULLS LAST", (harness, account))
'''
        violations = gauge_distinct_on_violations(source, "<regression>")
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("DISTINCT ON sur " + TABLE, violations[0])

    def test_the_guard_sees_a_query_concatene(self):
        """Un SQL coupé par `+` puis par un `+=` ne passe pas non plus."""
        source = '''
SQL = "SELECT DISTINCT ON (gauge_key) key"
SQL += " FROM quota_gauge_readings"
'''
        self.assertEqual(len(gauge_distinct_on_violations(source, "<concat>")), 1)

    def test_the_seek_may_carry_the_history(self):
        """`_gauge_heads` reste le point d'entrée autorisé à lire la table."""
        source = '''
def _gauge_heads(self, *, harness):
    return self.db.query(
        "SELECT DISTINCT ON (gauge_key) key FROM quota_gauge_readings"
        " WHERE harness = %s", (harness,))
'''
        self.assertEqual(gauge_distinct_on_violations(source, "<seek>"), [])


if __name__ == "__main__":
    unittest.main()
