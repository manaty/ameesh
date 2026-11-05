# SPDX-License-Identifier: AGPL-3.0-only
"""#5950 — une gigue de quelques secondes sur `resets_at` reste la même fenêtre.

Sur agents-2 le 05/11, le journal local portait la fenêtre hebdomadaire à
100 % (crédits consommés, échéance 1794205168) ; l'historique partagé gardait
un relevé du 02/11 à 34 % pour la MÊME fenêtre, échéance 1794205172. Avec une
tolérance d'une seconde, les 4 s d'écart passaient pour une fenêtre plus
tardive : le vieux relevé gagnait, l'overage disparaissait et l'alerte
plan_underused tombait à tort. Une vraie fenêtre suivante (échéance plus
tardive de plusieurs heures) gagne toujours.
"""
from __future__ import annotations

import unittest

from ameesh import accounts, cost

from .test_5466_codex_compte_partage import WEEK, _CodexBase, _history, _rollout


class GigueEcheanceTest(_CodexBase):

    def test_meme_fenetre_a_quelques_secondes_le_local_frais_gagne(self):
        resets = self.now + 2 * 86400
        _rollout(self.home, 100, resets, self.now - 60)
        self.ops.record_gauges([_history(
            0.34, resets + 4, produced_at=self.now - (2 * 86400 + 18 * 3600))])

        evaluation = self.evaluate()

        gauge = self.gauge(evaluation)
        self.assertAlmostEqual(gauge.used, 1.0)
        self.assertFalse(gauge.from_history)
        self.assertTrue(evaluation.gauges.overage.in_use)
        self.assertTrue(accounts.in_overage(evaluation.gauges, self.now))

    def test_gigue_dans_l_autre_sens_le_local_frais_gagne(self):
        resets = self.now + 2 * 86400
        _rollout(self.home, 100, resets, self.now - 60)
        self.ops.record_gauges([_history(
            0.34, resets - 5, produced_at=self.now - 86400)])

        gauge = self.gauge(self.evaluate())

        self.assertAlmostEqual(gauge.used, 1.0)
        self.assertFalse(gauge.from_history)

    def test_meme_fenetre_un_releve_partage_plus_frais_gagne(self):
        resets = self.now + 2 * 86400
        _rollout(self.home, 40, resets, self.now - 3600)
        self.ops.record_gauges([_history(0.90, resets + 4,
                                         produced_at=self.now - 60)])

        gauge = self.gauge(self.evaluate())

        self.assertAlmostEqual(gauge.used, 0.90)
        self.assertTrue(gauge.from_history)

    def test_fenetre_suivante_l_historique_gagne(self):
        """Échéance plus tardive de plusieurs heures : la fenêtre locale est
        close, le relevé partagé l'emporte et l'overage local tombe."""
        _rollout(self.home, 100, self.now + 3600, self.now - 60)
        self.ops.record_gauges([_history(0.01, self.now + 3600 + WEEK)])

        evaluation = self.evaluate()

        gauge = self.gauge(evaluation)
        self.assertAlmostEqual(gauge.used, 0.01)
        self.assertTrue(gauge.from_history)
        self.assertFalse(evaluation.gauges.overage.in_use)
        self.assertFalse(accounts.in_overage(evaluation.gauges, self.now))

    def test_la_tolerance_reste_loin_sous_la_plus_courte_fenetre(self):
        self.assertGreater(cost.CODEX_WINDOW_JITTER_S, 60.0)
        self.assertLess(cost.CODEX_WINDOW_JITTER_S, 300 * 60 / 10)


if __name__ == "__main__":
    unittest.main()
