# SPDX-License-Identifier: AGPL-3.0-only
"""L242b (PR 2a) : les non-critiques s'arrêtent plus tôt, pour laisser la marge
aux agents critiques — 10 points sous le plafond de rythme d'un forfait, et
`noncritical_hourly_share` (75 %) du plafond horaire payé au token du mesh ;
ce plafond horaire se compte sur la baisse réelle des soldes (décision de
mesh-design du 2026-10-12)."""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from unittest import mock

from ameesh import (account_turn, accounts, alert_briefs, budget, config, cost, exploitation,
                    notify, progress, registry, storage)

from .support import PgTestCase
from .test_l242b_critical_accounts import _Comptes
from .test_l30_accounts import _Base as _L30Base

HEURE = 3600.0


class ReserveForfaitTest(_Comptes):
    def test_un_non_critique_s_arrete_dix_points_plus_tot(self):
        self.jauge("primaire", 96)       # au seuil pour tous
        self.jauge("secondaire", 85)     # rythme 90 %, 80 % avec la réserve
        ordinaire = self.choose()
        self.assertIsNone(ordinaire.profile)
        self.assertIn("(pace cap 80%), 10 points kept for critical agents", ordinaire.reason)
        critique = self.choose(critical=True)
        self.assertEqual(critique.profile.name, "secondaire")
        # sous la réserve, tout le monde y va
        self.jauge("secondaire", 70)
        self.assertEqual(self.choose().profile.name, "secondaire")

    def test_le_plafond_court_ne_depasse_pas_le_rythme_reserve(self):
        # #373 r1 : plafond court 90 %, 1 % de la fenêtre écoulé
        profil = accounts.parse({"codex": [
            {"name": "primaire"}, dict(self.secondaire, short_window_cap=0.9)]})["codex"][1]
        jauge = cost.Gauge("codex", "codex-300min", 0.20, self.now + 0.99 * 5 * HEURE,
                           5 * HEURE)
        cap, label = accounts.limits(profil, [jauge], self.now, reserve=True)["codex-300min"]
        self.assertAlmostEqual(cap, 0.01, places=3)
        self.assertEqual(label, "pace cap")
        cap, label = accounts.limits(profil, [jauge], self.now)["codex-300min"]
        self.assertEqual((cap, label), (0.9, "short window cap"))

    def test_sans_comptes_declares_la_pause_dit_compte_et_heure(self):
        # #373 r1 : jauge à 96 % qui se remet à zéro dans 30 min
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        jauge = cost.Gauge("codex", "five_hour", 0.96, self.now + 1800, 5 * HEURE)
        with mock.patch.object(book, "gauges", return_value=[jauge]), \
                mock.patch.object(book, "clock", return_value=self.now):
            raison = book.pace_exceeded("codex", accounts.RESERVE)
        heure = cost.until_label(self.now + 1800, self.now)  # dated after 23:30
        self.assertTrue(raison.startswith(
            "pause (account (default), five_hour 96 %%) until %s; plan codex five_hour" % heure),
            raison)

    def test_la_pause_dure_jusqu_a_la_remise_la_plus_tardive(self):
        # #373 r2 : five_hour à 96 % (remise dans 30 min), seven_day à 96 %
        # (remise dans 2 j) : en pause 2 jours, pas 30 min
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        court = cost.Gauge("codex", "five_hour", 0.96, self.now + 1800, 5 * HEURE)
        long = cost.Gauge("codex", "seven_day", 0.96, self.now + 2 * 86400, 7 * 86400)
        with mock.patch.object(book, "gauges", return_value=[court, long]), \
                mock.patch.object(book, "clock", return_value=self.now):
            raison = book.pace_exceeded("codex")
        fin = time.strftime("%d/%m %H:%M", time.localtime(self.now + 2 * 86400))
        self.assertTrue(raison.startswith(
            "pause (account (default), seven_day 96 %%) until %s; plan codex seven_day" % fin),
            raison)
        # une fenêtre qui bloque sans date : aucune heure annoncée
        sans_date = cost.Gauge("codex", "seven_day", 0.96, None, 7 * 86400)
        with mock.patch.object(book, "gauges", return_value=[court, sans_date]), \
                mock.patch.object(book, "clock", return_value=self.now):
            self.assertTrue(book.pace_exceeded("codex").startswith("plan codex five_hour"))

    def test_la_reserve_ne_touche_pas_les_plafonds_fixes(self):
        limits = accounts.limits(self.items[1], [], self.now, reserve=True)
        self.assertEqual(limits, {})
        self.assertEqual(accounts.RESERVE, 0.10)


