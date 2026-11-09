# SPDX-License-Identifier: AGPL-3.0-only
"""#6566 — une unité regelée pendant la composition ne part pas sur la tête.

Constat (h-17, 09/11, #6562) : le tic a repris une composition morte
(`paquet-auto.py --force`, n=2) à 16:57:12Z ; #6534 a été regelé à 16:57:40Z
(6f28dbcaa11b) et sorti de la liste prête à 16:58Z, mais la composition a
poussé 7526190d677a à 17:14Z avec l'ancienne source 25abf298a4e2. La garde de
fusion a tenu (« does not carry its frozen head ») et le cycle de CI a été
perdu.

Attendu : la liste prête est relue sur la base exacte juste avant la poussée ;
une unité dont la source a changé (regel) ou qui n'est plus prête est écartée
« coupure suivante : … » et le paquet est recomposé sans elle. Aucune tête ne
part avec un gel périmé.
"""
from __future__ import annotations

import contextlib
import io
import unittest

from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
E = "e" * 40
F = "f" * 40

def two() -> paquet_auto.Composition:
    """Une composition fraîche : `compose_packet` écrit ses écartés dedans."""
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(1, A, B), paquet_auto.Unit(2, C, D)],
        discarded=[], head=F)


def one() -> paquet_auto.Composition:
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(1, A, B)], discarded=[], head=E)


def none() -> paquet_auto.Composition:
    return paquet_auto.Composition(
        units=[],
        discarded=[paquet_auto.Discarded(1, paquet_auto.KEEP_REFROZEN_REASON)],
        head=A)


def read(*sources: tuple[int, str], waiting: list[int] | None = None):
    """Une lecture de la liste prête : (lot, source) et lots en attente."""
    return ([paquet_auto.Candidate(lot, source) for lot, source in sources],
            {"ready": [lot for lot, _ in sources], "waiting": list(waiting or [])})


INITIAL = read((1, A), (2, C))
REFROZEN = read((1, A), (2, E))
GONE = read((1, A), waiting=[2])
ONLY = read((1, A))
ONLY_REFROZEN = read((1, E))


class ChangedUnitsTest(unittest.TestCase):
    """La comparaison gel composé / gel courant, sans git ni base."""

    def test_gel_inchange_rien(self) -> None:
        units = [paquet_auto.Unit(1, A, B), paquet_auto.Unit(2, C, D)]
        self.assertEqual(paquet_auto.changed_units(units, {1: A, 2: C}), [])

    def test_regel_ecarte_l_unite(self) -> None:
        changes = paquet_auto.changed_units(
            [paquet_auto.Unit(2, C, D)], {2: E})
        self.assertEqual([(item.lot, item.reason) for item in changes],
                         [(2, paquet_auto.KEEP_REFROZEN_REASON)])
        self.assertIn("nouvelle tête", changes[0].detail)
        self.assertIn(C[:12], changes[0].detail)
        self.assertIn(E[:12], changes[0].detail)

    def test_lot_sorti_de_la_liste_ecarte_l_unite(self) -> None:
        changes = paquet_auto.changed_units([paquet_auto.Unit(2, C, D)], {})
        self.assertEqual([(item.lot, item.reason) for item in changes],
                         [(2, paquet_auto.KEEP_NOT_READY_REASON)])
        self.assertIn("plus dans la liste prête", changes[0].detail)


class GelAvantPousseeTest(unittest.TestCase):
    """Le témoin de h-17 : la composition puis le regel, avant la poussée."""

    def test_unite_reglee_ne_part_pas_et_le_paquet_est_recompose(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[two(), one()],
                    candidate_reads=[INITIAL, REFROZEN])
        self.assertEqual(flux.run(), 0)
        # la tête composée avec l'ancien gel n'est jamais poussée ; seule la
        # recomposition sans #2 l'est.
        self.assertEqual(len(flux.calls("push")), 1)
        self.assertEqual([event[1][2] for event in flux.calls("freeze")],
                         ["2" * 40])
        recompose = flux.calls("recompose")
        self.assertEqual(len(recompose), 1)
        self.assertEqual([candidate.lot for candidate in recompose[0][1][2]], [1])
        self.assertEqual(recompose[0][2]["removed"], {2: C})
        final = flux.manifest_compositions[-1]
        self.assertEqual([(item.lot, item.reason) for item in final.discarded],
                         [(2, paquet_auto.KEEP_REFROZEN_REASON)])
        self.assertGreaterEqual(flux.candidate_calls, 2)

    def test_lot_sorti_de_la_liste_ne_part_pas_non_plus(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[two(), one()],
                    candidate_reads=[INITIAL, GONE])
        self.assertEqual(flux.run(), 0)
        self.assertEqual(len(flux.calls("push")), 1)
        self.assertEqual([event[1][2] for event in flux.calls("freeze")],
                         ["2" * 40])
        final = flux.manifest_compositions[-1]
        self.assertEqual([(item.lot, item.reason) for item in final.discarded],
                         [(2, paquet_auto.KEEP_NOT_READY_REASON)])

    def test_gel_inchange_pousse_une_fois_sans_recomposer(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[two()],
                    candidate_reads=[INITIAL, INITIAL])
        self.assertEqual(flux.run(), 0)
        self.assertEqual(len(flux.calls("push")), 1)
        self.assertEqual([event[1][2] for event in flux.calls("freeze")],
                         ["1" * 40])
        self.assertEqual(flux.calls("recompose"), [])
        self.assertEqual(flux.candidate_calls, 2)

    def test_plus_aucune_unite_rien_n_est_pousse(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])],
                    compositions=[one(), none()],
                    candidate_reads=[ONLY, ONLY_REFROZEN])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(flux.run(), 0)
        self.assertEqual(flux.calls("push"), [])
        self.assertEqual(flux.calls("freeze"), [])
        self.assertIn("aucune unité après recomposition", out.getvalue())


if __name__ == "__main__":
    unittest.main()
