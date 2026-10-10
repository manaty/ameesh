# SPDX-License-Identifier: AGPL-3.0-only
"""L70 — plafonds de budget réglés en base, pour tout le mesh (0019 §2, R20).

Garde horaire, par jour et par agent ; relecture à chaud par les exécuteurs
(cache court, réveil `ameesh_budget`) ; refus d'une identité d'agent ;
précédence base > configuration de l'hôte > défaut ; journal acteur, ancienne
et nouvelle valeur.
"""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from unittest import mock

from ameesh import budget, cost, registry, storage
from ameesh.config import CHANNEL_BUDGET, Config
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

PAID = ("deepseek",)


class ResolveTest(unittest.TestCase):
    """Précédence, sans base."""

    def test_defaut_sans_config_ni_base(self):
        lim = budget.resolve(Config(), [])
        self.assertEqual((lim.per_hour, lim.per_hour_source), (10.0, budget.SOURCE_DEFAULT))
        self.assertEqual((lim.per_day, lim.per_day_source), (0.0, budget.SOURCE_DEFAULT))

    def test_config_explicite_puis_base(self):
        cfg = Config(budget_usd_per_hour=20.0, budget_usd_per_day=100.0,
                     budget_explicit=("budget_usd_per_hour", "budget_usd_per_day"))
        lim = budget.resolve(cfg, [])
        self.assertEqual((lim.per_hour, lim.per_hour_source), (20.0, budget.SOURCE_CONFIG))
        self.assertEqual((lim.per_day, lim.per_day_source), (100.0, budget.SOURCE_CONFIG))
        lim = budget.resolve(cfg, [{"scope": "", "window_s": 3600, "usd": 3.0},
                                   {"scope": "a1", "window_s": 86400, "usd": 1.5}])
        self.assertEqual((lim.per_hour, lim.per_hour_source), (3.0, budget.SOURCE_DB))
        self.assertEqual((lim.per_day, lim.per_day_source), (100.0, budget.SOURCE_CONFIG))
        self.assertEqual(lim.agents, {"a1": {86400: 1.5}})
        self.assertEqual(lim.book_kwargs()["agent_limits"], {"a1": {86400: 1.5}})

    def test_config_charge_note_la_source(self):
        from ameesh import config as config_mod
        env = {"AMEESH_CONFIG": "/nonexistent/ameesh.json",
               "AMEESH_BUDGET_USD_PER_DAY": "50"}
        cfg = config_mod.load(env=env)
        self.assertEqual(cfg.budget_usd_per_day, 50.0)
        self.assertEqual(cfg.budget_explicit, ("budget_usd_per_day",))
        lim = budget.resolve(cfg, [])
        self.assertEqual(lim.per_hour_source, budget.SOURCE_DEFAULT)
        self.assertEqual(lim.per_day_source, budget.SOURCE_CONFIG)

    def test_cache_court_et_invalidation(self):
        lectures = []
        horloge = [100.0]

        class Faux(budget.Cache):
            pass

        cache = Faux(ttl=10.0, clock=lambda: horloge[0])
        rows = [[{"scope": "", "window_s": 3600, "usd": 1.0}]]

        def lire(_db):
            lectures.append(1)
            return rows[0]
        with mock.patch.object(budget, "read_rows", lire):
            self.assertEqual(cache.limits(Config(), None).per_hour, 1.0)
            rows[0] = [{"scope": "", "window_s": 3600, "usd": 2.0}]
            horloge[0] += 5
            self.assertEqual(cache.limits(Config(), None).per_hour, 1.0)   # en cache
            horloge[0] += 6
            self.assertEqual(cache.limits(Config(), None).per_hour, 2.0)   # délai passé
            rows[0] = [{"scope": "", "window_s": 3600, "usd": 3.0}]
            cache.invalidate()
            self.assertEqual(cache.limits(Config(), None).per_hour, 3.0)   # réveil
        self.assertEqual(len(lectures), 3)


class BudgetBaseTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM budget_limits")
        self.db.execute("DELETE FROM budget_events")
        self.db.execute("DELETE FROM spend_pending")

    def _depense(self, agent: str, usd: float, *, harness: str = "deepseek",
                 il_y_a: str = "0 seconds") -> None:
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, recorded_at) "
            "VALUES (%s, %s, 'm', %s, now() - %s::interval)", (agent, harness, usd, il_y_a))

    def _book(self, **kwargs) -> cost.CostBook:
        return cost.CostBook(state_dir=self.state, db=self.db,
                             tools={"a1": "deepseek", "a2": "deepseek", "orch": "claude"},
                             **kwargs)

    # -- garde -------------------------------------------------------------
    def test_garde_horaire_et_journaliere_du_mesh(self):
        self._depense("a1", 4.0, il_y_a="3 hours")
        self._depense("a2", 2.0)
        book = self._book(hourly_usd=5.0, daily_usd=0.0)
        self.assertEqual(book.over("a1", paid_harnesses=PAID, pace=False), "")
        book = self._book(hourly_usd=5.0, daily_usd=6.0)
        raison = book.over("a1", paid_harnesses=PAID, pace=False)
        self.assertIn("budget journalier", raison)
        self.assertIn("24 dernières heures", raison)
        book = self._book(hourly_usd=2.0, daily_usd=6.0)
        self.assertIn("budget horaire", book.over("a2", paid_harnesses=PAID, pace=False))
        # L49 : l'agent au forfait n'est soumis à aucun de ces plafonds
        self.assertEqual(book.over("orch", paid_harnesses=PAID, pace=False), "")

    def test_garde_par_agent(self):
        self._depense("a1", 3.0)
        self._depense("a2", 0.5)
        book = self._book(hourly_usd=100.0, agent_limits={"a1": {3600: 2.0},
                                                          "a2": {86400: 1.0}})
        raison = book.over("a1", paid_harnesses=PAID, pace=False)
        self.assertIn("de l'agent a1", raison)
        self.assertEqual(book.over("a2", paid_harnesses=PAID, pace=False), "")
        self.assertEqual(book.over("all", paid_harnesses=PAID, pace=False), "")
        self._depense("a2", 0.6, il_y_a="5 hours")
        self.assertIn("budget journalier de l'agent a2",
                      book.over("a2", paid_harnesses=PAID, pace=False))

    def test_precedence_base_config_defaut(self):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=20.0,
                                  budget_explicit=("budget_usd_per_hour",))
        self.assertEqual(budget.current(cfg, self.db).per_hour_source, budget.SOURCE_CONFIG)
        self.assertEqual(budget.current(dataclasses.replace(cfg, budget_explicit=()),
                                        self.db).per_hour_source, budget.SOURCE_DEFAULT)
        storage.of(self.db).budgets.put("", 3600, 7.0, actor="human:test")
        lim = budget.current(cfg, self.db)
        self.assertEqual((lim.per_hour, lim.per_hour_source), (7.0, budget.SOURCE_DB))
        storage.of(self.db).budgets.put("", 3600, None, actor="human:test")
        self.assertEqual(budget.current(cfg, self.db).per_hour, 20.0)

    def test_put_journalise_ancienne_et_nouvelle_valeur(self):
        st = storage.of(self.db).budgets
        self.assertEqual(st.put("", 86400, 30.0, actor="human:x"), (True, None))
        self.assertEqual(st.put("", 86400, 30.0, actor="human:x"), (False, 30.0))
        self.assertEqual(st.put("", 86400, 40.0, actor="human:y"), (True, 30.0))
        self.assertEqual(st.put("", 86400, None, actor="human:z"), (True, 40.0))
        events = st.events(10)
        self.assertEqual([(e["old_usd"], e["new_usd"], e["actor"]) for e in events],
                         [(40.0, None, "human:z"), (30.0, 40.0, "human:y"),
                          (None, 30.0, "human:x")])

    def test_notify_au_changement(self):
        abo = storage.of(self.db).wakeups.subscribe([CHANNEL_BUDGET])
        if abo is None:
            self.skipTest("LISTEN/NOTIFY indisponible avec ce pilote")
        autre = self.connect()
        try:
            storage.of(autre).budgets.put("", 3600, 9.0, actor="human:t")
            for _ in range(20):
                item = abo.wait(timeout=0.5)
                if item and item.get("channel") == CHANNEL_BUDGET:
                    break
            else:
                self.fail("aucun réveil ameesh_budget")
            self.assertEqual(json.loads(item["payload"])["window_s"], 3600)
        finally:
            abo.close()
            autre.close()

    # -- exécuteur : relecture à chaud -------------------------------------
    def _worker(self, name: str = "b70", plafond: float = 100.0):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=plafond,
                                  budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return runner, AgentWorker(runner, registry.get(self.db, name), lease)

    def test_relecture_a_chaud_sur_reveil_puis_au_delai(self):
        runner, worker = self._worker()
        try:
            self._depense("b70", 12.0)
            self.assertTrue(worker.budget_ok())               # 12 < 100 (config)
            storage.of(self.db).budgets.put("", 3600, 10.0, actor="human:t")
            self.assertTrue(worker.budget_ok())               # encore en cache
            runner.dispatch({"channel": CHANNEL_BUDGET, "payload": "{}"})
            self.assertFalse(worker.budget_ok())              # réveil : relu
            ligne = registry.get(self.db, "b70")
            self.assertIn("plafond 10.00", ligne["status_text"])
            # au délai, sans réveil : retrait du plafond en base → config
            storage.of(self.db).budgets.put("", 3600, None, actor="human:t")
            runner.budget_cache.ttl = 0.0
            self.assertTrue(worker.budget_ok())
            # plafond propre à l'agent, par jour
            storage.of(self.db).budgets.put("b70", 86400, 5.0, actor="human:t")
            raison = worker.budget_reason()
            self.assertIn("budget journalier de l'agent b70", raison)
        finally:
            worker.watchdog_stop.set()

    def test_table_absente_garde_la_config(self):
        cache = budget.Cache(ttl=0.0)
        autre = self.connect(schema="t_l70_vide_%s" % os.getpid())
        try:
            lim = cache.limits(dataclasses.replace(self.cfg, budget_usd_per_hour=4.0), autre)
            self.assertEqual(lim.per_hour, 4.0)
            self.assertTrue(cache.error)
        finally:
            autre.close()

    # -- CLI et autorité ---------------------------------------------------
    def test_cli_refuse_une_identite_d_agent(self):
        proc = self.mesh("budget", "set", "--per-hour", "5",
                         env=self.env(AGENT_MAIL_NAME="malin"))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("refus", proc.stderr)
        self.assertEqual(storage.of(self.db).budgets.limits(), [])
        self.assertEqual(storage.of(self.db).budgets.events(5), [])

    def test_cli_refuse_un_humain_hors_liste(self):
        proc = self.mesh("budget", "set", "--per-hour", "5",
                         env=self.env(AMEESH_HUMANS="quelqu-un-d-autre", USER="intrus"))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("n'est pas membre humain", proc.stderr)

    def test_cli_set_show_unset(self):
        env = self.env(AMEESH_HUMANS="chef", USER="chef")
        self._depense("a1", 1.25)
        proc = self.mesh("budget", "set", "--per-hour", "5", "--per-day", "40", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("aucun → 5.00 $", proc.stdout)
        lignes = {(r["scope"], r["window_s"]): r for r in storage.of(self.db).budgets.limits()}
        self.assertEqual(lignes[("", 3600)]["usd"], 5.0)
        self.assertEqual(lignes[("", 86400)]["set_by"], "human:chef")
        # fil : l'entrée d'audit est indexée
        self.assertTrue(self.db.query("SELECT 1 FROM thread_index"))
        os.makedirs(os.path.join(self.tmp, "a1"), exist_ok=True)
        self.register("a1", "deepseek", cwd=os.path.join(self.tmp, "a1"))
        proc = self.mesh("budget", "set", "--per-hour", "2", "--agent", "a1", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("budget", "set", "--per-hour", "2", "--agent", "inconnu", env=env)
        self.assertEqual(proc.returncode, 1)
        proc = self.mesh("budget", "set", "--per-hour", "0", env=env)
        self.assertEqual(proc.returncode, 2)
        proc = self.mesh("budget", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("5.00 $", proc.stdout)
        self.assertIn("(base — human:chef", proc.stdout)
        self.assertIn("a1", proc.stdout)
        proc = self.mesh("budget", "--json", env=env)
        data = json.loads(proc.stdout)
        self.assertEqual(data["schema"], "ameesh-budget/1")
        self.assertEqual(data["limits"]["per_day_usd"], 40.0)
        self.assertEqual(data["limits"]["agents"]["a1"]["per_hour_usd"], 2.0)
        self.assertEqual(len(data["events"]), 3)
        proc = self.mesh("cost", "report", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("plafonds du mesh", proc.stdout)
        self.assertIn("plafond de a1", proc.stdout)
        proc = self.mesh("progress", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("plafonds : 5.00/h (base) · 40.00/jour (base)", proc.stdout)
        proc = self.mesh("budget", "unset", "--per-hour", "--per-day", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["scope"] for r in storage.of(self.db).budgets.limits()], ["a1"])
        ev = storage.of(self.db).budgets.events(1)[0]
        self.assertEqual((ev["old_usd"], ev["new_usd"], ev["actor"]),
                         (40.0, None, "human:chef"))

    def test_responsable_de_l_agent_admis_pour_son_agent(self):
        os.makedirs(os.path.join(self.tmp, "a3"), exist_ok=True)
        self.register("a3", "deepseek", cwd=os.path.join(self.tmp, "a3"))
        self.db.execute("UPDATE agent_registry SET responsible = 'human:resp' "
                        "WHERE name = 'a3'")
        env = self.env(AMEESH_HUMANS="chef", USER="resp")
        proc = self.mesh("budget", "set", "--per-day", "3", "--agent", "a3", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("budget", "set", "--per-day", "3", env=env)   # mesh : non
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
