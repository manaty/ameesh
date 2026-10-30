# SPDX-License-Identifier: AGPL-3.0-only
"""#4740 : un fil qui survit à son test est nommé et fait échouer ce test.

Le témoin de #4736 écrit « {} » depuis un fil pour polluer une capture
`redirect_stdout`. Mal joint (échec avant `status_of`, délai dépassé), ce fil
survit à son test et son écriture tombe dans la capture du test voisin
(« Extra data: line 1 column 3 »). `ThreadLeakMixin` ferme cette porte : après
les cleanups du test, il laisse un court délai aux fils qui s'arrêtent sur un
tick, puis nomme les survivants et échoue.

Le témoin n'ouvre pas de base : ses classes d'exemple sont définies dans une
fabrique, hors de portée de la découverte, et passées à un lanceur `unittest`
imbriqué.
"""
from __future__ import annotations

import threading
import time
import unittest

from .support import ThreadLeakMixin


def _cases():
    """Les classes d'exemple, locales pour ne pas être découvertes."""

    class Leak(ThreadLeakMixin, unittest.TestCase):
        """Fuit volontairement un fil, pour prouver que la garde le nomme."""

        def setUp(self):
            super().setUp()
            self.stop = threading.Event()
            self.fil = threading.Thread(
                target=self.stop.wait, args=(2.0,), name="fil-survivant-4740",
                daemon=True)
            self.fil.start()

        def test_le_fil_est_encore_vivant(self):
            self.assertTrue(self.fil.is_alive())

    class Clean(ThreadLeakMixin, unittest.TestCase):
        """Joint son fil : la garde le laisse vert."""

        def test_joint_son_fil(self):
            fil = threading.Thread(target=lambda: None, name="fil-joint-4740")
            fil.start()
            fil.join(5)
            self.assertFalse(fil.is_alive())

    class Transient(ThreadLeakMixin, unittest.TestCase):
        """Un fil qui s'arrête sur le tick est laissé au délai de grâce."""

        def test_fil_qui_finit_apres_le_test(self):
            fil = threading.Thread(target=lambda: time.sleep(0.05),
                                   name="fil-transitoire-4740", daemon=True)
            fil.start()
            # Il peut encore vivre ici : la garde attend `thread_leak_grace_s`.
            self.assertIsNotNone(fil.ident)

    return Leak, Clean, Transient


def _run_case(case) -> unittest.TestResult:
    suite = unittest.TestLoader().loadTestsFromTestCase(case)
    result = unittest.TestResult()
    suite.run(result)
    return result


class ThreadLeakWitnessTest(unittest.TestCase):
    def test_un_fil_survivant_fait_echouer_son_test(self):
        leak, _clean, _transient = _cases()
        result = _run_case(leak)
        problems = result.failures + result.errors
        self.assertEqual(len(problems), 1, problems)
        message = problems[0][1]
        self.assertIn("#4740", message)
        self.assertIn("fil-survivant-4740", message)

    def test_un_fil_joint_laisse_le_test_vert(self):
        _leak, clean, _transient = _cases()
        self.assertTrue(_run_case(clean).wasSuccessful())

    def test_un_fil_transitoire_ne_fait_pas_echouer(self):
        _leak, _clean, transient = _cases()
        self.assertTrue(_run_case(transient).wasSuccessful())
