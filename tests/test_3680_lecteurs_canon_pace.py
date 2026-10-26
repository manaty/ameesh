# SPDX-License-Identifier: AGPL-3.0-only
"""#3680 — les lecteurs du rythme lisent celui de la garde (canon).

Avis N d'ameesh-dev3 (verdict #19857) sur le gel #19828 de #3647 : `ameesh
cost over` (`mesh_cli`), la marge de `ameesh accounts list` (`rebalance`) et
`cost report` / `progress.plans` lisaient encore la configuration locale de
l'hôte (`pace_multiplier` ×10, pas de plancher de sous-utilisation).

Témoin : secondaire local ×10, canon ×1 avec plancher 0.5, écoulé 13 %,
perte 85 % → plafond 0.9 au lieu de 0.5. Correctif : router ces lecteurs par
`accounts.with_canon_pace` et afficher le plafond de la garde
(`accounts.report`, `gauges[].cap`, plancher compris).
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts, cost, mechanics, mesh_cli, progress, rebalance

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

#: le canon de #3647 : Codex primaire ×1 (le ×2 du voucher n'est pas le
#: sujet ici), fenêtre sous-utilisée relevée au plancher 0.5.
CANON = """accounts:
  underused_loss: 0.5
  pace_multipliers:
    codex:
      primaire: 1
"""


def _document(block: str = CANON) -> mechanics.Document:
    return mechanics.compile_document(BASE + block)


def _week(used: float, elapsed: float = 0.13, key: str = "week") -> cost.Gauge:
    return cost.Gauge("codex", key, used, NOW + (1.0 - elapsed) * WEEK, WEEK)


class _Book:
    """Le livre minimal d'`accounts.evaluate` : les jauges d'un dossier."""

    db = None

    def __init__(self, gauges: dict | None = None):
        self._gauges = gauges or {}
        self.state_dir = None

    def codex_gauges(self, sessions=None, *, plan=None):
        return self._gauges.get(sessions, [])

    def record_gauges(self, gauges, account=None):
        return 0

    def tool_of(self, agent):
        return "codex"

    def over(self, agent, **kwargs):
        return ""

    def report(self, accounts=None, **kwargs):
        return []


class _CanonCase(unittest.TestCase):
    """Un dossier de compte Codex et le canon substitué (jamais celui de l'hôte)."""

    def setUp(self) -> None:
        accounts._CANON_PACE.clear()
        self.addCleanup(accounts._CANON_PACE.clear)
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3680-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "codex-2")
        os.makedirs(self.home, mode=0o700)
        self.gauge = _week(0.15)
        self.book = _Book({os.path.join(self.home, "sessions"): [self.gauge]})

    def _canon(self, document=None, error: Exception | None = None):
        patcher = mock.patch.object(
            mechanics, "configured",
            side_effect=error if error is not None else (lambda cfg: document))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _comptes(self, pace_multiplier=None) -> dict:
        raw = {"name": "secondaire", "path": self.home,
               "check_credentials": False}
        if pace_multiplier is not None:
            raw["pace_multiplier"] = pace_multiplier
        return {"codex": [raw]}


