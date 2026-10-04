# SPDX-License-Identifier: AGPL-3.0-only
"""Renommage agent-mesh → ameesh : commande `ameesh`, alias, variables.

Le renommage ne doit rien casser de l'existant : `agent-mail` (les hooks des
harnais), `agent-runner`, `AGENT_MESH_*` et les chemins d'hier restent acceptés.
"""
from __future__ import annotations


import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import adapters, config as config_mod, identity, signing


from .support import PgTestCase, SRC


class RenameTest(PgTestCase):
    def test_variables_ameesh_et_repli_agent_mesh(self):
        neuf = config_mod.load(env={"AMEESH_DSN": "postgres://neuf", "AMEESH_SCHEMA": "s1"})
        self.assertEqual(neuf.dsn, "postgres://neuf")
        self.assertEqual(neuf.schema, "s1")
        self.assertTrue(neuf.config_dir.endswith("agent-mail"))  # interface v0, inchangée

        ancien = config_mod.load(env={"AGENT_MESH_DSN": "postgres://ancien"})
        self.assertEqual(ancien.dsn, "postgres://ancien")

        # le nouveau nom gagne quand les deux sont posés
        deux = config_mod.load(env={"AMEESH_DSN": "postgres://neuf",
                                    "AGENT_MESH_DSN": "postgres://ancien"})
        self.assertEqual(deux.dsn, "postgres://neuf")

    def test_chemins_ameesh_et_repli_des_anciens(self):
        cfg = config_mod.load(env={})
        self.assertTrue(cfg.state_dir.endswith("ameesh")
                        or cfg.state_dir.endswith("agent-mesh"))  # repli si l'ancien existe
        self.assertTrue(config_mod.DEFAULT_CONFIG.endswith("ameesh/config.json"))
        self.assertTrue(config_mod.LEGACY_CONFIG.endswith("agent-mesh/config.json"))

    def test_state_dir_du_fichier_de_config_est_respecte(self):
        """Revue codex3 : le calcul du state_dir ne doit pas écraser le JSON."""
        import tempfile
        with tempfile.TemporaryDirectory() as dossier:
            cfg_path = os.path.join(dossier, "config.json")
            custom = os.path.join(dossier, "custom-state")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"state_dir": custom}, fh)
            cfg = config_mod.load(env={"AMEESH_CONFIG": cfg_path})
            self.assertEqual(cfg.state_dir, os.path.abspath(custom))

            # la variable d'environnement reste prioritaire sur le fichier
            env_state = os.path.join(dossier, "env-state")
            cfg2 = config_mod.load(env={"AMEESH_CONFIG": cfg_path, "AMEESH_STATE": env_state})
            self.assertEqual(cfg2.state_dir, os.path.abspath(env_state))

    def test_state_dir_json_egal_au_defaut_reste_un_choix(self):
        """Verdict codex3 B1 : valeur égale au défaut ≠ champ absent.

        JSON choisissant explicitement le nouveau DEFAULT_STATE, ancien dossier
        présent et nouveau absent : la présence du champ doit gagner. Comparer
        la valeur au défaut faisait basculer sur l'ancien dossier.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as dossier:
            cfg_path = os.path.join(dossier, "config.json")
            nouveau = os.path.join(dossier, "nouveau")
            ancien = os.path.join(dossier, "ancien")
            os.makedirs(ancien, exist_ok=True)

            # Les constantes de chemin par défaut sont adaptées aux fixtures :
            # le JSON contient donc exactement la valeur du défaut.
            with mock.patch.object(config_mod, "DEFAULT_STATE", nouveau), \
                    mock.patch.object(config_mod, "LEGACY_STATE", ancien):
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({"state_dir": nouveau}, fh)
                cfg = config_mod.load(env={"AMEESH_CONFIG": cfg_path})
                self.assertEqual(cfg.state_dir, os.path.abspath(nouveau))

                # La variable d'environnement reste prioritaire, JSON présent.
                env_state = os.path.join(dossier, "env-state")
                cfg_env = config_mod.load(
                    env={"AMEESH_CONFIG": cfg_path, "AMEESH_STATE": env_state})
                self.assertEqual(cfg_env.state_dir, os.path.abspath(env_state))

                # Champ réellement absent : là seulement, repli sur l'ancien
                # dossier (présent) puisque le nouveau est absent.
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({}, fh)
                cfg_vide = config_mod.load(env={"AMEESH_CONFIG": cfg_path})
                self.assertEqual(cfg_vide.state_dir, os.path.abspath(ancien))

    def test_identite_lit_les_deux_noms(self):
        cfg = config_mod.load(env={})
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "alpha"}, clear=False):
            os.environ.pop("AMEESH_RUNNER_ID", None)
            os.environ.pop("AGENT_MESH_RUNNER_ID", None)
            binding = identity.resolve_binding(cfg, db=None)
        self.assertTrue(binding.ok)
        self.assertEqual(binding.source, "explicit")

        # triplet runner hérité : lu aussi (sans base, pas de bail à vérifier)
        with mock.patch.dict(os.environ, {
                "AGENT_MAIL_NAME": "alpha", "AGENT_MESH_RUNNER_ID": "r",
                "AGENT_MESH_LEASE_EPOCH": "1"}, clear=False):
            os.environ.pop("AMEESH_RUNNER_ID", None)
            os.environ.pop("AMEESH_LEASE_EPOCH", None)
            herite = identity.resolve_binding(cfg, db=None)
        self.assertTrue(herite.ok)

    def test_binaires_et_backend_en_repli(self):
        with mock.patch.dict(os.environ, {"AGENT_MESH_BIN_DIR": os.path.join(
                SRC, "..", "tests", "fakebin")}, clear=False):
            os.environ.pop("AMEESH_BIN_DIR", None)
            adapter = adapters.adapter_for("claude")
            self.assertTrue(adapter.binary.endswith("fakebin/claude"))
        with mock.patch.dict(os.environ, {"AGENT_MESH_SIGNING_BACKEND": "pure"}, clear=False):
            os.environ.pop("AMEESH_SIGNING_BACKEND", None)
            self.assertEqual(signing.backend().name, "pure")

    def test_commande_ameesh_route(self):
        proc = subprocess.run([sys.executable, "-m", "ameesh", "--help"],
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 0)
        for mot in ("mail", "run", "list", "key", "work"):
            self.assertIn(mot, proc.stdout)

        proc = subprocess.run([sys.executable, "-m", "ameesh", "inconnue"],
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("inconnue", proc.stderr)

        proc = subprocess.run([sys.executable, "-m", "ameesh", "list", "--json"],
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(json.loads(proc.stdout), list)

        proc = subprocess.run([sys.executable, "-m", "ameesh", "run", "--help"],
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--once", proc.stdout)

    def test_alias_agent_mail_et_agent_runner(self):
        """Les modules d'alias existent et répondent (hooks, exécuteur)."""
        for module, marqueur in (("ameesh.cli", "agent-mail (mesh v1)"),
                                 ("ameesh.runner", "--once")):
            proc = subprocess.run([sys.executable, "-m", module, "--help"],
                                  capture_output=True, text=True, env=self.env())
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(marqueur, proc.stdout + proc.stderr)

        # `ameesh mail` route vers la CLI agent-mail
        proc = subprocess.run([sys.executable, "-m", "ameesh", "mail", "--help"],
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("send", proc.stdout)

    def test_runner_exporte_les_deux_noms(self):
        """Le harnais reçoit AMEESH_* et les anciens AGENT_MESH_* (compat)."""
        cwd = os.path.join(self.tmp, "travail")
        os.makedirs(cwd, exist_ok=True)
        self.register("renomme", "claude", cwd=cwd, prompt="tour")
        proc = self.runner("--once", "--agents", "renomme")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = self.turns()[0]["env"]
        self.assertEqual(env["AGENT_MAIL_NAME"], "renomme")
        self.assertEqual(env["AMEESH_LEASE_EPOCH"], env["AGENT_MESH_LEASE_EPOCH"])
        self.assertTrue(env["AMEESH_RUNNER_ID"])


if __name__ == "__main__":
    unittest.main()
