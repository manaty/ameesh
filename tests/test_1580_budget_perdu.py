# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 — le choix automatique ne suit plus un ordre fixe de priorités.

Demande du propriétaire (2026-10-15) : « un score de choix qui privilégie le
budget perdu au reset, à lecture fraîche uniquement ». Témoins du score
(`accounts.lost_at_reset`), de l'ordre des critères de `rank` (les critères de
budget avant l'ordre déclaré) et de la raison (`reason_of`).

Les gardes ne bougent pas : un compte au seuil reste hors du classement, et
l'ordre déclaré (L226b « primaries last ») reste le départage à budget égal.
"""
import unittest
from types import SimpleNamespace

from ameesh import accounts, cost

HOUR = 3600.0
WEEK = 7 * 24 * HOUR


def _profile(name, **settings):
    return accounts.Profile("claude", name, **settings)


def _gauge(used, resets_in, window=WEEK, key="seven_day"):
    now = 1_800_000_000.0
    return cost.Gauge("claude", key, used, now + resets_in, window)


class LostAtResetTest(unittest.TestCase):
    """`accounts.lost_at_reset` : la part perdue à la prochaine remise à zéro."""

    NOW = 1_800_000_000.0

    def test_fenetre_la_plus_proche(self):
        """C'est la fenêtre qui se remet à zéro le plus tôt qui porte la part
        perdue (`1 − utilisé`), pas la plus utilisée."""
        gauges = [cost.Gauge("claude", "five_hour", 0.20, self.NOW + 2 * HOUR, 5 * HOUR),
                  cost.Gauge("claude", "seven_day", 0.60, self.NOW + 4 * 24 * HOUR, WEEK)]
        self.assertAlmostEqual(accounts.lost_at_reset(gauges, self.NOW), 0.80)

    def test_releve_echu_n_annonce_aucune_perte(self):
        """Une fenêtre remise à zéro depuis (relevé échu) n'expire plus : le
        score est 0, la lecture n'est pas comptée comme courante (L71)."""
        echue = [cost.Gauge("claude", "seven_day", 0.95, self.NOW - 60, WEEK)]
        self.assertEqual(accounts.lost_at_reset(echue, self.NOW), 0.0)

    def test_sans_releve_ni_fenetre_datee(self):
        self.assertEqual(accounts.lost_at_reset([], self.NOW), 0.0)
        sans_date = [cost.Gauge("claude", "seven_day", 0.50, None, WEEK)]
        self.assertEqual(accounts.lost_at_reset(sans_date, self.NOW), 0.0)

    def test_perte_par_seconde_jusqu_a_la_remise_a_zero(self):
        """#1789/N1 : la part perdue rapportée au temps qui reste. Une fenêtre
        de 5 h à 20 % qui se remet à zéro dans 2 h perd 0,8 en 2 h, soit
        0,4/h ; la même part à 7 jours s'évapore 84 fois moins vite."""
        court = [cost.Gauge("claude", "five_hour", 0.20, self.NOW + 2 * HOUR, 5 * HOUR)]
        long = [cost.Gauge("claude", "seven_day", 0.20, self.NOW + 2 * 24 * HOUR, WEEK)]
        self.assertAlmostEqual(accounts.lost_rate(court, self.NOW), 0.80 / (2 * HOUR))
        self.assertAlmostEqual(accounts.lost_rate(long, self.NOW), 0.80 / (2 * 24 * HOUR))
        self.assertGreater(accounts.lost_rate(court, self.NOW),
                           accounts.lost_rate(long, self.NOW))
        # rien n'expire : aucune perte par seconde
        self.assertEqual(accounts.lost_rate([], self.NOW), 0.0)
        self.assertEqual(accounts.lost_rate(
            [cost.Gauge("claude", "seven_day", 0.95, self.NOW - 60, WEEK)], self.NOW), 0.0)
        self.assertEqual(accounts.lost_rate(
            [cost.Gauge("claude", "seven_day", 0.50, None, WEEK)], self.NOW), 0.0)


