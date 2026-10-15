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
        # comme `main()` : les options de ligne de commande sont appliquées à
        # la configuration avant la construction de l'exécuteur
        cfg = replace(self._cfg(), poll=1.0)
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

    def test_les_champs_figes_ne_suivent_pas_la_relecture(self):
        """R1 cx2/dev2 : recharger toute la `Config` appliquait le schéma, la
        base, le dossier d'état et le bail aux workers actifs, alors que la
        connexion et les baux sont ceux du démarrage. Seuls les champs de
        `RELOADABLE_FIELDS` suivent le fichier ; les autres restent figés."""
        runner = self._runner()
        avant = (runner.cfg.dsn, runner.cfg.schema, runner.cfg.state_dir,
                 runner.cfg.lease_ttl, runner.cfg.poll,
                 runner.cfg.budget_check_interval)
        self._write(self.host, {
            "budget_usd_per_hour": 250,
            "accounts": {"deepseek": [{"name": "cle-a"}]},
            "dsn": "postgresql://autre/base", "schema": "autre",
            "state_dir": os.path.join(self.tmp, "autre-etat"),
            "lease_ttl": 700, "poll": 99, "budget_check_interval": 5,
        })
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            # le fichier a bien été lu et portait ces valeurs…
            lu = config_mod.load(env={"AMEESH_CONFIG": self.profil})
            self.assertEqual((lu.schema, lu.lease_ttl), ("autre", 700.0))
            self.assertTrue(runner.reload_config())
        # …mais l'exécuteur en cours garde ce qui a décidé sa connexion et ses
        # baux ; le champ rechargeable, lui, suit
        self.assertEqual(runner.cfg.budget_usd_per_hour, 250.0)
        self.assertEqual(
            (runner.cfg.dsn, runner.cfg.schema, runner.cfg.state_dir,
             runner.cfg.lease_ttl, runner.cfg.poll,
             runner.cfg.budget_check_interval), avant)

    def test_un_fichier_casse_ne_remplace_rien(self):
        runner = self._runner()
        with open(self.profil, "w", encoding="utf-8") as fh:
            fh.write("{ pas du json")
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertFalse(runner.reload_config())
        self.assertEqual(runner.cfg.budget_usd_per_hour, 100.0)
        self.assertEqual(runner.cfg.config_path, self.profil)

    def test_le_stockage_ne_se_relit_pas(self):
        """Base, schéma et dossier d'état restent ceux du démarrage : la
        connexion de l'exécuteur et les sessions des agents y sont ouvertes."""
        runner = self._runner()
        avant = runner.cfg
        self._write(self.host, {"budget_usd_per_hour": 300,
                                "dsn": "postgresql://ailleurs/db",
                                "schema": "autre",
                                "state_dir": os.path.join(self.tmp, "etat-2"),
                                "accounts": {"deepseek": [{"name": "cle-a"}]}})
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "AMEESH_CONFIG": self.profil}):
            self.assertTrue(runner.reload_config())
        self.assertEqual(runner.cfg.budget_usd_per_hour, 300.0)
        for champ in ("dsn", "schema", "state_dir"):
            self.assertEqual(getattr(runner.cfg, champ), getattr(avant, champ))

    def test_un_fichier_disparu_ne_remplace_rien(self):
        """Sans AMEESH_CONFIG, `load` rend les valeurs par défaut quand
        config.json disparaît : la relecture doit le refuser."""
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            cfg = config_mod.load(env={})
            runner = Runner(cfg, None)
        self.assertEqual(cfg.config_path, self.host)
        os.remove(self.host)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            os.environ.pop("AMEESH_CONFIG", None)
            os.environ.pop("AGENT_MESH_CONFIG", None)
            self.assertFalse(runner.reload_config())
        self.assertEqual(list(runner.cfg.accounts), ["deepseek"])
        self.assertEqual(runner.cfg.budget_usd_per_hour, 100.0)

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

    def test_le_worker_prend_la_config_au_tour_suivant(self):
        """R1 cx2 : la configuration relue n'entre pas au milieu d'un tour ;
        le worker la prend à sa frontière de tour."""
        cfg = self._cfg()
        worker = SimpleNamespace(name="agent-2", cfg="celle-du-tour",
                                 _cfg_pending=None)
        AgentWorker.apply_config(worker, cfg)
        self.assertEqual(worker.cfg, "celle-du-tour")     # rien au milieu du tour
        AgentWorker._apply_pending_config(worker)
        self.assertIs(worker.cfg, cfg)
        AgentWorker._apply_pending_config(worker)          # idempotent
        self.assertIs(worker.cfg, cfg)


if __name__ == "__main__":
    unittest.main()
