# SPDX-License-Identifier: AGPL-3.0-only
"""#1127 : une configuration nommée explicitement doit exister.

Un `AMEESH_CONFIG` (ou son alias `AGENT_MESH_CONFIG`) qui désignait un fichier
absent se repliait en silence sur la config historique
(`~/.config/agent-mesh/config.json`) : le DSN effectif changeait sans le dire,
puis la sonde de schéma concluait à tort à un « agent-mesh schema missing » qui
envoyait chercher une panne de base inexistante. Le chemin explicite absent est
désormais refusé avant toute connexion ; le repli historique ne vaut que si
aucune config n'est nommée.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import config as config_mod

from . import support

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigExpliciteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-test-1127-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, payload: dict, home: str | None = None) -> str:
        path = os.path.join(home or self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def home_with_legacy(self) -> str:
        """Un HOME dont la config historique agent-mesh existe et dit quelque
        chose (`schema_historique`) : le repli légitime y mène, un chemin
        explicite absent ne doit pas y mener."""
        home = os.path.join(self.tmp, "home-legacy")
        self.write(os.path.join(".config", "agent-mesh", "config.json"),
                   {"schema": "schema_historique"}, home=home)
        return home

    def env_without_config(self) -> dict:
        """L'environnement du banc, sans config nommée : c'est lui qui doit
        déclencher le repli historique."""
        env = support.child_env()
        for name in ("AMEESH_CONFIG", "AGENT_MESH_CONFIG"):
            env.pop(name, None)
        return env

    def test_ameesh_config_absent_echoue(self):
        absent = os.path.join(self.tmp, "absent.json")
        with self.assertRaises(SystemExit) as caught:
            config_mod.load(env={"AMEESH_CONFIG": absent})
        self.assertIn("config file not found: %s" % absent, str(caught.exception))
        self.assertIn("AMEESH_CONFIG", str(caught.exception))

    def test_agent_mesh_config_absent_echoue(self):
        absent = os.path.join(self.tmp, "absent.json")
        with self.assertRaises(SystemExit) as caught:
            config_mod.load(env={"AGENT_MESH_CONFIG": absent})
        self.assertIn("config file not found: %s" % absent, str(caught.exception))
        self.assertIn("AGENT_MESH_CONFIG", str(caught.exception))

    def test_un_chemin_explicite_absent_ne_se_replie_pas_sur_l_historique(self):
        """Le cas du boguet : la config historique existe (et serait lue sans
        la variable), et pourtant le chemin explicite absent échoue au lieu
        d'être remplacé en silence."""
        home = self.home_with_legacy()
        absent = os.path.join(self.tmp, "absent.json")
        with mock.patch.dict(os.environ, {"HOME": home}):
            env = self.env_without_config()
            env["AMEESH_CONFIG"] = absent
            with self.assertRaises(SystemExit) as caught:
                config_mod.load(env=env)
        self.assertIn("config file not found: %s" % absent, str(caught.exception))

    def test_repli_historique_sans_config_explicite(self):
        """Sans variable, une config agent-mesh existante reste lue."""
        home = self.home_with_legacy()
        with mock.patch.dict(os.environ, {"HOME": home}):
            cfg = config_mod.load(env=self.env_without_config())
        self.assertEqual(cfg.schema, "schema_historique")

    def test_sans_aucune_config_les_defauts(self):
        home = os.path.join(self.tmp, "home-vide")
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"HOME": home}):
            cfg = config_mod.load(env=self.env_without_config())
        self.assertEqual(cfg.schema, config_mod.Config().schema)

    def test_chemin_explicite_valide_est_lu(self):
        cfg_file = self.write("config.json", {"schema": "schema_explicite"})
        cfg = config_mod.load(env={"AMEESH_CONFIG": cfg_file})
        self.assertEqual(cfg.schema, "schema_explicite")

    def test_ameesh_config_prime_et_l_alias_perime_est_ignore(self):
        """L'alias n'est lu que si le nom courant est absent : un alias
        périmé et introuvable ne fait pas échouer une config valide."""
        cfg_file = self.write("config.json", {"schema": "schema_ameesh"})
        absent = os.path.join(self.tmp, "absent.json")
        cfg = config_mod.load(env={"AMEESH_CONFIG": cfg_file,
                                   "AGENT_MESH_CONFIG": absent})
        self.assertEqual(cfg.schema, "schema_ameesh")

    def test_une_valeur_vide_compte_comme_non_posee(self):
        """`ameesh watch` épingle une valeur vide quand aucune config n'était
        posée à l'activation : vide = non posée, jamais un chemin absent."""
        home = self.home_with_legacy()
        with mock.patch.dict(os.environ, {"HOME": home}):
            cfg = config_mod.load(env={"AMEESH_CONFIG": "",
                                       "AGENT_MESH_CONFIG": ""})
        self.assertEqual(cfg.schema, "schema_historique")

    def test_la_cli_refuse_avant_toute_connexion(self):
        """Le DSN pointe un port fermé : si la CLI tentait la connexion, on
        lirait une panne de base ; on doit lire l'erreur de configuration."""
        absent = os.path.join(self.tmp, "absent.json")
        for name in ("AMEESH_CONFIG", "AGENT_MESH_CONFIG"):
            with self.subTest(variable=name):
                env = {"HOME": self.tmp,
                       "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "PYTHONPATH": os.path.join(REPO, "src"),
                       "AMEESH_DSN": "postgresql://personne@127.0.0.1:1/aucune",
                       name: absent}
                proc = subprocess.run(
                    [sys.executable, "-m", "ameesh", "work", "show", "1"],
                    cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("config file not found: %s" % absent, proc.stderr)
                self.assertIn(name, proc.stderr)
                # échec de configuration, pas une panne de base déguisée
                self.assertNotIn("schema missing", proc.stderr)
                self.assertNotIn("127.0.0.1:1", proc.stderr)


if __name__ == "__main__":
    unittest.main()
