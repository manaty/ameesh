# SPDX-License-Identifier: AGPL-3.0-only
"""#3647 — rythme des comptes compilé du canon, relevé d'un forfait sous-utilisé.

Constat du 26/10 : le compte codex primaire n'avait aucun `pace_multiplier`
sur les VM et ×10 sur le poste, recopié à la main hôte par hôte. Le canon §11
donne ×2 au seul Codex primaire (voucher) ; la décision owner #141793 a
(signée le 26/10/2026 à 05:09Z) ajoute : quand `ameesh accounts` prévoit
qu'un forfait perdra plus de 50 % de son quota au reset, il relève lui-même le
rythme, sans dépasser le plafond fixe (jamais d'overage).

Témoins (auditeur, note du lot) : perte prévue > 50 % → rythme relevé, borné
au plafond ; ≤ 50 % → rythme du canon ; deux hôtes aux configurations locales
différentes → même rythme ; un multiplicateur local hors canon lève une alerte.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts, cost, exploitation, mechanics

HOUR = 3600.0
WEEK = 7 * 24 * HOUR
NOW = 1_800_000_000.0

BASE = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""

ACCOUNTS = """accounts:
  underused_loss: 0.5
  pace_multipliers:
    codex:
      primaire: 2
"""


def _document(block: str = ACCOUNTS) -> mechanics.Document:
    return mechanics.compile_document(BASE + block)


def _week(used: float, elapsed: float, key: str = "codex-10080min") -> cost.Gauge:
    return cost.Gauge("codex", key, used, NOW + (1.0 - elapsed) * WEEK, WEEK)


class _Book:
    db = None

    def __init__(self, gauges: dict, state_dir: str | None = None):
        self._gauges = gauges
        self.state_dir = state_dir

    def codex_gauges(self, sessions=None, *, plan=None):
        return self._gauges.get(sessions, [])

    def record_gauges(self, gauges, account=None):
        return 0


class _CanonCase(unittest.TestCase):
    def setUp(self) -> None:
        accounts._CANON_PACE.clear()
        self.addCleanup(accounts._CANON_PACE.clear)

    def _canon(self, document=None, error: Exception | None = None):
        patcher = mock.patch.object(
            mechanics, "configured",
            side_effect=error if error is not None else (lambda cfg: document))
        patcher.start()
        self.addCleanup(patcher.stop)


class CompileTest(unittest.TestCase):
    """Le bloc `accounts` du canon, lu strictement."""

    def test_block_compiles(self):
        rule = _document().accounts
        self.assertEqual(rule.multiplier("codex", "primaire"), 2.0)
        self.assertEqual(rule.multiplier("codex", "secondaire"), 1.0)
        self.assertEqual(rule.multiplier("claude", "primaire"), 1.0)
        self.assertEqual(rule.underused_loss, 0.5)
        self.assertEqual(_document().to_dict()["accounts"],
                         {"pace_multipliers": {"codex": {"primaire": 2.0}},
                          "underused_loss": 0.5})

    def test_absent_block_is_none(self):
        self.assertIsNone(_document("").accounts)
        self.assertNotIn("accounts", _document("").to_dict())

    def test_malformed_block_refused(self):
        for block in ("accounts:\n  pace_multipliers:\n    codex:\n      primaire: 0\n",
                      "accounts:\n  pace_multipliers:\n    codex:\n      primaire: true\n",
                      "accounts:\n  pace_multipliers:\n    codex:\n      primaire: x\n",
                      "accounts:\n  pace_multipliers:\n    codex: 2\n",
                      "accounts:\n  underused_loss: 1\n",
                      "accounts:\n  underused_loss: 0\n",
                      "accounts:\n  voucher: true\n",
                      "accounts: 2\n"):
            with self.assertRaises(mechanics.MechanicsError, msg=block):
                _document(block)


class SameRateOnEveryHostTest(_CanonCase):
    """Deux hôtes aux configurations locales différentes, même rythme."""

    VM = {"codex": [{"name": "primaire"}, {"name": "secondaire", "path": "/x/codex-2"}],
          "claude": [{"name": "primaire"}]}
    POSTE = {"codex": [{"name": "primaire", "pace_multiplier": 10},
                       {"name": "secondaire", "path": "/x/codex-2"}],
             "claude": [{"name": "primaire", "pace_multiplier": 10}]}

    def _rates(self, raw) -> dict:
        cfg = SimpleNamespace(accounts=raw, host="h")
        return {(h, p.name): (p.pace_multiplier, p.pace_source, p.underused_loss)
                for h, items in accounts.with_canon_pace(cfg, accounts.parse(raw)).items()
                for p in items}

    def test_vm_and_poste_compute_the_same_rate(self):
        self._canon(_document())
        vm, poste = self._rates(self.VM), self._rates(self.POSTE)
        self.assertEqual(vm, poste)
        self.assertEqual(vm[("codex", "primaire")], (2.0, "canon", 0.5))
        self.assertEqual(vm[("codex", "secondaire")], (1.0, "canon", 0.5))
        self.assertEqual(vm[("claude", "primaire")], (1.0, "canon", 0.5))

    def test_profiles_applies_the_canon(self):
        self._canon(_document())
        cfg = SimpleNamespace(accounts=self.POSTE, host="h", accounts_shared=None,
                              account_sharing=None)
        with mock.patch("ameesh.account_claims.mark_refused",
                        side_effect=lambda cfg, items, **kw: items):
            primary = accounts.profiles(cfg, "codex")[0]
        self.assertEqual((primary.pace_multiplier, primary.pace_local), (2.0, 10.0))

    def test_local_rate_off_canon_raises_an_alert(self):
        self._canon(_document())
        cfg = SimpleNamespace(accounts=self.POSTE, host="poste")
        alerts = exploitation._pace_local_alerts(cfg)
        self.assertEqual(sorted((a["harness"], a["account"]) for a in alerts),
                         [("claude", "primaire"), ("codex", "primaire")])
        codex = [a for a in alerts if a["harness"] == "codex"][0]
        self.assertEqual((codex["type"], codex["value"], codex["threshold"]),
                         ("account_pace_local", 10.0, 2.0))
        self.assertIn("ignored", codex["detail"])
        self.assertIn("account_pace_local", exploitation.ALERT_TYPES)
        # la VM, sans réglage local, n'a rien à signaler
        self.assertEqual(exploitation._pace_local_alerts(
            SimpleNamespace(accounts=self.VM, host="vm")), [])

    def test_without_canon_block_the_local_rate_applies_signalled(self):
        self._canon(_document(""))
        rates = self._rates(self.POSTE)
        self.assertEqual(rates[("codex", "primaire")], (10.0, "local", None))
        drift = accounts.pace_drift(SimpleNamespace(accounts=self.POSTE, host="h"))
        self.assertEqual(len(drift), 2)
        self.assertTrue(all("applied" in item["detail"] for item in drift))

    def test_unreadable_canon_keeps_the_local_rate_signalled(self):
        self._canon(error=mechanics.MechanicsError("no readable canon"))
        self.assertEqual(self._rates(self.POSTE)[("codex", "primaire")],
                         (10.0, "local", None))
        drift = accounts.pace_drift(SimpleNamespace(accounts=self.POSTE, host="h"))
        self.assertIn("no readable canon", drift[0]["detail"])


class UnderusedRaiseTest(unittest.TestCase):
    """Perte prévue > 50 % : rythme relevé, borné au plafond fixe."""

    def _profile(self, **settings) -> accounts.Profile:
        raw = {"name": "secondaire", "path": "/x/codex-2", "check_credentials": False}
        raw.update(settings)
        profile = accounts.parse({"codex": [raw]})["codex"][0]
        return accounts.replace(profile, pace_source="canon", underused_loss=0.5)

    def test_loss_above_threshold_raises_the_pace(self):
        # 26/10 : 15 % utilisés pour 13 % écoulés, 85 % perdus au reset
        profile, gauge = self._profile(), _week(0.15, 0.13)
        limits = accounts.limits(profile, [gauge], NOW)
        self.assertAlmostEqual(limits[gauge.key][0], 0.5)
        self.assertEqual(limits[gauge.key][1], "pace cap")
        # la réserve des non-critiques s'applique toujours par-dessus
        self.assertAlmostEqual(
            accounts.limits(profile, [gauge], NOW, reserve=True)[gauge.key][0], 0.4)

    def test_raise_bounded_by_the_fixed_cap(self):
        profile, gauge = self._profile(cap=0.3), _week(0.15, 0.13)
        self.assertEqual(accounts.limits(profile, [gauge], NOW)[gauge.key],
                         (0.3, "account cap"))
        overage = self._profile(overage="allowed", overage_cap=0.45)
        self.assertEqual(accounts.limits(overage, [gauge], NOW)[gauge.key],
                         (0.45, "overage cap"))

    def test_raise_never_above_the_pace_ceiling(self):
        profile = self._profile(pace_ceiling=0.4)
        gauge = _week(0.15, 0.13)
        self.assertAlmostEqual(accounts.limits(profile, [gauge], NOW)[gauge.key][0], 0.4)

    def test_loss_at_or_below_threshold_keeps_the_canon_pace(self):
        profile = self._profile()
        for used, elapsed in ((0.50, 0.30), (0.60, 0.30)):
            gauge = _week(used, elapsed)
            self.assertIsNone(accounts.underused_floor(profile, [gauge], gauge, NOW))
            self.assertAlmostEqual(accounts.limits(profile, [gauge], NOW)[gauge.key][0],
                                   gauge.pace_cap(NOW))

    def test_no_threshold_no_raise(self):
        profile = accounts.replace(self._profile(), underused_loss=None)
        gauge = _week(0.15, 0.13)
        self.assertAlmostEqual(accounts.limits(profile, [gauge], NOW)[gauge.key][0], 0.23)

    def test_stale_or_undated_reading_no_raise(self):
        profile = self._profile()
        undated = cost.Gauge("codex", "codex-10080min", 0.1, None, WEEK)
        self.assertIsNone(accounts.underused_floor(profile, [undated], undated, NOW))
        passed = cost.Gauge("codex", "codex-300min", 0.1, NOW - 1, 5 * HOUR)
        week = _week(0.15, 0.13)
        self.assertIsNone(accounts.underused_floor(profile, [passed, week], week, NOW))

    def test_same_reading_same_rate_whatever_the_local_setting(self):
        gauge = _week(0.15, 0.13)
        a = accounts.replace(self._profile(), pace_multiplier=2.0)
        b = accounts.replace(self._profile(pace_multiplier=10), pace_multiplier=2.0)
        self.assertEqual(accounts.limits(a, [gauge], NOW), accounts.limits(b, [gauge], NOW))


class EvaluateAndJournalTest(unittest.TestCase):
    """Le choix relâche le compte, et le relèvement est journalisé une fois."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3647-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        path = os.path.join(self.tmp, "codex-1")
        os.makedirs(path, mode=0o700)
        raw = {"name": "primaire", "path": path, "check_credentials": False}
        self.profile = accounts.replace(accounts.parse({"codex": [raw]})["codex"][0],
                                        pace_source="canon", underused_loss=0.5)
        self.gauge = _week(0.15, 0.13)
        self.book = _Book({os.path.join(path, "sessions"): [self.gauge]}, self.tmp)

    def _journal(self) -> list[dict]:
        path = os.path.join(self.tmp, accounts.RAISES_JOURNAL)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_held_without_raise_free_with_it(self):
        nominal = accounts.replace(self.profile, underused_loss=None)
        held = accounts.evaluate(self.book, nominal, [nominal], NOW, reserve=True)
        self.assertFalse(held.ok)
        raised = accounts.evaluate(self.book, self.profile, [self.profile], NOW,
                                   reserve=True)
        self.assertTrue(raised.ok, raised.reason)
        self.assertAlmostEqual(raised.raises[self.gauge.key], 0.85)

    def test_reason_names_the_raise_when_still_held(self):
        gauge = _week(0.45, 0.13)
        book = _Book({os.path.join(self.profile.path, "sessions"): [gauge]}, self.tmp)
        ev = accounts.evaluate(book, self.profile, [self.profile], NOW, reserve=True,
                               record=False)
        self.assertFalse(ev.ok)
        self.assertIn("pace raised: forecast loss at reset 55% > 50%", ev.reason)

    def test_raise_journaled_once_with_the_gauges(self):
        for _ in range(3):
            accounts.evaluate(self.book, self.profile, [self.profile], NOW, reserve=True)
        lines = self._journal()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual((line["harness"], line["account"], line["key"]),
                         ("codex", "primaire", "codex-10080min"))
        self.assertEqual((line["lost"], line["threshold"], line["floor"]), (0.85, 0.5, 0.5))
        self.assertEqual(line["gauges"][0]["used"], 0.15)
        self.assertEqual(line["pace_source"], "canon")

    def test_display_does_not_journal(self):
        accounts.evaluate(self.book, self.profile, [self.profile], NOW, record=False)
        self.assertEqual(self._journal(), [])

    def test_new_window_journaled_again(self):
        accounts.evaluate(self.book, self.profile, [self.profile], NOW)
        later = NOW + WEEK
        gauge = cost.Gauge("codex", "codex-10080min", 0.05, later + 0.9 * WEEK, WEEK)
        book = _Book({os.path.join(self.profile.path, "sessions"): [gauge]}, self.tmp)
        accounts.evaluate(book, self.profile, [self.profile], later)
        self.assertEqual(len(self._journal()), 2)


if __name__ == "__main__":
    unittest.main()
