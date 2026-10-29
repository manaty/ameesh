# SPDX-License-Identifier: AGPL-3.0-only
"""#4166 — provider account `low`: declared prepaid, projected balance, 20 %/5 %
thresholds or a projected exhaustion under 24 h, one `provider_credits_low`
alert per episode, and the per-harness fallback account.

Suite of #4164 (manaty/ameesh#3181): #4164 stops granting turns on an exhausted
account, alerts once and probes. This lot warns before, and lets
`accounts.choose` move an agent to another declared account of the same harness
instead of waiting for the exhausted one.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import (accounts, balance, budget, cost, exploitation,
                    provider_credits, registry, storage)

from .support import FAKEBIN
from .test_l30_accounts import _Base

OPENAI_EXHAUSTED = ("429 insufficient_quota: You exceeded your current quota, please "
                    "check your plan and billing details. credit_balance_exhausted")


class _LowBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM provider_account_credits")

    # -- fixtures -----------------------------------------------------------
    def _declare(self, usd=100.0, *, harness="codex", account="primaire",
                 actor="human:test"):
        provider_credits.declare(self.db, harness, account, usd, actor=actor)
        return provider_credits.get(self.db, harness, account)

    def _prepaid_at(self, seconds_ago, *, harness="codex", account="primaire"):
        self.db.execute(
            "update provider_account_credits set prepaid_at = now()"
            " - make_interval(secs => %s) where harness = %s and account = %s",
            (float(seconds_ago), harness, account))

    def _spend(self, usd, *, harness="codex", account="primaire", seconds_ago=0.0):
        self.db.execute(
            "insert into turn_costs (agent, harness, turn, model, session, usd,"
            " input_tokens, cached_input_tokens, output_tokens, account, recorded_at)"
            " values ('test', %s, null, 'm', null, %s, 0, 0, 0, %s,"
            " now() - make_interval(secs => %s))",
            (harness, float(usd), account, float(seconds_ago)))

    def _reading(self, total, *, currency="USD", available=True,
                 seconds_ago=0.0, provider="deepseek", account=None):
        self.db.execute(
            "insert into provider_balances (provider, currency, total, granted,"
            " topped_up, available, account, observed_at)"
            " values (%s, %s, %s, null, null, %s, %s,"
            " now() - make_interval(secs => %s))",
            (provider, currency, float(total), available, account, float(seconds_ago)))

    def _row(self, harness="codex", account="primaire"):
        return provider_credits.get(self.db, harness, account)

    def _alerts(self, kind):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == kind]

    def _assess(self):
        return provider_credits.assess(self.db)

    # -- a small fake balance source (the DeepSeek path through `record`) ----
    class _Source(balance.BalanceSource):
        provider = "deepseek"

        def __init__(self, readings):
            self.readings = readings
            super().__init__(env={})

        def configured(self):
            return True

        def fetch(self):
            return self.readings


class ProjectionTest(_LowBase):

    def test_declared_prepaid_projects_the_balance_and_alerts_once(self):
        self.assertIsNone(self._row())
        self._declare(100.0)
        self._prepaid_at(3600)
        row = self._row()
        self.assertEqual((row["state"], row["prepaid_usd"]), ("ok", 100.0))
        self._spend(85.0)
        self._assess()
        row = self._row()
        self.assertEqual(row["state"], "low")
        self.assertAlmostEqual(row["balance_usd"], 15.0, places=3)
        self.assertAlmostEqual(row["ratio"], 0.15, places=3)
        low = self._alerts("provider_credits_low")
        self.assertEqual(len(low), 1)
        alerte = low[0]
        self.assertEqual((alerte["harness"], alerte["account"], alerte["reason"],
                          alerte["level"], alerte["targets"]),
                         ("codex", "primaire", provider_credits.REASON_LOW,
                          "low", ["owner"]))
        self.assertFalse(alerte["urgent"])
        cle = exploitation.alert_key(alerte)
        self.assertEqual(alerte["prepaid_usd"], 100.0)

        # more spend inside the episode: the numbers move, the alert does not
        self._spend(5.0)
        self._assess()
        self.assertAlmostEqual(self._row()["ratio"], 0.10, places=3)
        self.assertEqual([exploitation.alert_key(a) for a in
                          self._alerts("provider_credits_low")], [cle])

        # 5 %: critical tier, the same single episode
        self._spend(5.0)
        self._assess()
        low = self._alerts("provider_credits_low")
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["level"], "critical")
        self.assertTrue(low[0]["urgent"])
        self.assertEqual(exploitation.alert_key(low[0]), cle)

        # the projection reaches zero: exhausted, low alert resolves
        self._spend(10.0)
        self._assess()
        self.assertEqual(self._row()["state"], "exhausted")
        self.assertEqual(self._alerts("provider_credits_low"), [])
        self.assertEqual(len(self._alerts("provider_credits_exhausted")), 1)

        # a top-up declaration does not bypass the probe (#4164): exhausted
        self._declare(200.0)
        self._assess()
        self.assertEqual(self._row()["state"], "exhausted")
        self.assertEqual(len(self._alerts("provider_credits_exhausted")), 1)

        # the probe succeeds: ok, no alert
        provider_credits.mark_ok(self.db, "codex", "primaire", source="probe",
                                 balance=200.0, ratio=1.0)
        self._assess()
        self.assertEqual(self._row()["state"], "ok")
        self.assertEqual(self._alerts("provider_credits_low"), [])
        self.assertEqual(self._alerts("provider_credits_exhausted"), [])

    def test_runway_under_24h_is_low_despite_a_high_ratio(self):
        self._declare(100.0)
        self._prepaid_at(7200)          # declared two hours ago
        self._spend(10.0, seconds_ago=3600)
        self._assess()
        row = self._row()
        self.assertEqual(row["state"], "low")
        self.assertGreater(row["ratio"], 0.85)       # 90 % left...
        self.assertIsNotNone(row["runway_ts"])
        self.assertLess(row["runway_ts"] - time.time(), 24 * 3600)
        alerte = self._alerts("provider_credits_low")[0]
        self.assertEqual(alerte["level"], "low")     # the ratio alone is sane
        self.assertIn("projected empty", alerte["detail"])

    def test_a_withdrawn_declaration_clears_a_projection_low(self):
        self._declare(100.0)
        self._prepaid_at(3600)
        self._spend(99.0)
        self._assess()
        self.assertEqual(self._row()["state"], "low")
        provider_credits.clear(self.db, "codex", "primaire", actor="human:test")
        self._assess()
        self.assertEqual(self._row()["state"], "ok")
        self.assertEqual(self._alerts("provider_credits_low"), [])

    def test_no_projection_without_a_declaration(self):
        # #4166 is opt-in: without a declared amount (and without a balance
        # API) no low state is invented from the ledger.
        self._spend(99.0)
        self._assess()
        self.assertIsNone(self._row())


class RealBalanceTest(_LowBase):

    def _poll(self, total, *, available=True):
        """Le relevé réel (ce que fait `balance.record` après l'écriture)."""
        return balance.credit_state(
            self.db, "deepseek", "",
            [balance.BalanceReading("deepseek", "USD", total, available=available)])

    def test_real_balance_uses_its_peak_and_alerts_once(self):
        self._reading(100.0, seconds_ago=10800)
        self._reading(15.0, seconds_ago=3600)
        self._poll(15.0)
        row = self._row("deepseek", "")
        self.assertEqual(row["state"], "low")
        self.assertAlmostEqual(row["ratio"], 0.15, places=3)
        low = self._alerts("provider_credits_low")
        self.assertEqual(len(low), 1)
        cle = exploitation.alert_key(low[0])

        self._reading(4.0)
        self._poll(4.0)
        low = self._alerts("provider_credits_low")
        self.assertEqual((len(low), low[0]["level"], low[0]["urgent"]),
                         (1, "critical", True))
        self.assertEqual(exploitation.alert_key(low[0]), cle)

        # unavailable at zero: exhausted, one alert
        self._reading(0.0, available=False)
        self._poll(0.0, available=False)
        self.assertEqual(self._row("deepseek", "")["state"], "exhausted")
        self.assertEqual(self._alerts("provider_credits_low"), [])
        self.assertEqual(len(self._alerts("provider_credits_exhausted")), 1)

        # a real reading (the poller) is the proof that reopens it; a large
        # top-up leaves more than 24 h of runway at the measured rate
        self._reading(2000.0)
        # False is the recovered transition (True would be a new exhaustion)
        self.assertIs(self._poll(2000.0), False)
        self._assess()
        self.assertEqual(self._row("deepseek", "")["state"], "ok")
        self.assertEqual(self._alerts("provider_credits_low"), [])
        self.assertEqual(self._alerts("provider_credits_exhausted"), [])

    def test_a_healthy_reading_creates_no_row(self):
        self.assertIsNone(balance.credit_state(
            self.db, "deepseek", "",
            [balance.BalanceReading("deepseek", "USD", 3.0, available=True)]))
        self.assertIsNone(self._row("deepseek", ""))
        self.assertEqual(self._alerts("provider_credits_low"), [])

    def test_a_declared_prepaid_is_the_reference_of_a_real_balance(self):
        # DeepSeek: the balance is real, the declared prepaid only sets the
        # percentage; no projection is applied to a provider that publishes one.
        self._declare(200.0, harness="deepseek", account="")
        self._reading(30.0)
        self._assess()
        row = self._row("deepseek", "")
        self.assertEqual(row["state"], "low")
        self.assertAlmostEqual(row["ratio"], 0.15, places=3)

    def test_the_poller_updates_the_state(self):
        self._declare(100.0, harness="deepseek", account="")
        rows = balance.record(
            self.db, self._Source([balance.BalanceReading("deepseek", "USD", 12.0)]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._row("deepseek", "")["state"], "low")

    def test_a_zero_or_unavailable_currency_does_not_hide_a_usable_one(self):
        """B1 cx3 : un USD à zéro marqué disponible n'épuise pas un compte qui
        porte encore un solde en CNY ; le poll et la réévaluation choisissent
        la même devise positive, sans conversion implicite."""
        self._declare(100.0, harness="deepseek", account="")
        self._reading(0.0, currency="USD")
        self._reading(100.0, currency="CNY")
        usd = balance.BalanceReading("deepseek", "USD", 0.0, available=True)
        cny = balance.BalanceReading("deepseek", "CNY", 100.0, available=True)
        unavailable_usd = balance.BalanceReading(
            "deepseek", "USD", 0.0, available=False)
        for readings in ([usd, cny], [cny, usd], [cny], [unavailable_usd, cny]):
            with self.subTest(currencies=[r.currency for r in readings]):
                self.assertIsNot(
                    balance.credit_state(self.db, "deepseek", "", readings), True)
        # la réévaluation depuis la série (alerts -> assess) fait le même choix
        self._assess()
        row = self._row("deepseek", "")
        self.assertEqual(row["state"], "ok")
        self.assertAlmostEqual(row["balance_usd"], 100.0, places=3)
        self.assertAlmostEqual(row["ratio"], 1.0, places=3)
        self.assertEqual(self._alerts("provider_credits_exhausted"), [])
        self.assertEqual(self._alerts("provider_credits_low"), [])

    def test_a_ratio_above_the_declared_prepaid_does_not_overflow(self):
        """B1 dev2 : un solde réel au-dessus du prépayé donne un ratio > 1.
        La colonne ne plafonne plus à 9.999999, et la passe ne s'arrête pas
        avant les autres comptes."""
        self._declare(10.0, harness="deepseek", account="")
        self._reading(150.0, provider="deepseek")
        self._declare(100.0, harness="codex", account="primaire")
        self._prepaid_at(3600)
        self._spend(85.0)
        # le poll du solde réel écrit le ratio 15 sans erreur de colonne
        self.assertIsNot(balance.credit_state(
            self.db, "deepseek", "",
            [balance.BalanceReading("deepseek", "USD", 150.0, available=True)]), True)
        deepseek = self._row("deepseek", "")
        self.assertEqual(deepseek["state"], "ok")
        self.assertAlmostEqual(deepseek["ratio"], 15.0, places=3)
        # la passe complète évalue encore le compte projeté
        self._assess()
        self.assertEqual(self._row()["state"], "low")
        low = self._alerts("provider_credits_low")
        self.assertEqual([(a["harness"], a["account"]) for a in low],
                         [("codex", "primaire")])

    def test_one_failing_account_does_not_stop_the_others(self):
        """B1 dev2 : une ligne refusée par la base (ici simulée) ne doit pas
        empêcher l'évaluation des comptes suivants."""
        from ameesh import db as db_mod
        self._declare(10.0, harness="deepseek", account="")
        self._reading(150.0, provider="deepseek")
        self._declare(100.0, harness="openai", account="")
        self._prepaid_at(3600, harness="openai", account="")
        self._spend(85.0, harness="openai", account="")
        real = provider_credits._apply

        def flaky(db, harness, account, verdict, **kwargs):
            if harness == "deepseek":
                raise db_mod.DbError("simulated write failure")
            return real(db, harness, account, verdict, **kwargs)

        with mock.patch.object(provider_credits, "_apply", side_effect=flaky):
            provider_credits.assess(self.db)
        self.assertEqual(self._row("openai", "")["state"], "low")
        self.assertEqual(self._row("deepseek", "")["state"], "ok")

    def test_a_successful_probe_is_not_undone_by_the_stale_projection(self):
        """B1 dev4 : après une sonde réussie, la projection d'un prépayé non
        redéclaré ne doit pas re-tenir le compte ni relancer l'alerte."""
        self._declare(10.0, harness="codex", account="primaire")
        self._prepaid_at(3600)
        self._spend(15.0, seconds_ago=60)
        self._assess()
        self.assertEqual(self._row()["state"], "exhausted")
        # la sonde bornée réussit : le compte a de nouveau du crédit
        provider_credits.mark_ok(self.db, "codex", "primaire", source="probe")
        self.assertEqual(self._row()["state"], "ok")
        self._assess()
        self.assertEqual(self._row()["state"], "ok")
        self.assertEqual(self._alerts("provider_credits_exhausted"), [])
        self.assertEqual(self._alerts("provider_credits_low"), [])
        self.assertIn("re-declare", provider_credits.describe(self._row()))
        # une nouvelle déclaration rafraîchit la projection : elle s'applique
        self._declare(10.0, harness="codex", account="primaire")
        self._assess()
        self.assertEqual(self._row()["state"], "ok")
        self.assertAlmostEqual(self._row()["ratio"], 1.0, places=3)

    def test_assess_never_reopens_an_account_exhausted_during_the_pass(self):
        """B1 cx2/auditeur : un mark_exhausted concurrent entre la lecture et
        l'écriture d'assess ne doit pas être écrasé (seule la sonde rouvre)."""
        self._declare(100.0, harness="codex", account="primaire")
        self._prepaid_at(3600)
        self._spend(85.0)  # projeté 15 -> low attendu
        real = provider_credits._apply

        def racing(db, harness, account, verdict, **kwargs):
            provider_credits.mark_exhausted(db, harness, account,
                                            reason="concurrent exhaustion")
            return real(db, harness, account, verdict, **kwargs)

        with mock.patch.object(provider_credits, "_apply", side_effect=racing):
            provider_credits.assess(self.db)
        row = self._row()
        self.assertEqual(row["state"], "exhausted")
        self.assertIsNotNone(row.get("next_probe_ts"))

    def test_assess_mark_ok_never_reopens_a_concurrent_exhausted(self):
        """B1 cx2/auditeur : la branche « plus de preuve » ne rouvre pas un
        compte épuisé entre la lecture et l'écriture."""
        self._declare(100.0, harness="codex", account="primaire")
        self._prepaid_at(3600)
        self._spend(85.0)
        self._assess()
        self.assertEqual(self._row()["state"], "low")
        provider_credits.clear(self.db, "codex", "primaire", actor="human:test")
        real_ok = provider_credits.mark_ok

        def racing_ok(db, harness, account, **kwargs):
            provider_credits.mark_exhausted(db, harness, account,
                                            reason="concurrent exhaustion")
            return real_ok(db, harness, account, **kwargs)

        with mock.patch.object(provider_credits, "mark_ok", side_effect=racing_ok):
            provider_credits.assess(self.db)
        self.assertEqual(self._row()["state"], "exhausted")


class ChooseFallbackTest(_LowBase):

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def _choose(self, **kw):
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        return accounts.choose(self.db, self.cfg.host, "codex", self._items(), book,
                               **kw)

    def test_an_exhausted_account_is_never_chosen(self):
        provider_credits.mark_exhausted(self.db, "codex", "primaire",
                                        reason="insufficient_quota")
        choix = self._choose()
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertFalse(choix.auth_blocked)
        # continuity: a session on the exhausted account moves too
        choix = self._choose(session_account="primaire")
        self.assertEqual(choix.profile.name, "secondaire")

    def test_with_no_other_account_the_exhausted_one_is_returned(self):
        # the runner's credits guard must still see it (blocked + bounded
        # probe); a `None` pause would silently lose the probe.
        for name in ("primaire", "secondaire"):
            provider_credits.mark_exhausted(self.db, "codex", name,
                                            reason="insufficient_quota")
        choix = self._choose()
        self.assertIsNotNone(choix.profile)
        self.assertIn(choix.profile.name, {"primaire", "secondaire"})
        self.assertIn("provider credits exhausted", choix.why)

    def test_a_forced_exhausted_account_falls_back_to_a_free_one(self):
        self.db.execute(
            "insert into account_active (host, harness, account, forced)"
            " values (%s, 'codex', 'primaire', true)"
            " on conflict (host, harness) do update"
            " set account = 'primaire', forced = true", (self.cfg.host,))
        provider_credits.mark_exhausted(self.db, "codex", "primaire",
                                        reason="insufficient_quota")
        choix = self._choose()
        self.assertEqual(choix.profile.name, "secondaire")


class WorkerFallbackTest(_LowBase):
    """The real fallback: an agent whose account is exhausted runs on the
    other declared account of its harness, without operator action."""

    def _turn(self, worker, prompt=None):
        if prompt is not None:
            registry.set_pending_prompt(self.db, worker.name, prompt)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "PATH": self._python_du_banc() + os.pathsep + os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, env, clear=False):
            spec = worker.pick()
            if spec is not None:
                worker.run_turn(spec)
            return spec

    def test_the_agent_moves_to_the_other_declared_account(self):
        _r, worker = self._worker("repli", "codex", {"codex": self._codex_comptes()})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "primaire",
                                            reason="insufficient_quota")
            spec = self._turn(worker, "travail")
            self.assertIsNotNone(spec)
            self.assertEqual(len(self.turns()), 1)
            self.assertEqual(worker._account.name, "secondaire")
            bascules = self.db.query(
                "select from_account, to_account from account_switches")
            self.assertEqual([(b["from_account"], b["to_account"]) for b in bascules],
                             [("primaire", "secondaire")])
            ligne = registry.get(self.db, "repli")
            self.assertEqual(ligne["status"], "idle")
            self.assertFalse(ligne["pending_prompt"])
        finally:
            worker.watchdog_stop.set()

    def test_without_fallback_the_turn_stays_requested_and_the_agent_blocks(self):
        _r, worker = self._worker("seul", "codex",
                                  {"codex": [self._codex_comptes()[0]]})
        try:
            provider_credits.mark_exhausted(self.db, "codex", "primaire",
                                            reason="insufficient_quota")
            self.assertIsNone(self._turn(worker, "travail"))
            self.assertEqual(self.turns(), [])
            ligne = registry.get(self.db, "seul")
            self.assertEqual((ligne["status"], ligne["status_text"],
                              ligne["pending_prompt"]),
                             ("blocked", "provider_credits_exhausted: codex/primaire",
                              "travail"))
        finally:
            worker.watchdog_stop.set()


