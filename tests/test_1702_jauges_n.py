# SPDX-License-Identifier: AGPL-3.0-only
"""#1702 — les cinq N de la relecture Claude de #1132 (jauges de forfait).

N1 (documenté dans `docs/COMPTES.md`) : sur un exécuteur `backend=mediated`,
`operations.gauge_latest` n'est pas admise au contrat médié ; la réconciliation
d'historique est inerte et la lecture locale rendue silencieusement. Admettre
l'opération toucherait le contrat d'autorisation : hors du geste de ce lot.

N2 — une fenêtre reprise de l'historique partagé n'est pas réécrite dans
l'historique : chaque hôte évaluateur y réinscrivait le relevé d'un autre sous
sa source (échos toutes les dix minutes).

N3 — un relevé repris de l'historique dit son âge à l'affichage (`accounts
list`) : sans lecture locale, la mesure n'est pas un instantané du jour.

N4 — la lecture sans compte récupère elle aussi le marqueur de compte hors de
la tranche lue (comme le chemin par compte) : sans cela, une mesure dont le
marqueur précédait la tranche restait non datée et perdait contre toute mesure
datée d'un autre flux.

N5 — `GaugeCache.get` rend une copie défensive : l'observation partagée n'est
plus mutable par un appelant, l'invariant n'est plus un commentaire.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import accounts, cost, storage
from ameesh.cost import Gauge, Gauges
from ameesh.gauge_cache import GaugeCache

from .support import PgTestCase
from .test_cost import Sandbox
from .test_1132_jauges_perimees import line, marked, windows


class _JaugesBase(Sandbox, PgTestCase):
    """Schéma jetable et table des jauges vide : chaque témoin part de zéro."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings RESTART IDENTITY")


class EchoHistoriqueTest(_JaugesBase):
    """N2 : le relevé d'un autre hôte n'est pas réinscrit sous ma source."""

    def test_un_releve_repris_de_l_historique_n_est_pas_reecrit(self):
        ops = storage.of(self.db).operations
        ops.record_gauges([line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                                account="secondaire", produced_at=self.now - 3600,
                                source="ag-autre")])
        # aucune lecture locale : la fenêtre vient entièrement de l'historique
        gauges = self.book().claude_gauges(account="secondaire", primary="primaire")
        self.assertEqual([g.from_history for g in gauges], [True])
        self.assertAlmostEqual(gauges[0].used, 0.87)
        self.assertEqual(gauges[0].source, "ag-autre")
        self.assertEqual(self.book().record_gauges(gauges, account="secondaire"), 0)
        hist = ops.gauge_history(since_s=3600, harness="claude", account="secondaire")
        self.assertEqual([r["source"] for r in hist], ["ag-autre"])
        self.assertEqual(len(hist), 1)

    def test_une_lecture_locale_reste_ecrite(self):
        # contre-épreuve : le filtre ne bloque que les fenêtres d'historique
        ops = storage.of(self.db).operations
        ops.record_gauges([line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                                account="secondaire", produced_at=self.now - 3600,
                                source="ag-autre")])
        self.agent("hote", "claude")
        self.events("hote", marked("secondaire", self.now - 60),
                    windows(seven_day=(0.42, self.now + 3 * 86400)))
        gauges = self.book().claude_gauges(account="secondaire", primary="primaire")
        self.assertFalse(gauges[0].from_history)
        self.assertEqual(self.book().record_gauges(gauges, account="secondaire"), 1)
        hist = ops.gauge_history(since_s=3600, harness="claude", account="secondaire")
        self.assertIn("hote", [r["source"] for r in hist])


