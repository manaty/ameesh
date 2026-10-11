# SPDX-License-Identifier: AGPL-3.0-only
"""L225 — prix claude au barème ; un tour sans tarif connu n'est jamais sous-évalué.

Constat (issue #185) : ni `DEFAULT_PRICES` ni le barème du poste ne nommaient
un modèle claude. Le repli L105 prenait alors le maximum de **tout** le barème
(`[1,25 ; 0,14 ; 10]`, tarifs codex/deepseek) : un tour opus était compté
environ quatre fois trop bas, sans un mot. Désormais :

* les tarifs claude officiels (relevés le 2026-10-11) sont au barème ;
* un modèle claude inconnu prend le majorant de la **famille claude** ;
* une famille sans aucun tarif connu n'a pas de majorant : la ligne naît
  écartée (`void_reason`) et l'alerte `turn_unpriced` le dit.

Postgres réel ; journaux factices.
"""
from __future__ import annotations

import os
import time
import unittest

from ameesh import cost, exploitation, notify

from .support import PgTestCase
from .test_cost import Sandbox


def _appel(ident: str, entree: int, relus: int, sortie: int, ecrits: int = 0) -> dict:
    return {"type": "assistant", "session_id": "s1", "message": {
        "id": ident, "content": [{"type": "text"}],
        "usage": {"input_tokens": entree, "cache_creation_input_tokens": ecrits,
                  "cache_read_input_tokens": relus, "output_tokens": sortie}}}


def _cout(triplet, entree: int, relus: int, sortie: int) -> float:
    return (entree * triplet[0] + relus * triplet[1] + sortie * triplet[2]) / 1e6


class BaremeClaudeTest(unittest.TestCase):
    def test_tarifs_officiels_des_modeles_courants(self):
        # platform.claude.com/docs/en/about-claude/pricing, relevé du 2026-10-11
        attendus = {"claude-fable-5-1": [10.0, 0.25, 50.0],
                    "claude-opus-5-5": [4.0, 0.20, 20.0],
                    "claude-sonnet-5-5": [2.0, 0.10, 10.0],
                    "claude-haiku-5-5": [0.50, 0.05, 2.50]}
        for modele, triplet in attendus.items():
            with self.subTest(modele=modele):
                self.assertEqual(cost.DEFAULT_PRICES[modele], triplet)

    def test_majorant_claude_borne_chaque_modele_claude_et_pas_les_tarifs_codex(self):
        majorant = cost.max_price_of(cost.DEFAULT_PRICES, "claude")
        for cle, triplet in cost.DEFAULT_PRICES.items():
            if cle.startswith("claude-"):
                for i in range(3):
                    self.assertGreaterEqual(majorant[i], triplet[i], cle)
        # le repli d'avant L225 : le pire de tout le barème, tarifs codex
        self.assertGreater(majorant[2], 10.0)

    def test_suffixes_de_variante_et_de_date_gardent_le_tarif(self):
        prix = cost.DEFAULT_PRICES
        self.assertEqual(cost.known_price(prix, "claude-opus-5-5[1m]"), prix["claude-opus-5-5"])
        self.assertEqual(cost.known_price(prix, "claude-haiku-4-5-20251001"),
                         prix["claude-haiku-4-5"])
        self.assertIsNone(cost.known_price(prix, "claude-inconnu-9"))
        self.assertIsNone(cost.known_price(prix, "default-claude"))
        self.assertIsNone(cost.known_price(prix, ""))


