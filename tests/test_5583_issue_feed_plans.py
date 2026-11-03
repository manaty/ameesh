# SPDX-License-Identifier: AGPL-3.0-only
"""#5583 : garde de plan et de forme du flux GitHub — `merged_ts` en ensemble.

`WorkItems.issue_feed(5000)` (projection GitHub, `plan_github.project_lots` et
le veilleur) calculait `merged_ts` par une sous-requête corrélée : 5 000
appels à `agent_mesh_merged_annulled` (2,57 s au central), et construisait la
liste des créneaux de migration pour chacun des 5 000 lots. Avec le tri final
des 5 000 lignes larges (5 Mo), l'appel mesurait 3,95 s hors charge et ~10 s
sous charge (diagnostic #5579, queryid 6415975572021442135).

Trois gardes, complémentaires :

* un plan RÉEL de la lecture exacte, à volume réaliste (5 000 lots, 5 000
  jalons `merged`, 10 000 événements) : l'annulation n'est évaluée que sur les
  lots dont le dernier jalon a une correction `build` postérieure, aucun tri
  externe de plus de 1 Mo des lignes larges, exécution sous 1 s (plafond
  large : la structure du plan est la preuve) ;
* une garde statique : la sous-requête corrélée `merged_ts` ne revient pas,
  et les ensembles (`last_merge`, `candidates`, `annulled`, `merged`) restent
  matérialisés dans `issue_feed` ;
* un témoin de sélection : une fusion annulée rend `None`, une refusion
  postérieure rend la nouvelle date, un lot sans fusion reste `None`, et la
  liste bornée des créneaux rend les mêmes valeurs que la lecture par ligne.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
import unittest

from ameesh import db as db_mod, storage, work
from ameesh.storage.postgres import work as work_pg

from .support import PgTestCase

FEED = "issue_feed"
ANNULLED = "agent_mesh_merged_annulled"
#: au-delà, l'annulation est évaluée sur le flux, pas sur les seuls candidats.
BROAD_ROWS = 100
#: borne du constat #5579 (3,95 s hors charge) : plafond large seulement, le
#: temps de plan dépend de la charge du runner (canon §3, #5607).
MAX_MS = 1000.0
#: le tri externe du constat : 5 152 kB de lignes larges.
MAX_SORT_KB = 1024


# -- plan -------------------------------------------------------------------

def _nodes(roots):
    """Tous les nœuds d'un plan `EXPLAIN (FORMAT JSON)`, profondeur d'abord."""
    for node in roots:
        yield node
        yield from _nodes(node.get("Plans", []))


def _text(node) -> str:
    """Tous les champs textuels d'un nœud (Filter, Index Cond, …)."""
    return " ".join(value for value in node.values() if isinstance(value, str))


def _annulled_rows(node) -> int:
    """Le nombre de lignes sur lesquelles le nœud évalue l'annulation."""
    if ANNULLED not in _text(node):
        return 0
    return int(node.get("Actual Rows") or 0) * int(node.get("Actual Loops") or 1)


def _broad_annulled(roots) -> list[str]:
    """Les nœuds qui évaluent l'annulation sur le flux, pas sur les candidats."""
    return [
        "%s (%d lignes)" % (node.get("Node Type"), _annulled_rows(node))
        for node in _nodes(roots)
        if _annulled_rows(node) >= BROAD_ROWS
    ]


def _external_sorts(roots) -> list[str]:
    """Les tris externes de plus d'un mégaoctet (#5579 : 5 152 kB)."""
    found = []
    for node in _nodes(roots):
        if node.get("Node Type") not in ("Sort", "Incremental Sort"):
            continue
        method = node.get("Sort Method") or ""
        space = float(node.get("Sort Space Used") or 0)
        if "external" in method and space > MAX_SORT_KB:
            found.append("%s externe de %g kB" % (node.get("Node Type"), space))
    return found


