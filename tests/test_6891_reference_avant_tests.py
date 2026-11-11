# SPDX-License-Identifier: AGPL-3.0-only
"""#6891 — la référence du code est relue AVANT les tests locaux.

Constat h-11 (11/11, #6864) : le commit d'atterrissage de la coupure 1.49.0
arrive sur `main` pendant la composition. `paquet-candidats` refuse alors le
code exécuté (base c1588c6baaea, référence 2d206ca7596f), mais seulement au
moment de la relecture du gel juste avant la poussée : les contrats — 719,8 s
de tests locaux — sont déjà joués, et la tentative n=1 est perdue.

Attendu : la garde #3782 (`code_reference` / `code_provenance` /
`code_refusal`) est appliquée **avant** les tests locaux. Si la cible a avancé,
rien n'est poussé, le clone est à jour (`fetch`) et `paquet-horaire` relance
aussitôt la composition sur la nouvelle tête, sans attendre le tick suivant.
La relecture du gel juste avant la poussée (#6566) reste en place : une cible
qui bouge pendant les tests garde le refus actuel.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests.test_3845_paquet_horaire import Base, HEAD, carrier, ph
from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto

A = "a" * 40
B = "b" * 40


class FakeGuard:
    """Les trois gardes #3782 de `paquet-candidats.py`, sans git ni base."""

    def __init__(self, *, relation: str = "behind", refusal: str | None = None):
        self.relation = relation
        self.refusal = refusal if refusal is not None else (
            "le code exécuté n'est pas la tête de la cible : antérieur à la "
            "référence (code %s, référence %s) ; relancer depuis un worktree à "
            "cette référence exacte, ou --allow-code-mismatch pour assumer le "
            "décalage" % (A[:12], B[:12]))

    def code_reference(self, code_repo, project_repo, target_head):
        return target_head, "project-target"

    def code_provenance(self, code_repo, code_ref):
        return {"code_sha": A, "code_repo": str(code_repo),
                "code_relation": self.relation, "code_contains_ref": False,
                "code_dirty": False}

    def code_refusal(self, provenance, code_ref, ref_source="project-target"):
        return self.refusal


def composition():
    return paquet_auto.Composition(
        units=[paquet_auto.Unit(1, A, B)], discarded=[], head="c" * 40)


class ReferenceAvantTestsTest(unittest.TestCase):
    """La composition est arrêtée avant les contrats, pas après."""

    def test_cible_avancee_arrete_avant_les_contrats(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, B])
        with mock.patch.object(paquet_auto, "candidates_guard",
                               return_value=FakeGuard()):
            with self.assertRaises(paquet_auto.TargetMoved) as caught:
                flux.run()
        self.assertEqual(caught.exception.base, A)
        self.assertEqual(caught.exception.head, B)
        self.assertIn("avancé", str(caught.exception))
        self.assertIn(A[:12], str(caught.exception))
        self.assertIn(B[:12], str(caught.exception))
        # Aucun test local n'est parti et rien n'a été poussé.
        self.assertEqual(flux.calls("contracts"), [])
        self.assertEqual(flux.calls("tests"), [])
        self.assertEqual(flux.calls("push"), [])

    def test_cible_inchangee_laisse_les_contrats_puis_les_tests(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A])
        self.assertEqual(flux.run(), 0)
        self.assertEqual(len(flux.calls("contracts")), 1)
        self.assertEqual(len(flux.calls("tests")), 1)
        self.assertEqual(len(flux.calls("push")), 1)
        # La relecture du gel juste avant la poussée n'est pas retirée : deux
        # lectures de la liste prête (composition, puis gel).
        self.assertEqual(flux.candidate_calls, 2)

    def test_code_en_avance_accepte_par_code_refusal_ne_recompose_pas(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, B])
        with mock.patch.object(paquet_auto, "candidates_guard",
                               return_value=FakeGuard(relation="ahead", refusal="")):
            self.assertEqual(flux.run(), 0)
        self.assertEqual(len(flux.calls("contracts")), 1)
        self.assertEqual(len(flux.calls("push")), 1)


class ExitCodeTest(unittest.TestCase):
    def test_main_sort_en_6_pour_cible_avancee(self):
        error = paquet_auto.TargetMoved("cible avancée", base=A, head=B)
        with mock.patch.object(paquet_auto, "compose_packet", side_effect=error):
            self.assertEqual(paquet_auto.main(["--name", "banc", "--carrier", "1"]),
                             paquet_auto.TARGET_MOVED_RC)


class TickTargetMovedTest(Base):
    """Le tic journalise le refus et relance tout de suite, sans tick d'attente."""

    def test_refus_avant_tests_journalise_puis_recomposition_immediate(self):
        self.shell.items[4000] = carrier(sha="")
        self.shell.auto_rc = ph.auto.TARGET_MOVED_RC
        self.shell.auto_out = (
            "paquet auto : base main %s\n"
            "paquet-auto : la cible main a avancé pendant la composition "
            "(%s → %s) : le code exécuté n'est pas la tête de la cible : "
            "antérieur à la référence ; tests locaux non lancés, clone mis à "
            "jour, recomposition immédiate\n" % (A, A[:12], B[:12]))
        out = ph.tick(self.ctx)
        autos = [call for call in self.shell.calls
                 if "paquet-auto.py" in " ".join(call)]
        # n=1 puis deux relances immédiates, bornées par MAX_ATTEMPTS.
        self.assertEqual(len(autos), ph.MAX_ATTEMPTS)
        self.assertTrue(all("--force" in call for call in autos[1:]))
        notes = [call[call.index("note") + 2]
                 for call in self.shell.ran("work", "note")]
        self.assertTrue(any(note.startswith(ph.MARK_TARGET) for note in notes), notes)
        self.assertIn("n=1", [n for n in notes if n.startswith(ph.MARK_TARGET)][0])
        self.assertTrue(any(note.startswith(ph.MARK_ATTEMPT) and "n=2" in note
                            for note in notes), notes)
        self.assertTrue(any(note.startswith(ph.MARK_ATTEMPT) and "n=3" in note
                            for note in notes), notes)
        self.assertFalse(any(note.startswith(ph.MARK_FAILED) for note in notes), notes)
        self.assertIn("relance immédiate épuisée", out)
        self.assertEqual(self.shell.ran("ci", "run"), [])
        self.assertEqual(self.shell.ran("work", "reviewer"), [])

    def test_la_relance_reussie_reprend_la_publication(self):
        self.shell.items[4000] = carrier()
        self.shell.auto_rc = ph.auto.TARGET_MOVED_RC
        # Le premier lancement rend 6 ; la relance du même tick compose.
        self.shell.auto_rcs = [ph.auto.TARGET_MOVED_RC, 0]
        self.shell.auto_out = (
            "paquet-auto : la cible main a avancé pendant la composition "
            "(%s → %s) : antérieur à la référence\n"
            "manifeste reviews/x.md : tête %s\n" % (A[:12], B[:12], HEAD))
        out = ph.tick(self.ctx)
        autos = [call for call in self.shell.calls
                 if "paquet-auto.py" in " ".join(call)]
        self.assertEqual(len(autos), 2)
        self.assertIn("ci-full demandée", out)
        self.assertTrue(self.shell.ran("ci", "run", "4000", "--full"))
        notes = [call[call.index("note") + 2]
                 for call in self.shell.ran("work", "note")]
        self.assertTrue(any(note.startswith(ph.MARK_TARGET) for note in notes), notes)
        self.assertTrue(any(note.startswith(ph.MARK_CI) for note in notes), notes)


if __name__ == "__main__":
    unittest.main()
