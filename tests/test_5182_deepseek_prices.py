# SPDX-License-Identifier: AGPL-3.0-only
"""#5182 : le barème DeepSeek relu le 2026-11-01 sur
https://api-docs.deepseek.com/quick_start/pricing, le repli d'un modèle inconnu,
et `budget_drift` qui nomme les séries de soldes additionnées."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import balance, cost, exploitation


def utc(*args) -> float:
    return dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp()


#: lundi à 02:00 UTC, en pointe ; le même lundi à midi, et un samedi, creux
POINTE = utc(2026, 11, 2, 2)
CREUX = utc(2026, 11, 2, 12)
SAMEDI = utc(2026, 11, 7, 2)
#: prix par million lus sur la page : (entrée manquée, cache, sortie)
PAGE = {"deepseek-flash": ((0.30, 0.006, 1.20), (0.15, 0.003, 0.60)),
        "deepseek-v4-flash": ((0.30, 0.006, 1.20), (0.15, 0.003, 0.60)),
        "deepseek-v4-flash-vision-exp": ((0.30, 0.006, 1.20), (0.15, 0.003, 0.60)),
        "deepseek-v4-pro": ((1.32, 0.044, 3.96), (0.66, 0.022, 1.98))}
#: un appel type : 1 M manqué, 10 M en cache, 100 000 en sortie
APPEL = (1.0, 10.0, 0.1)


def facture(triplet) -> float:
    return sum(n * p for n, p in zip(APPEL, triplet))


class BaremeTest(unittest.TestCase):
    def setUp(self):
        self.prices = cost.load_prices(None)

    def appel(self, model, at):
        triplet = cost.period_price(self.prices, model, at, at + 60) \
            or cost.known_price(self.prices, model)
        return facture(triplet)

    def test_appel_type_de_chaque_modele_egale_la_page(self):
        for model, (pointe, creux) in PAGE.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(self.appel(model, POINTE), facture(pointe))
                self.assertAlmostEqual(self.appel(model, CREUX), facture(creux))
                self.assertAlmostEqual(self.appel(model, SAMEDI), facture(creux))

    def test_modele_inconnu_jamais_au_prix_le_plus_bas(self):
        plus_cher = [max(p[0][i] for p in PAGE.values()) for i in range(3)]
        for model in ("deepseek-v9-inconnu", None, ""):
            with self.subTest(model=model):
                self.assertEqual(cost.price_of(self.prices, "deepseek", model), plus_cher)
                self.assertEqual(cost.price_for(self.prices, "deepseek", model)[0], plus_cher)
                self.assertIsNone(cost.period_price(self.prices, model, CREUX, CREUX + 60))

    def test_famille_sans_tarif_garde_le_repli_du_harnais(self):
        prices = {"default-deepseek": [1.0, 0.1, 2.0]}
        self.assertEqual(cost.price_of(prices, "deepseek", "x"), [1.0, 0.1, 2.0])


def reading(total, at, account=None):
    return dict(provider="deepseek", currency="USD", total=total,
                observed_ts=at, account=account)


class SeriesTest(unittest.TestCase):
    """Le constat du 31/10 : un même solde relevé sous deux identités."""

    NOW = 3700.0
    ROWS = [reading(100, 0), reading(100, 1, "ambiant"),
            reading(90, 3600), reading(90, 3601, "ambiant")]

    def drift(self, cfg):
        with mock.patch.object(exploitation.storage, "of") as of:
            of.return_value.operations.balances.return_value = self.ROWS
            of.return_value.turn_costs.spent_between.return_value = 10.0
            return exploitation._budget_drift(object(), self.NOW, paid=("deepseek",), cfg=cfg)

    def test_real_spend_compte_les_series(self):
        [entry] = balance.real_spend(self.ROWS, 0, 3601)
        self.assertEqual((entry["spent"], entry["series"]), (20, 2))

    def test_deux_series_sont_nommees_avant_le_bareme(self):
        [alert] = self.drift(None)
        self.assertIn("2 balance series added up", alert["detail"])
        self.assertIn("adopts_unnamed", alert["detail"])

    def test_identite_declaree_fait_retomber_l_alerte(self):
        cfg = SimpleNamespace(accounts={"deepseek": [
            {"name": "ambiant", "type": "api_key_env", "key_env": "TEST_KEY",
             "adopts_unnamed": True}]})
        self.assertEqual(self.drift(cfg), [])


if __name__ == "__main__":
    unittest.main()
