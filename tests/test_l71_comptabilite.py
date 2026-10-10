# SPDX-License-Identifier: AGPL-3.0-only
"""L71 — justesse de la comptabilité et des jauges.

1. Le harnais d'un agent se lit dans le registre : les jauges Claude se
   trouvent sans le fichier d'état `<agent>/tool` (qui n'est plus écrit), et
   un exécuteur voit les relevés des autres agents Claude.
2. Les affichages (`accounts list`, `cost report`) n'écrivent aucun relevé.
3. Le premier tour d'une session reprise sans total connu n'est pas compté au
   cumul de toute la session (Claude `total_cost_usd`, Codex `turn.completed`).
4. Le solde d'un fournisseur n'est relevé qu'une fois par période, quel que
   soit le nombre d'exécuteurs.

Journaux factices seulement : aucun test ne lit `~/.claude` ni `~/.codex`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest

from ameesh import accounts, balance, cost, registry

from .support import PgTestCase
from .test_cost import Sandbox, write
from .test_l26_exploitation import REPONSE_SOLDE
from .test_l30_accounts import _Base as ComptesBase, _codex_rollout


def _rate_limit(used: float, resets_at: float) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": used, "resetsAt": resets_at}}}}


class HarnaisDuRegistreTest(Sandbox, PgTestCase):
    """Point 1 : le registre dit le harnais, pas un fichier d'état disparu."""

    def setUp(self) -> None:
        super().setUp()
        for name, harness in (("c1", "claude"), ("c2", "claude"), ("d1", "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
            os.makedirs(os.path.join(self.state, name), exist_ok=True)

    def test_jauges_claude_sans_fichier_tool(self):
        self.events("c2", _rate_limit(0.42, self.now + 3600))
        gauges = self.book().claude_gauges()
        self.assertEqual([round(g.used, 2) for g in gauges], [0.42])

    def test_executeur_voit_les_releves_des_autres_agents_claude(self):
        # l'exécuteur de c1 n'injecte que son propre harnais
        self.events("c2", _rate_limit(0.55, self.now + 3600))
        book = self.book(tools={"c1": "claude"})
        self.assertAlmostEqual(book.claude_gauges()[0].used, 0.55)
        # même sans base (registre illisible) : un `rate_limit_event` est Claude
        sans_base = self.book(db=None, tools={"c1": "claude"})
        self.assertAlmostEqual(sans_base.claude_gauges()[0].used, 0.55)

    def test_un_harnais_connu_autre_que_claude_est_ignore(self):
        self.events("d1", _rate_limit(0.99, self.now + 3600))
        self.assertEqual(self.book().claude_gauges(), [])

    def test_cost_report_harnais_du_registre_et_dossiers_hors_agents(self):
        os.makedirs(os.path.join(self.state, "fils"), exist_ok=True)
        rows = {r["agent"]: r for r in self.book().report()}
        self.assertEqual(sorted(rows), ["c1", "c2", "d1"])
        self.assertEqual(rows["c1"]["harness"], "claude")
        self.assertEqual(rows["d1"]["harness"], "deepseek")


class AffichageSansEcritureTest(ComptesBase):
    """Point 2 : `accounts list` et `cost report` sont des lectures."""

    def _releves(self) -> int:
        return int(self.db.query("SELECT count(*) AS n FROM quota_gauge_readings")[0]["n"])

    def test_report_ne_releve_pas_par_defaut_des_affichages(self):
        comptes = self._codex_comptes()
        _codex_rollout(comptes[0]["path"], 40, time.time() + 3600)
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes})
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        rows = accounts.report(cfg, self.db, book, record=False)
        self.assertTrue(rows and rows[0]["gauges"])
        self.assertEqual(self._releves(), 0)
        book.report()
        self.assertEqual(self._releves(), 0)
        # un relevé explicite écrit toujours
        accounts.report(cfg, self.db, book, record=True)
        self.assertGreater(self._releves(), 0)

    def test_cli_accounts_list_et_cost_report_n_ecrivent_rien(self):
        comptes = self._codex_comptes()
        _codex_rollout(comptes[0]["path"], 40, time.time() + 3600)
        chemin = os.path.join(self.tmp, "config-hote.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump({"accounts": {"codex": comptes}}, fh)
        env = self.env(AMEESH_CONFIG=chemin)
        for args in (("accounts", "list"), ("cost", "report"), ("cost", "report", "--json")):
            proc = self.mesh(*args, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._releves(), 0)


class FenetreEchueTest(ComptesBase):
    """Ajout : un relevé dont la fenêtre est échue compte pour 0 %."""

    def test_gauge_echue_vaut_zero(self):
        now = time.time()
        g = cost.Gauge("codex", "codex-300min", 0.93, now - 60, 300 * 60)
        self.assertEqual(g.used_at(now), 0.0)
        self.assertFalse(g.exceeded(now))
        self.assertAlmostEqual(g.used_at(now - 120), 0.93)   # avant l'échéance

    def test_releve_echu_compte_utilisable_et_affiche_a_zero(self):
        comptes = self._codex_comptes()
        now = time.time()
        _codex_rollout(comptes[0]["path"], 95, now + 3600)    # primaire au seuil
        _codex_rollout(comptes[1]["path"], 93, now - 3 * 86400)  # relevé échu
        items = accounts.parse({"codex": comptes})["codex"]
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        ev = accounts.evaluate(book, items[1], items, now, record=False)
        self.assertTrue(ev.ok, ev.reason)
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes})
        rows = {r["account"]: r for r in accounts.report(cfg, self.db, book, now=now,
                                                         record=False)}
        jauge = rows["secondaire"]["gauges"][0]
        self.assertEqual(jauge["used"], 0.0)
        self.assertAlmostEqual(jauge["last_used"], 0.93)
        texte = accounts.format_rows(list(rows.values()))
        self.assertIn("codex-300min 0% (rythme 90%, remise à zéro passée, dernier relevé 93%)",
                      texte)
        # la règle de choix existante peut donc basculer sur le secondaire
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertEqual(choix.profile.name, "secondaire")


