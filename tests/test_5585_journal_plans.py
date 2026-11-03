# SPDX-License-Identifier: AGPL-3.0-only
"""#5585 : garde de liaison et de plan des lectures groupées du journal.

Le diagnostic #5579 a mesuré, sur le central, 18 Ko de SQL et 4 483–4 500
paramètres pour `lot_transitions` / `lot_events` : la planification coûtait
94–330 ms et le planificateur abandonnait l'index du lot pour un balayage de
`work_item_events_pkey`. Chaque id lié séparément (`IN (%s, %s, …)`) est
remplacé par UN tableau (`= ANY(%s::bigint[])`), et les lectures groupées
sont découpées en pages de 1 000 lots.

Deux gardes, complémentaires :

* la forme : chaque requête du journal ne porte qu'un `%s`, lié à un tableau ;
  la page ne dépasse pas 1 000 ids et un lot n'est jamais coupé entre deux
  pages (`lag` par lot) ;
* le plan : à 4 500 ids et au volume du central (~100 000 événements), le
  plan cherche par un index du lot (`work_item_id` en condition d'index),
  jamais par `pkey` ni par balayage complet, et les lignes réellement
  évaluées restent bornées par la page. La garde est structurelle : la
  planification n'est jamais comparée à 5 ms, seuil dépendant de la charge
  (#5582, #5606) ; un plafond large de 1 s ne sert que de filet.
"""
from __future__ import annotations

import json
import unittest

from ameesh import db as db_mod, storage
from ameesh.storage.postgres.progress import GROUPED_IDS_MAX

from .support import PgTestCase

TABLE = "work_item_events"
ITEM_INDEX = "work_item_events_item_idx"
PKEY = "work_item_events_pkey"
PAGE = GROUPED_IDS_MAX
#: filet large, jamais la garde : une planification de centaines de ms est
#: déjà un effondrement, la variance de charge CI ne doit pas faire rougir.
BROAD_MS = 1_000.0


def _nodes(roots):
    """Tous les nœuds d'un plan `EXPLAIN (FORMAT JSON)`, profondeur d'abord."""
    for node in roots:
        yield node
        yield from _nodes(node.get("Plans", []))


def _journal_node(node) -> bool:
    """Le nœud lit-il le journal ? (Bitmap Index Scan n'a pas de relation.)"""
    relation = node.get("Relation Name")
    index = str(node.get("Index Name") or "")
    return relation == TABLE or index.startswith(TABLE + "_")


def _journal_plan_problems(roots, max_rows=None) -> list[str]:
    """Les accès au journal que #5585 interdit, sans mesure de temps.

    La garde est structurelle (#5582, #5606) : balayage complet, accès par
    `pkey`, index qui ne filtre pas par `work_item_id`, et — quand `max_rows`
    est donné — lignes ou boucles réellement évaluées au-delà de la page.
    """
    problems = []
    for node in _nodes(roots):
        kind = node.get("Node Type")
        index = node.get("Index Name")
        if not _journal_node(node):
            continue
        if kind in ("Seq Scan", "Parallel Seq Scan"):
            problems.append("Seq Scan sur " + TABLE)
        if index == PKEY:
            problems.append("%s sur %s" % (kind, PKEY))
        elif index:
            cond = str(node.get("Index Cond") or "")
            if "work_item_id" not in cond:
                problems.append("%s par %s sans work_item_id" % (kind, index))
        if max_rows is not None:
            rows = node.get("Actual Rows")
            loops = node.get("Actual Loops")
            if rows is not None and loops is not None:
                if rows * loops > max_rows:
                    problems.append(
                        "%s sur %s : %d lignes évaluées > %d" % (
                            kind, index or TABLE, rows * loops, max_rows))
                if loops > 1:
                    problems.append(
                        "%s sur %s : %d boucles" % (kind, index or TABLE, loops))
    return problems


def _journal_item_scans(roots) -> list:
    """Les nœuds qui filtrent le journal par `work_item_id` (index du lot)."""
    return [node for node in _nodes(roots)
            if _journal_node(node)
            and node.get("Index Name")
            and "work_item_id" in str(node.get("Index Cond") or "")]


def _journal_indexes(roots) -> list[str]:
    return [node.get("Index Name") for node in _nodes(roots)
            if node.get("Index Name")]


