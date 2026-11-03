# SPDX-License-Identifier: AGPL-3.0-only
"""#5582 : garde de plan du rattrapage de relecture.

`review_lot.catch_up` → `WorkItems.items_missing_ref` est appelé à la fin de
chaque relevé `plan_git`. Sur le central, le plan partait du balayage des
jalons `merged` depuis la borne d'activation (2 729 lignes), évaluait
`agent_mesh_merged_annulled` deux fois par jalon, puis parcourait le journal
une fois par lot pour la marque terminale et le compteur de tentatives :
3,7 s par appel, 234 s sur une fenêtre de 102 s (diagnostic #5579).

Deux gardes, complémentaires :

* un plan RÉEL de la requête exacte de `items_missing_ref`, à volume réaliste
  (5 100 lots, 53 810 événements, 10 200 jalons ; dont 2 700 fusions depuis la
  borne) : aucun `Seq Scan` de `work_item_events`, et le filtre
  `agent_mesh_merged_annulled` n'est jamais appliqué à un nœud qui lit des
  centaines de jalons — seulement aux candidats qui passent les barrières
  bon marché. L'exécution reste sous 100 ms ;
* une garde statique qui refuse de replier les barrières coûteuses dans la
  sélection matérialisée (`_missing_ref_pre_gates`) et exige la clôture
  `MATERIALIZED` dans `items_missing_ref` : la forme qui a coûté 3,7 s ne peut
  pas revenir sans faire rougir les tests ;
* un témoin de sélection : un jalon `merged` annulé par une correction
  `merged → build` ne rend pas un lot dû, une refusion postérieure le rend.
"""
from __future__ import annotations

import ast
import json
import os
import unittest

from ameesh import db as db_mod, review_lot, storage, work

from .support import PgTestCase, SRC

EVENTS = "work_item_events"
MILESTONES = "work_item_milestones"
ANNULLED = "agent_mesh_merged_annulled"
BOUND_DAYS = 17
#: au-delà, le nœud qui porte l'annulation lit un balayage, pas une poignée de
#: candidats (le central en compte 2 700 depuis la borne).
BROAD_ROWS = 500
#: borne d'exécution du constat #5579 (3,7 s avant correctif).
MAX_MS = 100.0


# -- plan -------------------------------------------------------------------

def _nodes(roots):
    """Tous les nœuds d'un plan `EXPLAIN (FORMAT JSON)`, profondeur d'abord."""
    for node in roots:
        yield node
        yield from _nodes(node.get("Plans", []))


def _seq_scans(roots, relation: str) -> list[str]:
    """Les balayages complets de `relation` que le plan contient."""
    return [
        "Seq Scan sur " + relation
        for node in _nodes(roots)
        if node.get("Node Type") in ("Seq Scan", "Parallel Seq Scan")
        and node.get("Relation Name") == relation
    ]


def _annulled_rows(node) -> int:
    """Le nombre de lignes sur lesquelles un nœud évalue l'annulation.

    Tous les champs textuels du nœud sont relus (`Filter`, `Index Cond`,
    `Recheck Cond`, `Join Filter`, `One-Time Filter`…) : selon le plan, le
    prédicat peut atterrir sur n'importe lequel.
    """
    text = " ".join(value for value in node.values() if isinstance(value, str))
    if ANNULLED not in text:
        return 0
    return int(node.get("Actual Rows") or 0) * int(node.get("Actual Loops") or 1)


def _broad_annulled(roots) -> list[str]:
    """Les nœuds qui évaluent l'annulation sur un balayage de jalons."""
    return [
        "%s (%d lignes)" % (node.get("Node Type"), _annulled_rows(node))
        for node in _nodes(roots)
        if _annulled_rows(node) >= BROAD_ROWS
    ]


