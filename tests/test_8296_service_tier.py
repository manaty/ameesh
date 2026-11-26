# SPDX-License-Identifier: AGPL-3.0-only
"""#8296 — grand livre : facturer le mode rapide Codex au tarif premium.

Le palier de service est un réglage de **lancement** (L26) : le runner le
connaît, le flux Codex ne le répète pas toujours. Le grand livre le relit dans
le marqueur de tour (durable, avant l'index de début), dans le flux quand il
l'annonce (un repli de Fast mode sert `default` et se facture Standard), puis
dans le journal du fil Codex (`turn_context`). Seul Codex a un palier : un
autre harnais garde son tarif, même si un réglage traîne dans son état.

Les prix sont ceux du barème local : Fast vaut 2× Standard, Flex 0.5×,
Ultrafast 6×, un palier inconnu prend le plus cher connu et porte
`price_fallback`. Aucun test ne lit les vrais journaux ni le vrai barème.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import cost, db as db_mod, ledger, storage

from .support import PgTestCase

#: 1 M d'entrée, 400 k relus, 100 k de sortie : les trois composantes varient.
USAGE = {"input_tokens": 1_000_000, "cached_input_tokens": 400_000,
         "output_tokens": 100_000}


def write(path: str, *events) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def standard_usd(model: str = "gpt-6.1-sol") -> float:
    """Le tour `USAGE` au tarif Standard du barème par défaut."""
    pin, pcache, pout = cost.DEFAULT_PRICES[model]
    uncached = USAGE["input_tokens"] - USAGE["cached_input_tokens"]
    return (uncached * pin + USAGE["cached_input_tokens"] * pcache
            + USAGE["output_tokens"] * pout) / 1e6


class Sandbox:
    """Un bac à sable disque : aucun journal réel, aucun appel distant."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8296-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state")
        self.codex = os.path.join(self.tmp, "codex-sessions")
        os.makedirs(self.state, exist_ok=True)
        self.prices_path = os.path.join(self.tmp, "prices.json")

    def book(self, **kw) -> cost.CostBook:
        kw.setdefault("state_dir", self.state)
        kw.setdefault("codex_sessions", self.codex)
        kw.setdefault("prices_path", self.prices_path)
        return cost.CostBook(**kw)

    def agent(self, name: str, tool: str, model: str = "") -> str:
        os.makedirs(os.path.join(self.state, name), exist_ok=True)
        for key, value in (("tool", tool), ("model", model)):
            with open(os.path.join(self.state, name, key), "w", encoding="utf-8") as fh:
                fh.write(value)
        return name

    def events(self, name: str, *events) -> None:
        write(os.path.join(self.state, name, "events.jsonl"), *events)

    def rollout(self, thread: str, *events) -> None:
        write(os.path.join(self.codex, "2026", "11", "26",
                           "rollout-2026-11-26T10-00-00-%s.jsonl" % thread), *events)


