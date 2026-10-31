# SPDX-License-Identifier: AGPL-3.0-only
"""#4944: explicit ownership of historical unnamed balances, never inferred."""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts, balance, exploitation, mesh_cli, progress, sous_utilisation


def config(*, adopt=True, other=False):
    profiles = [{"name": "shared", "type": "api_key_env", "key_env": "TEST_KEY",
                 "adopts_unnamed": adopt}]
    if other:
        profiles.append({"name": "other", "type": "api_key_env", "key_env": "OTHER_KEY"})
    return SimpleNamespace(accounts={"deepseek": profiles})


def reading(total, at, account=None, **extra):
    return dict(provider="deepseek", currency="USD", total=total,
                observed_ts=at, account=account, **extra)


def readings():
    return [reading(100, 0), reading(99.5, 900, "shared"),
            reading(98, 1800), reading(97, 3600, "shared")]


class BalanceIdentityTest(unittest.TestCase):
    def test_declared_identity_merges_interleaved_history_without_mutation(self):
        rows = readings()
        original = copy.deepcopy(rows)
        [entry] = balance.real_spend(reversed(rows), 0, 3600, cfg=config())
        self.assertEqual(entry["spent"], 3)
        self.assertEqual(entry["accounts"], ["shared"])
        self.assertEqual(entry["readings"], 4)
        self.assertEqual(rows, original)

    def test_no_declaration_preserves_separate_accounts_even_with_equal_balances(self):
        rows = [reading(100, 0), reading(100, 0, "shared"),
                reading(90, 3600), reading(90, 3600, "shared")]
        for cfg in (None, config(adopt=False)):
            [entry] = balance.real_spend(rows, 0, 3600, cfg=cfg)
            self.assertEqual(entry["spent"], 20)

    def test_truly_distinct_account_still_adds_after_adoption(self):
        rows = readings() + [reading(100, 0, "other"), reading(95, 3600, "other")]
        [entry] = balance.real_spend(rows, 0, 3600, cfg=config(other=True))
        self.assertEqual(entry["spent"], 8)
        self.assertEqual(entry["accounts"], ["other", "shared"])

    def test_adoption_is_scoped_to_provider_and_currency(self):
        rows = readings() + [dict(reading(50, 0), provider="another"),
                             dict(reading(40, 3600), provider="another"),
                             dict(reading(25, 0), currency="EUR"),
                             dict(reading(20, 3600, "shared"), currency="EUR")]
        result = {(r["provider"], r["currency"]): r["spent"]
                  for r in balance.real_spend(rows, 0, 3600, cfg=config())}
        self.assertEqual(result, {("deepseek", "USD"): 3, ("deepseek", "EUR"): 5,
                                  ("another", "USD"): 10})

    def test_topups_and_interval_boundary_use_merged_chronology(self):
        rows = readings() + [reading(110, 4000), reading(108, 5000, "shared")]
        [entry] = balance.real_spend(rows, 2000, 4500, cfg=config())
        self.assertEqual((entry["spent"], entry["topups"]), (1, 13))
        self.assertEqual((entry["start_ts"], entry["end_ts"]), (1800, 4000))

    def test_daily_hourly_latest_and_topups_share_identity(self):
        rows = readings() + [reading(110, 4000)]
        report = balance.spend(rows, cfg=config())
        self.assertEqual(sum(r["spent"] for r in report["daily"]), 3)
        self.assertEqual(sum(r["spent"] for r in report["hourly"]), 3)
        self.assertEqual(len(report["latest"]), 1)
        self.assertEqual(report["topups"][0]["amount"], 13)
        self.assertEqual(report["latest"][0]["account"], "shared")

    def test_compare_and_budget_alert_receive_configuration(self):
        store = SimpleNamespace(operations=SimpleNamespace(balances=mock.Mock(return_value=readings())),
                                turn_costs=SimpleNamespace(spent_between=mock.Mock(return_value=3)))
        with mock.patch("ameesh.storage.of", return_value=store):
            [raw] = balance.compare(None, ["deepseek"], 0, 3600)
            [fixed] = balance.compare(None, ["deepseek"], 0, 3600, cfg=config())
            self.assertTrue(raw["alert"])
            self.assertFalse(fixed["alert"])
            self.assertEqual(fixed["real_spent"], 3)
            self.assertEqual(exploitation._budget_drift(None, 3600, paid=["deepseek"], cfg=config()), [])
            self.assertTrue(exploitation._budget_drift(None, 3600, paid=["deepseek"]))

    def test_balance_low_produces_one_forecast(self):
        store = SimpleNamespace(operations=SimpleNamespace(balances=mock.Mock(return_value=readings())))
        with mock.patch("ameesh.storage.of", return_value=store):
            alerts = sous_utilisation.balance_low(None, [], 3600, hours=0, min_usd=200, cfg=config())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["account"], "shared")
        self.assertEqual(alerts[0]["rate_per_h"], 3)

    def test_cost_progress_and_underuse_use_the_supplied_config(self):
        cfg = config()
        store = SimpleNamespace(operations=SimpleNamespace(balances=mock.Mock(return_value=readings())),
                                turn_costs=SimpleNamespace(spent_between=mock.Mock(return_value=3)))
        with mock.patch("ameesh.storage.of", return_value=store), \
                mock.patch("ameesh.cost.paid_harnesses_of", return_value=("deepseek",)), \
                mock.patch("time.time", return_value=3600):
            self.assertEqual(mesh_cli._real_spend_24h(None, cfg=cfg)[0]["real_spent"], 3)
            self.assertTrue(all(r["real_spent"] == 3 for r in progress._real_spend(
                None, ["deepseek"], 0, 3600, cfg=cfg)))
            self.assertEqual(sous_utilisation._paid_spend(None, ["deepseek"], cfg=cfg), (3, 0))

    def test_configured_collector_writes_declared_name(self):
        [(source, name)] = balance.account_sources(config(), env={"TEST_KEY": "fake"})
        self.assertEqual(name, "shared")
        self.assertTrue(source.configured())


class AdoptionValidationTest(unittest.TestCase):
    def test_adoption_is_opt_in(self):
        raw = config().accounts
        del raw["deepseek"][0]["adopts_unnamed"]
        self.assertFalse(accounts.parse(raw)["deepseek"][0].adopts_unnamed)

    def test_non_boolean_flag_is_rejected(self):
        for invalid in ("false", "true", 1, None):
            raw = config().accounts
            raw["deepseek"][0]["adopts_unnamed"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(accounts.AccountError):
                accounts.parse(raw)

    def test_two_owners_are_rejected(self):
        raw = config(other=True).accounts
        raw["deepseek"][1]["adopts_unnamed"] = True
        with self.assertRaises(accounts.AccountError):
            accounts.parse(raw)

    def test_config_directory_cannot_adopt_api_balances(self):
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"codex": [{"name": "one", "adopts_unnamed": True}]})
