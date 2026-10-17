# SPDX-License-Identifier: AGPL-3.0-only
"""#1466 : un profil d'exécuteur hérite de la configuration de l'hôte.

Un exécuteur peut être lancé sur un fichier nommé (`AMEESH_CONFIG`), un
« profil » rangé sous le dossier de la configuration de l'hôte
(`~/.config/ameesh/…`). Un profil périmé faisait tourner des agents sur des
comptes et des plafonds d'hier (relevé du 15/10 : un profil
`runner-profiles/critiques-2.json` à 50 $/h contre 100 $/h dans
`config.json`).

Désormais un profil est une **superposition** : les clés qu'il ne déclare pas
viennent de `config.json`. Ses propres clés l'emportent (une isolation voulue
se déclare), et l'écart est retenu (`config_shadowed`, `config_source`) pour
rester visible. `"inherit": false` rend un profil autonome ; `"inherit":
"<chemin>"` nomme une autre base ; `"inherit": true` force l'héritage. Hors du
dossier des profils (`runner-profiles/`), un fichier reste autonome : le banc
de tests, et la configuration d'une autre base rangée à côté de `config.json`,
ne lisent jamais la configuration du poste.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import config as config_mod


class ConfigProfilTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-test-1466-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.cfgdir = os.path.join(self.home, ".config", "ameesh")
        self.host = os.path.join(self.cfgdir, "config.json")
        self.profil_dir = os.path.join(self.cfgdir, "runner-profiles")
        os.makedirs(self.profil_dir)
        self._write(self.host, {
            "accounts": {"deepseek": [{"name": "cle-a", "hourly_usd": 100}]},
            "budget_usd_per_hour": 100,
            "host": "hote-de-base",
        })
        self.profil = os.path.join(self.profil_dir, "critiques-2.json")

    def _write(self, path: str, payload: dict) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def load(self, **env: str):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return config_mod.load(env=dict(env))

    def test_un_profil_herite_des_comptes_et_des_plafonds_de_l_hote(self):
        """Le cas du boguet : le profil ne dit rien des comptes, il hérite de
        `config.json` — et non d'un instantané d'hier."""
        self._write(self.profil, {"canon": "/canon/du/profil"})
        cfg = self.load(AMEESH_CONFIG=self.profil)
        self.assertEqual(cfg.accounts, {"deepseek": [{"name": "cle-a", "hourly_usd": 100}]})
        self.assertEqual(cfg.budget_usd_per_hour, 100.0)
        self.assertEqual(cfg.host, "hote-de-base")
        self.assertEqual(cfg.canon, "/canon/du/profil")
        self.assertEqual(cfg.config_path, self.profil)
        self.assertEqual(cfg.config_base, self.host)
        self.assertEqual(cfg.config_shadowed, ())
        self.assertIn("profile", cfg.config_source)
        self.assertIn(self.host, cfg.config_source)

    def test_une_cle_du_profil_ecrase_la_base_et_l_ecart_est_visible(self):
        """Une isolation voulue se déclare : le profil gagne, et l'écart est
        nommé au lieu d'être silencieux."""
        self._write(self.profil, {
            "accounts": {"deepseek": [{"name": "cle-a", "hourly_usd": 50}]},
            "budget_usd_per_hour": 50,
        })
        cfg = self.load(AMEESH_CONFIG=self.profil)
        self.assertEqual(cfg.accounts, {"deepseek": [{"name": "cle-a", "hourly_usd": 50}]})
        self.assertEqual(cfg.budget_usd_per_hour, 50.0)
        self.assertEqual(cfg.config_shadowed, ("accounts", "budget_usd_per_hour"))
        self.assertIn("overrides: accounts, budget_usd_per_hour", cfg.config_source)
        # la clé absente du profil reste celle de l'hôte
        self.assertEqual(cfg.host, "hote-de-base")

    def test_un_profil_peut_se_declarer_autonome(self):
        self._write(self.profil, {"inherit": False, "host": "isole"})
        cfg = self.load(AMEESH_CONFIG=self.profil)
        self.assertEqual(cfg.accounts, {})
        self.assertEqual(cfg.host, "isole")
        self.assertEqual(cfg.config_base, "")
        self.assertEqual(cfg.config_source, self.profil)

    def test_un_fichier_hors_du_dossier_de_l_hote_reste_autonome(self):
        """Le banc et les configurations temporaires ne lisent jamais la
        configuration du poste (même règle que scripts/test.sh)."""
        ailleurs = self._write(os.path.join(self.tmp, "ailleurs.json"),
                               {"host": "ailleurs"})
        cfg = self.load(AMEESH_CONFIG=ailleurs)
        self.assertEqual(cfg.accounts, {})
        self.assertEqual(cfg.budget_usd_per_hour, config_mod.Config.budget_usd_per_hour)
        self.assertEqual(cfg.host, "ailleurs")
        self.assertEqual(cfg.config_base, "")
        self.assertEqual(cfg.config_shadowed, ())

    def test_un_fichier_a_cote_de_config_json_reste_autonome(self):
        """Revue Claude #1466 : `config-ancienne-base.json`, la configuration
        d'une AUTRE base rangée à côté de `config.json`, n'emprunte pas les clés
        de l'hôte (ni comptes, ni `github`, ni `critical_agents`)."""
        ancienne = self._write(os.path.join(self.cfgdir, "config-ancienne-base.json"),
                               {"host": "ancienne"})
        cfg = self.load(AMEESH_CONFIG=ancienne)
        self.assertEqual(cfg.accounts, {})
        self.assertEqual(cfg.host, "ancienne")
        self.assertEqual(cfg.config_base, "")
        self.assertEqual(cfg.config_source, ancienne)

    def test_inherit_vrai_force_l_heritage_hors_du_dossier_des_profils(self):
        """`"inherit": true` reste l'échappatoire explicite d'un fichier rangé
        ailleurs sous le dossier de l'hôte."""
        ancienne = self._write(os.path.join(self.cfgdir, "config-ancienne-base.json"),
                               {"inherit": True, "host": "ancienne"})
        cfg = self.load(AMEESH_CONFIG=ancienne)
        self.assertEqual(cfg.accounts, {"deepseek": [{"name": "cle-a", "hourly_usd": 100}]})
        self.assertEqual(cfg.config_base, self.host)

    def test_une_base_explicite_se_nomme(self):
        ailleurs = self._write(os.path.join(self.tmp, "ailleurs.json"),
                               {"inherit": self.host, "host": "profil"})
        cfg = self.load(AMEESH_CONFIG=ailleurs)
        self.assertEqual(cfg.accounts, {"deepseek": [{"name": "cle-a", "hourly_usd": 100}]})
        self.assertEqual(cfg.host, "profil")
        self.assertEqual(cfg.config_base, self.host)

    def test_base_explicite_absente_refusee(self):
        ailleurs = self._write(os.path.join(self.tmp, "ailleurs.json"),
                               {"inherit": os.path.join(self.tmp, "absente.json")})
        with self.assertRaises(SystemExit) as caught:
            self.load(AMEESH_CONFIG=ailleurs)
        self.assertIn("inherited config not found", str(caught.exception))

    def test_inherit_illisible_refuse(self):
        ailleurs = self._write(os.path.join(self.tmp, "ailleurs.json"),
                               {"inherit": 3})
        with self.assertRaises(SystemExit) as caught:
            self.load(AMEESH_CONFIG=ailleurs)
        self.assertIn("invalid `inherit`", str(caught.exception))

    def test_sans_variable_le_fichier_par_defaut_ne_s_herite_pas_lui_meme(self):
        cfg = self.load()
        self.assertEqual(cfg.config_path, self.host)
        self.assertEqual(cfg.config_base, "")
        self.assertEqual(cfg.accounts, {"deepseek": [{"name": "cle-a", "hourly_usd": 100}]})


if __name__ == "__main__":
    unittest.main()