class ReviewCatchUpPlanDbTest(PgTestCase):
    """#5582 : le rattrapage garde un plan par index, borné aux candidats."""

    #: le volume du central (#5579) : 5 453 lots, 119 813 événements,
    #: 31 112 jalons ; la garde en garde la forme et les 2 700 fusions.
    LOTS = 5_100
    RECENT = 2_700
    MISSING = tuple(range(2_691, 2_701))

    def setUp(self) -> None:
        super().setUp()
        self._plan_db = None

    def _fixture(self):
        """Le volume du central, et la borne d'activation du rattrapage.

        Construit dans le test : `PgTestCase.setUp` remet la base à zéro
        (les tables du lot sont truncatées) avant chaque témoin.
        """
        db = self.db
        db.execute("""
            INSERT INTO work_items (id, type, source, app, title, body, issue_ref,
                                    state, assignee, created_at, updated_at)
            SELECT g, 'evolution', 'diag', 'ameesh', 'lot ' || g, '', NULL,
                   'merged', 'agent-x',
                   now() - interval '60 days', now() - make_interval(mins => g)
              FROM generate_series(1, %s) g
        """, (self.LOTS,))
        db.execute("SELECT setval('work_items_id_seq', %s)", (self.LOTS,))
        # 2 700 fusions depuis la borne, les autres avant : le balayage que le
        # plan d'origine évaluait en entier.
        db.execute("""
            INSERT INTO work_item_milestones (work_item_id, kind, at, note)
            SELECT g, 'merged', now() - make_interval(mins => g), 'transition'
              FROM generate_series(1, %s) g
        """, (self.RECENT,))
        db.execute("""
            INSERT INTO work_item_milestones (work_item_id, kind, at, note)
            SELECT g, 'merged',
                   now() - make_interval(days => 18 + mod(g, 30)), 'transition'
              FROM generate_series(%s::int, %s::int) g
        """, (self.RECENT + 1, self.LOTS))
        # 10 événements ordinaires par lot : le journal reste volumineux, un
        # `Seq Scan` de `work_item_events` se voit dans le plan.
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            SELECT g, 'merged', 'ordinary note ' || j, 'agent-x',
                   now() - make_interval(mins => g * 3 + j)
              FROM generate_series(1, %s) g
             CROSS JOIN generate_series(1, 10) j
        """, (self.LOTS,))
        # 2 600 marques terminales : les candidats déjà relus ne doivent pas
        # être recalculés (annulation, tentatives) à chaque passage.
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            SELECT g, 'merged',
                   'review-after-merge:closed:#' || g || ' reviewed', 'system',
                   now() - make_interval(mins => g)
              FROM generate_series(1, 2600) g
        """)
        # 60 lots à trois tentatives et 30 lots à tentative récente : le reste
        # des 10 candidats (2691..2700) est le rattrapage dû.
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            SELECT g, 'merged',
                   'review-after-merge:attempt:#' || g || ' try' || j, 'system',
                   now() - make_interval(hours => j * 2)
              FROM generate_series(2601, 2660) g
             CROSS JOIN generate_series(1, 3) j
        """)
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            SELECT g, 'merged', 'review-after-merge:attempt:#' || g || ' try',
                   'system', now() - interval '5 minutes'
              FROM generate_series(2661, 2690) g
        """)
        for table in ("work_items", EVENTS, MILESTONES):
            db.execute("ANALYZE " + table)
        self.bound = db.query(
            "SELECT now() - make_interval(days => %s) AS at",
            (BOUND_DAYS,))[0]["at"]

    def _selection(self):
        """La requête exacte de `items_missing_ref`, et ses lignes.

        La garde suit les paramètres du rattrapage (`review_lot`) : borne
        d'activation, marque terminale, compteur et délai de tentative.
        """
        captured: dict = {}
        original = self.db.query

        def capture(sql, params=()):
            captured["sql"], captured["params"] = sql, tuple(params)
            return original(sql, params)

        self.db.query = capture
        try:
            rows = storage.of(self.db).work.items_missing_ref(
                states=work.MERGED_STATES,
                ref_prefix=review_lot.PREFIX + "#",
                limit=review_lot.CATCH_UP_LIMIT,
                attempt_prefix=review_lot.ATTEMPT_PREFIX,
                retry_s=review_lot.ATTEMPT_RETRY_S,
                terminal_prefix=review_lot.TERMINAL_PREFIX,
                max_attempts=review_lot.CATCH_UP_MAX_ATTEMPTS,
                since=self.bound)
        finally:
            self.db.query = original
        self.assertIn("MATERIALIZED", captured["sql"], captured["sql"])
        return captured["sql"], captured["params"], rows

    def _plan(self, sql, params):
        """Plan réel, sous psycopg (le pilote psql enveloppe `EXPLAIN`).

        La part psql de la CI n'installe pas psycopg : le témoin y est sauté,
        jamais faux. Le premier passage chauffe les index, le second est
        mesuré (le constat porte sur la requête chaude, appelée en continu).
        """
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            if self._plan_db is None:
                try:
                    self._plan_db = db_mod.PsycopgDriver(self.cfg)
                except db_mod.Unavailable:
                    self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                                  "et psycopg est absent (#5582)")
                self.addCleanup(self._plan_db.close)
            driver = self._plan_db
        with driver.transaction() as tx:
            rows = tx.query("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                            params)
        document = json.loads(json.dumps(rows))[0]["QUERY PLAN"]
        return ([item["Plan"] for item in document], document[0])

    def test_le_plan_ne_balaye_pas_le_journal_et_borne_l_annulation(self):
        self._fixture()
        sql, params, rows = self._selection()
        self.assertEqual(
            [int(row["id"]) for row in rows], list(self.MISSING),
            "le plan doit rendre les seuls manquants dûs")
        self._plan(sql, params)                      # chauffe les index
        roots, measured = self._plan(sql, params)

        self.assertEqual(_seq_scans(roots, EVENTS), [],
                         "aucun Seq Scan de work_item_events (#5579)")
        self.assertEqual(
            _broad_annulled(roots), [],
            "l'annulation ne s'évalue que sur les candidats, pas sur le "
            "balayage des %d jalons (#5582)\n%s"
            % (self.RECENT, json.dumps(roots, indent=1)))
        self.assertLess(
            float(measured["Execution Time"]), MAX_MS,
            "le rattrapage doit rester sous %g ms (était 3,7 s, #5579)"
            % MAX_MS)

    def test_les_index_du_journal_sont_presents(self):
        """La migration additive 0538 pose les deux index de la lecture."""
        found = {row["indexname"] for row in self.db.query(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = current_schema()"
            "   AND tablename = 'work_item_events'")}
        self.assertIn("work_item_events_item_note_first_idx", found)
        self.assertIn("work_item_events_item_state_created_idx", found)


class ReviewCatchUpSelectionTest(PgTestCase):
    """#5582 : l'annulation ne s'évalue que sur un jalon effectif.

    La sélection matérialisée ne regarde d'abord que l'**existence** d'un
    jalon `merged` depuis la borne ; l'annulation reste dans la clôture
    extérieure. Un jalon récent annulé par une correction `merged → build`
    n'est donc pas un candidat, et une refusion postérieure l'est.
    """

    def _lot(self, lot: int, updated_min: int) -> None:
        self.db.execute(
            "INSERT INTO work_items (id, title, state, updated_at)"
            " VALUES (%s, %s, 'merged', now() - make_interval(mins => %s))",
            (lot, "lot %d" % lot, updated_min))

    def _milestone(self, lot: int, minutes: int) -> None:
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, note)"
            " VALUES (%s, 'merged', now() - make_interval(mins => %s),"
            "         'transition')", (lot, minutes))

    def _event(self, lot: int, state: str, minutes: int) -> None:
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor,"
            "                              created_at)"
            " VALUES (%s, %s, '', 'agent-x', now() - make_interval(mins => %s))",
            (lot, state, minutes))

    def test_un_jalon_annule_n_est_pas_du_et_une_refusion_l_est(self):
        bound = self.db.query(
            "SELECT now() - make_interval(days => %s) AS at",
            (BOUND_DAYS,))[0]["at"]
        # 1 : fusion annulée par une correction, puis refusionnée — le jalon
        #     effectif est le second ; le lot reste dû.
        self._lot(1, updated_min=10)
        self._milestone(1, minutes=120)
        self._event(1, "merged", 180)
        self._event(1, "build", 60)          # correction merged -> build
        self._milestone(1, minutes=30)       # refusion postérieure
        # 2 : fusion annulée, jamais refusionnée — aucun jalon effectif.
        self._lot(2, updated_min=20)
        self._milestone(2, minutes=120)
        self._event(2, "merged", 180)
        self._event(2, "build", 60)
        # 3 : fusion récente sans correction — le témoin dû.
        self._lot(3, updated_min=30)
        self._milestone(3, minutes=30)

        rows = storage.of(self.db).work.items_missing_ref(
            states=work.MERGED_STATES, ref_prefix=review_lot.PREFIX + "#",
            limit=10, since=bound)
        self.assertEqual([int(row["id"]) for row in rows], [1, 3])


