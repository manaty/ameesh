# SPDX-License-Identifier: AGPL-3.0-only
"""#5026 : une configuration accounts illisible ne casse pas les lecteurs de soldes.

`balance.normalize_readings` valide la configuration avant d'appliquer une
adoption déclarée. Une configuration invalide doit rendre les lignes
inchangées (aucune adoption) et laisser continuer les lecteurs qui dégradent
déjà : alertes `balance_low`, rapport de coût `_real_spend_24h`, `progress`.
"""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from ameesh import accounts, balance


def config(*, adopt="yes"):
    return SimpleNamespace(accounts={"deepseek": [
        {"name": "shared", "type": "api_key_env", "key_env": "TEST_KEY",
         "adopts_unnamed": adopt}]})


def readings():
    return [dict(provider="deepseek", currency="USD", total=100.0,
                 observed_ts=0.0, account=None),
            dict(provider="deepseek", currency="USD", total=99.0,
                 observed_ts=3600.0, account=None)]


class UnreadableConfigTest(unittest.TestCase):
    def test_normalize_readings_returns_the_rows_unchanged(self):
        rows = readings()
        original = copy.deepcopy(rows)
        # la configuration est bien refusée par accounts.parse…
        with self.assertRaises(accounts.AccountError):
            accounts.parse(config().accounts)
        # …et les lecteurs de soldes ne la laissent pas remonter.
        self.assertEqual(balance.normalize_readings(rows, config()), original)
        self.assertEqual(rows, original)

    def test_spend_and_real_spend_continue_without_adoption(self):
        rows = readings()
        self.assertEqual(balance.spend(rows, cfg=config())["topups"], [])
        [entry] = balance.real_spend(rows, 0.0, 3600.0, cfg=config())
        self.assertEqual((entry["spent"], entry["accounts"]), (1.0, []))


if __name__ == "__main__":
    unittest.main()
