# SPDX-License-Identifier: AGPL-3.0-only
"""#1566 — les deux N (P3) de la relecture Claude de #1561.

N2 (ici) : `cost.stale_reading` ne regardait que les fenêtres datées. Une
fenêtre datée remise à zéro **plus** une fenêtre non datée ouverte (une source
qui ne date pas toutes ses fenêtres) donnait `all(passed)=True` sur les seules
datées, donc « non périmé » : le relevé partiel était pris pour un instantané
courant. Une fenêtre non datée ne prouve pas sa remise à zéro ; elle court
encore, comme une fenêtre datée non échue.

N1 (conception, `accounts.rank`) : un relevé partiel qui reste **informateur**
garde le rang d'un relevé courant (`accounts._informative_partial`). La règle
est bornée par le GO du pilote (mesh-design, 15/10/2026, lot #1566) : âge
connu, fini et non futur, `âge <= reste` d'**au moins une** fenêtre datée
ouverte (frontière #1672, égalité admise), et **chaque** fenêtre encore
ouverte (non datée comprise) sous son plafond effectif `Evaluation.limits`
moins `RESERVE` (`usage < max(0, plafond − RESERVE)`, égalité refusée). Sans
preuve : fraîcheur 2, jamais 0. Les témoins de N1 sont en bas de ce fichier.
"""
from __future__ import annotations

import unittest

from ameesh import accounts, cost

HOUR = 3600.0
DAY = 86400.0
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