class ServiceTierPriceTest(unittest.TestCase):
    """Le barème par palier, pur (aucun journal, aucune base)."""

    def setUp(self) -> None:
        self.prices = cost.load_prices(None)

    def test_fast_vaut_deux_fois_standard(self):
        standard, mark = cost.price_for(self.prices, "codex", "gpt-6.1-sol")
        fast, fast_mark = cost.price_for(self.prices, "codex", "gpt-6.1-sol",
                                         service_tier="fast")
        priority, _ = cost.price_for(self.prices, "codex", "gpt-6.1-sol",
                                     service_tier="priority")
        self.assertEqual(mark, {})
        self.assertEqual(fast_mark, {})
        self.assertEqual(fast, priority)             # alias encore servi par l'API
        for base, premium in zip(standard, fast):
            self.assertAlmostEqual(premium, base * 2)

    def test_paliers_declares(self):
        for tier, factor in (("default", 1.0), ("auto", 1.0), ("flex", 0.5),
                             ("fast", 2.0), ("priority", 2.0), ("ultrafast", 6.0)):
            with self.subTest(tier=tier):
                got, mark = cost.price_for(self.prices, "codex", "gpt-6.1-sol",
                                           service_tier=tier)
                self.assertEqual(mark, {})
                self.assertAlmostEqual(got[0], self.prices["gpt-6.1-sol"][0] * factor)
        self.assertEqual(cost.service_tier_factor(" FAST ")[0], 2.0)

    def test_ultrafast_62_promo_trois_fois_puis_six(self):
        # B #46602 : GPT-6.2 Astra/Sol Ultrafast vaut 3× jusqu'au 2027-02-28
        # inclus (heure du Pacifique) puis 6×. Le 1er mars 2027 est encore en
        # PST : la bascule est à 08:00Z, pas à minuit UTC.
        prices = {"gpt-6.2-sol": [2.0, 0.1, 10.0]}
        before = datetime.datetime(2026, 11, 26, 12, 0,
                                   tzinfo=datetime.timezone.utc).timestamp()
        last = datetime.datetime(2027, 3, 1, 7, 59, 59,
                                 tzinfo=datetime.timezone.utc).timestamp()
        after = datetime.datetime(2027, 3, 1, 8, 0,
                                  tzinfo=datetime.timezone.utc).timestamp()
        for moment, factor in ((before, 3.0), (last, 3.0), (after, 6.0)):
            with self.subTest(moment=moment):
                got, mark = cost.price_for(
                    prices, "codex", "gpt-6.2-sol",
                    service_tier="ultrafast", moment=moment)
                self.assertEqual(mark, {})
                for base, value in zip(prices["gpt-6.2-sol"], got):
                    self.assertAlmostEqual(value, base * factor)
        astra, _ = cost.price_for(
            {"gpt-6.2-astra": [8.0, 0.4, 40.0]}, "codex", "gpt-6.2-astra",
            service_tier="ultrafast", moment=before)
        for value, expected in zip(astra, [24.0, 1.2, 120.0]):
            self.assertAlmostEqual(value, expected)
        # Un autre modèle garde 6× même pendant la promotion.
        other, mark = cost.price_for(self.prices, "codex", "gpt-6.1-sol",
                                     service_tier="ultrafast", moment=before)
        self.assertEqual(mark, {})
        self.assertAlmostEqual(other[0], self.prices["gpt-6.1-sol"][0] * 6)

    def test_palier_inconnu_repli_prudent_marque(self):
        got, mark = cost.price_for(self.prices, "codex", "gpt-6.1-sol",
                                   service_tier="warp")
        self.assertAlmostEqual(got[0], self.prices["gpt-6.1-sol"][0]
                               * cost.SERVICE_TIER_FALLBACK)
        self.assertIn("price_fallback", mark)
        self.assertIn("warp", mark["price_fallback"])
        # le repli ne descend jamais sous le tarif Standard : jamais silencieux
        self.assertGreater(cost.SERVICE_TIER_FALLBACK, 1.0)

    def test_autre_harnais_ignore_le_palier(self):
        standard, _ = cost.price_for(self.prices, "claude", "claude-sonnet-5-5")
        fast, mark = cost.price_for(self.prices, "claude", "claude-sonnet-5-5",
                                    service_tier="fast")
        self.assertEqual(mark, {})
        self.assertEqual(standard, fast)

    def test_modele_inconnu_fast_majore_la_famille(self):
        got, mark = cost.price_for(self.prices, "codex", "gpt-inconnu",
                                   service_tier="fast")
        worst = cost.max_price_of(self.prices, "codex")
        self.assertEqual(mark, {})
        self.assertAlmostEqual(got[0], worst[0] * 2)
        unknown, unknown_mark = cost.price_for(self.prices, "codex", "gpt-inconnu",
                                               service_tier="warp")
        self.assertAlmostEqual(unknown[0], worst[0] * cost.SERVICE_TIER_FALLBACK)
        self.assertIn("price_fallback", unknown_mark)


