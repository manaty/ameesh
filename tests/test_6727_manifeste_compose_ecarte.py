# SPDX-License-Identifier: AGPL-3.0-only
"""#6727 — un lot composé n'est jamais aux écartés du même manifeste.

Constat (h-10, 10/11, #6726, manifeste à f3e2ece2af8c) : #6670, en conflit
avec #6669 au premier passage, est écarté ; #6669 est regelé avant la
poussée (#6566) et la recomposition sans lui compose #6670. L'écart du premier
passage restait au manifeste : #6670 figurait sous « Composition » et sous
« Écartés », et `packet_close.parse_manifest` le tenait pour retiré.

Attendu : la recomposition repart d'écarts vidés des lots qu'elle compose ;
l'écriture du manifeste refuse une intersection Composition ∩ Écartés.
"""
from __future__ import annotations

import unittest

from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 40
F = "f" * 40
G = "7" * 40
H = "8" * 40

CONFLICT = paquet_auto.Discarded(
    3, "conflit, coupure suivante", "fichiers : src/ameesh/model_evaluation_report.py")


def first_pass() -> paquet_auto.Composition:
    """#1 et #2 composés, #3 en conflit avec #2."""
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(1, A, B), paquet_auto.Unit(2, C, D)],
        discarded=[CONFLICT], head=F)


def without_two() -> paquet_auto.Composition:
    """Sans #2 (regelé), #3 passe proprement."""
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(1, A, B), paquet_auto.Unit(3, G, H)],
        discarded=[], head=E)


def read(*sources: tuple[int, str]):
    return ([paquet_auto.Candidate(lot, source) for lot, source in sources],
            {"ready": [lot for lot, _ in sources], "waiting": []})


class RecompositionApresRegelTest(unittest.TestCase):

    def test_lot_ecarte_puis_compose_quitte_les_ecartes(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[first_pass(), without_two()],
                    candidate_reads=[read((1, A), (2, C), (3, G)),
                                     read((1, A), (2, E), (3, G)),
                                     read((1, A), (2, E), (3, G))])
        self.assertEqual(flux.run(), 0)
        final = flux.manifest_compositions[-1]
        self.assertEqual([unit.lot for unit in final.units], [1, 3])
        self.assertEqual([(item.lot, item.reason) for item in final.discarded],
                         [(2, paquet_auto.KEEP_REFROZEN_REASON)])


class EcartsDedoublesTest(unittest.TestCase):

    def test_ecart_d_un_lot_compose_tombe(self) -> None:
        other = paquet_auto.Discarded(2, paquet_auto.KEEP_REFROZEN_REASON)
        self.assertEqual(
            paquet_auto.unique_discarded([CONFLICT, other],
                                         [paquet_auto.Unit(3, G, H)]),
            [other])

    def test_sans_unites_rien_ne_tombe(self) -> None:
        self.assertEqual(paquet_auto.unique_discarded([CONFLICT]), [CONFLICT])


class GardeManifesteTest(unittest.TestCase):

    def manifest(self, discarded: list) -> str:
        return paquet_auto.build_manifest(
            name="h-20261110-10", target="main", base_sha=A, carrier=6726,
            composition=paquet_auto.Composition(
                units=[paquet_auto.Unit(1, A, B), paquet_auto.Unit(3, G, H)],
                discarded=discarded, head=E),
            table="", tests=None, freeze_seconds=1.0)

    def test_compose_et_ecarte_refuse(self) -> None:
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            self.manifest([CONFLICT])
        self.assertIn("#3", str(caught.exception))

    def test_ecartes_disjoints_ecrits(self) -> None:
        text = self.manifest([paquet_auto.Discarded(2, "regel")])
        self.assertIn("## Écartés", text)


if __name__ == "__main__":
    unittest.main()