class PartielInformateurTest(unittest.TestCase):
    """N1 : la confiance bornée du classement d'un relevé partiel.

    Le relevé type est celui du scénario de la relecture : la fenêtre 5 h s'est
    remise à zéro depuis la lecture (elle vaut 0 %, L71), la fenêtre 7 j court
    encore — le relevé n'est plus un instantané (N2/#1561), mais il reste assez
    jeune pour dire quelque chose des fenêtres ouvertes."""

    NOW = 1_800_000_000.0
    DAY = 86400.0
    CAP = 0.80

    def _ev(self, name, gauges, limits=None):
        result = accounts.Evaluation(accounts.Profile("claude", name), gauges=gauges)
        if limits is None:
            limits = {g.key: (self.CAP, "pace cap") for g in gauges}
        result.limits = dict(limits)
        return result

    def _partial(self, *, age=HOUR, reset_in=3 * DAY, seven_used=0.30,
                 five_used=0.90, five_resets=None, seven_read_at=True,
                 five_read_at=True):
        """Le relevé partiel type : 5 h remise à zéro depuis la lecture, 7 j
        encore ouverte, même date de lecture sur les deux fenêtres."""
        now = self.NOW
        return [
            cost.Gauge("claude", "seven_day", seven_used, now + reset_in, WEEK,
                       now - age if seven_read_at else None),
            cost.Gauge("claude", "five_hour", five_used,
                       now - 60 if five_resets is None else five_resets,
                       5 * HOUR, now - age if five_read_at else None),
        ]

    def test_partiel_informateur_garde_le_rang_courant(self):
        """Fraîcheur 1 (jamais 0) : il concourt avec les relevés courants et
        peut passer devant eux par le budget — c'est la fin de l'inversion du
        scénario de la relecture (le secondaire dont la 5 h s'est rouverte)."""
        now = self.NOW
        partiel = self._partial()               # 5 h remise à zéro, 7 j ouverte
        courant = [
            cost.Gauge("claude", "five_hour", 0.60, now + HOUR, 5 * HOUR, now - HOUR),
            cost.Gauge("claude", "seven_day", 0.10, now + 3 * DAY, WEEK, now - HOUR),
        ]
        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire")]
        evaluations = {"primaire": self._ev("primaire", courant),
                       "secondaire": self._ev("secondaire", partiel)}
        self.assertTrue(cost.stale_reading(partiel, now))
        self.assertEqual(accounts._rank_key(items[1], evaluations, now)[0], 1)
        self.assertLess(accounts.lag(partiel, now, evaluations["secondaire"].limits)[0],
                        accounts.lag(courant, now, evaluations["primaire"].limits)[0])
        self.assertEqual([p.name for p in accounts.rank(items, evaluations, now)],
                         ["secondaire", "primaire"])
        why = accounts.reason_of(accounts.rank(items, evaluations, now), evaluations, now)
        self.assertIn("unverified", why)
        self.assertIn("partial reading still informative", why)
        self.assertNotIn("furthest behind its pace", why)

    def test_age_frontiere_egalite_admise(self):
        """`âge <= reste` : l'égalité passe (même frontière que #1672, qui
        n'écarte qu'au-delà) ; une seconde de plus refuse."""
        now = self.NOW
        resets_at = now + 3 * DAY
        remaining = resets_at - now
        exact = self._partial(age=remaining, reset_in=3 * DAY)
        self.assertTrue(accounts._informative_partial(
            exact, self._ev("x", exact), now))
        trop = self._partial(age=remaining + 1.0, reset_in=3 * DAY)
        self.assertFalse(accounts._informative_partial(
            trop, self._ev("x", trop), now))

    def test_marge_frontiere_egalite_refusee(self):
        """`usage < max(0, plafond − RESERVE)` : l'égalité refuse, un cheveu
        sous le seuil passe."""
        now = self.NOW
        seuil = self.CAP - accounts.RESERVE
        pile = self._partial(seven_used=seuil)
        self.assertFalse(accounts._informative_partial(
            pile, self._ev("x", pile), now))
        dessous = self._partial(seven_used=seuil - 1e-9)
        self.assertTrue(accounts._informative_partial(
            dessous, self._ev("x", dessous), now))
        # plafond sous la réserve : aucune marge démontrable, même à 0 %
        sous = self._partial(seven_used=0.0,
                             five_used=0.0, five_resets=now + HOUR)
        self.assertFalse(accounts._informative_partial(
            sous, self._ev("x", sous, {"seven_day": (0.05, "cap"),
                                       "five_hour": (0.05, "cap")}), now))

    def test_fenetre_non_datee_ouverte_compte_dans_la_marge(self):
        """N2 : une fenêtre non datée ne prouve pas sa remise à zéro ; elle
        court encore et doit garder la marge sous son plafond, elle aussi."""
        now = self.NOW
        seuil = self.CAP - accounts.RESERVE
        melange = [
            cost.Gauge("claude", "seven_day", 0.30, now + 3 * DAY, WEEK, now - HOUR),
            cost.Gauge("claude", "five_hour", seuil, None, 5 * HOUR, now - HOUR),
        ]
        self.assertFalse(accounts._informative_partial(
            melange, self._ev("x", melange), now))
        melange[1] = cost.Gauge("claude", "five_hour", seuil - 1e-9, None,
                                5 * HOUR, now - HOUR)
        self.assertTrue(accounts._informative_partial(
            melange, self._ev("x", melange), now))

    def test_fenetre_non_datee_seule_ne_prouve_aucune_ouverture(self):
        """Sans aucune fenêtre **datée** encore ouverte, la borne d'âge ne
        s'applique pas : fraîcheur 2, jamais 0."""
        now = self.NOW
        melange = [
            cost.Gauge("claude", "seven_day", 0.90, now - 60, WEEK, now - HOUR),
            cost.Gauge("claude", "five_hour", 0.10, None, 5 * HOUR, now - HOUR),
        ]
        self.assertFalse(accounts._informative_partial(
            melange, self._ev("x", melange), now))

    def test_sans_age_connu_refus(self):
        """Une date manquante ne prouve aucune fraîcheur : le relevé partiel
        reste périmé (fraîcheur 2)."""
        now = self.NOW
        sans_date = self._partial(seven_read_at=False, five_read_at=False)
        self.assertTrue(cost.stale_reading(sans_date, now))
        self.assertFalse(accounts._informative_partial(
            sans_date, self._ev("x", sans_date), now))
        items = [accounts.Profile("claude", "secondaire")]
        evaluations = {"secondaire": self._ev("secondaire", sans_date)}
        self.assertEqual(accounts._rank_key(items[0], evaluations, now)[0], 2)
        why = accounts.reason_of(accounts.rank(items, evaluations, now),
                                 evaluations, now)
        self.assertIn("tried (no current reading)", why)

    def test_plafond_effectif_et_non_held_cap(self):
        """La marge se lit sur `Evaluation.limits` (plafond fixe, court ou
        d'overage), pas sur le seul plafond de rythme : ici le rythme
        autoriserait 45 %, le plafond effectif de 50 % non."""
        now = self.NOW
        gauges = self._partial(seven_used=0.45)
        self.assertGreater(gauges[0].held_cap(now), 0.45)
        limits = {"seven_day": (0.50, "short window cap"),
                  "five_hour": (self.CAP, "pace cap")}
        self.assertFalse(accounts._informative_partial(
            gauges, self._ev("x", gauges, limits), now))
        # plafond absent pour une fenêtre ouverte : aucune preuve, refus
        self.assertFalse(accounts._informative_partial(
            gauges, self._ev("x", gauges, {"five_hour": (self.CAP, "pace cap")}),
            now))

    def test_incident_1672_reste_refuse(self):
        """Le cas réel du 15/10 (relevé de 4 jours, 7 j à 9 h de sa remise à
        zéro) échoue la borne d'âge : non informateur, écarté du placement,
        même garde de budget désactivée."""
        now = self.NOW
        vieux = [
            cost.Gauge("claude", "seven_day", 0.30, now + 9 * HOUR, WEEK, now - 4 * DAY),
            cost.Gauge("claude", "five_hour", 0.90, now - 3600, 5 * HOUR, now - 4 * DAY),
        ]
        evaluation = self._ev("secondaire", vieux)
        self.assertFalse(accounts._informative_partial(vieux, evaluation, now))
        self.assertFalse(accounts.placeable(evaluation, now))
        items = [accounts.Profile("claude", "secondaire")]
        evaluations = {"secondaire": evaluation}
        self.assertEqual(accounts.rank(items, evaluations, now), [])
        self.assertEqual(accounts.rank(items, evaluations, now, paced=False), [])

    def test_paced_false_garde_la_regle_mais_ouvre_le_seuil_de_rythme(self):
        """Garde de budget désactivée : un partiel informateur au seuil de son
        rythme redevient utilisable (`unpaced_ok`), jamais un relevé écarté
        par la prudence d'âge (couvert ci-dessus)."""
        now = self.NOW
        partiel = self._partial()
        au_seuil = self._ev("secondaire", partiel)
        au_seuil.reason = "pace cap reached"
        au_seuil.hard = False
        items = [accounts.Profile("claude", "secondaire")]
        evaluations = {"secondaire": au_seuil}
        self.assertEqual(accounts.rank(items, evaluations, now), [])
        self.assertEqual([p.name for p in accounts.rank(items, evaluations, now,
                                                         paced=False)],
                         ["secondaire"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
