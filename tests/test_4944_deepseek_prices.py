# SPDX-License-Identifier: AGPL-3.0-only
"""#4944: official DeepSeek prices checked 2026-10-30.

https://api-docs.deepseek.com/quick_start/pricing/
Flash unchanged; legacy Flash aliases bill Flash, V4-Pro is distinct.
"""
import datetime as dt
import unittest

from ameesh import cost


def utc(day, hour):
    return dt.datetime(2026, 10, day, hour, tzinfo=dt.timezone.utc).timestamp()


class DeepSeekPricesTest(unittest.TestCase):
    def test_official_models_and_each_billing_period(self):
        models = {
            "deepseek-flash": ([0.30, 0.006, 1.20], [0.15, 0.003, 0.60]),
            "deepseek-v4-flash": ([0.30, 0.006, 1.20], [0.15, 0.003, 0.60]),
            "deepseek-v4-flash-vision-exp": ([0.30, 0.006, 1.20], [0.15, 0.003, 0.60]),
            "deepseek-v4-pro": ([1.32, 0.044, 3.96], [0.66, 0.022, 1.98]),
        }
        prices = cost.load_prices(None)
        for model, (peak, off_peak) in models.items():
            with self.subTest(model=model):
                self.assertEqual(cost.known_price(prices, model), peak)
                for day, hour, expected in ((30, 2, None), (30, 12, off_peak),
                                            (31, 2, off_peak)):
                    self.assertEqual(cost.period_price(prices, model, utc(day, hour),
                                                       utc(day, hour) + 60), expected)
                # A caller override remains authoritative, including its cache price.
                custom = dict(prices, **{model: [9.0, 8.0, 7.0]})
                self.assertIsNone(cost.period_price(custom, model, utc(30, 12), utc(30, 13)))

    def test_unknown_model_majorant_covers_all_known_token_classes(self):
        prices = cost.load_prices(None)
        maximum = cost.max_price_of(prices, "deepseek")
        for model, triplet in prices.items():
            if model.startswith("deepseek-"):
                for rate, bound in zip(triplet, maximum):
                    self.assertLessEqual(rate, bound, model)