class TurnServiceTierTest(Sandbox, unittest.TestCase):
    """La résolution du palier d'un tour et son effet sur le coût."""

    #: instant du marqueur de tour (epoch) ; les `task_started` du journal
    #: tombent juste après.
    NOW = 1_800_000_000.0

    def _tour(self, name: str, tier_marker: str | None, *extra):
        """Un tour Codex : marqueur (s'il est donné) puis flux du tour."""
        marker = {"type": cost.TURN_MARKER, "resume": "", "agent": name,
                  "ts": self.NOW}
        if tier_marker is not None:
            marker["service_tier"] = tier_marker
        self.agent(name, "codex", "gpt-6.1-sol")
        self.events(name, marker,
                    {"type": "thread.started", "thread_id": "t-" + name},
                    {"type": "turn.completed", "usage": USAGE}, *extra)

    def _journal(self, thread: str, *turns) -> None:
        """Un rollout : un `task_started` + son `turn_context` par tour."""
        events = []
        for index, (started, tier) in enumerate(turns):
            turn_id = "c%d" % index
            events.append({"type": "event_msg", "payload": {
                "type": "task_started", "started_at": started,
                "turn_id": turn_id}})
            payload = {"model": "gpt-6.1-sol", "turn_id": turn_id}
            if tier:
                payload["service_tier"] = tier
            events.append({"type": "turn_context", "payload": payload})
        self.rollout(thread, *events)

    def _journal_exact(self, thread: str, *turns) -> None:
        """Un rollout dont les `task_started` portent aussi l'ISO exact.

        Le payload garde `started_at` arrondi, comme le vrai journal : c'est
        la provenance exacte (timestamp ISO) qui doit primer, et ne jamais
        passer pour un début tronqué.
        """
        events = []
        for index, (started, tier) in enumerate(turns):
            turn_id = "c%d" % index
            stamp = datetime.datetime.fromtimestamp(
                started, datetime.timezone.utc).isoformat().replace(
                    "+00:00", "Z")
            events.append({"timestamp": stamp, "type": "event_msg",
                           "payload": {"type": "task_started",
                                       "started_at": int(started),
                                       "turn_id": turn_id}})
            payload = {"model": "gpt-6.1-sol", "turn_id": turn_id}
            if tier:
                payload["service_tier"] = tier
            events.append({"type": "turn_context", "payload": payload})
        self.rollout(thread, *events)

    def test_tour_ultrafast_62_utilise_la_date_de_fin(self):
        # B #46602 : après le 2027-02-28 (Pacifique), GPT-6.2 Sol Ultrafast
        # vaut 6×. La date du tour doit être transmise au barème : sans elle,
        # le prix par défaut (promo) sous-facturerait.
        with open(self.prices_path, "w", encoding="utf-8") as fh:
            json.dump({"gpt-6.2-sol": [2.0, 0.1, 10.0]}, fh)
        self.NOW = datetime.datetime(2027, 3, 1, 9, 0,
                                     tzinfo=datetime.timezone.utc).timestamp()
        self.agent("promo", "codex", "gpt-6.2-sol")
        self.events("promo",
                    {"type": cost.TURN_MARKER, "resume": "", "agent": "promo",
                     "ts": self.NOW, "service_tier": "ultrafast"},
                    {"type": "thread.started", "thread_id": "t-promo"},
                    {"type": "turn.completed", "usage": USAGE})
        usage = self.book().turn_usage("promo", start=1, ended=self.NOW)
        self.assertEqual(usage.service_tier, "ultrafast")
        # 0,6 M entrées non cachées × 2 $ + 0,4 M relues × 0,1 $ + 0,1 M
        # sorties × 10 $ = 2,24 $ Standard, ×6 après la promotion.
        self.assertAlmostEqual(usage.usd, 2.24 * 6, places=9)

    def test_marqueur_fast_double_le_tour_meme_jetons(self):
        self._tour("rapide", "fast")
        self._tour("standard", "default")
        fast = self.book().turn_usage("rapide", start=1)
        std = self.book().turn_usage("standard", start=1)
        self.assertEqual(fast.service_tier, "fast")
        self.assertEqual(std.service_tier, "default")
        self.assertAlmostEqual(std.usd, standard_usd(), places=9)
        self.assertAlmostEqual(fast.usd, std.usd * 2, places=9)

    def test_flux_default_gagne_sur_le_marqueur_fast(self):
        # Repli de Fast mode (ramp rate) : la réponse dit `default`, donc le
        # tour est facturé Standard, pas au palier demandé.
        self.agent("replie", "codex", "gpt-6.1-sol")
        self.events("replie",
                    {"type": cost.TURN_MARKER, "resume": "", "agent": "replie",
                     "service_tier": "fast"},
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "service_tier": "default",
                     "usage": USAGE})
        usage = self.book().turn_usage("replie", start=1)
        self.assertEqual(usage.service_tier, "default")
        self.assertAlmostEqual(usage.usd, standard_usd(), places=9)

    def test_marqueur_du_lancement_survit_a_un_changement_de_reglage(self):
        # Le réglage de l'agent passe à `default` après le lancement, avant la
        # réparation par un autre worker : le marqueur du tour fait foi.
        self._tour("lance", "fast")
        with open(os.path.join(self.state, "lance", "tier"), "w",
                  encoding="utf-8") as fh:
            fh.write("default")
        usage = self.book().turn_usage("lance", start=1)
        self.assertEqual(usage.service_tier, "fast")
        self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)

    def test_marqueur_du_lancement_prime_sur_le_parametre_de_l_appelant(self):
        # Un worker qui répare passe le réglage courant (`fast`) : le marqueur
        # du tour, écrit au lancement avec `default`, reste la preuve.
        self._tour("lance", "fast")
        usage = self.book().turn_usage("lance", start=1, service_tier="default")
        self.assertEqual(usage.service_tier, "fast")
        self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)
        self._tour("standard", "default")
        usage = self.book().turn_usage("standard", start=1, service_tier="fast")
        self.assertEqual(usage.service_tier, "default")
        self.assertAlmostEqual(usage.usd, standard_usd(), places=9)

    def test_marqueur_sans_override_lit_le_journal(self):
        # B de la revue : sans `--tier`, le runner n'écrit plus de clé ; un
        # marqueur vide (ancien binaire #8296) ne dit rien non plus. La
        # configuration native du compte peut avoir choisi Fast : le journal
        # du tour fait foi, sans `price_fallback`.
        for name, marker in (("sanscle", None), ("vide", "")):
            with self.subTest(marker=marker):
                self._tour(name, marker)
                self._journal("t-" + name, (self.NOW + 1.0, "fast"))
                usage = self.book().turn_usage(name, start=1)
                self.assertEqual(usage.service_tier, "fast")
                self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)
                self.assertIsNone(usage.price_fallback)

    def test_journal_ne_prend_pas_le_contexte_du_tour_suivant(self):
        # B de la revue : le journal a avancé (contexte `default` à +10 min) ;
        # le palier du tour mesuré reste celui de SON `turn_context`.
        self._tour("ancien", None)
        self._journal("t-ancien", (self.NOW + 1.0, "fast"),
                      (self.NOW + 600.0, "default"))
        usage = self.book().turn_usage("ancien", start=1,
                                       ended=self.NOW + 100.0)
        self.assertEqual(usage.service_tier, "fast")
        self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)

    def test_journal_ne_prend_pas_le_tour_precedent_dans_la_tolerance(self):
        # Cas de la revue : un `task_started` à −2 s (default) précède le vrai
        # début à +1 s (fast). Le tour mesuré commence après le marqueur ;
        # l'ancien à −2 s ne doit pas gagner parce qu'il est le premier.
        self._tour("precedent", None)
        self._journal("t-precedent", (self.NOW - 2.0, "default"),
                      (self.NOW + 1.0, "fast"))
        usage = self.book().turn_usage("precedent", start=1,
                                       ended=self.NOW + 100.0)
        self.assertEqual(usage.service_tier, "fast")
        self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)

    def test_journal_meme_seconde_quand_un_debut_suit_la_fenetre(self):
        # Cas de la revue du 26/11 14:01 : marqueur à 10:00:00,250Z, vrai
        # `task_started` entier à 10:00:00Z (même seconde, avant le marqueur),
        # puis un tour à +10 min hors de la fenêtre. Le début hors fenêtre ne
        # doit pas faire perdre le repli de même seconde : le tour fast connu
        # reste fast, et n'est pas facturé au tarif standard.
        self.NOW = 1_800_000_000.25
        self._tour("borne", None)
        self._journal("t-borne", (1_800_000_000.0, "fast"),
                      (self.NOW + 600.0, "default"))
        usage = self.book().turn_usage("borne", start=1,
                                       ended=self.NOW + 100.0)
        self.assertEqual(usage.service_tier, "fast")
        self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)

    def test_journal_horodatage_exact_avant_le_marqueur_ne_prend_pas_le_repli(self):
        # B #46592 : un timestamp ISO exact à 10:00:00,100Z avant le marqueur
        # à 10:00:00,750Z prouve un début réellement antérieur, donc le tour
        # précédent. Le repli de même seconde ne vaut que pour `started_at`
        # tronqué : ne pas facturer ce tour fast au tour mesuré.
        self.NOW = 1_800_000_000.75
        self._tour("exactavant", None)
        self._journal_exact("t-exactavant", (1_800_000_000.1, "fast"),
                            (self.NOW + 600.0, "default"))
        usage = self.book().turn_usage("exactavant", start=1,
                                       ended=self.NOW + 100.0)
        self.assertIsNone(usage.service_tier)
        self.assertAlmostEqual(usage.usd, standard_usd(), places=9)

    def test_journal_horodatage_exact_apres_le_marqueur_reste_preuve(self):
        # Le timestamp ISO exact reste la preuve du tour mesuré quand il
        # commence après le marqueur.
        self.NOW = 1_800_000_000.75
        self._tour("exactapres", None)
        self._journal_exact("t-exactapres", (self.NOW + 0.1, "fast"))
        usage = self.book().turn_usage("exactapres", start=1,
                                       ended=self.NOW + 100.0)
        self.assertEqual(usage.service_tier, "fast")
        self.assertAlmostEqual(usage.usd, standard_usd() * 2, places=9)

    def test_horodatage_exact_du_rollout_prime_sur_la_seconde_arrondie(self):
        event = {"timestamp": "1970-01-01T00:00:01.500Z",
                 "payload": {"type": "task_started", "started_at": 1}}
        self.assertAlmostEqual(cost._codex_task_started_ts(event), 1.5)
        self.assertEqual(cost._codex_task_started(event), (1.5, True))

    def test_journal_meme_seconde_ambigu_ne_devine_pas(self):
        # `started_at` est tronqué à la seconde : deux débuts dans la seconde
        # du marqueur, aucun après lui. Le tour ne peut pas être prouvé.
        self.NOW = 1_800_000_000.5
        self._tour("ambigu2", None)
        self._journal("t-ambigu2", (1_800_000_000.0, "default"),
                      (1_800_000_000.0, "fast"))
        usage = self.book().turn_usage("ambigu2", start=1,
                                       ended=self.NOW + 100.0)
        self.assertIsNone(usage.service_tier)
        self.assertAlmostEqual(usage.usd, standard_usd(), places=9)

    def test_journal_avance_sans_fenetre_ne_devine_pas(self):
        # Sans instant de début, deux tours dans le journal : aucun palier
        # n'est prouvé. Facturer Standard serait silencieux, emprunter le
        # dernier contexte serait faux : None, et la ligne porte le défaut.
        self.agent("ambigu", "codex", "gpt-6.1-sol")
        self.events("ambigu",
                    {"type": cost.TURN_MARKER, "resume": "", "agent": "ambigu"},
                    {"type": "thread.started", "thread_id": "t-ambigu"},
                    {"type": "turn.completed", "usage": USAGE})
        self._journal("t-ambigu", (self.NOW + 1.0, "fast"),
                      (self.NOW + 600.0, "default"))
        usage = self.book().turn_usage("ambigu", start=1)
        self.assertIsNone(usage.service_tier)
        self.assertAlmostEqual(usage.usd, standard_usd(), places=9)

    def test_marqueur_default_prime_sur_le_journal(self):
        # Un marqueur #8296 qui dit `default` est une preuve : le journal du
        # fil (qui peut être plus ancien) ne le contredit pas.
        self._tour("neuf", "default")
        self._journal("t-neuf", (self.NOW + 1.0, "fast"))
        usage = self.book().turn_usage("neuf", start=1)
        self.assertEqual(usage.service_tier, "default")
        self.assertAlmostEqual(usage.usd, standard_usd(), places=9)


    def test_autre_harnais_ne_prend_pas_de_palier(self):
        self.agent("claude", "claude", "claude-sonnet-5-5")
        self.events("claude",
                    {"type": cost.TURN_MARKER, "resume": "", "agent": "claude",
                     "service_tier": "fast"},
                    {"type": "result", "session_id": "s1", "total_cost_usd": 1.0,
                     "usage": {"input_tokens": 1}})
        usage = self.book().turn_usage("claude", start=1)
        self.assertIsNone(usage.service_tier)
        self.assertAlmostEqual(usage.usd, 1.0)


