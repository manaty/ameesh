# SPDX-License-Identifier: AGPL-3.0-only
"""#1566 — les deux N (P3) de la relecture Claude de #1561.

N2 (ici) : `cost.stale_reading` ne regardait que les fenêtres datées. Une
fenêtre datée remise à zéro **plus** une fenêtre non datée ouverte (une source
qui ne date pas toutes ses fenêtres) donnait `all(passed)=True` sur les seules
datées, donc « non périmé » : le relevé partiel était pris pour un instantané
courant. Une fenêtre non datée ne prouve pas sa remise à zéro ; elle court
encore, comme une fenêtre datée non échue.

N1 (conception, `accounts.rank`) est traité dans le même lot ; ses témoins
rejoignent ce fichier quand la règle est arrêtée avec le pilote.
"""
from __future__ import annotations

import unittest

from ameesh import accounts, cost

HOUR = 3600.0
WEEK = 7 * 86400.0


class MelangeDateeNonDateeTest(unittest.TestCase):
    """N2 : le mélange « datée échue + non datée » reste un relevé partiel."""

    NOW = 1_800_000_000.0

    def _ev(self, name: str, gauges) -> accounts.Evaluation:
        return accounts.Evaluation(accounts.Profile("claude", name), gauges=gauges)

    def test_datee_echue_et_non_datee_ouverte_est_perime(self):
        """La fenêtre non datée n'est pas prouvée remise à zéro : le relevé
        est partiel, pas « toutes les fenêtres datées échues, 0 % » (L71)."""
        now = self.NOW
        melange = [
            cost.Gauge("claude", "seven_day", 0.80, now - 60, WEEK),
            cost.Gauge("claude", "five_hour", 0.30, None, 5 * HOUR, now - HOUR),
        ]
        self.assertTrue(cost.stale_reading(melange, now))
        self.assertIn("stale reading (window reset since the reading): unverified",
                      accounts.explain(self._ev("x", melange), now))

    def test_datee_echue_et_non_datee_ouverte_ne_prevaut_pas_courant(self):
        """Le classement suit : le relevé partiel ne passe pas avant un relevé
        courant, même quand son rythme lu est plus favorable."""
        now = self.NOW
        melange = [
            cost.Gauge("claude", "seven_day", 0.80, now - 60, WEEK),
            cost.Gauge("claude", "five_hour", 0.30, None, 5 * HOUR, now - HOUR),
        ]
        courant = [
            cost.Gauge("claude", "five_hour", 0.30, now + 2.5 * HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.50, now + 0.5 * WEEK, WEEK),
        ]
        self.assertLess(accounts.lag(melange, now)[0], accounts.lag(courant, now)[0])
        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire")]
        evaluations = {"primaire": self._ev("primaire", courant),
                       "secondaire": self._ev("secondaire", melange)}
        self.assertEqual([p.name for p in accounts.rank(items, evaluations, now)],
                         ["primaire", "secondaire"])

    def test_toutes_les_fenetres_datees_echues_reste_zero(self):
        """L71 inchangé : aucune fenêtre non datée, toutes les datées échues —
        la suivante est vierge, 0 %, pas « périmé »."""
        now = self.NOW
        echu = [cost.Gauge("claude", "seven_day", 0.80, now - 60, WEEK)]
        self.assertFalse(cost.stale_reading(echu, now))
        self.assertEqual(cost.stale_reading(echu, now), False)

    def test_datee_ouverte_et_non_datee_n_est_pas_perime_sans_remise_a_zero(self):
        """Sans aucune fenêtre datée remise à zéro, le relevé n'est pas
        partiel : la fenêtre non datée ne change rien."""
        now = self.NOW
        melange = [
            cost.Gauge("claude", "seven_day", 0.50, now + 0.5 * WEEK, WEEK),
            cost.Gauge("claude", "five_hour", 0.30, None, 5 * HOUR, now - HOUR),
        ]
        self.assertFalse(cost.stale_reading(melange, now))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
