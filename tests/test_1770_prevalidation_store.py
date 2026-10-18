# SPDX-License-Identifier: AGPL-3.0-only
"""#1770 : le rapport de prévalidation persisté (0450), écrit après le run.

Deux niveaux : le domaine de stockage sur un schéma Postgres réel (migration
0450 appliquée par `PgTestCase`), et l'accrochage de la commande — le rapport
s'écrit **après** l'exécution distante, jamais pendant, et un refus avant run
n'écrit rien. Aucune VM n'est contactée.
"""
from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from ameesh import prevalidation as pv
from ameesh import storage

from .support import PgTestCase

A = "a" * 40
B = "b" * 40
C = "c" * 40


def rapport(lot: int = 1770, sha: str = A, ok: bool = True,
            outcome: str = "green", causes=(), **over) -> dict:
    base = {
        "schema": pv.REPORT_SCHEMA, "lot": lot, "sha": sha, "base": B,
        "card": C, "modules": ["test_a", "test_cluster"],
        "planned": ["test_a", "test_cluster"],
        "parts": [{"driver": "psycopg", "part": 1, "of": 1,
                   "modules": ["test_a", "test_cluster"]}],
        "tests": 3, "host": "vm1", "outcome": outcome,
        "exit": 0 if ok else 1, "seconds": 12.5,
        "causes": list(causes), "ok": ok,
    }
    base.update(over)
    return base


class PrevalidationStoreTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        # le socle ne tronque que ses tables connues : la nôtre est à nous
        self.db.execute("DELETE FROM prevalidation_reports")
        self.store = storage.of(self.db).prevalidation

    def test_rapport_enregistre_et_relu(self):
        self.store.record(rapport())
        got = self.store.report(1770, A)
        self.assertIsNotNone(got)
        self.assertEqual(got["work_item_id"], 1770)
        self.assertEqual(got["head_sha"], A)
        self.assertEqual(got["base_sha"], B)
        self.assertEqual(got["card_commit"], C)
        self.assertEqual(got["schema"], pv.REPORT_SCHEMA)
        self.assertEqual(got["outcome"], "green")
        self.assertTrue(got["ok"])
        self.assertEqual(got["causes"], [])
        self.assertEqual(got["report"]["modules"], ["test_a", "test_cluster"])
        self.assertEqual(got["report"]["seconds"], 12.5)
        self.assertEqual(got["report"]["host"], "vm1")

    def test_meme_cle_remplace_le_rapport(self):
        self.store.record(rapport())
        self.store.record(rapport(ok=False, outcome="run-red",
                                  causes=["run-red"], exit=1))
        got = self.store.report(1770, A)
        self.assertFalse(got["ok"])
        self.assertEqual(got["outcome"], "run-red")
        self.assertEqual(got["causes"], ["run-red"])
        rows = self.db.query("SELECT count(*) AS n FROM prevalidation_reports"
                             " WHERE work_item_id = 1770")
        self.assertEqual(rows[0]["n"], 1, "une seule ligne par (lot, tête)")
        self.assertEqual(self.store.latest(1770)["outcome"], "run-red")

    def test_latest_rend_le_plus_recent_du_lot(self):
        self.store.record(rapport(sha=A))
        self.store.record(rapport(sha=B, ok=False, outcome="run-red",
                                  causes=["run-red"], exit=1))
        self.assertEqual(self.store.latest(1770)["head_sha"], B)
        self.assertEqual(self.store.report(1770, A)["outcome"], "green")
        self.assertIsNone(self.store.report(1770, C), "tête jamais validée")

    def test_lot_sans_rapport(self):
        self.assertIsNone(self.store.report(1770, A))
        self.assertIsNone(self.store.latest(1770))
        self.assertIsNone(self.store.report(999, A), "un autre lot ne voit rien")

    def test_cle_invalide_refuse(self):
        for bad in ({"sha": A}, {"lot": 1770}, {"lot": 0, "sha": A},
                    {"lot": "1770", "sha": A}, {"lot": True, "sha": A}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.record(bad)
        self.assertIsNone(self.store.latest(1770), "aucune ligne écrite")

    def test_causes_non_vides_conservees(self):
        self.store.record(rapport(ok=False, outcome="run-incomplete",
                                  causes=["run-incomplete", "transport-lost"]))
        got = self.store.report(1770, A)
        self.assertEqual(got["causes"], ["run-incomplete", "transport-lost"])


class _Db:
    """Connexion factice : capture les instructions, ne touche aucune base."""

    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, sql: str, params=()) -> int:
        self.calls.append((sql, params))
        return 0


def params(**over) -> SimpleNamespace:
    values = {"classes": ("P0", "P1"), "contracts": (), "hosts": ("vm1",),
              "timeout_seconds": 60}
    values.update(over)
    return SimpleNamespace(**values)


class CommandRecordTest(unittest.TestCase):
    def _args(self, **over) -> SimpleNamespace:
        values = {"lot": 1770, "sha": A, "plan": "plan.json", "host": "vm1",
                  "base": "", "repo": "", "canon_repo": "", "canon_commit": C,
                  "project": "ameesh", "published_ref": "", "remote": "origin",
                  "json": True}
        values.update(over)
        return SimpleNamespace(**values)

    def test_ecriture_apres_le_run_seulement(self):
        db = _Db()
        seen = {}

        def fake_validate(**_kw):
            seen["pendant"] = list(db.calls)
            return rapport()

        with mock.patch.object(pv, "load_parameters",
                               return_value=(None, params())), \
                mock.patch.object(pv, "validate", side_effect=fake_validate):
            with redirect_stdout(StringIO()):
                code = pv.run(None, db, self._args())
        self.assertEqual(code, 0)
        self.assertEqual(seen["pendant"], [], "aucune écriture pendant le run")
        self.assertEqual(len(db.calls), 1)
        sql, values = db.calls[0]
        self.assertIn("INSERT INTO prevalidation_reports", sql)
        self.assertEqual(values[0], 1770)
        self.assertEqual(values[1], A)
        self.assertIn('"modules": ["test_a", "test_cluster"]', values[-1])

    def test_rapport_rouge_enregistre_aussi(self):
        db = _Db()
        with mock.patch.object(pv, "load_parameters",
                               return_value=(None, params())), \
                mock.patch.object(pv, "validate",
                                  return_value=rapport(ok=False, outcome="run-red",
                                                       causes=["run-red"], exit=1)):
            with redirect_stdout(StringIO()):
                code = pv.run(None, db, self._args())
        self.assertEqual(code, 1)
        self.assertEqual(len(db.calls), 1, "un rouge est un résultat, pas un refus")

    def test_refus_avant_run_n_ecrit_rien(self):
        db = _Db()
        with redirect_stdout(StringIO()):
            code = pv.run(None, db, self._args(canon_commit=""))
        self.assertEqual(code, 1)
        self.assertEqual(db.calls, [], "block-missing n'écrit aucun rapport")


if __name__ == "__main__":
    unittest.main()