class CostOverTest(_CanonCase):
    """`ameesh cost over` : le verdict de la garde, pas le réglage local."""

    def _over(self, gauge: cost.Gauge, comptes: dict) -> tuple[int, str]:
        self.book = _Book({os.path.join(self.home, "sessions"): [gauge]})
        cfg = SimpleNamespace(accounts=comptes, host="poste", state_dir=self.tmp)
        args = SimpleNamespace(cost_command="over", agent="cx1")
        limites = SimpleNamespace(book_kwargs=lambda: {})
        out = io.StringIO()
        fake_db = SimpleNamespace(close=lambda: None)
        with mock.patch.object(mesh_cli, "_open", return_value=fake_db), \
                mock.patch("ameesh.budget.current", return_value=limites), \
                mock.patch.object(mesh_cli.time, "time", return_value=NOW), \
                mock.patch.object(mesh_cli.cost_mod, "CostBook",
                                  return_value=self.book), \
                redirect_stdout(out):
            code = mesh_cli.cmd_cost(cfg, args)
        return code, out.getvalue()

    def test_local_rate_says_not_over_where_the_canon_holds_the_account(self):
        # 55 % utilisés, 13 % écoulés : le ×10 local vise 0.9 (non dépassé),
        # le canon ×1 vise 0.23 (dépassé), même lecture.
        gauge = _week(0.55)
        self._canon(_document())
        code, out = self._over(gauge, self._comptes(pace_multiplier=10))
        self.assertEqual(code, 0, out)          # dépassé, comme la garde
        self.assertIn("all codex accounts at the threshold", out)
        # sans le canon, le réglage local appliqué : non dépassé
        self._canon(_document(""))
        code, out = self._over(gauge, self._comptes(pace_multiplier=10))
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")

    def test_the_underused_floor_is_applied(self):
        # 30 % utilisés, 70 % perdus au reset : la garde relève le plafond à
        # 0.5 et ne tient pas le compte ; sans plancher, 0.23 le tiendrait.
        gauge = _week(0.30)
        self._canon(_document())
        code, out = self._over(gauge, self._comptes())
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")
        # même canon, plancher absent : le rythme nominal reprend la main
        self._canon(_document("""accounts:
  pace_multipliers:
    codex:
      primaire: 1
"""))
        code, out = self._over(gauge, self._comptes())
        self.assertEqual(code, 0, out)
        self.assertIn("pace cap 23%", out)


class RebalanceMarginTest(_CanonCase):
    """La marge annoncée par `accounts list` lit le rythme du canon."""

    def _proposal(self, comptes: dict):
        cfg = SimpleNamespace(accounts=comptes, host="poste", state_dir=self.tmp,
                              critical_agents=())
        fleet = {"cx1": "codex", "d1": "deepseek", "d2": "deepseek",
                 "d3": "deepseek"}
        with mock.patch("ameesh.cost.paid_harnesses_of", return_value={"deepseek"}), \
                mock.patch("ameesh.auditor.load_all", return_value=[]), \
                mock.patch("ameesh.auditor.is_critical", return_value=False), \
                mock.patch("ameesh.sous_utilisation.declared_orchestrators",
                           return_value=()):
            return rebalance.proposal(cfg, object(), self.book, now=NOW, fleet=fleet)

    def test_the_margin_is_not_overestimated_by_the_local_rate(self):
        # 15 % utilisés, 13 % écoulés, 85 % perdus au reset.
        self._canon(_document())
        canon = self._proposal(self._comptes(pace_multiplier=10))
        self.assertEqual(canon.reason, "")
        offer = canon.offers[0]
        self.assertEqual((offer.harness, offer.accounts), ("codex", ("secondaire",)))
        # plafond de la garde 0.5, réserve non-critique 0.1, 0.15 utilisés
        self.assertAlmostEqual(offer.free, 0.25)
        # un seul agent Codex sous bail : l'unité observée vaut 0.15
        self.assertEqual(offer.capacity, 1)
        # sans le canon, le ×10 local gonfle la marge : 0.9 − 0.1 − 0.15
        self._canon(_document(""))
        local = self._proposal(self._comptes(pace_multiplier=10))
        self.assertAlmostEqual(local.offers[0].free, 0.65)
        self.assertEqual(local.offers[0].capacity, 4)
        self.assertLess(offer.free, local.offers[0].free)