class JournalPlanCheckTest(unittest.TestCase):
    """Le contrôle de plan voit bien la forme qui coûtait 330 ms."""

    def test_a_seq_scan_is_refused(self):
        self.assertEqual(
            _journal_plan_problems([{"Node Type": "Seq Scan",
                                     "Relation Name": TABLE}]),
            ["Seq Scan sur " + TABLE])

    def test_a_pkey_scan_is_refused(self):
        self.assertEqual(
            _journal_plan_problems([{"Node Type": "Index Scan",
                                     "Relation Name": TABLE,
                                     "Index Name": PKEY}]),
            ["Index Scan sur " + PKEY])

    def test_the_item_index_is_accepted(self):
        roots = [{"Node Type": "Bitmap Heap Scan", "Relation Name": TABLE,
                  "Plans": [{"Node Type": "Bitmap Index Scan",
                             "Relation Name": TABLE,
                             "Index Name": ITEM_INDEX,
                             "Index Cond": "(work_item_id = ANY ('{1}'::bigint[]))"}]}]
        self.assertEqual(_journal_plan_problems(roots), [])
        self.assertIn(ITEM_INDEX, _journal_indexes(roots))
        self.assertEqual(len(_journal_item_scans(roots)), 1)

    def test_another_item_index_is_accepted(self):
        """0538 pose un second index du lot sur le central : il vaut mieux."""
        roots = [{"Node Type": "Bitmap Heap Scan", "Relation Name": TABLE,
                  "Plans": [{"Node Type": "Bitmap Index Scan",
                             "Relation Name": TABLE,
                             "Index Name": "work_item_events_item_note_first_idx",
                             "Index Cond": "(work_item_id = ANY ('{1}'::bigint[]))"}]}]
        self.assertEqual(_journal_plan_problems(roots), [])
        self.assertEqual(len(_journal_item_scans(roots)), 1)

    def test_a_broad_index_is_refused(self):
        roots = [{"Node Type": "Index Scan", "Relation Name": TABLE,
                  "Index Name": "work_item_events_actor_idx",
                  "Index Cond": "(actor = 'agent')"}]
        self.assertEqual(
            _journal_plan_problems(roots),
            ["Index Scan par work_item_events_actor_idx sans work_item_id"])

    def test_too_many_rows_evaluated_are_refused(self):
        roots = [{"Node Type": "Index Scan", "Relation Name": TABLE,
                  "Index Name": ITEM_INDEX,
                  "Index Cond": "(work_item_id = ANY ('{1}'::bigint[]))",
                  "Actual Rows": 60_500, "Actual Loops": 1}]
        self.assertEqual(
            _journal_plan_problems(roots, max_rows=11_000),
            ["Index Scan sur work_item_events_item_idx :"
             " 60500 lignes évaluées > 11000"])

    def test_a_text_element_cannot_close_the_sql_literal(self):
        """B/P1 de la revue : l'apostrophe d'un élément est doublée."""
        evil = "x'}'; SELECT 1; --"
        self.assertEqual(
            db_mod.bind("SELECT %s::text[]", [[evil]]),
            "SELECT '{\"x''}''; SELECT 1; --\"}'::text[]")
        self.assertEqual(db_mod.bind("SELECT %s::bigint[]", [[1, 2, 3]]),
                         "SELECT '{1,2,3}'::bigint[]")

    def test_a_nul_array_element_is_refused(self):
        with self.assertRaises(db_mod.DbError):
            db_mod.bind("SELECT %s::text[]", [["a\x00b"]])


