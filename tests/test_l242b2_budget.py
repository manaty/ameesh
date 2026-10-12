# SPDX-License-Identifier: AGPL-3.0-only
"""L242b (PR 2a) : les non-critiques s'arrêtent plus tôt, pour laisser la marge
aux agents critiques — 10 points sous le plafond de rythme d'un forfait, et
`noncritical_hourly_share` (75 %) du plafond horaire payé au token du mesh ;
ce plafond horaire se compte sur la baisse réelle des soldes (décision de
mesh-design du 2026-10-12)."""
from __future__ import annotations

from unittest import mock

from ameesh import accounts, config, cost, storage

from .support import PgTestCase
from .test_l242b_critical_accounts import _Comptes

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

    def test_la_reserve_ne_touche_pas_les_plafonds_fixes(self):
        limits = accounts.limits(self.items[1], [], self.now, reserve=True)
        self.assertEqual(limits, {})
        self.assertEqual(accounts.RESERVE, 0.10)


class _Paye(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "provider_balances", "turn_cost_corrections"):
            self.db.execute("DELETE FROM %s" % table)
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db, hourly_usd=20.0,
                                  tools={"ds": "deepseek"})

    def ligne(self, usd, ago_s, agent="ds"):
        storage.of(self.db).turn_costs.insert(
            agent=agent, harness="deepseek", turn="t", model="m", session=None, usd=usd,
            cum_usd=None, cum_input_tokens=None, cum_cached_input_tokens=None,
            cum_output_tokens=None, input_tokens=10, cached_input_tokens=0, output_tokens=5)
        self.db.execute("UPDATE turn_costs SET recorded_at = now() - make_interval(secs => %s)"
                        " WHERE id = (SELECT max(id) FROM turn_costs)", (float(ago_s),))

    def solde(self, total, ago_s):
        self.db.execute(
            "INSERT INTO provider_balances (provider, currency, total, observed_at)"
            " VALUES ('deepseek', 'USD', %s, now() - make_interval(secs => %s))",
            (total, float(ago_s)))


class PartDuPlafondHoraireTest(_Paye):
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

    def test_soldes_illisibles_l_estimation(self):
        self.ligne(5.0, 600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db, hourly_usd=20.0)
        with mock.patch("ameesh.balance.compare", side_effect=RuntimeError("down")):
            self.assertAlmostEqual(book.paid_hour(["deepseek"]), 5.0)
