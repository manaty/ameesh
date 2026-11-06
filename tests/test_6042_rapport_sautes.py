# SPDX-License-Identifier: AGPL-3.0-only
"""#6042 — le rapport de tests locaux ne fait plus passer les modules joués
pour des modules sautés.

`paquet-auto` listait les modules des séries juste après « N sauté(s) : » ;
le rapport de composition (h-06-12) se lisait donc comme si les ~190 modules
joués avaient été sautés. Le compte des sautés est celui des tests ; la liste
est nommée « modules des séries ».
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-auto.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_6042_paquet_auto", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_6042_paquet_auto"] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script()


class SummarySkippedModulesTest(unittest.TestCase):
    def test_les_modules_joues_ne_suivent_pas_le_compte_des_sautes(self) -> None:
        run = paquet_auto.TestRun(
            "unité #1", ["tests.test_a", "tests.test_b"], 0,
            "Ran 2 tests in 0.1s\n\nOK (skipped=10)\n", 0.1)

        summary = paquet_auto._tests_summary([run])

        self.assertIn("10 test(s) sauté(s)", summary)
        self.assertIn("modules des séries : `tests.test_a`, `tests.test_b`",
                      summary)
        # le défaut #6042 : la liste suivait « N sauté(s) : » et se lisait
        # comme la liste des modules sautés
        self.assertNotIn("sauté(s) : `", summary)

    def test_la_ligne_reste_lue_par_la_porte_du_paquet(self) -> None:
        from ameesh.prevalidation_calls import COMPOSER_TESTS_RE

        run = paquet_auto.TestRun(
            "unités", ["tests.test_a"], 0,
            "Ran 12 tests in 0.1s\n\nOK (skipped=2)\n", 0.1)
        line = "- Tests locaux : %s." % paquet_auto._tests_summary([run])

        found = COMPOSER_TESTS_RE.search(line)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1), "12")
        self.assertEqual(found.group(2), "0")

    def test_aucune_serie_dit_aucun_module(self) -> None:
        self.assertIn("modules des séries : aucun",
                      paquet_auto._tests_summary([]))


if __name__ == "__main__":
    unittest.main()
