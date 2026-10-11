# SPDX-License-Identifier: AGPL-3.0-only
"""L225 — prix claude au barème ; un tour sans tarif connu n'est jamais sous-évalué.

Constat (issue #185) : ni `DEFAULT_PRICES` ni le barème du poste ne nommaient
un modèle claude. Le repli L105 prenait alors le maximum de **tout** le barème
(`[1,25 ; 0,14 ; 10]`, tarifs codex/deepseek) : un tour opus était compté
environ quatre fois trop bas, sans un mot. Désormais :

* les tarifs claude officiels (relevés le 2026-10-11) sont au barème ;
* un modèle claude inconnu prend le majorant de la **famille claude** ;
* une famille sans tarif (ou un harnais sans famille) prend le repli prudent,
  maximum de tout le barème, marqué `price_fallback` : le tour reste dans la
  dépense et l'alerte `turn_unpriced` le dit ; seul un tour sans aucun barème
  est écarté (`void_reason`) ;
* dans les replis claude, les écritures de cache coûtent ×1,25 (5 min ou
  durée inconnue) ou ×2 (1 h) le prix d'entrée.

Postgres réel ; journaux factices.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ameesh import cost, exploitation, grand_livre, notify

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


class EcrituresDeCacheTest(Sandbox, PgTestCase):
    """Replis claude : écritures de cache ×1,25 (5 min ou durée inconnue), ×2 (1 h)."""

    def _cout_du_tour(self, usage: dict) -> float:
        nom = self.agent("a1", "claude", "claude-opus-5-5")
        self.events(nom, {"type": "system", "subtype": "init", "session_id": "s1"},
                    {"type": "assistant", "session_id": "s1", "message": {
                        "id": "m1", "content": [{"type": "text"}], "usage": usage}})
        return self.book().turn_usage(nom, model="claude-opus-5-5").usd

    def test_duree_inconnue_au_tarif_cinq_minutes(self):
        self.assertAlmostEqual(
            self._cout_du_tour({"cache_creation_input_tokens": 1_000_000}), 4.0 * 1.25)

    def test_cache_une_heure_au_double(self):
        self.assertAlmostEqual(self._cout_du_tour({
            "cache_creation_input_tokens": 1_000_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                               "ephemeral_5m_input_tokens": 0}}), 4.0 * 2)

    def test_melange_cinq_minutes_et_une_heure(self):
        self.assertAlmostEqual(self._cout_du_tour({
            "input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 400_000,
                               "ephemeral_5m_input_tokens": 600_000}}),
            4.0 + 4.0 * (0.6 * 1.25 + 0.4 * 2))


class SansTarifTest(Sandbox, PgTestCase):
    """Sans tarif du modèle ni de sa famille : repli prudent ET alerte ; seul un
    tour sans aucun barème est écarté (mesh-design, 2026-10-11)."""

    def _tour(self, nom: str) -> None:
        self.events(nom, {"type": "status", "phase": "step_end",
                          "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                    "outputTokens": 1_000_000}})

    def _alertes(self, **kw) -> list:
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == "turn_unpriced"]

    def test_famille_connue_sans_tarif_prend_le_repli_prudent_marque(self):
        prix = {"gpt-x": [3, 1, 30], "default-deepseek": [0.2, 0.2, 0.2]}
        triplet, marque = cost.price_for(prix, "claude", "claude-opus-x")
        self.assertEqual(triplet, [3.0, 1.0, 30.0])
        self.assertTrue(marque["price_fallback"].startswith(cost.FALLBACK_REASON))
        self.assertEqual(cost.price_for(prix, "codex", "gpt-y"), ([3.0, 1.0, 30.0], {}))
        triplet, marque = cost.price_for({}, "claude", "claude-opus-x")
        self.assertIsNone(triplet)
        self.assertTrue(marque["void_reason"].startswith(cost.UNPRICED_REASON))

    def test_harnais_sans_famille_reste_dans_la_depense_avec_alerte(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        tour = self.book().record(nom, model="modele-x")
        pire = cost.worst_price_of(cost.DEFAULT_PRICES)
        self.assertAlmostEqual(tour.usd, pire[0] + pire[2])
        self.assertIsNone(tour.void_reason)
        self.assertTrue(tour.price_fallback.startswith(cost.FALLBACK_REASON))
        self.assertIn("modele-x", tour.price_fallback)
        row = self.db.query("SELECT void_reason, price_fallback FROM turn_costs")[0]
        self.assertIsNone(row["void_reason"])
        self.assertEqual(row["price_fallback"], tour.price_fallback)
        self.assertAlmostEqual(self.book().spent("all", 3600), round(tour.usd, 6), places=5)

        alertes = self._alertes()
        self.assertEqual(len(alertes), 1, alertes)
        alerte = alertes[0]
        self.assertEqual((alerte["agent"], alerte["harness"], alerte["model"], alerte["value"],
                          alerte["voided"]), ("a1", "nouveau-harnais", "modele-x", 1, 0))
        self.assertAlmostEqual(alerte["usd"], round(tour.usd, 6), places=5)
        self.assertEqual(alerte["output_tokens"], 1_000_000)
        self.assertIn("turn_unpriced", notify.DEFAULT_TYPES)
        self.assertIn("turn_unpriced", notify.TYPE_LABELS)

    def test_sans_aucun_bareme_la_ligne_est_ecartee_et_alertee(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        with mock.patch.dict(cost.DEFAULT_PRICES, clear=True):
            tour = self.book().record(nom, model="modele-x")
        self.assertTrue(tour.void_reason.startswith(cost.UNPRICED_REASON), tour.void_reason)
        self.assertIsNone(tour.price_fallback)
        self.assertEqual(self.book().spent("all", 3600), 0.0)
        alertes = self._alertes()
        self.assertEqual([(a["value"], a["voided"]) for a in alertes], [(1, 1)])

    def test_tour_sans_jeton_n_est_pas_marque(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self.events(nom, {"type": "status", "phase": "step_end", "usage": {}})
        tour = self.book().record(nom, model="modele-x")
        self.assertIsNone(tour.price_fallback)
        self.assertIsNone(tour.void_reason)
        self.assertEqual(self._alertes(), [])

    def test_modele_nomme_au_bareme_local_n_est_pas_marque(self):
        chemin = os.path.join(self.tmp_cost, "prices.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write('{"modele-x": [1, 0.5, 2]}')
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        tour = self.book(prices_path=chemin).record(nom, model="modele-x")
        self.assertIsNone(tour.price_fallback)
        self.assertAlmostEqual(tour.usd, 3.0)
        self.assertEqual(self._alertes(), [])

    def test_alerte_limitee_aux_dernieres_24_heures(self):
        nom = self.agent("a1", "nouveau-harnais", "modele-x")
        self._tour(nom)
        self.book().record(nom, model="modele-x")
        plus_tard = time.time() + exploitation.UNPRICED_WINDOW_S + 60
        self.assertEqual(self._alertes(now=plus_tard), [])


def _ligne(ident, usd, ts, **extra):
    row = {"id": ident, "agent": "c1", "harness": "claude", "turn": "messages",
           "model": "claude-opus-5-5", "session": "s1", "account": None, "usd": usd,
           "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
           "cum_usd": None, "cum_input_tokens": None, "cum_cached_input_tokens": None,
           "cum_output_tokens": None, "recorded_ts": ts, "void_reason": None}
    row.update(extra)
    return row


class CumulEtEcrituresDeCacheTest(unittest.TestCase):
    """`cost correct` ne ramène jamais un premier tour sous son coût réel : ses
    écritures de cache comptent (`cache_write_tokens`, 0059), et une ligne qui
    ne les garde pas est laissée telle quelle (relectures codex P1)."""

    def _plan(self, rows):
        return grand_livre.plan(rows, prices=cost.DEFAULT_PRICES)

    # 600 000 jetons écrits en cache (×1,25 de 4 $/M) + 1 entrée + 1 sortie
    USD = 3.000024

    def test_premier_tour_seul_garde_ses_ecritures_de_cache(self):
        self.assertEqual(self._plan([_ligne(1, self.USD, 1000.0, input_tokens=1,
                                            output_tokens=1, cum_usd=self.USD,
                                            cache_write_tokens=600_000)]), [])

    def test_tour_suivant_apres_expiration_du_cache(self):
        # le tour suivant ne relit rien (cache expiré) : rien ne borne les
        # écritures par les relectures, seules les écritures gardées comptent
        rows = [_ligne(1, self.USD, 1000.0, input_tokens=1, output_tokens=1,
                       cum_usd=self.USD, cache_write_tokens=600_000),
                _ligne(2, 0.01, 9000.0, input_tokens=500, output_tokens=300,
                       cum_usd=self.USD + 0.01, cache_write_tokens=0)]
        self.assertEqual(self._plan(rows), [])

    def test_ligne_sans_ecritures_connues_gardee(self):
        rows = [_ligne(1, self.USD, 1000.0, input_tokens=1, output_tokens=1,
                       cum_usd=self.USD),
                _ligne(2, 0.01, 9000.0, input_tokens=500, output_tokens=300,
                       cum_usd=self.USD + 0.01)]
        self.assertEqual(self._plan(rows), [])

    def test_cumul_d_une_session_reprise_reste_corrige(self):
        rows = [_ligne(1, 66.98, 1000.0, input_tokens=6, cached_input_tokens=100_000,
                       output_tokens=892, cum_usd=66.98, cache_write_tokens=2_000),
                _ligne(2, 0.05, 1060.0, input_tokens=2, cached_input_tokens=100_000,
                       output_tokens=300, cum_usd=67.03, cache_write_tokens=0)]
        plan = self._plan(rows)
        self.assertEqual([(c["id"], c["kind"]) for c in plan], [(1, "cumul")])
        attendu = (_cout(cost.DEFAULT_PRICES["claude-opus-5-5"], 6, 100_000, 892)
                   + 2_000 * 4.0 * cost.CACHE_WRITE_1H / 1e6)
        self.assertAlmostEqual(plan[0]["after"]["usd"], attendu, places=6)


class PalierHaikuTest(unittest.TestCase):
    """Haiku 5.5 : chaque requête est facturée au palier de son invite
    (relecture codex P2)."""
    PRIX = cost.DEFAULT_PRICES["claude-haiku-5-5"]

    def _usd(self, usage, model="claude-haiku-5-5"):
        tier = cost.prompt_tier(cost.DEFAULT_PRICES, model)
        return cost._claude_usd(usage, *self.PRIX, tier)

    def test_petite_invite_au_palier_bas(self):
        usage = {"input_tokens": 10_000, "output_tokens": 1_000}
        self.assertAlmostEqual(self._usd(usage), 0.0015, places=9)

    def test_invite_au_dela_du_seuil_au_palier_haut(self):
        # le cache relu et écrit compte dans la longueur de l'invite
        usage = {"input_tokens": 10, "cache_read_input_tokens": 99_000,
                 "cache_creation_input_tokens": 1_000, "output_tokens": 100}
        attendu = (10 * 0.50 + 99_000 * 0.05 + 1_000 * 0.50 * 1.25 + 100 * 2.50) / 1e6
        self.assertAlmostEqual(self._usd(usage), attendu, places=9)

    def test_seuil_inclus_et_suffixes(self):
        usage = {"input_tokens": 100_000, "output_tokens": 0}
        self.assertAlmostEqual(self._usd(usage, "claude-haiku-5-5-20261001"), 0.01, places=9)

    def test_bareme_local_renomme_sans_palier(self):
        prix = dict(cost.DEFAULT_PRICES, **{"claude-haiku-5-5": [0.9, 0.09, 4.5]})
        self.assertIsNone(cost.prompt_tier(prix, "claude-haiku-5-5"))
        self.assertIsNone(cost.prompt_tier(cost.DEFAULT_PRICES, "claude-opus-5-5"))


class CleDeTurnUnpricedTest(unittest.TestCase):
    """Deux groupes du même agent, premiers dans la même seconde : deux
    alertes (relecture codex P2)."""

    def test_harnais_et_modele_entrent_dans_la_cle(self):
        def alerte(harness, model):
            return exploitation._alert("turn_unpriced", "a1", 1000.2, 1, 0, "",
                                       harness=harness, model=model)
        cles = {exploitation.alert_key(alerte("h1", "m1")),
                exploitation.alert_key(alerte("h1", "m2")),
                exploitation.alert_key(alerte("h2", "m1"))}
        self.assertEqual(len(cles), 3)
        self.assertEqual(exploitation.alert_key(alerte("h1", "m1")),
                         exploitation.alert_key(alerte("h1", "m1")))


if __name__ == "__main__":
    unittest.main()