def _cte_rows(roots, name: str) -> list[int]:
    """Les lignes des `CTE Scan` nommés `name` (plan matérialisé)."""
    return [int(node.get("Actual Rows") or 0) for node in _nodes(roots)
            if node.get("Subplan Name") == "CTE " + name]


def _relations(roots) -> set:
    return {node.get("Relation Name") for node in _nodes(roots)
            if node.get("Relation Name")}


class IssueFeedPlanDbTest(PgTestCase):
    """#5583 : le flux calcule `merged_ts` en ensemble, sans tri des lignes."""

    LOTS = 5_000
    #: ~1,2 ko de corps : largeur du central (1 621 o par ligne, #5579).
    BODY = "x" * 1200
    ANNULLED_LOTS = tuple(range(1, 11))
    REMERGED = 11
    SLOT = 4242

    def setUp(self) -> None:
        super().setUp()
        self._plan_db = None

    def _fixture(self):
        """Le volume du central : 5 000 lots du flux, 5 000 fusions, 10 000
        événements ; 10 corrections `merged → build` (le central en compte 2)
        et une refusion postérieure."""
        db = self.db
        db.execute("""
            INSERT INTO work_items (id, type, source, app, title, body, issue_ref,
                                    state, assignee, created_at, updated_at)
            SELECT g, 'bug', 'diag', 'ameesh', 'lot ' || g, %s, 'acme/web#' || g,
                   'merged', 'agent-x',
                   now() - interval '30 days', now() - make_interval(mins => g)
              FROM generate_series(1, %s) g
        """, (self.BODY, self.LOTS))
        db.execute("SELECT setval('work_items_id_seq', %s)", (self.LOTS,))
        db.execute("""
            INSERT INTO work_item_milestones (work_item_id, kind, at, note)
            SELECT g, 'merged', now() - make_interval(mins => g), 'transition'
              FROM generate_series(1, %s) g
        """, (self.LOTS,))
        # Deux événements par lot : un `build` avant le jalon, la transition
        # `merged` juste après. Rien après la fusion : aucune annulation.
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            SELECT g, e.state, e.note, 'agent-x',
                   now() - make_interval(mins => g) + e.offset_s * interval '1 second'
              FROM generate_series(1, %s) g
             CROSS JOIN (VALUES ('build', 'work', -10),
                                ('merged', 'transition', 5)) AS e(state, note, offset_s)
             ORDER BY g, e.offset_s
        """, (self.LOTS,))
        # Dix corrections `merged → build` : la transition précédente du lot
        # est `merged`, le jalon est donc annulé.
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            SELECT g, 'build', 'rework', 'agent-x',
                   now() - make_interval(mins => g) + interval '10 seconds'
              FROM generate_series(1, 10) g
        """)
        # Une refusion postérieure à la correction : le second jalon est
        # effectif, l'ancien reste annulé.
        db.execute("""
            INSERT INTO work_item_milestones (work_item_id, kind, at, note)
            VALUES (%s, 'merged', now() - interval '10 minutes' + interval '20 seconds',
                    'transition')
        """, (self.REMERGED,))
        db.execute("""
            INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)
            VALUES (%s, 'merged', 'transition', 'agent-x',
                    now() - interval '10 minutes' + interval '20 seconds')
        """, (self.REMERGED,))
        # Un créneau réservé : la liste bornée doit rendre la même ligne que
        # la lecture par lot (la sous-requête corrélée d'origine).
        db.execute("""
            INSERT INTO work_migration_reservations
                (work_item_id, slot, number, project, target_repo, target_branch, status)
            VALUES (12, 'default', %s, 'ameesh', 'acme/web', 'main', 'reserved')
        """, (self.SLOT,))
        for table in ("work_items", "work_item_milestones", "work_item_events",
                      "work_migration_reservations"):
            db.execute("ANALYZE " + table)

    def _feed(self):
        """La requête exacte de `issue_feed`, ses paramètres et ses lignes."""
        captured: dict = {}
        original = self.db.query

        def capture(sql, params=()):
            captured["sql"], captured["params"] = sql, tuple(params)
            return original(sql, params)

        self.db.query = capture
        try:
            rows = storage.of(self.db).work.issue_feed(self.LOTS)
        finally:
            self.db.query = original
        return captured["sql"], captured["params"], rows

    def _plan(self, sql, params):
        """Plan réel, sous psycopg.

        Le pilote psql enveloppe `EXPLAIN` dans un agrégat JSON : la part psql
        de la CI n'installe pas psycopg, le témoin y est sauté, jamais faux.
        Le premier passage chauffe les index ; le second est mesuré.
        """
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            if self._plan_db is None:
                try:
                    self._plan_db = db_mod.PsycopgDriver(self.cfg)
                except db_mod.Unavailable:
                    self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                                  "et psycopg est absent (#5583)")
                self.addCleanup(self._plan_db.close)
            driver = self._plan_db
        with driver.transaction() as tx:
            rows = tx.query("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                            params)
        document = json.loads(json.dumps(rows))[0]["QUERY PLAN"]
        return [item["Plan"] for item in document], document[0]

    def test_merged_ts_en_ensemble_sans_tri_des_lignes(self):
        self._fixture()
        sql, params, rows = self._feed()
        self.assertEqual(len(rows), self.LOTS)
        self.assertEqual([int(row["id"]) for row in rows],
                         list(range(1, self.LOTS + 1)),
                         "le flux reste rendu par id croissant")
        by_id = {int(row["id"]): row for row in rows}
        for lot_id in self.ANNULLED_LOTS:
            self.assertIsNone(by_id[lot_id]["merged_ts"],
                              "un jalon annulé ne rend pas de date (#1693)")
        self.assertIsNotNone(by_id[self.REMERGED]["merged_ts"])
        self.assertIsNotNone(by_id[12]["merged_ts"])
        # Le dernier lot n'a pas de correction : son jalon reste effectif.
        self.assertIsNotNone(by_id[self.LOTS]["merged_ts"])

        self._plan(sql, params)                      # chauffe les index
        roots, measured = self._plan(sql, params)

        self.assertEqual(_broad_annulled(roots), [],
                         "l'annulation ne s'évalue que sur les candidats, pas "
                         "sur les 5 000 lots du flux (#5583)\n%s"
                         % json.dumps(roots, indent=1))
        self.assertEqual(_external_sorts(roots), [],
                         "plus de tri externe des lignes larges (#5579)\n%s"
                         % json.dumps(roots, indent=1))
        self.assertLess(float(measured["Execution Time"]), MAX_MS,
                        "le flux doit rester sous %g ms (était 3,95 s, #5579)"
                        % MAX_MS)
        # Les ensembles matérialisés restent la forme de la lecture : une
        # agrégation de jalons, puis l'annulation sur les seuls candidats.
        self.assertTrue(_cte_rows(roots, "last_merge"),
                        "le calcul des fusions doit rester matérialisé")
        self.assertEqual(_cte_rows(roots, "annulled"), [len(self.ANNULLED_LOTS)],
                         "l'annulation ne rend que les corrections effectives")
        self.assertIn("work_item_milestones", _relations(roots))
        self.assertIn("work_item_events", _relations(roots))


class IssueFeedFormTest(unittest.TestCase):
    """#5583 : la forme qui a coûté 2,57 s ne peut pas revenir sans rougir."""

    def _sql(self, slots=True):
        captured: dict = {}

        class _Db:
            _has_reviewer = True
            _has_migration_slots = slots
            _has_current_item_columns = True

            def query(self, sql, params=()):
                captured["sql"], captured["params"] = sql, tuple(params)
                return []

        work_pg.WorkItems(_Db()).issue_feed(10)
        return captured["sql"]

    def test_le_flux_ne_calcule_pas_les_fusions_par_ligne(self):
        sql = self._sql()
        self.assertNotIn("(SELECT extract(epoch from max(m.at))", sql,
                         "la sous-requête corrélée merged_ts est revenue")
        for name in ("last_merge", "candidates", "annulled", "merged"):
            self.assertRegex(sql, r"\b%s AS MATERIALIZED\b" % name,
                             "l'ensemble %s doit rester matérialisé" % name)
        self.assertIn("IN (SELECT id FROM ids)", sql)

    def test_la_liste_des_creneaux_est_bornee(self):
        sql = self._sql()
        self.assertNotIn("r.work_item_id = work_items.id", sql,
                         "la liste des migrations est redevenue corrélée")
        self.assertIn("JOIN ids ON ids.id = r.work_item_id", sql)

    def test_base_sans_creneaux(self):
        """Base d'avant 0419 : le flux rend `[]`, sans nommer la table."""
        sql = self._sql(slots=False)
        self.assertNotIn("work_migration_reservations", sql)
        self.assertIn("'[]'::jsonb AS migrations", sql)

    def test_garde_statique_sur_la_source(self):
        source = inspect.getsource(work_pg.WorkItems.issue_feed)
        self.assertNotRegex(source, r"SELECT extract\(epoch from max\(m\.at\)")
        tree = ast.parse(textwrap.dedent(source))
        self.assertTrue(any(isinstance(node, ast.JoinedStr) for node in ast.walk(tree))
                        or "MATERIALIZED" in source)


class IssueFeedSelectionDbTest(PgTestCase):
    """#5583 : les valeurs du flux ne changent pas (jalon effectif, créneaux)."""

    SLOT = 4242

    def _lot(self, title):
        """Un lot du flux : `issue_ref` le garde dans la fenêtre une fois
        fusionné (les lots fermés sans référence n'y sont pas, #5584)."""
        item_id = work.add(self.db, title=title, app="ameesh")["id"]
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("acme/feed#%d" % item_id, item_id))
        return item_id

    def _close(self, item_id):
        work.set_branch(self.db, item_id, "lot/%d-etape" % item_id,
                        actor="orch", source="test")
        work.close_merged(self.db, item_id, sha="a" * 40, actor="orch")

    def test_jalon_annule_puis_refusion(self):
        annulled = self._lot("annulée")
        self._close(annulled)
        work.move(self.db, annulled, "build", actor="orch",
                  correct="PR d'étape fusionnée trop tôt (#759)")
        refondu = self._lot("refondue")
        self._close(refondu)
        work.move(self.db, refondu, "build", actor="orch",
                  correct="PR d'étape fusionnée trop tôt (#759)")
        work.move(self.db, refondu, "qa", actor="orch")
        work.move(self.db, refondu, "merged", actor="orch")
        sans = self._lot("sans fusion")

        feed = {int(row["id"]): row
                for row in storage.of(self.db).work.issue_feed(50)}
        self.assertIsNone(feed[annulled]["merged_ts"])
        self.assertIsNone(feed[sans]["merged_ts"])
        self.assertEqual(feed[refondu]["merged_ts"],
                         work.timeline(self.db, refondu)["merged_ts"])

    def test_la_liste_des_creneaux_est_rendue(self):
        item = self._lot("créneau")
        self._close(item)
        self.db.execute("""
            INSERT INTO work_migration_reservations
                (work_item_id, slot, number, project, target_repo, target_branch, status)
            VALUES (%s, 'default', %s, 'ameesh', 'acme/web', 'main', 'reserved')
        """, (item, self.SLOT))
        feed = {int(row["id"]): row
                for row in storage.of(self.db).work.issue_feed(50)}
        self.assertEqual(feed[item]["migrations"],
                         [{"number": self.SLOT, "repo": "acme/web", "slot": "default"}])
        self.assertEqual(work.get(self.db, item)["migrations"],
                         feed[item]["migrations"])


if __name__ == "__main__":
    unittest.main()
