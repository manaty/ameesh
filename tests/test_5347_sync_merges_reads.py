# SPDX-License-Identifier: AGPL-3.0-only
"""#5347 — `sync-merges` dépasse le `statement_timeout` sur la base commune.

Constat (mesh-design, agents-2, 01/11) : `ameesh work sync-merges` échoue
après ~32 s sur « canceling statement due to statement timeout ». Deux
lectures portaient le coût :

* `WorkItems.items` (les 1 000 lots du balayage) recopiait
  `to_jsonb(work_items)` six fois par ligne **avant** la borne : 5 230 lignes
  converties, 12 à 22 s mesurées sur la base commune ; c'est la requête qui
  expirait à la reproduction du 01/11 au soir ;
* `_PACKET_SELECT` recalculait les deux jalons du porteur (dernier `merged`,
  dernier `frozen`) par ligne de membre, 2 × 653 sous-requêtes corrélées sur
  la même tête de porteur.

Attendu : une seule conversion JSON par ligne rendue, faite après le tri et
la borne ; les jalons du porteur calculés une fois par porteur distinct, avec
les mêmes valeurs (dernier `merged` nommant un commit, dernier `frozen` sans
filtre de sha, #3634).
"""
from __future__ import annotations

import re
import unittest

from ameesh import storage, work
from ameesh.storage.postgres import work as work_pg

from .support import PgTestCase


class _RecordingDb:
    """Assez de la surface de `Db` pour composer les deux lectures."""

    _has_reviewer = True
    _has_migration_slots = True

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    def query(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        return []


class ItemsSqlTest(unittest.TestCase):
    """La conversion JSON d'`items` est unique et postérieure à la borne."""

    def setUp(self) -> None:
        self.db = _RecordingDb()
        work_pg.WorkItems(self.db).items(state=None, assignee=None, limit=1000)
        self.sql, self.params = self.db.statements[-1]

    def test_to_jsonb_once_per_returned_row(self):
        self.assertEqual(len(re.findall(r"to_jsonb\(", self.sql)), 1, self.sql)

    def test_the_limit_precedes_the_conversion(self):
        self.assertRegex(
            self.sql,
            r"FROM \(SELECT \* FROM work_items.*ORDER BY updated_at DESC LIMIT %s\)"
            r" work_items CROSS JOIN LATERAL "
            r"\(SELECT to_jsonb\(work_items\) AS doc OFFSET 0\) j")
        # la borne est le seul paramètre de la lecture
        self.assertEqual(self.params, (1000,))

    def test_the_outer_order_is_kept(self):
        self.assertTrue(self.sql.endswith("ORDER BY work_items.updated_at DESC"),
                        self.sql)

    def test_filters_stay_on_the_inner_read(self):
        db = _RecordingDb()
        work_pg.WorkItems(db).items(state="intake", assignee="mty-amh-docs-01",
                                    limit=25)
        sql, params = db.statements[-1]
        self.assertIn("AND state = %s", sql)
        self.assertIn("AND assignee = %s", sql)
        self.assertEqual(params, ("intake", "mty-amh-docs-01", 25))


class PacketMembersSqlTest(unittest.TestCase):
    """Les jalons du porteur sont calculés une fois par porteur, pas par membre."""

    def setUp(self) -> None:
        self.db = _RecordingDb()
        work_pg.WorkItems(self.db).packet_members(item_ids=[5347, 5348, 5349])
        self.sql, self.params = self.db.statements[-1]

    def test_one_carrier_row_per_distinct_package(self):
        self.assertIn("carrier AS (SELECT DISTINCT package_id FROM member)", self.sql)
        self.assertIn("LEFT JOIN heads h ON h.package_id = m.package_id", self.sql)

    def test_no_milestone_probe_is_correlated_to_a_member_row(self):
        self.assertEqual(len(re.findall(r"ms\.work_item_id = c\.package_id",
                                       self.sql)), 2, self.sql)
        self.assertNotIn("ms.work_item_id = m.package_id", self.sql)
        self.assertEqual(self.sql.count("kind = 'merged'"), 1, self.sql)
        self.assertEqual(self.sql.count("kind = 'frozen'"), 1, self.sql)

    def test_the_item_filter_bounds_the_materialized_members(self):
        self.assertIn("WITH member AS (SELECT m.* FROM work_packet_members m"
                      " WHERE m.work_item_id IN (%s, %s, %s))", self.sql)
        self.assertEqual(self.params, (5347, 5348, 5349))

    def test_a_package_filter_still_works(self):
        db = _RecordingDb()
        work_pg.WorkItems(db).packet_members(package_ids=[41])
        sql, params = db.statements[-1]
        self.assertIn("WHERE m.package_id IN (%s)", sql)
        self.assertEqual(params, (41,))


class PacketHeadsTest(PgTestCase):
    """Valeurs rendues identiques à l'ancienne lecture par membre."""

    BASE = "a" * 40
    HEAD = "b" * 40
    UNIT = "c" * 40

    def setUp(self) -> None:
        super().setUp()
        self.store = storage.of(self.db)
        self.package = work.add(self.db, title="paquet #5347", actor="test",
                                source="composition")
        self.first = work.add(self.db, title="unité 1", actor="test")
        self.second = work.add(self.db, title="unité 2", actor="test")
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (self.HEAD, int(self.package["id"])))
        self.store.work.record_packet(
            int(self.package["id"]), branch="integration/paquet-5347",
            base_sha=self.BASE, head_sha=self.HEAD,
            units=[{"work_item_id": int(self.first["id"]), "unit_sha": self.UNIT,
                    "source_sha": self.UNIT, "frozen_sha": self.UNIT},
                   {"work_item_id": int(self.second["id"]), "unit_sha": self.UNIT,
                    "source_sha": self.UNIT, "frozen_sha": self.UNIT}],
            note="packet composition recorded", actor="test")

    def mark(self, kind: str, sha: str, *, seconds_ago: float) -> int:
        rows = self.db.query(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, %s, now() - make_interval(secs => %s), %s, 'test')"
            " RETURNING id",
            (int(self.package["id"]), kind, float(seconds_ago), sha))
        return int(rows[0]["id"])

    def test_last_merged_with_a_commit_and_last_frozen_whatever_its_sha(self):
        self.mark("frozen", "1" * 40, seconds_ago=600)
        self.mark("merged", "2" * 40, seconds_ago=500)
        # #3634 : le dernier gel fait autorité même sans commit nommé.
        self.mark("frozen", "", seconds_ago=400)
        # un `merged` sans sha ne remplace pas le dernier qui nomme un commit
        self.mark("merged", "", seconds_ago=300)

        rows = self.store.work.packet_members(item_ids=[int(self.first["id"])])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["package_merged_sha"], "2" * 40)
        self.assertEqual(rows[0]["package_frozen_sha"], "")

    def test_same_at_is_broken_by_the_higher_id(self):
        self.mark("merged", "3" * 40, seconds_ago=100)
        winner = self.mark("merged", "4" * 40, seconds_ago=100)
        rows = self.store.work.packet_members(package_ids=[int(self.package["id"])])
        self.assertEqual({r["work_item_id"] for r in rows},
                         {int(self.first["id"]), int(self.second["id"])})
        self.assertEqual({r["package_merged_sha"] for r in rows}, {"4" * 40})
        self.assertEqual({r["package_frozen_sha"] for r in rows}, {None})
        self.assertGreater(winner, 0)


if __name__ == "__main__":
    unittest.main()
