# SPDX-License-Identifier: AGPL-3.0-only
"""#5404 : `ameesh alerts` — lectures sans `to_jsonb(ligne)`, passes à blanc sans git."""
import unittest
from unittest import mock

from ameesh import canon, db as db_mod
from ameesh.modules.dev import lot_class
from ameesh.storage.postgres import operations, roadmap, work, work_dependencies


class SqlSansLigneEntiere(unittest.TestCase):
    def test_colonnes_directes(self):
        for sql in (work.item_columns(None, "w"),
                    operations.Operations._LOTS_ACTIVITY,
                    work_dependencies.freeze_due("w"), work_dependencies.fix_owed("w"),
                    roadmap._ITEM_COLUMNS):
            self.assertNotIn("to_jsonb(w)", sql)
            self.assertNotIn("to_jsonb(work_items)", sql)

    def test_item_columns_garde_la_forme_json_de_reviewers(self):
        self.assertIn("to_jsonb(w.reviewers) AS reviewers", work.item_columns(None, "w"))

    def test_frise_compte_sur_les_identifiants(self):
        select = roadmap._ITEMS_SQL.split("SELECT __COLS__")[0]
        self.assertIn("count(*) OVER ()", select)
        self.assertNotIn("__COLS__", select)

    def test_frise_lie_ses_parametres_dans_l_ordre(self):
        rows = []

        class Db:
            def query(self, sql, params):
                rows.append((sql, params))
                return []

        store = roadmap.Roadmap.__new__(roadmap.Roadmap)
        store.db = Db()
        store.items(since_ts=12.0, limit=3, done_states=("a", "b"), project="p")
        sql, params = rows[0]
        self.assertEqual(sql.count("%s"), len(params))
        self.assertEqual(params, ("a", "b", "a", "b", 12.0, "p", "p", 3))


class Copie(unittest.TestCase):
    def test_reponses_isolees(self):
        answers = db_mod._Answers()
        answers.put("q", (), [{"a": [1], "b": {"c": 2}}])
        first = answers.get("q", ())
        first[0]["a"].append(9)
        first[0]["b"]["c"] = 9
        first[0]["d"] = 9
        self.assertEqual(answers.get("q", ()), [{"a": [1], "b": {"c": 2}}])


class PasseABlanc(unittest.TestCase):
    def test_aucune_sonde_git(self):
        recorder = db_mod._Recorder(mock.Mock(cfg=None), db_mod._Answers())
        with mock.patch.object(lot_class.plan_git, "local_commit") as probe, \
                mock.patch.object(lot_class.canon_mod, "git_toplevel") as top:
            self.assertEqual(
                lot_class.repo_for_freeze(recorder, None, {"assignee": "a"}, "abc1234"),
                ("", ""))
        probe.assert_not_called()
        top.assert_not_called()


class CanonUneFois(unittest.TestCase):
    def test_une_lecture_par_fenetre(self):
        with mock.patch.object(canon, "_load", side_effect=lambda *a, **k: object()) as read:
            with canon.configured_cache():
                first = canon.load("/c")
                self.assertIs(canon.load("/c"), first)
                self.assertIsNot(canon.load("/c", default=False), first)
            self.assertEqual(read.call_count, 2)
            canon.load("/c")  # hors fenêtre : le canon se relit
            self.assertEqual(read.call_count, 3)

    def test_lecture_bornee_jamais_memoisee(self):
        with mock.patch.object(canon, "_load", side_effect=lambda *a, **k: object()) as read:
            with canon.configured_cache():
                canon.load("/c", fetch=True)
                canon.load("/c", fetch=True)
            self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