class OrdreDuChoixTest(unittest.TestCase):
    """`accounts.rank` : les critères de budget avant l'ordre déclaré."""

    NOW = 1_800_000_000.0

    def _items(self):
        # l'ordre déclaré du cas réel : tertiaire 10, secondaire 20, primaire 100
        return [_profile("primaire", priority=100), _profile("secondaire", priority=20),
                _profile("tertiaire", priority=10)]

    def _ev(self, name, gauges, limits=None, reason=""):
        return accounts.Evaluation(_profile(name), reason=reason, gauges=gauges,
                                   limits=limits or {})

    def test_le_plus_en_retard_passe_meme_declare_dernier(self):
        """Le primaire est déclaré dernier (100) et c'est le moins utilisé : il
        passe avant le tertiaire déclaré premier (10)."""
        gauges = {
            "primaire": [_gauge(0.05, 3 * 24 * HOUR)],
            "secondaire": [_gauge(0.40, 3 * 24 * HOUR)],
            "tertiaire": [_gauge(0.80, 3 * 24 * HOUR)],
        }
        items = self._items()
        evs = {p.name: self._ev(p.name, gauges[p.name]) for p in items}
        self.assertEqual([p.name for p in accounts.rank(items, evs, self.NOW)],
                         ["primaire", "secondaire", "tertiaire"])

    def test_la_part_perdue_departage_a_rythme_et_echeance_egaux(self):
        """Même retard, même échéance : la plus grosse part perdue à la remise
        à zéro passe avant l'ordre déclaré (#1580)."""
        # plafonds explicites : 0,25/0,50 = 0,30/0,60 = 0,5 de rythme
        primaire = [_gauge(0.25, 3 * 24 * HOUR)]
        tertiaire = [_gauge(0.30, 3 * 24 * HOUR)]
        items = self._items()
        evs = {
            "primaire": self._ev("primaire", primaire,
                                 limits={"seven_day": (0.50, "primary cap")}),
            "secondaire": self._ev("secondaire", [_gauge(0.60, 3 * 24 * HOUR)],
                                   limits={"seven_day": (0.90, "pace cap")}),
            "tertiaire": self._ev("tertiaire", tertiaire,
                                  limits={"seven_day": (0.60, "pace cap")}),
        }
        # secondaire : 0,60/0,90 = 0,667 de rythme, il reste derrière les deux
        # autres (0,5) ; entre eux, la perte par seconde décide : 0,75/3 j >
        # 0,70/3 j (la part perdue, elle, départage à perte égale)
        self.assertEqual(accounts.lost_at_reset(primaire, self.NOW), 0.75)
        self.assertEqual(accounts.lost_at_reset(tertiaire, self.NOW), 0.70)
        ranked = accounts.rank(items, evs, self.NOW)
        self.assertEqual([p.name for p in ranked], ["primaire", "tertiaire", "secondaire"])

    def test_la_perte_la_plus_rapide_passe_avant_le_retard(self):
        """#1789/N1 : entre deux comptes utilisables, celui dont le budget
        s'évapore le plus vite par seconde passe avant le plus en retard sur
        son rythme."""
        # primaire : fenêtre de 5 h à 10 %, remise à zéro dans 30 min → 0,9
        # perdu en 0,5 h, soit 1,8/h ; tertiaire : semaine intacte, remise à
        # zéro dans 5 jours → 1,0 perdu en 120 h, soit ~0,008/h.
        evs = {
            "primaire": self._ev("primaire", [_gauge(0.10, 0.5 * HOUR,
                                                     window=5 * HOUR,
                                                     key="five_hour")]),
            "tertiaire": self._ev("tertiaire", [_gauge(0.0, 5 * 24 * HOUR)]),
            "secondaire": self._ev("secondaire", [_gauge(0.50, 3 * 24 * HOUR)]),
        }
        self.assertGreater(accounts.lost_rate(evs["primaire"].gauges, self.NOW),
                           accounts.lost_rate(evs["tertiaire"].gauges, self.NOW))
        # le tertiaire est le plus en retard (0 %), mais ne perd rien : le
        # primaire passe d'abord, le tertiaire avant le secondaire (0,5 de
        # rythme et 0,0069/h)
        self.assertEqual([p.name for p in accounts.rank(self._items(), evs, self.NOW)],
                         ["primaire", "tertiaire", "secondaire"])
        # la raison dit le critère qui a décidé, pas le retard (#1789/N1)
        self.assertIn("losing its budget fastest", accounts.reason_of(
            accounts.rank(self._items(), evs, self.NOW), evs, self.NOW))

    def test_a_egalite_de_budget_l_ordre_declare_departage(self):
        """Budget identique : L226b reste le départage, et la raison le dit."""
        items = self._items()
        evs = {p.name: self._ev(p.name, [_gauge(0.50, 3 * 24 * HOUR)]) for p in items}
        ranked = accounts.rank(items, evs, self.NOW)
        self.assertEqual([p.name for p in ranked], ["tertiaire", "secondaire", "primaire"])
        self.assertIn("first in the failover order",
                      accounts.reason_of(ranked, evs, self.NOW))

    def test_forecast_uses_the_same_caps_as_the_choice(self):
        """The primary's fixed cap reverses the order of the raw usage ratios."""
        cfg = SimpleNamespace(host="test", accounts={"claude": [
            {"name": "primaire", "cap": 0.50, "priority": 100},
            {"name": "tertiaire", "cap": 0.90, "priority": 10},
        ]})
        # #1789/N1: the loss rates are tied on purpose — 0.80 / (0.1 W) =
        # 0.70 / (0.0875 W) — so the fixed caps decide through `lag` alone:
        # primary .20/.50 = .40, tertiary .30/.90 ≈ .333. Without the caps
        # both read .20/pace and .30/pace, and the primary comes first.
        gauges = {"primaire": [_gauge(0.20, 0.1 * WEEK)],
                  "tertiaire": [_gauge(0.30, 0.0875 * WEEK)]}
        evaluations = {}

        def evaluate(book, profile, items, now, **kwargs):
            result = accounts.Evaluation(
                profile, gauges=gauges[profile.name],
                limits=accounts.limits(profile, gauges[profile.name], now))
            evaluations[profile.name] = result
            return result

        rows = accounts.report(cfg, None, None, now=self.NOW, evaluate_fn=evaluate,
                               record=False)
        items = accounts.parse(cfg.accounts)["claude"]
        ranked = accounts.rank(items, evaluations, self.NOW)
        # Actual ratios: primary .20/.50=.40, tertiary .30/.90=.333.
        # Without the fixed caps, the forecast incorrectly picks primary.
        self.assertEqual(ranked[0].name, "tertiaire")
        self.assertEqual([row["account"] for row in rows if row["next"]],
                         [ranked[0].name])
        self.assertEqual(next(row["why"] for row in rows if row["next"]),
                         accounts.reason_of(ranked, evaluations, self.NOW))

    def test_un_compte_au_seuil_reste_hors_du_classement(self):
        """Aucun dépassement : le score ne ressuscite jamais un compte écarté
        par la garde, même s'il a le plus gros budget perdu."""
        primaire = [_gauge(0.95, 3 * 24 * HOUR)]
        items = self._items()
        evs = {
            "primaire": self._ev("primaire", primaire,
                                 reason="seven_day 95% used (primary cap 50%)"),
            "secondaire": self._ev("secondaire", [_gauge(0.40, 3 * 24 * HOUR)]),
            "tertiaire": self._ev("tertiaire", [_gauge(0.60, 3 * 24 * HOUR)]),
        }
        self.assertEqual([p.name for p in accounts.rank(items, evs, self.NOW)],
                         ["secondaire", "tertiaire"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