class AffichageDeLaPauseTest(_Comptes):
    def test_la_raison_commence_par_la_pause_en_court(self):
        self.jauge("primaire", 96)
        self.jauge("secondaire", 97)
        pause = self.choose()
        self.assertIsNone(pause.profile)
        heure = cost.until_label(self.now + 1800, self.now)  # dated after 23:30
        self.assertTrue(pause.reason.startswith("pause (account "), pause.reason)
        self.assertIn(") until %s; all codex accounts at threshold" % heure, pause.reason)
        self.assertRegex(pause.reason, r"codex-300min (96|97) %")

    def test_la_pause_d_un_compte_dit_sa_fenetre_la_plus_longue(self):
        # #373 r2 : deux fenêtres au seuil sur chaque compte, la semaine se
        # remet à zéro dans 2 j : c'est elle que la pause annonce
        for home in (self.home, self.secondaire["path"]):
            dossier = os.path.join(home, "sessions", "2026", "10", "05")
            os.makedirs(dossier, exist_ok=True)
            with open(os.path.join(dossier, "rollout-test.jsonl"), "w",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
                fh.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "token_count", "rate_limits": {
                        "plan_type": "pro",
                        "primary": {"used_percent": 96, "window_minutes": 300,
                                    "resets_at": self.now + 1800},
                        "secondary": {"used_percent": 96, "window_minutes": 10080,
                                      "resets_at": self.now + 2 * 86400}}}}) + "\n")
        pause = self.choose()
        self.assertIsNone(pause.profile)
        fin = time.strftime("%d/%m %H:%M", time.localtime(self.now + 2 * 86400))
        self.assertRegex(pause.reason, r"^pause \(account \w+, codex-10080min 96 %%\) "
                                       r"until %s; " % re.escape(fin))

    def test_une_pause_budget_n_est_jamais_un_arret(self):
        for status, lease in (("blocked", False), ("running", False), ("idle", False)):
            with self.subTest(status=status):
                row = {"status": status, "lease_live": lease,
                       "status_text": "budget : pause (account a, five_hour 96 %) until 14:20"}
                self.assertEqual(progress.agent_state(row), "paused")
        self.assertEqual(progress.agent_state({"status": "stopped",
                                               "status_text": "budget : x"}), "stopped")
        self.assertEqual(progress.BUDGET_PAUSE_PREFIX, budget.PAUSE_PREFIX)


class _Paye(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "provider_balances", "turn_cost_corrections"):
            self.db.execute("DELETE FROM %s" % table)
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db, hourly_usd=20.0,
                                  tools={"ds": "deepseek"})

    def ligne(self, usd, ago_s, agent="ds", account=None):
        storage.of(self.db).turn_costs.insert(
            agent=agent, harness="deepseek", turn="t", model="m", session=None, usd=usd,
            cum_usd=None, cum_input_tokens=None, cum_cached_input_tokens=None,
            cum_output_tokens=None, input_tokens=10, cached_input_tokens=0, output_tokens=5,
            account=account)
        self.db.execute("UPDATE turn_costs SET recorded_at = now() - make_interval(secs => %s)"
                        " WHERE id = (SELECT max(id) FROM turn_costs)", (float(ago_s),))

    def solde(self, total, ago_s, account=None):
        self.db.execute(
            "INSERT INTO provider_balances (provider, currency, total, observed_at, account)"
            " VALUES ('deepseek', 'USD', %s, now() - make_interval(secs => %s), %s)",
            (total, float(ago_s), account))