class AgeAffichageTest(_JaugesBase):
    """N3 : la mesure reprise de l'historique affiche son âge."""

    def setUp(self) -> None:
        super().setUp()
        self._compte = os.path.join(self.tmp_cost, "compte")
        os.makedirs(self._compte, exist_ok=True)
        os.chmod(self._compte, 0o700)     # `check` exige un dossier privé

    def test_un_releve_d_historique_affiche_son_age(self):
        storage.of(self.db).operations.record_gauges([
            line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                 account="secondaire", produced_at=self.now - 7200,
                 source="ag-autre")])
        cfg = mock.Mock(host="hote-test", accounts={"claude": [
            {"name": "secondaire", "path": self._compte,
              "check_credentials": False}]})
        rows = accounts.report(cfg, self.db, self.book(), now=self.now, record=False)
        row = [r for r in rows if r["account"] == "secondaire"][0]
        gauge = row["gauges"][0]
        self.assertTrue(gauge["from_history"])
        self.assertAlmostEqual(gauge["read_age_s"], 7200, places=0)
        self.assertIn("read 2 h ago", accounts.format_rows(rows))

    def test_une_lecture_locale_n_affiche_pas_d_age_d_historique(self):
        self.agent("hote", "claude")
        self.events("hote", marked("secondaire", self.now - 60),
                    windows(seven_day=(0.42, self.now + 3 * 86400)))
        cfg = mock.Mock(host="hote-test", accounts={"claude": [
            {"name": "secondaire", "path": self._compte,
              "check_credentials": False}]})
        rows = accounts.report(cfg, self.db, self.book(), now=self.now, record=False)
        row = [r for r in rows if r["account"] == "secondaire"][0]
        self.assertFalse(row["gauges"][0]["from_history"])
        self.assertNotIn("read 0 s ago", accounts.format_rows(rows))
        self.assertNotIn("read 1 min ago", accounts.format_rows(rows))


class MarqueurHorsTailTest(Sandbox, unittest.TestCase):
    """N4 : la lecture sans compte récupère le marqueur hors de la tranche."""

    def test_lecture_sans_compte_date_par_le_marqueur_hors_tail(self):
        self.agent("a", "claude")
        marqueur = self.now - 3600
        self.events("a", marked("secondaire", marqueur),
                    # la rembourrure pousse le marqueur hors de la tranche lue
                    {"type": "result", "pad": "x" * 2000},
                    windows(five_hour=(0.42, self.now + 3600)))
        with mock.patch.object(cost, "TAIL_BYTES", 300):
            gauges = self.book(db=None).claude_gauges()
        self.assertAlmostEqual(gauges[0].used, 0.42)
        self.assertAlmostEqual(gauges[0].produced_at, marqueur, places=0)
        self.assertEqual(gauges[0].source, "a")

    def test_sans_marqueur_la_mesure_reste_non_datee(self):
        # contre-épreuve : la récupération ne date pas un flux sans marqueur
        self.agent("legacy", "claude")
        self.events("legacy", windows(five_hour=(0.10, self.now + 3600)))
        with mock.patch.object(cost, "TAIL_BYTES", 300):
            gauges = self.book(db=None).claude_gauges()
        self.assertAlmostEqual(gauges[0].used, 0.10)
        self.assertIsNone(gauges[0].produced_at)


class CacheCopieTest(unittest.TestCase):
    """N5 : `get` rend une copie ; l'observation partagée n'est pas mutable."""

    def test_get_rend_une_copie_independante(self):
        cache = GaugeCache()
        produite = Gauges([Gauge("claude", "seven_day", 0.42, None, 604800.0)])
        cache["journal"] = ("signature", {"secondaire": (0.0, 0.0, produite)})
        lue = cache.get("journal")
        self.assertEqual(len(lue[1]["secondaire"][2]), 1)
        lue[1]["secondaire"][2].append(
            Gauge("claude", "five_hour", 0.10, None, 18000.0))
        self.assertEqual(len(cache.get("journal")[1]["secondaire"][2]), 1)
        # le producteur non plus ne peut plus muter l'observation gardée
        produite.append(Gauge("claude", "five_hour", 0.10, None, 18000.0))
        self.assertEqual(len(cache.get("journal")[1]["secondaire"][2]), 1)
        self.assertIsNone(cache.get("absent"))


if __name__ == "__main__":
    unittest.main()
