# SPDX-License-Identifier: AGPL-3.0-only
"""#3601 : le banc échoue dans la classe qui laisse un global sensible patché.

Le témoin n'ouvre pas de base : il définit des classes d'exemple hors de
portée de la découverte et les passe à un lanceur `unittest` imbriqué. La
classe qui fuit échoue à son propre `tearDownClass`, en nommant la classe et
le global ; la classe suivante reste saine (#3600 : l'erreur n'apparaissait
que 35 classes plus loin, en `setUpClass`).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock

from ameesh import db as db_mod

from .support import PgTestCase, SENSITIVE_GLOBALS, SensitiveGlobalsMixin


def _fuiter_les_globaux() -> list:
    """Remplace les trois globaux sans arrêter les patcheurs (fuite voulue)."""
    patcheurs = []
    try:
        for _, module, attribut, _ in SENSITIVE_GLOBALS:
            patcheur = mock.patch.object(module, attribut, new=mock.Mock())
            patcheur.start()
            patcheurs.append(patcheur)
    except BaseException:
        for patcheur in reversed(patcheurs):
            patcheur.stop()
        raise
    return patcheurs


def _lancer(*classes) -> unittest.TestResult:
    resultat = unittest.TestResult()
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(classe)
        for classe in classes)
    suite.run(resultat)
    return resultat


class SensitiveGlobalsTest(unittest.TestCase):
    def test_pgtestcase_fuite_echoue_dans_sa_classe_et_la_suivante_est_saine(self):
        connect, stdout, stderr = db_mod.connect, sys.stdout, sys.stderr
        patcheurs = []
        saine = []

        class FuitePg(PgTestCase):
            # Pas de Postgres : le ménage de PgTestCase travaille sur un faux
            # pilote ; seule la vérification des globaux nous intéresse.
            @classmethod
            def setUpClass(cls):
                cls.schema = "t_fuite_3601"
                cls.tmp = tempfile.mkdtemp(prefix="ameesh-3601-")
                cls.db = mock.MagicMock()

            def setUp(self):
                pass

            def test_fuite(self):
                patcheurs.extend(_fuiter_les_globaux())

        class SuivantePg(PgTestCase):
            @classmethod
            def setUpClass(cls):
                cls.schema = "t_suivante_3601"
                cls.tmp = tempfile.mkdtemp(prefix="ameesh-3601-")
                cls.db = mock.MagicMock()

            def setUp(self):
                pass

            def test_saine(self):
                saine.append((db_mod.connect is connect,
                              sys.stdout is stdout, sys.stderr is stderr))

        self.addCleanup(lambda: [patcheur.stop() for patcheur in patcheurs])
        resultat = _lancer(FuitePg, SuivantePg)

        self.assertEqual(resultat.testsRun, 2)
        self.assertEqual(resultat.failures, [])
        self.assertEqual(len(resultat.errors), 1, resultat.errors)
        epreuve, trace = resultat.errors[0]
        self.assertIn("tearDownClass", str(epreuve))
        self.assertIn("FuitePg", str(epreuve))
        for nom in ("ameesh.db.connect", "sys.stdout", "sys.stderr"):
            self.assertIn(nom, trace)
        self.assertIn("FuitePg", trace)
        self.assertEqual(saine, [(True, True, True)])
        self.assertEqual((db_mod.connect, sys.stdout, sys.stderr),
                         (connect, stdout, stderr))

    def test_patchs_restaures_ne_font_pas_echouer_la_classe(self):
        connect, stdout, stderr = db_mod.connect, sys.stdout, sys.stderr

        class Propre(SensitiveGlobalsMixin, unittest.TestCase):
            def test_propre(self):
                with mock.patch.object(db_mod, "connect", new=mock.Mock()), \
                        mock.patch.object(sys, "stdout", new=mock.Mock()), \
                        mock.patch.object(sys, "stderr", new=mock.Mock()):
                    self.assertIsNot(db_mod.connect, connect)
                    self.assertIsNot(sys.stdout, stdout)
                    self.assertIsNot(sys.stderr, stderr)

        resultat = _lancer(Propre)

        self.assertEqual(resultat.testsRun, 1)
        self.assertEqual(resultat.errors, [])
        self.assertEqual(resultat.failures, [])
        self.assertEqual((db_mod.connect, sys.stdout, sys.stderr),
                         (connect, stdout, stderr))
