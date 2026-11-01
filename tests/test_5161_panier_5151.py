# SPDX-License-Identifier: AGPL-3.0-only
"""#5161 — panier de la relecture de #5151 : un lot qa à correctif dû.

Témoins du lot :

* N1 : un début planifié futur (date à la main ou du planificateur) n'écarte
  pas un lot qa dont l'assigné doit la correction (`start_defers`) ;
* N2 : ce lot occupe son auteur — `active_lots` de `operations.listing`,
  file de `work schedule` (`waits_on_others`) ;
* N3 : le prédicat SQL `fix_owed` et `due_fixes` (`_owed_read`, le calcul
  de référence) s'accordent, scénario par scénario.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import lot_schedule, registry, storage, work
from ameesh import review_designation_activation as rda
from ameesh.storage.postgres import work_dependencies as pg_deps

from .support import PgTestCase

SHA = "c" * 40
NEW_SHA = "d" * 40


H = 3600.0
BODY = {"schema": "ameesh-estimate-reference/1", "min_lots": 3, "groups": [
    {"level": "type", "type": "bug", "lots": 5, "median_minutes": 360.0,
     "p80_minutes": 600.0}]}


class WaitsOnOthersTest(unittest.TestCase):
    def test_a_qa_lot_with_a_fix_owed_holds_the_queue(self):
        self.assertTrue(lot_schedule.waits_on_others({"state": "qa"}))
        self.assertFalse(lot_schedule.waits_on_others({"state": "qa", "fix_owed": True}))
        now = time.time()
        lots = [{"id": 1, "type": "bug", "state": "qa", "started_ts": now - H,
                 "fix_owed": True},
                {"id": 2, "type": "bug", "state": "intake"}]
        by = {e["id"]: e for e in lot_schedule.plan_queue(lots, {}, BODY, {}, now=now)}
        self.assertAlmostEqual(by[2]["start_ts"], now + 5 * H, places=2)


class FixOwedTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev1", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)

    def qa_lot(self, reviewers=(), title="qa"):
        lot = int(work.add(self.db, title=title, app="ameesh", actor="pilote")["id"])
        work.assign(self.db, lot, "dev1", actor="pilote")
        work.move(self.db, lot, "build")
        work.move(self.db, lot, "qa")
        work.milestone(self.db, lot, "frozen", sha=SHA, actor="dev1")
        self.db.execute("UPDATE work_items SET reviewers ="
                        " coalesce(string_to_array(nullif(%s, ''), ','), '{}')"
                        " WHERE id = %s", (",".join(reviewers), lot))
        return lot

    def verdict(self, lot, actor, kind, sha=SHA):
        work.milestone(self.db, lot, "verdict", verdict=kind, sha=sha, actor=actor)

    def listing_row(self):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == "dev1")

    def sql_owed(self):
        return {int(r["id"]) for r in self.db.query(
            "SELECT w.id FROM work_items w WHERE " + pg_deps.fix_owed("w"))}

    def ref_owed(self):
        return {int(r["lot"]) for r in rda.due_fixes(self.db)}

    # -- N1 ------------------------------------------------------------------
    def test_a_future_start_does_not_set_aside_a_fix_owed(self):
        lot = self.qa_lot(["rev-a"])
        self.verdict(lot, "rev-a", "blocked")
        self.db.execute("UPDATE work_items SET planned_start = current_date + 1,"
                        " planned_source = 'set by mesh-design' WHERE id = %s", (lot,))
        row = self.listing_row()
        self.assertEqual((row["actionable_lots"], row["actionable_lot_id"]), (1, lot))
        # le blocage levé, la date redevient une attente
        self.verdict(lot, "rev-a", "ok")
        self.assertEqual(self.listing_row()["actionable_lots"], 0)

    # -- N2 ------------------------------------------------------------------
    def test_a_fix_owed_occupies_its_author(self):
        lot = self.qa_lot(["rev-a"])
        self.assertEqual(self.listing_row()["active_lots"], 0)
        self.verdict(lot, "rev-a", "blocked")
        self.assertEqual(self.listing_row()["active_lots"], 1)

    def test_the_schedule_marks_a_fix_owed(self):
        owed = self.qa_lot(["rev-a"], title="correctif dû")
        self.verdict(owed, "rev-a", "blocked")
        waiting = self.qa_lot(["rev-a"], title="relu, en attente")
        seen = {}

        def spy(lots, *args, **kwargs):
            seen.update({int(l["id"]): bool(l.get("fix_owed")) for l in lots})
            return []

        with mock.patch.object(lot_schedule, "plan_queue", side_effect=spy):
            lot_schedule.build(self.db, assignee="dev1")
        self.assertEqual(seen, {owed: True, waiting: False})

    # -- N3 : témoin d'accord ---------------------------------------------------
    def test_the_sql_predicate_agrees_with_the_reference(self):
        cases = []
        # sièges : B de A puis ok de B → dû
        lot = self.qa_lot(["rev-a", "rev-b"])
        self.verdict(lot, "rev-a", "blocked")
        self.verdict(lot, "rev-b", "ok")
        cases.append(lot)
        # sièges : B puis ok du même → rien
        lot = self.qa_lot(["rev-a"])
        self.verdict(lot, "rev-a", "blocked")
        self.verdict(lot, "rev-a", "ok")
        cases.append(lot)
        # sièges : B d'un non-désigné → rien
        lot = self.qa_lot(["rev-a"])
        self.verdict(lot, "intrus", "blocked")
        cases.append(lot)
        # sans siège : le dernier verdict décide
        lot = self.qa_lot([])
        self.verdict(lot, "rev-a", "ok")
        self.verdict(lot, "rev-b", "blocked")
        cases.append(lot)
        # B sur l'ancienne tête, regel d'un autre sha → rien
        lot = self.qa_lot(["rev-a"])
        self.verdict(lot, "rev-a", "blocked")
        work.milestone(self.db, lot, "frozen", sha=NEW_SHA, actor="dev1")
        cases.append(lot)
        # B en sha abrégé → dû
        lot = self.qa_lot(["rev-a"])
        self.verdict(lot, "rev-a", "blocked", sha=SHA[:12])
        cases.append(lot)
        sql, ref = self.sql_owed(), self.ref_owed()
        for lot in cases:
            with self.subTest(lot=lot):
                self.assertEqual(lot in sql, lot in ref)
        self.assertEqual(ref & set(cases), {cases[0], cases[3], cases[5]})


if __name__ == "__main__":
    unittest.main()
