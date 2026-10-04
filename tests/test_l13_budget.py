# SPDX-License-Identifier: AGPL-3.0-only
"""L13 — garde de budget avant chaque tour, modèle/effort par agent (0019)."""
from __future__ import annotations

import dataclasses
import os
import time
import unittest
from unittest import mock

from ameesh import cost, registry
from ameesh.adapters import adapter_for
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class SetModelEffortTest(PgTestCase):
    def _travail(self, name: str = "l13") -> str:
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        return cwd

    def test_set_ecrit_modele_et_effort_puis_le_tour_les_applique(self):
        self._travail()
        proc = self.mesh("set", "l13", "model=claude-opus-4", "effort=high")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(registry.get(self.db, "l13")["model"], "claude-opus-4")
        with open(os.path.join(self.cfg.state_dir, "l13", "effort"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "high")
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "l13", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "l13"), lease)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": []}))
        finally:
            worker.watchdog_stop.set()
        argv = " ".join(self.turns()[-1]["argv"])
        self.assertIn("--model claude-opus-4", argv)
        self.assertIn("--effort high", argv)

    def test_set_valeur_vide_revient_au_defaut(self):
        self._travail()
        self.mesh("set", "l13", "model=claude-opus-4", "effort=high")
        proc = self.mesh("set", "l13", "model=", "effort=")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(registry.get(self.db, "l13")["model"])
        self.assertFalse(os.path.exists(
            os.path.join(self.cfg.state_dir, "l13", "effort")))

    def test_options_declarees_par_harnais(self):
        claude = adapter_for("claude", binary="/bin/echo")
        argv = claude.command("txt", None, model="m", effort="e")
        self.assertIn("--model", argv)
        self.assertIn("m", argv)
        self.assertIn("--effort", argv)
        self.assertIn("e", argv)
        codex = adapter_for("codex", binary="/bin/echo")
        argv = codex.command("txt", None, model="m", effort="e")
        self.assertIn("-m", argv)
        self.assertIn('model_reasoning_effort="e"', argv)
        dsh = adapter_for("deepseek", binary="/bin/echo")
        patch = os.path.join(self.tmp, "model.patch.yml")
        argv = dsh.command("txt", None, model="deepseek-pro", effort="max", patch=patch)
        self.assertIn("--patch", argv)
        with open(patch, encoding="utf-8") as fh:
            contenu = fh.read()
        self.assertIn("model: deepseek-pro", contenu)
        self.assertIn("reasoningEffort: max", contenu)


class BudgetGateTest(PgTestCase):
    def _worker(self, name: str = "budget", *, plafond: float = 10.0):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=plafond,
                                  budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        # le plafond horaire porte sur toutes les dépenses de l'hôte : on part
        # d'un grand livre propre pour que chaque cas soit isolé
        self.db.execute("DELETE FROM turn_costs")
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return runner, AgentWorker(runner, registry.get(self.db, name), lease)

    def _depense(self, agent: str, usd: float) -> None:
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, recorded_at) "
            "VALUES (%s, 'deepseek', 'deepseek-flash', %s, now())", (agent, usd))

    def test_depense_horaire_met_en_pause_et_trace_dans_le_fil(self):
        runner, worker = self._worker(plafond=10.0)
        self._depense("budget", 12.0)
        try:
            self.assertFalse(worker.budget_ok())
            self.assertIsNone(worker.pick())
            ligne = registry.get(self.db, "budget")
            self.assertEqual(ligne["status"], "blocked")
            self.assertIn("budget horaire", ligne["status_text"])
        finally:
            worker.watchdog_stop.set()

    def test_sous_le_plafond_le_tour_passe(self):
        runner, worker = self._worker(plafond=10.0)
        self._depense("budget", 1.0)
        self.assertTrue(worker.budget_ok())

    def test_garde_desactivee_si_plafond_nul(self):
        runner, worker = self._worker(plafond=0.0)
        self._depense("budget", 999.0)
        self.assertTrue(worker.budget_ok())
        self.assertEqual(worker.budget_reason(), "")

    def test_forfait_au_dessus_du_rythme_met_en_pause(self):
        runner, worker = self._worker(plafond=10.0)
        worker.agent["harness"] = "claude"
        jauge = cost.Gauge(harness="claude", key="5h", used=0.95,
                           resets_at=time.time() + 3600, window_s=5 * 3600)
        with mock.patch("ameesh.cost.CostBook.gauges", return_value=[jauge]):
            raison = worker.budget_reason()
        self.assertIn("forfait claude", raison)

    def test_modele_de_la_ligne_est_applique(self):
        runner, worker = self._worker(plafond=10.0)
        registry.upsert(self.db, "budget", model="deepseek-pro")
        worker.agent = registry.get(self.db, "budget")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": []}))
        finally:
            worker.watchdog_stop.set()
        argv = " ".join(self.turns()[-1]["argv"])
        self.assertIn("--patch", argv)
        with open(os.path.join(self.cfg.state_dir, "budget", "model.patch.yml"),
                  encoding="utf-8") as fh:
            self.assertIn("model: deepseek-pro", fh.read())


if __name__ == "__main__":
    unittest.main()
