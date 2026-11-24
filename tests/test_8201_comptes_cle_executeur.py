# SPDX-License-Identifier: AGPL-3.0-only
"""#8201 — `accounts list` ne juge pas une clé portée par l'exécuteur.

Constat : lancé depuis un shell sans `DEEPSEEK_API_KEY` (la clé vit dans
l'EnvironmentFile de l'exécuteur), `accounts list` affichait
« deepseek/ambiant invalid : missing key variable » alors que les tours
partaient. Le rapport complète l'environnement des variables DÉCLARÉES,
comme `account_turn._declared_environ`, et la ligne dit que la clé est portée
par l'exécuteur, non vérifiée depuis ce shell — sans jamais lire ni afficher
une valeur de clé. Un vrai défaut (fichier de clé absent) reste `invalid`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts

CLE = "AMEESH_8201_DEEPSEEK_KEY"


class ClePorteeParExecuteurTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8201-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = SimpleNamespace(
            host="test-8201", state_dir=self.tmp,
            account_registry=os.path.join(self.tmp, "registre"),
            accounts={"deepseek": [{"name": "ambiant", "type": "api_key_env",
                                    "key_env": CLE}]})

    def _rows(self, environ, cfg=None):
        with mock.patch.dict(os.environ, environ, clear=True):
            return accounts.report(cfg or self.cfg, None, None, record=False,
                                   environ=os.environ)

    def test_cle_absente_du_shell_nest_pas_invalide(self):
        """Témoin du lot : la clé déclarée, absente de `os.environ`."""
        rows = self._rows({})
        (row,) = rows
        self.assertEqual(row["problems"], [])
        self.assertTrue(row["ok"], row["reason"])
        self.assertEqual(row["key_env_unverified"], CLE)
        texte = accounts.format_rows(rows)
        self.assertNotIn("invalid", texte)
        self.assertIn("carried by the executor, not verifiable from this shell", texte)
        self.assertIn(CLE, texte)

    def test_cle_presente_dans_le_shell_nest_pas_signalee(self):
        """La clé est dans ce shell : rien à signaler, comportement d'avant."""
        rows = self._rows({CLE: "present"})
        (row,) = rows
        self.assertEqual(row["key_env_unverified"], "")
        self.assertTrue(row["ok"], row["reason"])
        self.assertNotIn("carried by the executor", accounts.format_rows(rows))

    def test_check_refuse_toujours_une_cle_absente(self):
        """La compensation ne vaut que pour l'affichage : le lancement garde
        `check` strict (une clé réellement absente refuse le tour)."""
        (profile,) = accounts.parse(self.cfg.accounts)["deepseek"]
        self.assertIn("missing key variable: %s" % CLE,
                      accounts.check(profile, environ={}))

    def test_fichier_de_cle_absent_reste_invalide(self):
        """Un défaut vérifiable (fichier de clé) n'est pas masqué."""
        cfg = SimpleNamespace(
            host="test-8201", state_dir=self.tmp,
            account_registry=os.path.join(self.tmp, "registre"),
            accounts={"deepseek": [{
                "name": "fichier", "type": "api_key_env",
                "key_file": os.path.join(self.tmp, "absent.key")}]})
        (row,) = self._rows({}, cfg=cfg)
        self.assertFalse(row["ok"])
        self.assertTrue(any("missing key file" in problem
                            for problem in row["problems"]))
        self.assertEqual(row["key_env_unverified"], "")
        self.assertIn("invalid", accounts.format_rows([row]))


if __name__ == "__main__":
    unittest.main()