class ReplisClaudeTest(Sandbox, PgTestCase):
    def _tour_sans_result(self, nom: str) -> None:
        # L105 : tour clos avant son `result`, compté appel par appel au barème
        self.events(nom, {"type": "system", "subtype": "init", "session_id": "s1"},
                    _appel("m1", 1_000, 2_000_000, 500),
                    _appel("m2", 3_000, 6_000_000, 1_500))

    def test_tour_opus_au_tarif_opus(self):
        nom = self.agent("a1", "claude", "claude-opus-5-5")
        self._tour_sans_result(nom)
        tour = self.book().record(nom)
        self.assertEqual(tour.model, "claude-opus-5-5")
        self.assertAlmostEqual(tour.usd, _cout([4.0, 0.20, 20.0], 4_000, 8_000_000, 2_000))
        self.assertIsNone(tour.void_reason)

    def test_modele_claude_inconnu_au_majorant_claude_jamais_aux_tarifs_codex(self):
        nom = self.agent("a1", "claude", "")
        self._tour_sans_result(nom)
        tour = self.book().record(nom, model="")
        majorant = cost.max_price_of(cost.DEFAULT_PRICES, "claude")
        self.assertAlmostEqual(tour.usd, _cout(majorant, 4_000, 8_000_000, 2_000))
        self.assertGreater(tour.usd, _cout([1.25, 0.14, 10.0], 4_000, 8_000_000, 2_000))
        self.assertIsNone(tour.void_reason)
        self.assertAlmostEqual(self.book().spent("a1", 3600), round(tour.usd, 6), places=5)

    def test_result_normal_garde_le_cout_du_harnais(self):
        """Le barème ne sert qu'aux replis : un `result` complet fait foi."""
        nom = self.agent("a1", "claude", "claude-opus-5-5")
        self.events(nom, {"type": "system", "subtype": "init", "session_id": "s1"},
                    _appel("m1", 10, 100, 10),
                    {"type": "result", "session_id": "s1", "total_cost_usd": 0.42,
                     "usage": {"input_tokens": 10, "output_tokens": 10}})
        self.assertAlmostEqual(self.book().record(nom).usd, 0.42)


class SansTarifTest(Sandbox, PgTestCase):
    """Une famille sans aucun tarif connu : ligne écartée, alerte levée."""

    def _tour(self, nom: str) -> None:
        self.events(nom, {"type": "status", "phase": "step_end",
                          "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                    "outputTokens": 1_000_000}})

    def test_ligne_ecartee_des_sommes_et_alerte(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        tour = self.book().record(nom, model="modele-x")
        self.assertTrue(tour.void_reason.startswith(cost.UNPRICED_REASON), tour.void_reason)
        self.assertIn("modele-x", tour.void_reason)
        row = self.db.query("SELECT usd::float8 AS usd, void_reason FROM turn_costs")[0]
        self.assertEqual(row["usd"], 0.0)
        self.assertEqual(row["void_reason"], tour.void_reason)
        self.assertEqual(self.book().spent("all", 3600), 0.0)

        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "turn_unpriced"]
        self.assertEqual(len(alertes), 1, alertes)
        alerte = alertes[0]
        self.assertEqual((alerte["agent"], alerte["harness"], alerte["model"], alerte["value"]),
                         ("a1", "nouveau-harnais", "modele-x", 1))
        self.assertEqual(alerte["output_tokens"], 1_000_000)
        self.assertIn("turn_unpriced", notify.DEFAULT_TYPES)
        self.assertIn("turn_unpriced", notify.TYPE_LABELS)

    def test_tour_sans_jeton_n_est_pas_ecarte(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self.events(nom, {"type": "status", "phase": "step_end", "usage": {}})
        self.assertIsNone(self.book().record(nom, model="modele-x").void_reason)
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "turn_unpriced"], [])

    def test_modele_nomme_au_bareme_local_n_est_pas_ecarte(self):
        chemin = os.path.join(self.tmp_cost, "prices.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write('{"modele-x": [1, 0.5, 2]}')
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        tour = self.book(prices_path=chemin).record(nom, model="modele-x")
        self.assertIsNone(tour.void_reason)
        self.assertAlmostEqual(tour.usd, 3.0)

    def test_alerte_limitee_aux_dernieres_24_heures(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        self.book().record(nom, model="modele-x")
        plus_tard = time.time() + exploitation.UNPRICED_WINDOW_S + 60
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=plus_tard)
                          if a["type"] == "turn_unpriced"], [])


if __name__ == "__main__":
    unittest.main()