class PartDuPlafondHoraireTest(_Paye):
    def test_declaration_appareil_exclue_du_plafond_horaire(self):
        self.ligne(1.0, 600)
        storage.of(self.db).turn_costs.insert(
            agent="ds", harness="deepseek", turn="device-report", model="m",
            session=None, usd=50.0, cum_usd=None, cum_input_tokens=None,
            cum_cached_input_tokens=None, cum_output_tokens=None,
            input_tokens=10, cached_input_tokens=0, output_tokens=5, source="device")
        # Without balance observations, only the server's ledger line counts.
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 1.0)
        self.assertEqual(self.book.over("ds", paid_harnesses=["deepseek"], pace=False), "")
        # Server costs still enforce the cap even alongside a device report.
        self.ligne(19.0, 300)
        self.assertIn("(cap 20.00 $)",
                      self.book.over("ds", paid_harnesses=["deepseek"], pace=False))

    def test_un_non_critique_s_arrete_a_75_pour_cent(self):
        self.ligne(16.0, 600)             # pas de relevé : l'estimation
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 16.0)
        raison = self.book.over("ds", hourly_share=0.75)
        self.assertIn("16.00 $ over the last 60 minutes (cap 20.00 $, 15.00 $ for a non-critical agent)",
                      raison)
        self.assertEqual(self.book.over("ds"), "")
        self.ligne(4.0, 300)
        self.assertIn("(cap 20.00 $)", self.book.over("ds"))

    def test_configuration(self):
        cfg = config.load({"AMEESH_CONFIG": "/nonexistent"})
        self.assertEqual(cfg.noncritical_hourly_share, 0.75)
        cfg = config.load({"AMEESH_CONFIG": "/nonexistent",
                           "AMEESH_NONCRITICAL_HOURLY_SHARE": "0.5"})
        self.assertEqual(cfg.noncritical_hourly_share, 0.5)
        for wrong in ("2", "-0.1", "x"):
            cfg = config.load({"AMEESH_CONFIG": "/nonexistent",
                               "AMEESH_NONCRITICAL_HOURLY_SHARE": wrong})
            self.assertEqual(cfg.noncritical_hourly_share, 0.75, wrong)


class BaisseReelleTest(_Paye):
    def test_baisse_reelle_puis_estimation_corrigee(self):
        # relevés à -50 et -10 min : 2 $ réels pour 8 $ estimés (rapport 0,25) ;
        # après le dernier relevé, 4 $ estimés valent 1 $
        self.solde(100.0, 50 * 60)
        self.ligne(8.0, 30 * 60)
        self.solde(98.0, 10 * 60)
        self.ligne(4.0, 5 * 60)
        self.assertAlmostEqual(self.book.spent("all", HEURE, harnesses=["deepseek"]), 12.0)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 3.0, places=3)
        self.assertEqual(self.book.over("ds", hourly_share=0.75), "")

    def test_jamais_compte_nul_entre_deux_releves(self):
        # aucune baisse relevée pour 8 $ estimés : le rapport tombe au plancher
        self.solde(100.0, 50 * 60)
        self.ligne(8.0, 30 * 60)
        self.solde(100.0, 10 * 60)
        self.ligne(10.0, 5 * 60)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]),
                               10.0 * cost.PAID_RATIO_FLOOR, places=3)

    def test_peu_estime_rapport_ignore(self):
        # moins de GAP_MIN_USD estimés sur l'intervalle relevé : pas de correction
        self.solde(100.0, 50 * 60)
        self.ligne(0.2, 30 * 60)
        self.solde(99.9, 10 * 60)
        self.ligne(4.0, 5 * 60)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 0.1 + 4.0, places=3)

    def test_un_compte_releve_ne_couvre_pas_les_autres(self):
        # #373 r1 (P1) : A relevé baisse de 1 $, B sans relevé dépense 20 $
        self.solde(50.0, 40 * 60, account="A")
        self.ligne(1.0, 30 * 60, account="A")
        self.solde(49.0, 20 * 60, account="A")
        self.ligne(20.0, 25 * 60, account="B")
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 21.0, places=3)
        self.assertIn("15.00 $ for a non-critical agent", self.book.over("ds", hourly_share=0.75))

    def test_seule_la_part_dans_l_heure_compte(self):
        # #373 r1 : relevés à -83 et -10 min, baisse de 16 $ dépensée à -66 min
        self.solde(100.0, 83 * 60)
        self.ligne(16.0, 66 * 60)
        self.solde(84.0, 10 * 60)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 0.0, places=3)
        # sans estimation pour la répartir : au prorata du temps (50 min sur 73)
        self.db.execute("DELETE FROM turn_costs")
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 16.0 * 50 / 73, places=1)

    def test_des_releves_espaces_lisent_tout_l_intervalle(self):
        # #373 r2 : relevés à -48 h et -10 min, baisse de 40 $ ; 30 $ estimés
        # à -30 h et 10 $ à -30 min : 10 $ dans l'heure, pas 40 $
        self.solde(100.0, 48 * HEURE)
        self.ligne(30.0, 30 * HEURE)
        self.ligne(10.0, 30 * 60)
        self.solde(60.0, 10 * 60)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 10.0, places=3)
        self.assertEqual(self.book.over("ds"), "")

    def test_soldes_illisibles_l_estimation(self):
        # #373 r2 (P3) : les relevés disent 0 $ (solde immobile) ; leur
        # lecture échoue : l'estimation seule (5 $)
        self.solde(100.0, 50 * 60)
        self.ligne(5.0, 30 * 60)
        self.solde(100.0, 10 * 60)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 0.0, places=3)
        operations = type(storage.of(self.db).operations)
        with mock.patch.object(operations, "balances", side_effect=RuntimeError("down")) as lu:
            self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 5.0)
        lu.assert_called()


class AlerteCritiqueTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("orch", "auditeur", "mesh-design", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def test_un_critique_en_pause_budget_leve_une_alerte_urgente(self):
        raison = "pause (account a, five_hour 96 %) until 14:20; all claude accounts at threshold"
        registry.set_status(self.db, "mesh-design", "blocked", status_text="%s%s%s" % (
            budget.PAUSE_PREFIX, budget.CRITICAL_MARK, raison))
        registry.set_status(self.db, "dev1", "blocked",
                            status_text=budget.PAUSE_PREFIX + raison)
        [alerte] = [a for a in exploitation.alerts(self.cfg, self.db,
                                                   orchestrators_declared=("orch",))
                    if a["type"] == "critical_budget_pause"]
        self.assertEqual(alerte["agent"], "mesh-design")
        self.assertIn("critical agent mesh-design paused on budget", alerte["detail"])
        self.assertIn(raison, alerte["detail"])
        self.assertIn("orch", alerte["orchestrators"])
        self.assertIn("critical_budget_pause", notify.URGENT_TYPES)
        self.assertIn("critical_budget_pause", notify.DEFAULT_TYPES)
        listing = registry.overview(self.db)
        briefs = alert_briefs.briefs(alerte, listing, {}, ["auditeur", "mesh-design"])
        self.assertEqual(sorted(briefs), ["auditeur", "orch"])
        self.assertIn("Critical agent paused on budget", briefs["auditeur"])


class MarqueDeLExecuteurTest(_L30Base):
    def test_la_pause_d_un_critique_est_marquee(self):
        comptes = self._claude_comptes()
        # sans harnais de repli (L242b (3), testé à part) : la pause reste
        _runner, worker = self._worker("orch", "claude", {"claude": comptes},
                                       critical_agents=("orch",),
                                       critical_fallback_harnesses=())
        try:
            with mock.patch.object(account_turn, "choose", return_value="pause (x) until 14:20"):
                self.assertFalse(worker.budget_ok())
            texte = registry.get(self.db, "orch")["status_text"]
            self.assertEqual(texte, "budget : critical agent: pause (x) until 14:20")
        finally:
            worker.watchdog_stop.set()
        _runner, worker = self._worker("dev1", "claude", {"claude": comptes})
        try:
            with mock.patch.object(account_turn, "choose", return_value="pause (x) until 14:20"):
                self.assertFalse(worker.budget_ok())
            self.assertEqual(registry.get(self.db, "dev1")["status_text"],
                             "budget : pause (x) until 14:20")
        finally:
            worker.watchdog_stop.set()


class EtiquetteDeFinTest(unittest.TestCase):
    def test_la_date_n_apparait_que_si_la_fin_n_est_pas_aujourd_hui(self):
        # #373 r2 : l'heure seule tant que la remise tombe aujourd'hui ; la
        # date au-delà — près de minuit, une remise dans 30 min est demain.
        midi = time.mktime((2026, 10, 12, 12, 0, 0, 0, 0, -1))
        self.assertEqual(cost.until_label(midi + 1800, midi), "12:30")
        self.assertEqual(cost.until_label(midi + 86400, midi), "13/10 12:00")