class LedgerCorrectionTest(unittest.TestCase):
    """Une correction du grand livre ne réécrit pas un tour rapide au Standard."""

    @staticmethod
    def _row(**kw) -> dict:
        row = {
            "id": 1, "agent": "a1", "harness": "codex", "turn": None,
            "model": "", "session": None, "usd": 1.0,
            "input_tokens": 1_000_000, "cached_input_tokens": 0,
            "output_tokens": 0, "cum_usd": None, "cum_input_tokens": None,
            "cum_cached_input_tokens": None, "cum_output_tokens": None,
            "recorded_ts": 1_800_000_000.0, "void_reason": None, "source": None,
            "attribution": None, "cache_write_tokens": None,
            "service_tier": None,
        }
        row.update(kw)
        return row

    def _plan(self, row: dict, **kw) -> dict:
        corrections = ledger.plan(
            [row], prices=cost.load_prices(None),
            default_model_of=lambda harness: "gpt-6.1-sol", **kw)
        self.assertEqual(len(corrections), 1)
        return corrections[0]

    def test_modele_inconnu_fast_reste_fast(self):
        fast = self._plan(self._row(service_tier="fast"))
        standard = self._plan(self._row(service_tier="default"))
        self.assertAlmostEqual(fast["after"]["usd"], 2.50, places=6)
        self.assertAlmostEqual(standard["after"]["usd"], 1.25, places=6)

    def test_cumul_du_fil_fast_reste_fast(self):
        thread = "0192f000-0000-7000-8000-000000000001"
        rollout = os.path.join(tempfile.mkdtemp(prefix="ameesh-8296-ledger-"),
                               "rollout-%s.jsonl" % thread)
        self.addCleanup(shutil.rmtree, os.path.dirname(rollout), ignore_errors=True)
        write(rollout,
              {"type": "event_msg", "payload": {
                  "type": "token_count", "info": {"total_token_usage": {
                      "input_tokens": 500_000, "cached_input_tokens": 0,
                      "output_tokens": 0}}}},
              {"type": "event_msg", "payload": {"type": "task_started"}},
              {"type": "event_msg", "payload": {
                  "type": "token_count", "info": {"total_token_usage": {
                      "input_tokens": 1_000_000, "cached_input_tokens": 0,
                      "output_tokens": 0}}}})
        row = self._row(session=thread, service_tier="fast",
                        cum_input_tokens=1_000_000,
                        cum_cached_input_tokens=0, cum_output_tokens=0)
        fast = self._plan(row, codex_rollout=lambda _thread: rollout)
        # 500 k entrées non cachées au tarif Fast de gpt-6.1-sol (2,50 $/M)
        self.assertAlmostEqual(fast["after"]["usd"], 1.25, places=6)
        row["service_tier"] = "default"
        standard = self._plan(row, codex_rollout=lambda _thread: rollout)
        self.assertAlmostEqual(standard["after"]["usd"], 0.625, places=6)


