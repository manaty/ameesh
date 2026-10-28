# SPDX-License-Identifier: AGPL-3.0-only
"""#3641 : une fuite de flux d'une classe simple, hors du mixin, reste nommée.

#3625 prenait `sys.stdout`/`sys.stderr` à la tête de chaque classe pour ne pas
casser `python -m unittest -b`. Une classe qui hérite du mixin après une classe
simple ayant laissé `sys.stdout = io.StringIO()` figeait donc cette fuite comme
référence, et son `tearDownClass` ne la voyait plus. La référence par classe
n'est désormais prise que si le flux courant est le tampon du résultat tamponné
actif ; sinon celle du chargement reste la vérité.
"""
from __future__ import annotations

import io
import sys
import unittest

from .support import SensitiveGlobalsMixin


def _suite(*classes) -> unittest.TestSuite:
    return unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(classe)
        for classe in classes)


def _lancer(*classes) -> unittest.TestResult:
    resultat = unittest.TestResult()
    _suite(*classes).run(resultat)
    return resultat


def _lancer_tamponne(*classes) -> unittest.TestResult:
    runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, buffer=True)
    return runner.run(_suite(*classes))


class StreamLeakOutsideMixinTest(unittest.TestCase):
    def setUp(self):
        self.stdout, self.stderr = sys.stdout, sys.stderr
        self.addCleanup(setattr, sys, "stdout", self.stdout)
        self.addCleanup(setattr, sys, "stderr", self.stderr)

    def test_une_fuite_hors_du_mixin_est_nommee_par_la_classe_suivante(self):
        class Fuite(unittest.TestCase):
            def test_fuite(self):
                sys.stdout = io.StringIO()

        class Suivante(SensitiveGlobalsMixin, unittest.TestCase):
            def test_saine(self):
                self.assertIsInstance(sys.stdout, io.StringIO)

        resultat = _lancer(Fuite, Suivante)

        self.assertEqual(resultat.testsRun, 2)
        self.assertEqual(resultat.failures, [])
        self.assertEqual(len(resultat.errors), 1, resultat.errors)
        epreuve, trace = resultat.errors[0]
        self.assertIn("tearDownClass", str(epreuve))
        self.assertIn("Suivante", str(epreuve))
        self.assertIn("sys.stdout", trace)
        # La garde restaure la référence du chargement avant d'échouer.
        self.assertIs(sys.stdout, self.stdout)

    def test_une_classe_saine_passe_sous_b_et_le_lanceur_survit(self):
        class Saine(SensitiveGlobalsMixin, unittest.TestCase):
            def test_saine(self):
                print("bruit tamponné")

        resultat = _lancer_tamponne(Saine)

        self.assertEqual(resultat.testsRun, 1)
        self.assertEqual(resultat.errors, [])
        self.assertEqual(resultat.failures, [])
        self.assertIs(sys.stdout, self.stdout)
        self.assertIs(sys.stderr, self.stderr)

    def test_la_reference_par_classe_ne_prend_que_le_tampon_du_resultat(self):
        """Un StringIO qui n'est pas le tampon actif n'est jamais une référence."""
        from .support import _active_result_streams

        fuite = io.StringIO()
        previous, sys.stdout = sys.stdout, fuite
        try:
            self.assertEqual(_active_result_streams(), {})
        finally:
            sys.stdout = previous
