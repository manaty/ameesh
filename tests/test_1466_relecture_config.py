# SPDX-License-Identifier: AGPL-3.0-only
"""#1466 : l'exécuteur relit la configuration sans redémarrer.

Un exécuteur gardait la configuration de son démarrage : un compte retiré,
un plafond relevé ou un profil corrigé restaient sans effet, et des agents
restaient en pause sur un plafond périmé. `Runner.reload_config` relit le
fichier (et sa base héritée) dès qu'une empreinte change, applique ce que la
décision de pause lit (comptes, plafonds, rôles critiques) et réveille les
agents ; un fichier cassé ne remplace rien.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from ameesh import config as config_mod
from ameesh.config import Config
from ameesh.runner import AgentWorker, Runner


class RelectureConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-test-1466-relecture-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.cfgdir = os.path.join(self.home, ".config", "ameesh")
        self.profil_dir = os.path.join(self.cfgdir, "runner-profiles")
        os.makedirs(self.profil_dir)
        self.host = os.path.join(self.cfgdir, "config.json")
        self.profil = os.path.join(self.profil_dir, "critiques-2.json")
        self._write(self.host, {"budget_usd_per_hour": 100,
                                "accounts": {"deepseek": [{"name": "cle-a"}]}})
        self._write(self.profil, {"canon": "/canon"})

    def _write(self, path: str, payload: dict) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def _cfg(self) -> Config:
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return config_mod.load(env={"AMEESH_CONFIG": self.profil})

    def _runner(self) -> Runner:
        cfg = self._cfg()
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            return Runner(cfg, None, cfg_overrides={"poll": 1.0})

    def test_rien_a_relire_sans_changement(self):
        runner = self._runner()
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertFalse(runner.reload_config())

    def test_un_plafond_releve_dans_l_hote_est_relu(self):
        """Le cas du boguet : le profil ne dit rien du plafond ; l'hôte le
        relève, et l'exécuteur en cours doit le suivre sans redémarrer."""
        runner = self._runner()
        self.assertEqual(runner.cfg.budget_usd_per_hour, 100.0)
        self._write(self.host, {"budget_usd_per_hour": 250,
                                "accounts": {"deepseek": [{"name": "cle-a"}]}})
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertTrue(runner.reload_config())
        self.assertEqual(runner.cfg.budget_usd_per_hour, 250.0)
        self.assertEqual(runner.budget_usd_per_hour, 250.0)
        # l'option de ligne de commande survit à la relecture
        self.assertEqual(runner.cfg.poll, 1.0)

    def test_des_comptes_corriges_dans_l_hote_sont_relus(self):
        runner = self._runner()
        self.assertEqual(list(runner.cfg.accounts), ["deepseek"])
        self._write(self.host, {"budget_usd_per_hour": 100,
                                "accounts": {"claude": [{"name": "primaire"}]}})
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertTrue(runner.reload_config())
        self.assertEqual(list(runner.cfg.accounts), ["claude"])

    def test_un_fichier_casse_ne_remplace_rien(self):
        runner = self._runner()
        with open(self.profil, "w", encoding="utf-8") as fh:
            fh.write("{ pas du json")
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertFalse(runner.reload_config())
        self.assertEqual(runner.cfg.budget_usd_per_hour, 100.0)
        self.assertEqual(runner.cfg.config_path, self.profil)

    def test_les_workers_suivent_la_relecture(self):
        runner = self._runner()

        class Faux(SimpleNamespace):
            def apply_config(self, cfg):
                self.vu = cfg
                self.state_dir = cfg.agent_dir(self.name)

        faux = Faux(name="agent-1", cfg=None, state_dir="", wake=threading.Event())
        runner.workers["agent-1"] = faux
        self._write(self.host, {"budget_usd_per_hour": 500,
                                "accounts": {"deepseek": [{"name": "cle-a"}]}})
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertTrue(runner.reload_config())
        self.assertEqual(faux.vu.budget_usd_per_hour, 500.0)
        self.assertTrue(faux.wake.is_set())
        self.assertEqual(faux.state_dir, runner.cfg.agent_dir("agent-1"))

    def test_apply_config_du_worker_reprend_le_dossier_d_etat(self):
        cfg = self._cfg()
        worker = SimpleNamespace(name="agent-2", cfg=None, state_dir="")
        AgentWorker.apply_config(worker, cfg)
        self.assertIs(worker.cfg, cfg)
        self.assertEqual(worker.state_dir, cfg.agent_dir("agent-2"))


if __name__ == "__main__":
    unittest.main()