class JournalPlanDbTest(PgTestCase):
    """#5585 : les lectures du journal lient un tableau et restent indexées."""

    #: même volume que le central au moment du diagnostic : 5 500 lots,
    #: 60 500 événements ; les 4 500 premiers ids couvrent 82 % des lots,
    #: comme la liste observée (4 483 sur 5 460).
    LOTS = 5_500
    IDS = 4_500
    EVENTS_PER_LOT = 11

    def setUp(self) -> None:
        super().setUp()
        self._seed()
        self._plan_db = None

    def _seed(self) -> None:
        self.db.execute("""
            INSERT INTO work_items (title, state, created_at, updated_at)
            SELECT 'plan-5585-' || n, 'merged',
                   now() - interval '2 days', now()
              FROM generate_series(1, %d) n
        """ % self.LOTS)
        self.db.execute("""
            INSERT INTO work_item_events
                   (work_item_id, state, note, actor, created_at)
            SELECT w.id,
                   CASE WHEN j %% 4 = 0 THEN 'qa' ELSE 'build' END,
                   'note ' || w.id || '/' || j, 'agent',
                   now() - make_interval(mins => j)
              FROM (SELECT id FROM work_items ORDER BY id) w
             CROSS JOIN generate_series(1, %d) j
        """ % self.EVENTS_PER_LOT)
        self.db.execute("ANALYZE work_item_events")
        self.ids = [int(row["id"]) for row in self.db.query(
            "SELECT id FROM work_items ORDER BY id LIMIT %s", (self.IDS,))]
        self.rows = int(self.db.query(
            "SELECT count(*) AS n FROM work_item_events")[0]["n"])
        self.max_per_lot = int(self.db.query(
            "SELECT max(n) AS n FROM (SELECT count(*) AS n"
            "  FROM work_item_events GROUP BY work_item_id) counts")[0]["n"])

    def _max_rows(self, page: int) -> int:
        """Lignes du journal qu'une page de `page` lots peut évaluer."""
        return page * (self.max_per_lot + 1)

    # -- capture et plan ---------------------------------------------------
    def _captured(self, read):
        """La requête exacte d'une lecture, sans la rejouer à la main."""
        captured: list = []
        original = self.db.query

        def capture(sql, params=()):
            captured.append((sql, tuple(params)))
            return original(sql, params)

        self.db.query = capture
        try:
            rows = read()
        finally:
            self.db.query = original
        self.assertTrue(rows, "la lecture doit rendre des lignes")
        return rows, captured

    def _plan(self, sql, params=()):
        """Plan et temps de planification de la requête exacte, sous psycopg.

        Le pilote psql enveloppe chaque lecture dans un agrégat JSON où
        `EXPLAIN` ne peut pas entrer ; quand psycopg est disponible (poste,
        part psycopg de la CI), le plan se lit sur une seconde connexion au
        même schéma. La part psql de la CI n'installe pas psycopg : le témoin
        y est sauté, jamais faux (#5555/#5585).
        """
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            if self._plan_db is None:
                try:
                    self._plan_db = db_mod.PsycopgDriver(self.cfg)
                except db_mod.Unavailable:
                    self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                                  "et psycopg est absent (#5585)")
                self.addCleanup(self._plan_db.close)
            driver = self._plan_db
        with driver.transaction() as tx:
            plans = [tx.query("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                              params) for _ in range(3)]
        # La planification est déterministe : le minimum de trois mesures
        # écarte les pics d'ordonnancement de l'hôte, jamais un plan faux.
        parsed = [json.loads(json.dumps(rows))[0]["QUERY PLAN"][0]
                  for rows in plans]
        plan = min(parsed, key=lambda item: float(item["Planning Time"]))
        return [plan["Plan"]], float(plan["Planning Time"])

    # -- témoins ------------------------------------------------------------
    def test_journal_reads_bind_one_array_per_page(self):
        """Un tableau par page, 1 000 ids au plus, un lot jamais coupé."""
        rows = self.db.query(
            "SELECT %s = ANY(%s::bigint[]) AS hit,"
            "       %s = ANY(%s::bigint[]) AS miss",
            (2, [1, 2, 3], 9, [1, 2, 3]))
        self.assertEqual(rows, [{"hit": True, "miss": False}])

        # B/P1 de la revue : un tableau de textes garde apostrophe, accolade,
        # virgule, guillemet et barre oblique inverse comme UN élément, sous
        # psql comme sous psycopg.
        for value in ("x'}'; SELECT 1; --", 'a"b', "c\\d", "e,f", "g}h"):
            with self.subTest(value=value):
                rows = self.db.query(
                    "SELECT %s::text = ANY(%s::text[]) AS hit",
                    (value, [value]))
                self.assertEqual(rows, [{"hit": True}])

        st = storage.of(self.db).progress
        for name, read in (
                ("transitions", lambda: st.lot_transitions(self.ids)),
                ("events", lambda: st.lot_events(self.ids))):
            with self.subTest(read=name):
                _, captured = self._captured(read)
                pages = []
                for sql, params in captured:
                    self.assertEqual(sql.count("%s"), 1, sql)
                    self.assertIn("ANY(%s::bigint[])", sql)
                    self.assertNotIn("IN (", sql)
                    self.assertEqual(len(params), 1)
                    self.assertIsInstance(params[0], (list, tuple))
                    pages.append(len(params[0]))
                self.assertEqual(sum(pages), len(self.ids))
                self.assertLessEqual(max(pages), PAGE)

        # Le lot à cheval sur la deuxième page garde toutes ses transitions :
        # les ids d'un lot ne sont jamais répartis entre deux requêtes.
        lot = self.ids[PAGE]
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'build', 'page-boundary', 'agent'),"
            "        (%s, 'qa', 'page-boundary', 'agent'),"
            "        (%s, 'qa', 'page-boundary', 'agent'),"
            "        (%s, 'build', 'page-boundary', 'agent')",
            (lot, lot, lot, lot))
        single = st.lot_transitions([lot])
        bulk = st.lot_transitions(self.ids)
        mine = [row for row in bulk if int(row["work_item_id"]) == lot]
        self.assertEqual(
            [(int(r["id"]), r["state"]) for r in mine],
            [(int(r["id"]), r["state"]) for r in single])
        self.assertEqual(st.lot_transitions([lot, lot]), single)

    def test_plans_use_an_item_index_and_bound_rows(self):
        self.assertGreaterEqual(self.rows, 50_000)
        st = storage.of(self.db).progress
        _, captured = self._captured(lambda: st.lot_transitions(self.ids))
        for sql, params in captured:
            page = len(params[0])
            with self.subTest(page=page):
                roots, planning_ms = self._plan(sql, params)
                self.assertEqual(
                    _journal_plan_problems(roots, max_rows=self._max_rows(page)),
                    [], json.dumps(roots, indent=1))
                self.assertTrue(_journal_item_scans(roots),
                                "index du lot attendu\n" + json.dumps(roots, indent=1))
                self.assertLess(planning_ms, BROAD_MS,
                                "planification %.2f ms sur %d ids (plafond large)"
                                % (planning_ms, page))

        # La requête de #5585 telle qu'elle existait, à 4 500 ids : l'array
        # seul (sans découpage) garde une structure bornée par les 4 500 lots.
        sql = (
            "SELECT id, work_item_id, state, actor,"
            "       extract(epoch from created_at)::float8 AS created_ts"
            "  FROM (SELECT id, work_item_id, state, actor, created_at,"
            "               lag(state) OVER (PARTITION BY work_item_id"
            "                               ORDER BY id) AS prev"
            "          FROM work_item_events"
            "         WHERE work_item_id = ANY(%s::bigint[])) marked"
            " WHERE prev IS DISTINCT FROM state"
            " ORDER BY id")
        page = len(self.ids)
        roots, planning_ms = self._plan(sql, (self.ids,))
        self.assertEqual(
            _journal_plan_problems(roots, max_rows=self._max_rows(page)), [],
            json.dumps(roots, indent=1))
        self.assertTrue(_journal_item_scans(roots),
                        "index du lot attendu\n" + json.dumps(roots, indent=1))
        self.assertLess(planning_ms, BROAD_MS,
                        "planification %.2f ms sur 4 500 ids (plafond large)"
                        % planning_ms)

    def test_the_old_in_list_is_refused_by_the_guard(self):
        """Témoin négatif : la forme `IN (%s, %s, …)` d'avant #5585 tombe.

        À 4 500 ids (82 % des lots), l'ancienne liste de marqueurs fait
        balayer le journal (Seq Scan ou pkey) : la même garde structurelle la
        refuse, sans dépendre de la charge de l'hôte.
        """
        marks = ",".join(["%s"] * len(self.ids))
        sql = (
            "SELECT id, work_item_id, state, note, actor,"
            "       extract(epoch from created_at)::float8 AS created_ts"
            "  FROM work_item_events"
            " WHERE work_item_id IN (" + marks + ")"
            " ORDER BY id")
        roots, _ = self._plan(sql, tuple(self.ids))
        problems = _journal_plan_problems(
            roots, max_rows=self._max_rows(len(self.ids)))
        self.assertNotEqual(
            problems, [],
            "la garde doit refuser l'ancienne liste IN à %d ids\n%s"
            % (len(self.ids), json.dumps(roots, indent=1)))


if __name__ == "__main__":
    unittest.main()
