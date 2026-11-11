# SPDX-License-Identifier: AGPL-3.0-only
"""#6958 — l'actif en « limit » et le relevé partiel de primaire.

Constat du 11/11 sur ameesh-agents-1 : `accounts list` montre claude/tertiaire
« active, limit » (seven_day 15 %, plafond non critique 10 %) et claude/primaire
« next choice ... tried (no current reading) », alors que le relevé 7 j de
primaire est frais (3 %, lu il y a 19 min) et que sa fenêtre 5 h est remise à
zéro depuis la lecture.

Deux défauts distincts, tous deux tirés de ce constat :

* la ligne « active » est le dernier choix **partagé** (`account_active`) ; un
  agent critique choisit son compte sans l'écrire (L242b), donc un hôte sans
  consommateur non critique peut afficher un actif au-dessus de sa pause sans
  qu'aucune session n'y soit retenue. `format_rows` le dit maintenant ;
* `_informative_partial` retranchait `RESERVE` une seconde fois quand
  `Evaluation.limits` avait déjà été calculé avec la réserve
  (`evaluate(reserve=True)`, le chemin de `choose` pour un non-critique et de
  `report`) : le plafond 7 j déjà réservé de primaire (10 %) devenait 0 %, et
  son relevé partiel frais était classé périmé (fraîcheur 2) au lieu
  d'informateur.
"""
from __future__ import annotations

import unittest

from ameesh import accounts, cost

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


class InformativePartialReserveTest(unittest.TestCase):
    """Le plafond déjà réservé n'est pas réservé deux fois (#6958)."""

    NOW = 1_800_000_000.0

    def _primaire(self, seven_used=0.03):
        """Forme réelle de primaire : 5 h remise à zéro, 7 j fraîche."""
        now = self.NOW
        gauges = [
            cost.Gauge("claude", "seven_day", seven_used, now + 3 * DAY, WEEK,
                       now - 19 * 60),
            cost.Gauge("claude", "five_hour", 0.0, now - 60, 5 * HOUR,
                       now - 11 * DAY),
        ]
        return gauges

    def _ev(self, gauges, limits, *, reserve):
        result = accounts.Evaluation(accounts.Profile("claude", "primaire"),
                                     gauges=gauges, reserve=reserve)
        result.limits = dict(limits)
        return result

    def test_plafond_deja_reserve_reste_informateur(self):
        """3 % sous un plafond de rythme déjà réservé à 10 % : informatif.

        Sans le correctif, le seuil valait max(0, 0,10 − RESERVE) = 0 et le
        relevé frais de primaire était déclaré périmé (fraîcheur 2)."""
        gauges = self._primaire(0.03)
        result = self._ev(gauges, {"seven_day": (0.10, "pace cap")}, reserve=True)
        self.assertTrue(accounts._informative_partial(gauges, result, self.NOW))

    def test_egalite_au_plafond_deja_reserve_refusee(self):
        """L'égalité au plafond effectif reste refusée, comme avant."""
        gauges = self._primaire(0.10)
        result = self._ev(gauges, {"seven_day": (0.10, "pace cap")}, reserve=True)
        self.assertFalse(accounts._informative_partial(gauges, result, self.NOW))

    def test_plafond_fixe_porte_toujours_la_reserve(self):
        """Les plafonds fixes ne sont jamais réservés par `limits` : la marge
        s'y applique une fois, même quand `reserve=True`."""
        gauges = self._primaire(0.45)
        result = self._ev(gauges, {"seven_day": (0.50, "primary cap")},
                          reserve=True)
        self.assertFalse(accounts._informative_partial(gauges, result, self.NOW))
        gauges = self._primaire(0.39)
        result = self._ev(gauges, {"seven_day": (0.50, "primary cap")},
                          reserve=True)
        self.assertTrue(accounts._informative_partial(gauges, result, self.NOW))

    def test_sans_reserve_la_marge_reste_retranchee(self):
        """Chemin critique (`reserve=False`) : les plafonds de `limits` ne
        portent pas la réserve, elle se retranche ici — comportement #1566
        inchangé."""
        gauges = self._primaire(0.03)
        result = self._ev(gauges, {"seven_day": (0.10, "pace cap")},
                          reserve=False)
        self.assertFalse(accounts._informative_partial(gauges, result, self.NOW))
        result = self._ev(gauges, {"seven_day": (0.20, "pace cap")},
                          reserve=False)
        self.assertTrue(accounts._informative_partial(gauges, result, self.NOW))


class FormatRowsActiveTest(unittest.TestCase):
    """`accounts list` dit qu'un actif au-dessus de sa pause est un choix
    partagé, pas une session retenue (#6958)."""

    @staticmethod
    def _row(name, *, active=False, ok=True, next_=False, forced=False,
             harness="claude"):
        return {"harness": harness, "account": name, "active": active,
                "forced": forced, "ok": ok, "problems": [], "gauges": [],
                "reason": "" if ok else "plan claude seven_day: 15% used",
                "next": next_, "why": "next choice" if next_ else ""}

    def test_actif_au_dessus_de_sa_pause_est_explique(self):
        rows = [self._row("tertiaire", active=True, ok=False),
                self._row("primaire", next_=True)]
        texte = accounts.format_rows(rows)
        self.assertIn("active = last shared choice", texte)
        self.assertIn("the next non-critical session switches", texte)
        self.assertIn("a critical agent chooses its own", texte)

    def test_pas_de_mention_quand_l_actif_est_sous_son_seuil(self):
        rows = [self._row("tertiaire", active=True, ok=True),
                self._row("primaire", next_=True)]
        self.assertNotIn("active = last shared choice",
                         accounts.format_rows(rows))

    def test_pas_de_mention_sans_autre_choix(self):
        rows = [self._row("tertiaire", active=True, ok=False)]
        self.assertNotIn("active = last shared choice",
                         accounts.format_rows(rows))

    def test_pas_de_mention_sur_un_forcage(self):
        rows = [self._row("tertiaire", active=True, ok=False, forced=True),
                self._row("primaire", next_=True)]
        self.assertNotIn("active = last shared choice",
                         accounts.format_rows(rows))


if __name__ == "__main__":
    unittest.main()