class MediatedCompatTest(unittest.TestCase):
    """Un serveur 1.16 ne connaît pas `service_tier` : il perd la donnée, pas
    le tour (le coût reste compté au barème local)."""

    def _book(self) -> cost.CostBook:
        return cost.CostBook(state_dir=tempfile.mkdtemp(prefix="ameesh-8296-"),
                             db=object())

    def test_parametre_absent_quand_le_palier_est_inconnu(self):
        calls = []
        fake = SimpleNamespace(turn_costs=SimpleNamespace(
            insert=lambda **kw: calls.append(dict(kw))))
        with mock.patch.object(cost.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(agent="a1", spend_key="k")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("service_tier", calls[0])

    def test_serveur_116_retente_sans_le_palier(self):
        calls = []

        def insert(**kw):
            calls.append(dict(kw))
            if "service_tier" in kw:
                raise db_mod.DbError(
                    "bad_args (400) : turn_costs.insert : paramètre inconnu service_tier")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(agent="a1", spend_key="k",
                                           service_tier="fast")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["service_tier"], "fast")
        self.assertNotIn("service_tier", calls[1])

    def test_serveur_116_ne_perd_que_le_palier(self):
        # Le refus nomme `service_tier` : `attribution` et `inputs`, que ce
        # serveur accepte, ne doivent pas être retirés par ricochet.
        calls = []

        def insert(**kw):
            calls.append(dict(kw))
            if "service_tier" in kw:
                raise db_mod.DbError(
                    "bad_args (400) : turn_costs.insert : paramètre inconnu service_tier")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(
                agent="a1", spend_key="k", service_tier="fast",
                attribution={"source": "ima"}, inputs=("m1",))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["attribution"], {"source": "ima"})
        self.assertEqual(calls[1]["inputs"], ("m1",))
        self.assertNotIn("service_tier", calls[1])

    def test_une_vraie_panne_nest_pas_avalee(self):
        def insert(**kw):
            raise db_mod.DbError("connexion perdue")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost.storage, "of", return_value=fake):
            with self.assertRaises(db_mod.DbError):
                self._book()._insert_turn_cost(agent="a1", service_tier="fast")


