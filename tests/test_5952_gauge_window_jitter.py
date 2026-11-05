# SPDX-License-Identifier: AGPL-3.0-only
"""#5952 — dans une même fenêtre gigotée, le relevé le plus récent l'emporte.

Suite de #5950 côté stockage. Codex republie l'échéance d'une même fenêtre à
quelques secondes près (1794205167, 168, 172). `gauge_latest_window` classait
`resets_at` d'abord : le relevé à l'échéance gigotée la plus haute gagnait,
ici celui de 34 % du 02/11 21:52Z, au lieu du plus récent de la même fenêtre ;
un hôte sans relevé local frais le recevait tel quel. Une vraie fenêtre
suivante (échéance plus tardive de plusieurs heures) gagne toujours.
"""
from __future__ import annotations

import sys
import unittest

from ameesh import cost, db as db_mod, storage
from ameesh.storage.postgres import operations

from .test_1132_jauges_perimees import _JaugesBase

INSERT = """
    INSERT INTO quota_gauge_readings
        (harness, account, gauge_key, used, window_s, resets_at,
         observed_at, produced_at, source)
    VALUES ('codex', 'primaire', 'codex-10080min', %s, 604800,
            to_timestamp(%s), to_timestamp(%s), NULL, %s)
"""


class GigueFenetreStockageTest(_JaugesBase):

    def _drivers(self):
        # les deux pilotes, nommés : `self.db` est celui du banc
        for name in ("psycopg", "psql"):
            try:
                db = self.connect(driver=name)
            except (db_mod.Unavailable, ImportError) as exc:
                if name == "psql":
                    raise
                print("skip %s: %s" % (name, exc), file=sys.__stderr__)
                continue
            self.addCleanup(getattr(db, "close", lambda: None))
            self.assertEqual(db.name, name)
            yield name, db

    def _latest(self, db):
        return storage.of(db).operations.gauge_latest_window(
            harness="codex", account="primaire")

    def test_meme_fenetre_gigotee_le_plus_recent_gagne(self):
        resets = 1794205167.0
        self.db.execute(INSERT, (0.34, resets + 5, self.now - 3 * 86400, "vieux"))
        self.db.execute(INSERT, (0.80, resets + 1, self.now - 3600, "moyen"))
        self.db.execute(INSERT, (1.00, resets, self.now - 60, "frais"))
        for name, db in self._drivers():
            with self.subTest(driver=name):
                rows = self._latest(db)
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(rows[0]["used"], 1.0)
                self.assertEqual(rows[0]["source"], "frais")
                self.assertAlmostEqual(rows[0]["resets_at_ts"], resets, places=0)

    def test_fenetre_suivante_gagne_meme_inseree_avant(self):
        resets = 1794205167.0
        self.db.execute(INSERT, (0.02, resets + 6 * 3600, self.now - 7200, "suivante"))
        self.db.execute(INSERT, (0.99, resets + 3, self.now - 60, "close"))
        self.db.execute(INSERT, (0.98, resets, self.now - 30, "close-gigue"))
        for name, db in self._drivers():
            with self.subTest(driver=name):
                rows = self._latest(db)
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(rows[0]["used"], 0.02)
                self.assertEqual(rows[0]["source"], "suivante")

    def test_la_production_prime_sur_l_insertion_dans_la_fenetre(self):
        resets = 1794205167.0
        self.db.execute("""
            INSERT INTO quota_gauge_readings
                (harness, account, gauge_key, used, window_s, resets_at,
                 observed_at, produced_at, source)
            VALUES ('codex', 'primaire', 'codex-10080min', 0.70, 604800,
                    to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), 'daté'),
                   ('codex', 'primaire', 'codex-10080min', 0.40, 604800,
                    to_timestamp(%s), to_timestamp(%s), NULL, 'non-daté')
        """, (resets, self.now - 3600, self.now - 3600,
              resets + 4, self.now - 10))
        rows = self._latest(self.db)
        self.assertEqual([r["source"] for r in rows], ["daté"])

    def test_meme_tolerance_que_la_garde(self):
        self.assertEqual(operations.GAUGE_WINDOW_JITTER_S,
                         cost.CODEX_WINDOW_JITTER_S)


if __name__ == "__main__":
    unittest.main()
