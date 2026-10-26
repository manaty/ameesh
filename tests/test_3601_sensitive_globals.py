# SPDX-License-Identifier: AGPL-3.0-only
"""#3601 : le banc échoue dans la classe qui laisse un global sensible patché.

Le témoin n'ouvre pas de base : il définit des classes d'exemple hors de
portée de la découverte et les passe à un lanceur `unittest` imbriqué. La
classe qui fuit échoue à son propre `tearDownClass`, en nommant la classe et
le global ; la classe suivante reste saine (#3600 : l'erreur n'apparaissait
que 35 classes plus loin, en `setUpClass`).

#3625 : sous `python -m unittest -b`, `TextTestRunner(buffer=True)` remplace
`sys.stdout`/`sys.stderr` avant les fixtures de classe ; la référence des flux
est donc prise à la tête de chaque classe, et le lanceur tamponné ne casse
plus au `tearDownClass` d'un `PgTestCase` (`AttributeError: getvalue`).
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import db as db_mod

from .support import (
    PgTestCase, SENSITIVE_GLOBALS, SensitiveGlobalsMixin,
    capture_sensitive_globals,
)


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


def _lancer_bufferise(*classes) -> unittest.TestResult:
    """Le cas `python -m unittest -b` : le lanceur tamponne les flux."""
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(classe)
        for classe in classes)
    runner = unittest.TextTestRunner(stream=io.StringIO(), buffer=True,
                                     verbosity=0)
    return runner.run(suite)


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
                capture_sensitive_globals(cls)
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
                capture_sensitive_globals(cls)
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

    def test_runner_tamponne_ne_casse_pas_au_teardownclass(self):
        """#3625 : `-b` ne doit ni faire échouer une classe saine, ni casser
        le lanceur en restaurant le flux réel à la place de son tampon."""
        class TamponneePg(PgTestCase):
            @classmethod
            def setUpClass(cls):
                capture_sensitive_globals(cls)
                cls.schema = "t_tamponnee_3625"
                cls.tmp = tempfile.mkdtemp(prefix="ameesh-3625-")
                cls.db = mock.MagicMock()

            def setUp(self):
                pass

            def test_saine(self):
                pass

        resultat = _lancer_bufferise(TamponneePg)

        self.assertEqual(resultat.testsRun, 1)
        self.assertEqual(resultat.errors, [])
        self.assertEqual(resultat.failures, [])

    def test_runner_tamponne_nomme_une_fuite_de_connect(self):
        """Sous `-b`, une vraie fuite reste nommée et le lanceur survit à la
        restauration (#3625) : les flux, eux, sont rendus au tampon."""
        class FuiteConnectPg(PgTestCase):
            patcheur = None

            @classmethod
            def setUpClass(cls):
                capture_sensitive_globals(cls)
                cls.schema = "t_fuite_connect_3625"
                cls.tmp = tempfile.mkdtemp(prefix="ameesh-3625-")
                cls.db = mock.MagicMock()

            def setUp(self):
                pass

            def test_fuite(self):
                FuiteConnectPg.patcheur = mock.patch.object(
                    db_mod, "connect", new=mock.Mock())
                FuiteConnectPg.patcheur.start()

        try:
            resultat = _lancer_bufferise(FuiteConnectPg)
        finally:
            if FuiteConnectPg.patcheur is not None:
                FuiteConnectPg.patcheur.stop()

        self.assertEqual(resultat.testsRun, 1)
        self.assertEqual(resultat.failures, [])
        self.assertEqual(len(resultat.errors), 1, resultat.errors)
        self.assertIn("ameesh.db.connect", resultat.errors[0][1])