class TurnCostsDbTest(Sandbox, PgTestCase):
    """La ligne, la dépense et la vue budget portent le palier."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")

    def test_ligne_et_spent_portent_la_prime(self):
        self.agent("a1", "codex", "gpt-6.1-sol")
        self.events("a1",
                    {"type": cost.TURN_MARKER, "resume": "", "agent": "a1",
                     "service_tier": "fast"},
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": USAGE})
        usage = self.book(db=self.db).record("a1", start=1)
        self.assertEqual(usage.service_tier, "fast")
        row = self.db.query("SELECT service_tier, usd::float8 AS usd"
                            " FROM turn_costs ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(row["service_tier"], "fast")
        self.assertAlmostEqual(row["usd"], standard_usd() * 2, places=9)
        self.assertAlmostEqual(self.book(db=self.db).spent("a1", 3600),
                               standard_usd() * 2, places=9)
        turns = storage.of(self.db).operations.turns(agent="a1", since_s=3600,
                                                     limit=10)
        self.assertEqual(turns[0]["service_tier"], "fast")

    def test_ligne_ecrite_sans_palier_reste_lisible(self):
        # N-1 : une ligne d'avant la 0609 a la colonne NULL ; la vue la lit
        # sans erreur et sans inventer de palier.
        self.agent("a1", "codex", "gpt-6.1-sol")
        self.events("a1",
                    {"type": cost.TURN_MARKER, "resume": "", "agent": "a1"},
                    {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": USAGE})
        self.book(db=self.db).record("a1", start=1)
        self.db.execute("UPDATE turn_costs SET service_tier = NULL")
        turns = storage.of(self.db).operations.turns(agent="a1", since_s=3600,
                                                     limit=10)
        self.assertIsNone(turns[0]["service_tier"])

    def test_migration_0609_presente(self):
        rows = self.db.query(
            "SELECT version, name FROM schema_migrations WHERE version = 609")
        self.assertEqual([(r["version"], r["name"]) for r in rows],
                         [(609, "turn_costs_service_tier")])
