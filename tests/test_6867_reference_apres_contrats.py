# SPDX-License-Identifier: AGPL-3.0-only
"""#6867 — la cible qui atterrit pendant les contrats recompose, sans §16.

Constat h-11 (11/11, #6864) : le commit d'atterrissage de la coupure 1.49.0
(2d206ca7596f) arrive sur `main` pendant la composition. La relecture #3782 de
#6891 tombe avant les contrats ; celle du gel (#6566) tombe après, et son
`compute_candidates` traduit le refus `paquet-candidats` (rc 2) en
`PaquetAutoError` : `paquet-auto` sort en 1, `paquet-horaire` ouvre un lot
d'empreinte §16 et la tentative n=1 est perdue, alors que la relance immédiate
de #6891 aurait recomposé sur la nouvelle tête.

Attendu : la référence #3782 est relue **après** les contrats, juste avant le
gel et la poussée, tant que rien n'est poussé. Une cible avancée lève
`TargetMoved` (rc 6, relance immédiate) ; une cible inchangée laisse le gel et
la poussée intacts.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto
from tests.test_6891_reference_avant_tests import A, B, FakeGuard, composition


class ReferenceApresContratsTest(unittest.TestCase):
    def test_cible_avancee_pendant_les_contrats_arrete_avant_le_gel(self):
        # base A, relecture avant contrats A, relecture après contrats B : la
        # cible a atterri pendant les ~12 min de contrats.
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[composition()], fresh_bases=[A, A, B])
        with mock.patch.object(paquet_auto, "candidates_guard",
                               return_value=FakeGuard()):
            with self.assertRaises(paquet_auto.TargetMoved) as caught:
                flux.run()
        self.assertEqual(caught.exception.base, A)
        self.assertEqual(caught.exception.head, B)
        self.assertIn("avancé", str(caught.exception))
        # Les contrats ont été joués, le gel et la poussée non : le refus tombe
        # avant `freeze_changes`, donc avant le rc 1 de #6867.
        self.assertEqual(len(flux.calls("contracts")), 1)
        self.assertEqual(flux.calls("push"), [])
        self.assertEqual(flux.calls("freeze"), [])
        self.assertEqual(flux.candidate_calls, 1)

    def test_cible_inchangee_laisse_le_gel_et_la_poussee(self):
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[composition()], fresh_bases=[A])
        self.assertEqual(flux.run(), 0)
        # La relecture du gel avant la poussée reste en place (#6566) : deux
        # lectures de la liste prête (composition, puis gel).
        self.assertEqual(flux.candidate_calls, 2)
        self.assertEqual(len(flux.calls("push")), 1)
        self.assertEqual(len(flux.calls("freeze")), 1)


if __name__ == "__main__":
    unittest.main()
