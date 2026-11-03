# SPDX-License-Identifier: AGPL-3.0-only
"""#5584 : la table de correspondance `issue_ref → lots` lit `(id, issue_ref)`.

`plan_github._lots_by_issue_ref` construisait sa table en lisant tout
`issue_feed(5000)` (lignes larges, sous-requêtes `merged_ts` et migrations,
tri de 5 Mo) pour n'en garder que `id` et `issue_ref` : ~10 s par appel sous
charge sur le central (diagnostic #5579). `WorkItems.issue_refs(limit)` lit la
même fenêtre (mêmes lots, même borne) avec ces deux colonnes seulement ; le
flux complet reste le repli d'un stockage qui ne porte pas la lecture.
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, storage, work
from ameesh.modules.dev import plan_github

from .support import PgTestCase


def _mapping(rows) -> dict:
    return {int(row["id"]): (row.get("issue_ref") or "").strip()
            for row in rows if (row.get("issue_ref") or "").strip()}


def _relations(roots) -> set:
    found = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.get("Relation Name"):
            found.add(node["Relation Name"])
        stack.extend(node.get("Plans", []))
    return found


class IssueRefsDbTest(PgTestCase):
    """La lecture dédiée rend la même table que le flux, à fenêtre égale."""

    def setUp(self) -> None:
        super().setUp()
        self.ids = {}
        for name, state, ref, pinned in (
                ("ouvert-sans", None, None, False),
                ("ouvert-avec", None, "manaty/ameesh#10", False),
                ("clos-avec", "closed", "manaty/ameesh#11", False),
                ("clos-sans", "closed", None, False),
                ("clos-blanc", "closed", "   ", False),
                ("epingle-avec", "closed", "  manaty/ameesh#12  ", True),
                ("ouvert-avec-2", None, "Manaty/Ameesh#10", False)):
            lot = work.add(self.db, title=name, app="ameesh")
            self.db.execute(
                "UPDATE work_items SET issue_ref = %s,"
                " state = coalesce(%s, state),"
                " close_reason = CASE WHEN %s = 'closed' THEN 'done' ELSE close_reason END,"
                " workflow_ref = CASE WHEN %s THEN 'canon@abc:process.md' ELSE workflow_ref END"
                " WHERE id = %s", (ref, state, state, pinned, lot["id"]))
            self.ids[name] = int(lot["id"])

    def test_same_mapping_as_the_feed_for_every_window(self):
        ops = storage.of(self.db).work
        for limit in (1, 2, 3, 5, 100):
            with self.subTest(limit=limit):
                self.assertEqual(_mapping(ops.issue_refs(limit)),
                                 _mapping(ops.issue_feed(limit)))
        refs = _mapping(ops.issue_refs(100))
        self.assertEqual(refs[self.ids["epingle-avec"]], "manaty/ameesh#12")
        self.assertNotIn(self.ids["clos-blanc"], refs)
        self.assertNotIn(self.ids["clos-sans"], refs)

    def test_the_lookup_no_longer_reads_the_feed(self):
        st = storage.of(self.db)
        with mock.patch.object(type(st.work), "issue_feed",
                               side_effect=AssertionError("issue_feed read")):
            table = plan_github._lots_by_issue_ref(st)
        self.assertEqual(sorted(table["manaty/ameesh#10"]),
                         sorted([self.ids["ouvert-avec"], self.ids["ouvert-avec-2"]]))
        self.assertEqual(table["manaty/ameesh#12"], [self.ids["epingle-avec"]])
        self.assertEqual(table["manaty/ameesh#11"], [self.ids["clos-avec"]])

    def test_the_plan_reads_work_items_only(self):
        """Ni jalons, ni fiches, ni registre : aucune des sous-requêtes du flux."""
        captured: dict = {}
        original = self.db.query

        def capture(sql, params=()):
            captured["sql"], captured["params"] = sql, tuple(params)
            return original(sql, params)

        self.db.query = capture
        try:
            storage.of(self.db).work.issue_refs(plan_github.FEED_LIMIT)
        finally:
            self.db.query = original
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            try:
                driver = db_mod.PsycopgDriver(self.cfg)
            except db_mod.Unavailable:
                self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                              "et psycopg est absent (#5584)")
            self.addCleanup(driver.close)
        with driver.transaction() as tx:
            rows = tx.query("EXPLAIN (FORMAT JSON) " + captured["sql"],
                            captured["params"])
        roots = [item["Plan"] for item in
                 json.loads(json.dumps(rows))[0]["QUERY PLAN"]]
        self.assertEqual(_relations(roots), {"work_items"},
                         json.dumps(roots, indent=1))


class IssueRefsFallbackTest(unittest.TestCase):
    """Un stockage sans la lecture dédiée (exécuteur médié) garde le flux."""

    def test_fallback_on_the_feed(self):
        calls = []

        def feed(limit):
            calls.append(limit)
            return [{"id": 3, "issue_ref": " o/r#1 "}, {"id": 4, "issue_ref": ""},
                    {"id": 5, "issue_ref": "O/R#1"}]

        st = types.SimpleNamespace(work=types.SimpleNamespace(issue_feed=feed))
        self.assertEqual(plan_github._lots_by_issue_ref(st), {"o/r#1": [3, 5]})
        self.assertEqual(calls, [plan_github.FEED_LIMIT])


if __name__ == "__main__":
    unittest.main()
