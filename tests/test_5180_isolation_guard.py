# SPDX-License-Identifier: AGPL-3.0-only
"""#5180 : le banc échoue dans le module qui laisse l'état du banc changé.

Le témoin n'ouvre pas de base : il fabrique des modules d'exemple (hors de
portée de la découverte) et les passe à un lanceur `unittest` imbriqué. Le
module qui fuit (#5175 : `CODEX_HOME` retiré ; #5083 : `Popen.__init__`
laissé remplacé) échoue en son nom ; le module suivant voit l'état du banc.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
import unittest

from .support import EMPTY_CODEX_HOME


def _module(nom: str, **tests) -> types.ModuleType:
    """Un module de test jetable `tests._t5180_<nom>`, une classe, `tests`."""
    module = types.ModuleType("tests._t5180_" + nom)
    cas = type("Cas", (unittest.TestCase,),
               {"__module__": module.__name__,
                **{"test_" + n: f for n, f in tests.items()}})
    module.Cas = cas
    return module


class IsolationGuardTest(unittest.TestCase):

    def lancer(self, *modules) -> unittest.TestResult:
        for module in modules:
            sys.modules[module.__name__] = module
            self.addCleanup(sys.modules.pop, module.__name__, None)
        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromTestCase(m.Cas) for m in modules)
        result = unittest.TestResult()
        suite.run(result)
        return result

    def erreurs_de_garde(self, result) -> list:
        return [(str(test), trace) for test, trace in result.errors
                if "garde d'isolement #5180" in str(test)]

    def temoin(self, vu: dict):
        def test(_cas):
            vu["CODEX_HOME"] = os.environ.get("CODEX_HOME")
            vu["Popen.__init__"] = subprocess.Popen.__dict__.get("__init__")
        return test

    def test_codex_home_retire_nomme_le_module_et_remis(self):
        vu = {}
        fuite = _module("codex", pop=lambda _c: os.environ.pop("CODEX_HOME") and None)
        suivant = _module("suivant_codex", lire=self.temoin(vu))
        result = self.lancer(fuite, suivant)
        erreurs = self.erreurs_de_garde(result)
        self.assertEqual(len(erreurs), 1, result.errors)
        nom, trace = erreurs[0]
        self.assertIn("tests._t5180_codex", nom)
        self.assertIn("CODEX_HOME retirée", trace)
        self.assertEqual(vu["CODEX_HOME"], EMPTY_CODEX_HOME)
        self.assertEqual(os.environ.get("CODEX_HOME"), EMPTY_CODEX_HOME)

    def test_popen_init_laisse_remplace_nomme_le_module_et_remis(self):
        original = subprocess.Popen.__dict__["__init__"]
        vu = {}

        def patcher(_cas):
            def remplace(self, *args, **kwargs):
                original(self, *args, **kwargs)
            subprocess.Popen.__init__ = remplace

        self.addCleanup(setattr, subprocess.Popen, "__init__", original)
        result = self.lancer(_module("popen", patch=patcher),
                             _module("suivant_popen", lire=self.temoin(vu)))
        erreurs = self.erreurs_de_garde(result)
        self.assertEqual(len(erreurs), 1, result.errors)
        nom, trace = erreurs[0]
        self.assertIn("tests._t5180_popen", nom)
        self.assertIn("subprocess.Popen.__init__ remplacé", trace)
        self.assertIs(vu["Popen.__init__"], original)
        self.assertIs(subprocess.Popen.__dict__["__init__"], original)

    def test_variable_ameesh_ajoutee_est_retiree(self):
        self.assertNotIn("AMEESH_DSN_5180", os.environ)
        result = self.lancer(_module(
            "dsn", poser=lambda _c: os.environ.__setitem__("AMEESH_DSN_5180", "x")))
        erreurs = self.erreurs_de_garde(result)
        self.assertEqual(len(erreurs), 1, result.errors)
        self.assertIn("AMEESH_DSN_5180 ajoutée", erreurs[0][1])
        self.assertNotIn("AMEESH_DSN_5180", os.environ)

    def test_module_propre_ou_remis_par_tear_down_module(self):
        propre = _module("propre", rien=lambda _c: None)
        remis = _module("remis", poser=lambda _c: os.environ.__setitem__(
            "AMEESH_DSN_5180", "x"))
        remis.tearDownModule = lambda: os.environ.pop("AMEESH_DSN_5180")
        result = self.lancer(propre, remis)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