class CostReportCapTest(unittest.TestCase):
    """`cost report` affiche le plafond de la garde quand `accounts` le donne."""

    def test_the_guard_cap_wins_over_the_recomputed_pace(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-3680-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "a1"))
        gauge = _week(0.30)
        book = cost.CostBook(state_dir=tmp, db=None, tools={"a1": "codex"},
                             clock=lambda: NOW)
        rows = book.report(accounts={"codex": ("secondaire", [gauge])},
                           multipliers={"codex": 1.0},
                           caps={"codex": {"week": 0.5}})
        self.assertEqual((rows[0]["account"], rows[0]["harness"]),
                         ("secondaire", "codex"))
        self.assertAlmostEqual(rows[0]["gauges"][0]["cap"], 0.5)
        # sans plafond de la garde, le rythme seul : ni plancher ni plafond
        rows = book.report(accounts={"codex": ("secondaire", [gauge])},
                           multipliers={"codex": 1.0})
        self.assertAlmostEqual(rows[0]["gauges"][0]["cap"], 0.23)

    def test_cost_report_reads_pass_the_guard_caps(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-3680-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = SimpleNamespace(accounts={}, host="poste", state_dir=tmp)
        row = {"harness": "codex", "account": "secondaire", "active": True,
               "pace_multiplier": 1.0, "_gauges": [_week(0.30)],
               "gauges": [{"key": "week", "cap": 0.5, "pace_ceiling": 0.9}],
               "_limits": {}, "_ceilings": {}}
        book = mock.Mock()
        with mock.patch("ameesh.budget.current",
                        return_value=SimpleNamespace(book_kwargs=lambda: {})), \
                mock.patch.object(mesh_cli.cost_mod, "CostBook", return_value=book), \
                mock.patch("ameesh.accounts.report", return_value=[row]), \
                mock.patch.object(mesh_cli, "_real_spend_24h", return_value=[]):
            mesh_cli._cost_report_reads(cfg, object())
        kwargs = book.report.call_args.kwargs
        self.assertEqual(kwargs["caps"], {"codex": {"week": 0.5}})
        self.assertEqual(kwargs["accounts"]["codex"][0], "secondaire")


class ProgressPlanTest(_CanonCase):
    """`progress.plans` : le plafond et le verdict de la garde."""

    def _row(self, active: bool, cap: float) -> dict:
        gauge = {"key": "week", "pace_ceiling": 0.9, "used": 0.30,
                 "last_used": 0.30, "elapsed": 0.13, "resets_at": NOW + 0.87 * WEEK,
                 "reset_passed": False, "read_at": None, "cap": cap}
        return {"harness": "codex", "account": "secondaire", "active": active,
                "pace_multiplier": 1.0, "gauges": [gauge],
                "_gauges": [self.gauge]}

    def test_plan_of_the_active_account_uses_the_guard_cap(self):
        # 30 % utilisés, 70 % perdus : nominal 0.23 (dépassé), garde 0.5.
        self.gauge = _week(0.30)
        budget = progress.build_budget([], [self.gauge], NOW,
                                       accounts=[self._row(True, 0.5)])
        self.assertEqual(len(budget["plans"]), 1)
        plan = budget["plans"][0]
        self.assertAlmostEqual(plan["pace_cap"], 0.5)
        self.assertFalse(plan["exceeded"])
        # sans compte déclaré (lecteur d'avant), le rythme nominal : 0.23
        budget = progress.build_budget([], [self.gauge], NOW)
        plan = budget["plans"][0]
        self.assertAlmostEqual(plan["pace_cap"], 0.23)
        self.assertTrue(plan["exceeded"])

    def test_plan_of_an_undeclared_harness_keeps_the_single_account_guard_cap(self):
        # harnais sans compte actif : la jauge du dossier par défaut reçoit le
        # réglage du seul compte déclaré qui porte la fenêtre, plafond compris.
        self.gauge = _week(0.30)
        budget = progress.build_budget([], [self.gauge], NOW,
                                       accounts=[self._row(False, 0.5)])
        plan = budget["plans"][0]
        self.assertAlmostEqual(plan["pace_cap"], 0.5)
        self.assertFalse(plan["exceeded"])


if __name__ == "__main__":
    unittest.main()