class SessionRepriseTest(Sandbox, PgTestCase):
    """Point 3 : différence avec le dernier total connu, jamais le cumul."""

    SESSION = "8fbfa9b3-session-reprise"

    def _tour_claude(self, agent: str, resume: str, total: float, usage: dict) -> int:
        path = os.path.join(self.state, agent, "events.jsonl")
        start = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                start = sum(1 for _ in fh)
        write(path, {"type": cost.TURN_MARKER, "resume": resume, "ts": self.now})
        start += 1
        write(path,
              {"type": "system", "subtype": "init", "session_id": self.SESSION},
              {"type": "result", "session_id": self.SESSION, "total_cost_usd": total,
               "usage": usage})
        return start

    def test_premier_tour_d_une_session_reprise_n_est_pas_le_cumul(self):
        self.agent("claude1", "claude", "claude-test")
        with open(os.path.join(self.tmp_cost, "prices.json"), "w", encoding="utf-8") as fh:
            json.dump({"claude-test": [5.0, 0.5, 25.0]}, fh)
        start = self._tour_claude("claude1", self.SESSION, 66.98, {
            "input_tokens": 6, "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 100_000, "output_tokens": 892})
        usage = self.book().record("claude1", start=start)
        # (6 + 1000) × 5 + 100 000 × 0,5 + 892 × 25 = 77 330 µ$ ≈ 0,0773 $
        self.assertAlmostEqual(usage.usd, 0.07733, places=5)
        # le cumul est gardé comme repère : le tour suivant est la différence
        start = self._tour_claude("claude1", self.SESSION, 67.16, {"output_tokens": 303})
        self.assertAlmostEqual(self.book().record("claude1", start=start).usd, 0.18, places=6)

    def test_session_neuve_le_cumul_est_le_tour(self):
        self.agent("claude1", "claude", "claude-test")
        start = self._tour_claude("claude1", "", 0.42, {"output_tokens": 10})
        self.assertAlmostEqual(self.book().record("claude1", start=start).usd, 0.42)

    def test_total_connu_chez_un_autre_agent(self):
        self.agent("ancien", "claude", "claude-test")
        self.agent("claude1", "claude", "claude-test")
        start = self._tour_claude("ancien", "", 66.0, {"output_tokens": 1})
        self.book().record("ancien", start=start)
        start = self._tour_claude("claude1", self.SESSION, 66.25, {"output_tokens": 1})
        self.assertAlmostEqual(self.book().record("claude1", start=start).usd, 0.25)

    def test_total_connu_dans_le_flux_avant_le_tour(self):
        # base neuve : aucun relevé au grand livre, mais le flux local en a un
        self.agent("claude1", "claude", "claude-test")
        self._tour_claude("claude1", "", 60.0, {"output_tokens": 1})
        start = self._tour_claude("claude1", self.SESSION, 60.5, {"output_tokens": 1})
        self.assertAlmostEqual(self.book().record("claude1", start=start).usd, 0.5)

    def test_fil_codex_repris_lu_dans_son_journal_de_session(self):
        fil = "01a10465-fil-codex"
        self.agent("codex2", "codex", "gpt-6.1-sol")
        dossier = os.path.join(self.codex, "2026", "10", "04")
        os.makedirs(dossier, exist_ok=True)

        def token_count(inp, cached, out):
            return {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                      "output_tokens": out}}}}
        write(os.path.join(dossier, "rollout-2026-10-04T02-52-08-%s.jsonl" % fil),
              {"type": "event_msg", "payload": {"type": "task_started"}},
              token_count(117_000_000, 113_000_000, 389_000),
              {"type": "event_msg", "payload": {"type": "task_started"}},
              token_count(117_100_000, 113_090_000, 389_100))
        path = os.path.join(self.state, "codex2", "events.jsonl")
        write(path, {"type": cost.TURN_MARKER, "resume": fil, "ts": self.now})
        write(path, {"type": "thread.started", "thread_id": fil},
              {"type": "turn.completed", "usage": {
                  "input_tokens": 117_100_000, "cached_input_tokens": 113_090_000,
                  "output_tokens": 389_100}})
        usage = self.book().record("codex2", start=1)
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens, usage.output_tokens),
                         (100_000, 90_000, 100))
        # (10 000 × 1,25 + 90 000 × 0,125 + 100 × 10) / 1e6
        self.assertAlmostEqual(usage.usd, 0.02475, places=6)


