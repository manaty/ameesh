# SPDX-License-Identifier: AGPL-3.0-only
"""#513 : le barème DeepSeek à l'heure du tour (pointe et heures creuses), les
plafonds comptés sur la dépense mesurée (soldes) et le contrôle quotidien
`budget_drift` qui compare le compté au solde.

Constat de bureau du 2026-10-12 : 232,38 $ comptés pour deepseek-flash sur la
journée, 34,97 $ réellement dépensés, 36 pauses budget."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import unittest
from unittest import mock

from ameesh import budget, cost, exploitation, notify, relay

from .test_cost import Sandbox
from .test_l242b2_budget import HEURE, _Paye
from .support import PgTestCase


def utc(*args) -> float:
    return dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp()


#: lundi 12 octobre 2026
LUNDI = (2026, 10, 12)
SAMEDI = (2026, 10, 17)
FLASH = cost.DEFAULT_PRICES["deepseek-flash"]
CREUX = cost.PRICE_PERIODS["deepseek-flash"]["off_peak"]


class PeriodPriceTest(unittest.TestCase):
    def prix(self, start, end, prices=None, model="deepseek-flash"):
        return cost.period_price(prices or cost.load_prices(None), model, start, end)

    def test_le_tableau_est_la_moitie_de_la_pointe(self):
        # page officielle relue le 2026-10-12 : 0.15 / 0.003 / 0.60
        self.assertEqual(CREUX, [0.15, 0.003, 0.60])
        self.assertEqual(FLASH, [0.30, 0.006, 1.20])
        for creux, pointe in zip(CREUX, FLASH):
            self.assertAlmostEqual(creux, pointe / 2)

    def test_heures_de_pointe_en_semaine(self):
        for heure in (1, 2, 3, 6, 9):
            self.assertIsNone(self.prix(utc(*LUNDI, heure, 10), utc(*LUNDI, heure, 50)), heure)
        for heure in (0, 4, 5, 10, 12, 23):
            self.assertEqual(self.prix(utc(*LUNDI, heure, 10), utc(*LUNDI, heure, 50)), CREUX,
                             heure)

    def test_le_week_end_est_creux(self):
        self.assertEqual(self.prix(utc(*SAMEDI, 2), utc(*SAMEDI, 9, 30)), CREUX)

    def test_un_tour_a_cheval_paie_la_pointe(self):
        # 03:30 → 04:30 : commence en pointe
        self.assertIsNone(self.prix(utc(*LUNDI, 3, 30), utc(*LUNDI, 4, 30)))
        # 04:30 → 06:10 : finit en pointe
        self.assertIsNone(self.prix(utc(*LUNDI, 4, 30), utc(*LUNDI, 6, 10)))
        # 04:10 → 05:50 : tout creux
        self.assertEqual(self.prix(utc(*LUNDI, 4, 10), utc(*LUNDI, 5, 50)), CREUX)
        # samedi 11:00 → lundi 00:30, dimanche compris : tout creux
        self.assertEqual(self.prix(utc(*SAMEDI, 11), utc(2026, 10, 19, 0, 30)), CREUX)
        # samedi → lundi 01:30 : finit en pointe
        self.assertIsNone(self.prix(utc(*SAMEDI, 12), utc(2026, 10, 19, 1, 30)))

    def test_heure_inconnue_ou_absurde_paie_la_pointe(self):
        midi = utc(*LUNDI, 12)
        self.assertIsNone(self.prix(None, midi))
        self.assertIsNone(self.prix(midi, None))
        self.assertIsNone(self.prix(midi, midi - 60))           # à l'envers
        self.assertIsNone(self.prix(midi, float("inf")))
        self.assertIsNone(self.prix(utc(*SAMEDI, 12) - 3 * 86400, utc(*SAMEDI, 12)))

    def test_seulement_au_tarif_officiel_et_pour_les_modeles_du_tableau(self):
        midi = utc(*LUNDI, 12)
        local = dict(cost.load_prices(None), **{"deepseek-flash": [0.2, 0.004, 0.8]})
        self.assertIsNone(self.prix(midi, midi + 60, prices=local))
        self.assertIsNone(self.prix(midi, midi + 60, model="deepseek-pro"))
        self.assertIsNone(self.prix(midi, midi + 60, model=""))
        # variante datée du même modèle : même tarif
        self.assertEqual(self.prix(midi, midi + 60, model="deepseek-flash-20261001"), CREUX)


class TurnAtItsHourTest(Sandbox, PgTestCase):
    USAGE = {"inputTokens": 1_000_000, "cacheReadTokens": 1_000_000,
             "outputTokens": 1_000_000}

    def tour(self, started, ended):
        self.agent("a1", "deepseek", "deepseek-flash")
        self.events("a1", {"type": cost.TURN_MARKER, "resume": "", "ts": started,
                           "agent": "a1"},
                    {"type": "status", "phase": "step_end", "usage": self.USAGE})
        return self.book().record("a1", start=1, model="deepseek-flash", ended=ended)

    def test_tour_creux_au_prix_creux(self):
        usage = self.tour(utc(*LUNDI, 12), utc(*LUNDI, 12, 20))
        self.assertAlmostEqual(usage.usd, sum(CREUX), places=9)
        ligne = self.db.query("SELECT usd::float8 AS usd FROM turn_costs")[0]
        self.assertAlmostEqual(ligne["usd"], sum(CREUX), places=6)

    def test_tour_de_pointe_et_tour_a_cheval(self):
        self.assertAlmostEqual(self.tour(utc(*LUNDI, 7), utc(*LUNDI, 7, 5)).usd,
                               sum(FLASH), places=9)
        self.db.execute("DELETE FROM turn_costs")
        os.remove(os.path.join(self.state, "a1", "events.jsonl"))
        self.assertAlmostEqual(self.tour(utc(*LUNDI, 5, 50), utc(*LUNDI, 6, 5)).usd,
                               sum(FLASH), places=9)

    def test_sans_marqueur_la_pointe(self):
        self.agent("a1", "deepseek", "deepseek-flash")
        self.events("a1", {"type": "status", "phase": "step_end", "usage": self.USAGE})
        usage = self.book().turn_usage("a1", model="deepseek-flash", ended=utc(*LUNDI, 12))
        self.assertAlmostEqual(usage.usd, sum(FLASH), places=9)

    def test_le_bareme_local_garde_son_prix_a_toute_heure(self):
        with open(os.path.join(self.tmp_cost, "prices.json"), "w", encoding="utf-8") as fh:
            json.dump({"deepseek-flash": [1.0, 1.0, 1.0]}, fh)
        self.assertAlmostEqual(self.tour(utc(*SAMEDI, 12), utc(*SAMEDI, 12, 5)).usd, 3.0,
                               places=9)


class RelayAtItsHourTest(unittest.TestCase):
    def test_une_requete_creuse_paie_le_prix_creux(self):
        usage = relay.Usage(model="deepseek-flash", input_tokens=1_000_000,
                            output_tokens=1_000_000)
        prices = cost.load_prices(None)
        pointe = relay.price_usage(usage, "deepseek", prices)
        self.assertAlmostEqual(
            relay.price_usage(usage, "deepseek", prices, utc(*LUNDI, 12), utc(*LUNDI, 12, 1)),
            pointe / 2, places=9)
        self.assertAlmostEqual(
            relay.price_usage(usage, "deepseek", prices, utc(*LUNDI, 8), utc(*LUNDI, 8, 1)),
            pointe, places=9)


class MeasuredCapsTest(_Paye):
    """(b) : le plafond journalier et les plafonds d'agent comptent, comme le
    plafond horaire (L242b), la baisse réelle des soldes."""

    def test_le_plafond_journalier_suit_le_solde(self):
        # la journée du 12/10 en petit : 6,6 fois trop compté
        self.solde(100.0, 20 * HEURE)
        self.ligne(66.0, 12 * HEURE)
        self.solde(90.0, 2 * HEURE)
        self.book.daily_usd = 30.0
        self.assertAlmostEqual(self.book.spent("all", 86400, harnesses=["deepseek"]), 66.0)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400), 10.0, places=3)
        self.assertEqual(self.book.over("ds", paid_harnesses=["deepseek"], pace=False), "")
        # entre deux relevés, l'estimation corrigée par le rapport (10/66)
        self.ligne(132.0, HEURE)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400), 30.0, places=2)
        self.assertIn("daily budget (pay-per-token): 30.00 $",
                      self.book.over("ds", paid_harnesses=["deepseek"], pace=False))

    def test_sans_releve_l_estimation(self):
        self.ligne(40.0, 5 * HEURE)
        self.book.daily_usd = 30.0
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400), 40.0)
        self.assertIn("daily budget", self.book.over("ds", paid_harnesses=["deepseek"],
                                                     pace=False))

    def test_la_part_d_un_agent(self):
        # une baisse de 6 $ pour 20 $ estimés : 15 $ de ds, 5 $ de autre
        self.solde(100.0, 10 * HEURE)
        self.ligne(15.0, 6 * HEURE)
        self.ligne(5.0, 5 * HEURE, agent="autre")
        self.solde(94.0, 4 * HEURE)
        # après le dernier relevé : 10 $ estimés de ds, au rapport 0,3
        self.ligne(10.0, HEURE)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400, "ds"),
                               4.5 + 3.0, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400, "autre"), 1.5,
                               places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400), 9.0, places=3)
        self.book.agent_limits = {"ds": {86400: 8.0}}
        self.assertEqual(self.book.over("ds", paid_harnesses=["deepseek"], pace=False), "")
        self.book.agent_limits = {"ds": {86400: 7.0}}
        self.assertIn("daily budget of agent ds (pay-per-token): 7.50 $",
                      self.book.over("ds", paid_harnesses=["deepseek"], pace=False))

    def test_sous_le_seuil_l_agent_suit_quand_meme_la_baisse(self):
        # #513 r2 (P2 des deux relecteurs) : baisse de 0,80 $ pour 0,40 $
        # estimés (pair < GAP_MIN_USD = 0,50) ; l'ancien repli `part * ratio`
        # (ratio 1,0) n'en comptait que 0,40 $ et le plafond de l'agent ne se
        # déclenchait pas.
        self.solde(10.0, 10 * HEURE)
        self.ligne(0.40, 6 * HEURE)
        self.solde(9.20, 4 * HEURE)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400), 0.80, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400, "ds"), 0.80, places=3)
        self.book.agent_limits = {"ds": {86400: 0.50}}
        self.assertIn("daily budget of agent ds (pay-per-token): 0.80 $ over the last 24 hours "
                      "(cap 0.50 $)",
                      self.book.over("ds", paid_harnesses=["deepseek"], pace=False))

    def test_sous_le_seuil_les_parts_des_agents_somment_a_la_baisse(self):
        # deux agents estimés 0,20 $ chacun (pair = 0,40 $ < seuil) pour une
        # baisse de 0,80 $ : chacun 0,40 $, et la somme des parts vaut le mesh.
        self.solde(10.0, 10 * HEURE)
        self.ligne(0.20, 6 * HEURE)
        self.ligne(0.20, 5 * HEURE, agent="autre")
        self.solde(9.20, 4 * HEURE)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400, "ds"), 0.40, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400, "autre"), 0.40,
                               places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 86400), 0.80, places=3)

    def test_fenetre_partielle_l_agent_ne_depasse_pas_le_mesh(self):
        # #513 r3 (B/P2 de la relecture du 13/10) : fenêtre glissante qui
        # commence entre deux relevés. L242b répartit la baisse au temps tant
        # que l'intervalle porte moins de GAP_MIN_USD estimés ; la part de
        # l'agent doit être une part de ce que le mesh compte sur la fenêtre,
        # pas l'estimation entière : mesh 0,40 $ contre 0,80 $ pour l'agent
        # faisait déclencher seul le plafond de 0,50 $.
        self.solde(10.0, 10 * HEURE)
        self.ligne(0.40, 4 * HEURE)
        self.solde(9.20, 0)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE), 0.40, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE, "ds"), 0.40,
                               places=3)

    def test_fenetre_partielle_le_plafond_de_l_agent_suit_le_mesh(self):
        # même géométrie sur l'heure : relevés à -83 et -10 min, ligne de
        # 0,40 $ à -50 min, baisse de 0,80 $ ; le mesh garde le prorata
        # temporel (0,40 $ < 0,50 $ estimés) et l'agent suit.
        self.solde(10.0, 83 * 60)
        self.ligne(0.40, 50 * 60)
        self.solde(9.20, 10 * 60)
        mesh = 0.80 * 50 / 73
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], HEURE), mesh, places=2)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], HEURE, "ds"), mesh, places=2)
        self.book.agent_limits = {"ds": {HEURE: 0.60}}
        self.assertEqual(self.book.over("ds", paid_harnesses=["deepseek"], pace=False), "")

    def test_fenetre_partielle_les_parts_des_agents_somment_au_mesh(self):
        # deux agents, 0,15 $ dans la fenêtre et 0,05 $ avant : la part de
        # chacun est une part du montant du mesh (0,15 sur 0,30 dans la
        # fenêtre), pas la baisse entière répartie sur l'intervalle relevé.
        self.solde(10.0, 10 * HEURE)
        self.ligne(0.15, 4 * HEURE)
        self.ligne(0.05, 8 * HEURE)
        self.ligne(0.15, 3 * HEURE, agent="autre")
        self.ligne(0.05, 7 * HEURE, agent="autre")
        self.solde(9.20, 0)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE), 0.40, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE, "ds"), 0.20, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE, "autre"), 0.20,
                               places=3)

    def test_fenetre_partielle_sans_estimation_le_mesh_garde_le_temps(self):
        # L242b inchangé : aucune estimation sur l'intervalle, le mesh garde
        # le prorata temporel ; rien n'est attribuable à un agent.
        self.solde(10.0, 10 * HEURE)
        self.solde(9.20, 0)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE), 0.40, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], 5 * HEURE, "ds"), 0.0, places=3)

    def test_le_plafond_horaire_decide_sur_le_solde_le_grand_livre_reste_brut(self):
        # 30 $ comptés dans l'heure, 6 $ réels : pas de pause sous 20 $
        self.solde(100.0, 50 * 60)
        self.ligne(30.0, 30 * 60)
        self.solde(94.0, 5 * 60)
        self.assertEqual(self.book.over("ds", paid_harnesses=["deepseek"], pace=False), "")
        # le grand livre n'est pas réécrit : la ligne garde son estimation
        self.assertAlmostEqual(self.book.spent("all", HEURE, harnesses=["deepseek"]), 30.0)
        rows = self.db.query("SELECT usd::float8 AS usd FROM turn_costs")
        self.assertEqual([round(r["usd"], 6) for r in rows], [30.0])
        # au-delà en réel : la pause
        self.solde(70.0, 60)
        self.assertIn("hourly budget (pay-per-token): 30.00 $",
                      self.book.over("ds", paid_harnesses=["deepseek"], pace=False))

    def test_le_relais_avec_depense_non_ecrite_decide_sur_le_solde(self):
        # relais : plafond journalier du mesh et de l'agent, plus la dépense
        # gardée en mémoire faute d'écriture (comptée telle quelle)
        self.solde(100.0, 20 * HEURE)
        self.ligne(66.0, 12 * HEURE)
        self.solde(90.0, 2 * HEURE)
        self.book.hourly_usd = 0.0
        self.book.daily_usd = 15.0
        self.book.agent_limits = {"ds": {86400: 15.0}}
        moi = relay.Identity(executor="e", host="h", agent="ds", lease_owner="e",
                             lease_epoch=1)
        stub = mock.Mock(_unrecorded=[{"agent": "ds", "usd": 4.0}])
        self.assertEqual(relay.Relay._over_with_unrecorded(stub, self.book, moi,
                                                           ("deepseek",)), "")
        stub._unrecorded.append({"agent": "ds", "usd": 2.0})
        self.assertIn("budget du mesh atteint", relay.Relay._over_with_unrecorded(
            stub, self.book, moi, ("deepseek",)))
        self.book.daily_usd = 0.0
        self.assertIn("budget de l'agent ds atteint", relay.Relay._over_with_unrecorded(
            stub, self.book, moi, ("deepseek",)))

    def test_l_heure_ne_change_pas(self):
        # L242b inchangé : `paid_hour` est `paid_spent` sur 3600 s
        self.solde(100.0, 50 * 60)
        self.ligne(8.0, 30 * 60)
        self.solde(98.0, 10 * 60)
        self.ligne(4.0, 5 * 60)
        self.assertAlmostEqual(self.book.paid_hour(["deepseek"]), 3.0, places=3)
        self.assertAlmostEqual(self.book.paid_spent(["deepseek"], HEURE), 3.0, places=3)

    def test_budget_affiche_le_compte_du_jour(self):
        self.solde(100.0, 20 * HEURE)
        self.ligne(66.0, 12 * HEURE)
        self.solde(90.0, 2 * HEURE)
        with mock.patch.object(cost, "paid_harnesses_of", return_value=("deepseek",)):
            data = budget.status(self.cfg, self.db)
        self.assertAlmostEqual(data["spend"]["24h_usd"], 66.0)
        self.assertAlmostEqual(data["spend"]["24h_counted_usd"], 10.0, places=3)
        self.assertIn("daily cap counts 10.00 $", budget.format_status(data))


class BudgetDriftTest(_Paye):
    """(c) : le contrôle quotidien, compté contre solde, alerte au-delà de ±30 %."""

    def drift(self, **env):
        with mock.patch.dict(os.environ, env):
            return exploitation._budget_drift(self.db, time.time(), paid=("deepseek",))

    def test_alerte_au_dela_de_30_pour_cent(self):
        # 232,38 $ comptés, 34,97 $ réels
        self.solde(100.0, 23 * HEURE)
        self.ligne(232.38, 12 * HEURE)
        self.solde(65.03, HEURE)
        alertes = self.drift()
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual((alerte["type"], alerte["provider"]), ("budget_drift", "deepseek"))
        self.assertAlmostEqual(alerte["counted_usd"], 232.38)
        self.assertAlmostEqual(alerte["real_usd"], 34.97, places=4)
        self.assertAlmostEqual(alerte["value"], 232.38 / 34.97 - 1, places=3)
        self.assertEqual(alerte["threshold"], 0.30)
        self.assertIn("232.38 $ counted against 34.97 $ of real balance drop (+565%)",
                      alerte["detail"])
        # une alerte par jour UTC : la clé ne bouge pas d'un passage à l'autre
        self.assertEqual(alerte["since"] % 86400, 0)

    def test_sous_estimation_aussi(self):
        self.solde(100.0, 23 * HEURE)
        self.ligne(5.0, 12 * HEURE)
        self.solde(90.0, HEURE)
        self.assertAlmostEqual(self.drift()[0]["value"], -0.5)

    def test_dans_la_marge_rien(self):
        self.solde(100.0, 23 * HEURE)
        self.ligne(12.5, 12 * HEURE)                    # +25 %
        self.solde(90.0, HEURE)
        self.assertEqual(self.drift(), [])
        # seuil réglable
        self.assertEqual(len(self.drift(AMEESH_BUDGET_DRIFT="0.2")), 1)
        self.assertEqual(self.drift(AMEESH_BUDGET_DRIFT="nan"), [])

    def test_petits_montants_ou_sans_releve_rien(self):
        self.solde(100.0, 23 * HEURE)
        self.ligne(0.4, 12 * HEURE)                     # +300 % mais 0,30 $
        self.solde(99.9, HEURE)
        self.assertEqual(self.drift(), [])
        self.db.execute("DELETE FROM provider_balances")
        self.ligne(50.0, 2 * HEURE)
        self.assertEqual(self.drift(), [])

    def test_rien_depense_beaucoup_compte(self):
        self.solde(100.0, 23 * HEURE)
        self.ligne(5.0, 12 * HEURE)
        self.solde(100.0, HEURE)
        alerte = self.drift()[0]
        self.assertIsNone(alerte["value"])
        self.assertIn("(nothing spent)", alerte["detail"])

    def test_enregistree_et_poussee(self):
        self.assertIn("budget_drift", exploitation.ALERT_TYPES)
        self.assertIn("budget_drift", notify.DEFAULT_TYPES)
        self.assertIn("budget_drift", notify.TYPE_LABELS)
        self.solde(100.0, 23 * HEURE)
        self.ligne(50.0, 12 * HEURE)
        self.solde(90.0, HEURE)
        with mock.patch.object(cost, "paid_harnesses_of", return_value=("deepseek",)):
            kinds = [a["type"] for a in exploitation.alerts(self.cfg, self.db)]
        self.assertIn("budget_drift", kinds)


if __name__ == "__main__":
    unittest.main()
