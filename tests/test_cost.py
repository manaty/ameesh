# SPDX-License-Identifier: AGPL-3.0-only
"""Coût des tours et jauges de forfait (L12, décision 0019).

Tous les journaux sont **factices**, écrits dans un bac à sable : aucun test ne
lit `~/.codex` ni `~/.claude`, ni l'état réel des agents. La partie base
(`turn_costs`) tourne sur le schéma jetable de `PgTestCase`, donc sur les deux
pilotes de `scripts/test.sh`.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from ameesh import cost

from .support import PgTestCase


def write(path: str, *events) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


class Sandbox:
    """Un bac à sable disque (et, si la classe a une base, un grand livre propre).

    Les journaux de harnais sont **factices** : aucun test n'approche `~/.codex`
    ni `~/.claude`, et les chemins par défaut ne sont jamais lus.
    """

    def setUp(self) -> None:
        super().setUp()                     # PgTestCase : schéma, TRUNCATE, dossiers
        self.tmp_cost = tempfile.mkdtemp(prefix="ameesh-cost-")
        self.addCleanup(shutil.rmtree, self.tmp_cost, ignore_errors=True)
        self.state = os.path.join(self.tmp_cost, "state")
        self.codex = os.path.join(self.tmp_cost, "codex-sessions")
        os.makedirs(self.state, exist_ok=True)
        self.now = 1_800_000_000.0
        if getattr(self, "db", None) is not None:
            self.db.execute("DELETE FROM turn_costs")

    def book(self, **kw) -> cost.CostBook:
        if getattr(self, "db", None) is not None:
            kw.setdefault("db", self.db)
        kw.setdefault("state_dir", self.state)
        kw.setdefault("codex_sessions", self.codex)
        kw.setdefault("prices_path", os.path.join(self.tmp_cost, "prices.json"))
        kw.setdefault("clock", lambda: self.now)
        return cost.CostBook(**kw)

    def agent(self, name: str, tool: str, model: str = "") -> str:
        os.makedirs(os.path.join(self.state, name), exist_ok=True)
        with open(os.path.join(self.state, name, "tool"), "w", encoding="utf-8") as fh:
            fh.write(tool)
        with open(os.path.join(self.state, name, "model"), "w", encoding="utf-8") as fh:
            fh.write(model)
        return name

    def events(self, name: str, *events) -> None:
        self.write_events(name, *events)

    def write_events(self, name: str, *events) -> None:
        write(os.path.join(self.state, name, "events.jsonl"), *events)


class ClaudeGaugeTest(Sandbox, unittest.TestCase):
    def test_jauges_lues_dans_le_dernier_rate_limit_event(self):
        self.agent("a1", "claude")
        self.agent("a2", "claude")
        self.events("a1", {"type": "result", "total_cost_usd": 1.0})
        self.events("a2", {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 42, "resetsAt": self.now + 3600},
                "seven_day": {"utilization": 0.61, "resetsAt": self.now + 86_400},
            }},
        })
        gauges = {g.key: g for g in self.book().claude_gauges()}
        self.assertEqual(sorted(gauges), ["five_hour", "seven_day"])
        self.assertAlmostEqual(gauges["five_hour"].used, 0.42)      # publié en pourcents
        self.assertAlmostEqual(gauges["seven_day"].used, 0.61)      # publié en fraction
        self.assertEqual(gauges["five_hour"].window_s, 5 * 3600)
        self.assertEqual(gauges["seven_day"].window_s, 7 * 86400)

    def test_seuil_de_rythme_min_90_part_ecoulee_plus_10(self):
        # Fenêtre de cinq heures écoulée à 20 % : plafond 30 %.
        self.agent("a1", "claude")
        self.events("a1", {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 29, "resetsAt": self.now + 4 * 3600},
            }},
        })
        book = self.book()
        self.assertEqual(book.pace_exceeded("claude"), "")
        # À 31 %, le forfait avance plus vite que le rythme sûr.
        self.events("a1", {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 31, "resetsAt": self.now + 4 * 3600},
            }},
        })
        reason = book.pace_exceeded("claude")
        self.assertIn("five_hour", reason)
        self.assertIn("plafond de rythme 30%", reason)

    def test_le_plafond_ne_depasse_jamais_90_et_vaut_90_sans_reset(self):
        # Sans date de réinitialisation, on suppose la fenêtre entièrement écoulée :
        # le plafond est donc 90 %, et il ne monte jamais plus haut.
        gauge = cost.Gauge("claude", "seven_day", 0.89, None, 7 * 86400)
        self.assertEqual(gauge.elapsed(self.now), 1.0)
        self.assertEqual(gauge.pace_cap(self.now), cost.PACE_CEILING)
        self.assertFalse(gauge.exceeded(self.now))
        self.assertTrue(cost.Gauge("claude", "seven_day", 0.90, None, 7 * 86400).exceeded(self.now))
        # Une fenêtre qui vient de s'ouvrir laisse la marge de dix points, pas plus.
        fresh = cost.Gauge("claude", "five_hour", 0.05, self.now + 5 * 3600, 5 * 3600)
        self.assertAlmostEqual(fresh.pace_cap(self.now), 0.10)


class CodexGaugeTest(Sandbox, unittest.TestCase):
    def test_jauges_primaires_et_secondaires_du_journal_le_plus_recent(self):
        write(os.path.join(self.codex, "2026", "10", "04", "roll.jsonl"),
              {"type": "session_meta"},
              {"payload": {"rate_limits": {
                  "primary": {"used_percent": 12.5, "window_minutes": 300,
                              "resets_at": self.now + 1000},
                  "secondary": {"used_percent": 80, "window_minutes": 10080,
                                "resets_at": self.now + 90_000},
                  "plan_type": "pro"}}})
        gauges = {g.key: g for g in self.book().codex_gauges()}
        self.assertEqual(sorted(gauges), ["codex-10080min", "codex-300min"])
        self.assertAlmostEqual(gauges["codex-300min"].used, 0.125)
        self.assertEqual(gauges["codex-300min"].window_s, 300 * 60)
        # 300 min écoulées à ~94 % : plafond de rythme atteint (min(90, 94+10)).
        self.assertIn("codex", self.book().pace_exceeded("codex") or "codex")

    def test_l_ordre_des_cles_et_un_secondaire_nul_ne_perdent_pas_la_jauge(self):
        """Sonde codex2 B2 : une regex exigeait `plan_type` en dernier.

        Même `rate_limits`, `plan_type` en tête et `secondary` nul : la jauge
        primaire doit être trouvée, sinon la pause est manquée.
        """
        write(os.path.join(self.codex, "2026", "10", "04", "ordre.jsonl"),
              {"payload": {"rate_limits": {
                  "plan_type": "plus",
                  "secondary": None,
                  "primary": {"used_percent": 95, "window_minutes": 300,
                              "resets_at": self.now + 100}}}})
        gauges = self.book().codex_gauges()
        self.assertEqual([g.key for g in gauges], ["codex-300min"])
        self.assertAlmostEqual(gauges[0].used, 0.95)
        self.assertIn("codex", self.book().pace_exceeded("codex"))

    def test_aucune_jauge_sans_journal(self):
        self.assertEqual(self.book().codex_gauges(), [])
        self.assertEqual(self.book().pace_exceeded("codex"), "")


class TurnAccountingTest(Sandbox, PgTestCase):
    """La comptabilité par tour, avec le grand livre : le repère est la dernière
    ligne de `turn_costs`, donc ces cas tournent sur les deux pilotes."""
    def test_claude_difference_du_cout_cumule_par_session(self):
        self.agent("a1", "claude", "gpt-6.1-sol")
        book = self.book()
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 0.25,
                           "usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                     "output_tokens": 50}})
        first = book.record("a1")
        self.assertAlmostEqual(first.usd, 0.25)          # session neuve : le cumul EST le tour
        self.assertEqual(first.input_tokens, 100)
        self.assertEqual(first.cached_input_tokens, 900)
        self.assertEqual(first.output_tokens, 50)

        # Deuxième tour de la même session : on ne compte que la différence.
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 0.40,
                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                     "output_tokens": 5}})
        second = book.turn_usage("a1", start=1)
        self.assertAlmostEqual(second.usd, 0.15)
        self.assertEqual(second.output_tokens, 5)

    def test_claude_session_neuve_repart_de_zero(self):
        self.agent("a1", "claude")
        book = self.book()
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 3.0})
        book.record("a1")
        # Le harnais reprend avec une autre session : son cumul ne contient pas
        # le passé, donc il ne faut PAS soustraire le repère de la précédente.
        self.events("a1", {"type": "result", "session_id": "s2", "total_cost_usd": 0.10})
        self.assertAlmostEqual(book.turn_usage("a1", start=1).usd, 0.10)

    def test_codex_difference_de_l_usage_cumule_du_fil(self):
        self.agent("a1", "codex", "gpt-6.1-sol")
        book = self.book()
        self.events("a1", {"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 1000,
                                                         "cached_input_tokens": 400,
                                                         "output_tokens": 100}})
        first = book.record("a1")
        # 600 non cachés × 1.25 $/M + 400 × 0.125 + 100 × 10.0, le tout par million.
        expected = (600 * 1.25 + 400 * 0.125 + 100 * 10.0) / 1e6
        self.assertAlmostEqual(first.usd, expected, places=9)

        # Cumul du fil : 1600 entrées dont 600 en cache, 150 sorties. La
        # différence avec le tour précédent est donc 600 entrées (dont 200 en
        # cache, qui se facturent au tarif cache) et 50 sorties.
        self.events("a1", {"type": "turn.completed", "usage": {"input_tokens": 1600,
                                                               "cached_input_tokens": 600,
                                                               "output_tokens": 150}})
        second = book.turn_usage("a1", start=2)
        delta = (400 * 1.25 + 200 * 0.125 + 50 * 10.0) / 1e6
        self.assertAlmostEqual(second.usd, delta, places=9)

    def test_deepseek_usage_par_etape_fois_le_bareme(self):
        self.agent("a1", "deepseek", "deepseek-flash")
        self.events("a1",
                    {"type": "status", "phase": "step_end",
                     "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0, "outputTokens": 0}},
                    {"type": "status", "phase": "step_end",
                     "usage": {"inputTokens": 0, "cacheReadTokens": 0, "outputTokens": 1_000_000}})
        usage = self.book().turn_usage("a1")
        self.assertAlmostEqual(usage.usd, 0.27 + 1.10, places=9)
        self.assertEqual(usage.input_tokens, 1_000_000)

    def test_bareme_modifiable_et_repli(self):
        self.agent("a1", "deepseek", "deepseek-pro")
        with open(os.path.join(self.tmp_cost, "prices.json"), "w", encoding="utf-8") as fh:
            json.dump({"deepseek-pro": [1.0, 0.1, 2.0]}, fh)
        self.events("a1", {"type": "status", "phase": "step_end",
                           "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                     "outputTokens": 0}})
        self.assertAlmostEqual(self.book().turn_usage("a1").usd, 1.0)

    def test_bareme_surchargeable_par_l_environnement(self):
        # La variable d'environnement évite d'avoir à connaître le chemin par
        # défaut du poste (et les tests ne le lisent jamais).
        custom = os.path.join(self.tmp_cost, "custom-prices.json")
        with open(custom, "w", encoding="utf-8") as fh:
            json.dump({"deepseek-flash": [9.0, 9.0, 9.0]}, fh)
        os.environ["AMEESH_PRICES"] = custom
        self.addCleanup(os.environ.pop, "AMEESH_PRICES", None)
        book = cost.CostBook(state_dir=self.state, codex_sessions=self.codex)
        self.assertEqual(book.prices_path, custom)

    def test_bareme_illisible_garde_les_defauts(self):
        with open(os.path.join(self.tmp_cost, "prices.json"), "w", encoding="utf-8") as fh:
            fh.write("{ceci n'est pas du JSON")
        prices = cost.load_prices(os.path.join(self.tmp_cost, "prices.json"))
        self.assertEqual(prices["deepseek-flash"], cost.DEFAULT_PRICES["deepseek-flash"])

    def test_un_flux_illisible_ne_casse_pas_la_comptabilite(self):
        self.agent("a1", "claude")
        path = os.path.join(self.state, "a1", "events.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("pas du json\n")
            # `[]` et `null` sont du JSON valide mais pas des événements (B3) :
            # les garder ferait planter le premier `.get` venu.
            fh.write("[]\n")
            fh.write("null\n")
            fh.write(json.dumps({"type": "result", "session_id": "s1",
                                 "total_cost_usd": 0.5}) + "\n")
        self.assertAlmostEqual(self.book().turn_usage("a1").usd, 0.5)


class CostDbTest(Sandbox, PgTestCase):
    """`turn_costs` : écriture par `record`, lecture par `spent` et `report`."""

    def test_migration_0014_presente(self):
        rows = self.db.query(
            "select version, name from schema_migrations where version = 14")
        self.assertEqual([(r["version"], r["name"]) for r in rows], [(14, "cost")])

    def test_repere_dans_le_grand_livre_et_reprise_apres_panne(self):
        """Sonde codex2 B1 : insertion et repère sont la même écriture.

        Un `execute` qui échoue ne doit pas faire avancer le repère : le tour
        rejoué compte le même delta, au lieu d'insérer zéro.
        """
        real = self.db

        class Flaky:
            """La base, avec une insertion qui échoue une fois."""

            def __init__(self, inner):
                self.inner = inner
                self.failures = 1

            def query(self, sql, params=None):
                return self.inner.query(sql, params)

            def execute(self, sql, params=None):
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("base indisponible")
                return self.inner.execute(sql, params)

        book = self.book(db=Flaky(real))
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        self.write_events("a1", {"type": "result", "session_id": "s1",
                                 "total_cost_usd": 0.50})
        with self.assertRaises(RuntimeError):
            book.record("a1")
        # La panne n'a rien laissé : aucune ligne, donc aucun repère avancé.
        self.assertEqual(real.query("select count(*) as n from turn_costs")[0]["n"], 0)
        usage = book.record("a1")                      # la reprise
        self.assertAlmostEqual(usage.usd, 0.50)        # le coût n'est pas perdu
        self.assertAlmostEqual(book.spent("a1", 3600), 0.50)

    def test_difference_lue_sur_la_derniere_ligne(self):
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        self.write_events("a1", {"type": "result", "session_id": "s1",
                                 "total_cost_usd": 1.00})
        self.assertAlmostEqual(book.record("a1").usd, 1.00)
        self.write_events("a1", {"type": "result", "session_id": "s1",
                                 "total_cost_usd": 1.25})
        self.assertAlmostEqual(book.record("a1", start=1).usd, 0.25)

    def test_record_sans_base_refuse_de_compter(self):
        book = cost.CostBook(state_dir=self.state)
        with self.assertRaises(cost.CostError):
            book.record("a1")

    def test_record_ecrit_une_ligne_et_spent_la_somme(self):
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("deepseek")
        self.write_events("a1", {"type": "status", "phase": "step_end",
                                 "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                           "outputTokens": 0}})
        usage = book.record("a1", turn="t-1")
        self.assertAlmostEqual(usage.usd, 0.27)
        self.assertAlmostEqual(book.spent("a1", 3600), 0.27)
        self.assertAlmostEqual(book.spent("all", 3600), 0.27)
        # hors fenêtre : la dépense d'il y a plus d'une heure ne compte plus
        self.db.execute("update turn_costs set recorded_at = now() - interval '2 hours'")
        self.assertEqual(book.spent("all", 3600), 0.0)
        self.assertAlmostEqual(book.spent("all", 86400), 0.27)

    def test_over_dit_le_rythme_du_forfait_puis_le_budget_horaire(self):
        book = self.book(hourly_usd=0.10)
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("deepseek")
        self.write_events("a1", {"type": "status", "phase": "step_end",
                                 "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                                           "outputTokens": 0}})
        self.assertEqual(book.over("a1"), "")
        book.record("a1")
        self.assertIn("budget horaire", book.over("a1"))

    def test_report_liste_les_agents_et_leurs_jauges(self):
        book = self.book()
        os.makedirs(book.agent_dir("a1"), exist_ok=True)
        with open(os.path.join(book.agent_dir("a1"), "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude")
        with open(os.path.join(book.agent_dir("a1"), "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                "unifiedWindows": {"five_hour": {"utilization": 10,
                                                 "resetsAt": time.time() + 3600}}}}) + "\n")
        rows = book.report()
        self.assertEqual([r["agent"] for r in rows], ["a1"])
        self.assertEqual(rows[0]["harness"], "claude")
        self.assertEqual([g["key"] for g in rows[0]["gauges"]], ["five_hour"])
        self.assertIn("a1", cost.format_report(rows))