class DisplayTest(_LowBase):

    def test_budget_and_show_display_the_projected_balance(self):
        self._declare(100.0)
        self._prepaid_at(3600)
        self._spend(85.0)
        self._assess()
        data = budget.status(self.cfg, self.db)
        ligne = [r for r in data["provider_credits"]
                 if (r["harness"], r["account"]) == ("codex", "primaire")][0]
        self.assertEqual((ligne["state"], ligne["prepaid_usd"]),
                         ("low", 100.0))
        json.dumps(data)
        texte = budget.format_status(data)
        self.assertIn("codex/primaire", texte)
        self.assertIn("low since", texte)
        self.assertRegex(texte, r"balance 15\.00 of 100\.00 \(15%\)")

    def test_the_prepaid_cli_declares_shows_and_removes(self):
        proc = self.mesh("budget", "prepaid", "set", "--harness", "codex",
                         "--account", "primaire", "--usd", "100")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("prepaid amount declared", proc.stdout)
        self.assertIn("balance 100.00 of 100.00", proc.stdout)
        proc = self.mesh("budget", "prepaid", "show", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)["provider_credits"]
        self.assertEqual([(r["harness"], r["account"], r["prepaid_usd"])
                          for r in rows], [("codex", "primaire", 100.0)])
        proc = self.mesh("budget")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("provider accounts (real credit state", proc.stdout)
        self.assertIn("codex/primaire", proc.stdout)
        proc = self.mesh("budget", "prepaid", "unset", "--harness", "codex",
                         "--account", "primaire")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("prepaid amount removed", proc.stdout)
        self.assertIsNone(self._row()["prepaid_usd"])

    def test_an_agent_session_cannot_declare_the_prepaid(self):
        env = dict(self.env(), AGENT_MAIL_NAME="mty-amh-impl-04")
        proc = self.mesh("budget", "prepaid", "set", "--harness", "codex",
                         "--account", "primaire", "--usd", "100", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused: this session belongs to agent", proc.stderr)
        self.assertIsNone(self._row())


class ClassificationTest(unittest.TestCase):

    def test_the_low_alert_has_its_own_type_and_a_stable_reason(self):
        self.assertIn("provider_credits_low", exploitation.ALERT_TYPES)
        self.assertEqual(provider_credits.REASON_LOW, "provider_credits_low")
        self.assertLess(provider_credits.CRITICAL_RATIO,
                        provider_credits.LOW_RATIO)


if __name__ == "__main__":
    unittest.main()