# -- garde statique ---------------------------------------------------------

def _function_source(source: str, name: str) -> str:
    """Le texte de la fonction `name`, ou '' si elle a disparu."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


class ReviewCatchUpPlanGuardTest(unittest.TestCase):
    """#5582 : les barrières coûteuses restent après la matérialisation.

    Sans cette garde, un refactor peut replier `agent_mesh_merged_annulled`
    dans les barrières bon marché ou retirer la clôture `MATERIALIZED` : le
    plan de 3,7 s revient, et seul le témoin de plan (qui saute sous psql et
    demande Postgres) le verrait.
    """

    def test_les_barrieres_couteuses_restent_apres_la_materialisation(self):
        path = os.path.join(
            SRC, "ameesh", "storage", "postgres", "work.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        selection = _function_source(source, "items_missing_ref")
        self.assertIn("MATERIALIZED", selection,
                      "items_missing_ref doit matérialiser ses barrières (#5582)")
        pre = _function_source(source, "_missing_ref_pre_gates")
        self.assertTrue(pre)
        self.assertNotIn(ANNULLED, pre,
                         "l'annulation doit rester hors des barrières bon marché")
        self.assertNotIn("_effective_merged", pre)
        late = _function_source(source, "_missing_ref_late_gates")
        self.assertTrue(late)
        self.assertIn("_effective_merged", late)


if __name__ == "__main__":
    unittest.main()