class SoldePartageTest(PgTestCase):
    """Point 4 : un relevé de solde par période, pas un par exécuteur."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM provider_balances")
        self.appels = 0

    def _source(self):
        def transport(url, headers, timeout):
            self.appels += 1
            return json.loads(REPONSE_SOLDE.decode("utf-8")), ""
        return balance.DeepSeekBalance(env={"DEEPSEEK_API_KEY": "cle-factice"},
                                       transport=transport)

    def _lignes(self) -> int:
        return int(self.db.query("SELECT count(*) AS n FROM provider_balances")[0]["n"])

    def test_deux_releves_rapproches_une_seule_ligne(self):
        self.assertEqual(len(balance.record(self.db, self._source(), min_interval_s=450)), 1)
        self.assertEqual(balance.record(self.db, self._source(), min_interval_s=450), [])
        self.assertEqual(self._lignes(), 1)
        self.assertEqual(self.appels, 1)          # le fournisseur n'est pas rappelé
        # période écoulée : nouveau relevé
        self.db.execute("UPDATE provider_balances SET observed_at = now() - interval '10 minutes'")
        self.assertEqual(len(balance.record(self.db, self._source(), min_interval_s=450)), 1)
        self.assertEqual(self._lignes(), 2)

    def test_ecriture_conditionnelle_meme_apres_la_lecture(self):
        # deux relèves passées toutes deux avant l'écriture de l'autre
        ops = __import__("ameesh.storage", fromlist=["of"]).of(self.db).operations
        kw = dict(provider="deepseek", currency="USD", total=1.0, granted=None,
                  topped_up=None, available=True, unless_within_s=450)
        self.assertIsNotNone(ops.record_balance(**kw))
        self.assertIsNone(ops.record_balance(**kw))
        self.assertEqual(self._lignes(), 1)

    def test_sans_intervalle_comportement_historique(self):
        balance.record(self.db, self._source())
        balance.record(self.db, self._source())
        self.assertEqual(self._lignes(), 2)


if __name__ == "__main__":
    unittest.main()
