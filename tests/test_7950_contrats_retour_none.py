# SPDX-License-Identifier: AGPL-3.0-only
"""#7950 — un module de contrat jamais lancé n'est pas « code de retour None ».

Constat : dans h-10 (contrats de 15:26Z), 20 modules suivaient un contrat rouge
et sortaient « code de retour None » ; h-04 en comptait environ 15, et h-07
sortait les mêmes en « délai dépassé » quand la borne globale coupait la passe.
Un retour None ne distingue pas une série jamais partie d'une série lancée puis
coupée : le journal doit le dire, et la fin de passe répartit le temps restant
pour qu'un module long ne consomme plus la borne des suivants.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from ameesh import packet_contract_proof as proof
from tests.test_4304_paquet_poussee_precoce import paquet_auto as auto

RED = 1


def _red(module: str) -> auto.TestRun:
    return auto.TestRun(
        "contrats " + module, [module], RED,
        "FAIL: test_x (%s.T.test_x)\nAssertionError: regression\n" % module, 0.1)


class NeverLaunchedTest(unittest.TestCase):
    """Un module que le composeur n'a pas lancé, et pourquoi."""

    def test_a_negative_stops_the_pass_and_names_each_unstarted_module(self):
        ran: list[str] = []

        def execute(*args, **kwargs):
            module = args[2][0]
            ran.append(module)
            return _red(module)

        with mock.patch.object(auto, "contract_order",
                               side_effect=lambda _, modules: modules), \
                mock.patch.object(auto, "run_tests", side_effect=execute), \
                mock.patch.object(auto, "save_test_log",
                                  side_effect=lambda run: run):
            run = auto.run_contract_modules(
                Path("."), ["tests.test_a", "tests.test_b", "tests.test_c"],
                end=time.monotonic() + 60, parallel=1)

        self.assertEqual(ran, ["tests.test_a"])
        self.assertEqual(len(run.children), 3)
        self.assertTrue(run.children[0].real_failure)
        for child in run.children[1:]:
            self.assertEqual(
                child.motive(),
                "non lancé : arrêt après le contrat négatif tests.test_a")
            self.assertFalse(child.finished)
            self.assertNotIn("code de retour None", child.motive())
        # #4814 : un module non lancé ne compte pas comme joué jusqu'au bout.
        summary = auto._tests_summary([run])
        self.assertIn("3 modules dont 0 joués jusqu'au bout", summary)

    def test_the_projection_says_not_started_for_the_leaf(self):
        def execute(*args, **kwargs):
            return _red(args[2][0])

        with mock.patch.object(auto, "contract_order",
                               side_effect=lambda _, modules: modules), \
                mock.patch.object(auto, "run_tests", side_effect=execute), \
                mock.patch.object(auto, "save_test_log",
                                  side_effect=lambda run: run):
            run = auto.run_contract_modules(
                Path("."), ["tests.test_a", "tests.test_b"],
                end=time.monotonic() + 60, parallel=1)
        report = auto.ContractPass("a" * 40, "b" * 40, run)
        projected = proof.projection(report, base="c" * 40, target="main")
        leaf = projected["result"]["children"][1]
        self.assertEqual(leaf["state"], "inconclusive")
        self.assertEqual(
            leaf["reason"],
            "non lancé : arrêt après le contrat négatif tests.test_a")
        self.assertNotIn("code de retour None",
                         proof.encode(report, base="c" * 40, target="main"))

    def test_a_spent_global_budget_is_named_with_its_duration(self):
        clock = [0.0]

        def execute(*args, **kwargs):
            clock[0] = 13.0
            return auto.TestRun("contrats", [args[2][0]], None, "cut", 1.0,
                                timed_out=True)

        with mock.patch.object(auto.time, "monotonic",
                               side_effect=lambda: clock[0]), \
                mock.patch.object(auto, "contract_order",
                                  side_effect=lambda _, modules: modules), \
                mock.patch.object(auto, "run_tests", side_effect=execute), \
                mock.patch.object(auto, "save_test_log",
                                  side_effect=lambda run: run):
            run = auto.run_contract_modules(
                Path("."), ["tests.test_a", "tests.test_b"],
                end=12, parallel=1)

        self.assertEqual(run.children[0].share_cut, 6)
        self.assertIn("coupé par sa part (6 s)",
                      run.children[0].motive())
        child = run.children[1]
        self.assertEqual(
            child.not_started,
            "délai global de la série atteint (12 s de budget)")
        self.assertEqual(
            child.motive(),
            "non lancé : délai global de la série atteint (12 s de budget)")
        self.assertFalse(child.finished)

    def test_tail_shares_the_remaining_budget_so_every_module_starts(self):
        clock = [0.0]
        seen: list[tuple[str, int, int]] = []

        def execute(*args, **kwargs):
            seen.append((args[2][0], kwargs["timeout"], kwargs["wall"]))
            clock[0] += kwargs["timeout"]
            return auto.TestRun("contrats", [args[2][0]], None, "cut",
                                kwargs["timeout"], timed_out=True)

        with mock.patch.object(auto.time, "monotonic",
                               side_effect=lambda: clock[0]), \
                mock.patch.object(auto, "contract_order",
                                  side_effect=lambda _, modules: modules), \
                mock.patch.object(auto, "run_tests", side_effect=execute), \
                mock.patch.object(auto, "save_test_log",
                                  side_effect=lambda run: run):
            auto.run_contract_modules(
                Path("."), ["tests.test_a", "tests.test_b", "tests.test_c"],
                end=60, parallel=1)

        # Sans partage, le premier module prenait les 60 s et les deux autres
        # n'étaient jamais lancés : chacun reçoit ici sa part des 60 s.
        self.assertEqual([module for module, _, _ in seen],
                         ["tests.test_a", "tests.test_b", "tests.test_c"])
        self.assertEqual([timeout for _, timeout, _ in seen], [20, 20, 20])
        self.assertEqual([wall for _, _, wall in seen], [60, 40, 20])

    def test_tail_share_accounts_for_the_parallel_lanes(self):
        """#8207 : deux lanes laissent la place à une part plus longue.

        Dans h-01, h-02 et h-03 (24/11), la part par module valait
        `restant / (modules + 1)` alors que deux modules tournaient : à 240 s
        restantes, 15 modules recevaient 16 s et les contrats de 17 à 59 s
        étaient coupés avec 100 à 150 s de borne encore libres.
        """
        # 15 modules, deux lanes = 8 vagues : 240 // 8 = 30 s, mais un module
        # mesuré/absent de la table des durées peut prendre jusqu'à 90 s.
        self.assertEqual(auto.contract_tail_share(240, 14, 1200, 2), 90)
        # La réserve de 60 s pour la suite laisse 89 s au module en cours.
        self.assertEqual(auto.contract_tail_share(149, 3, 1200, 2), 89)
        # Jamais plus de la moitié du reste : l'autre lane doit pouvoir partir.
        self.assertEqual(auto.contract_tail_share(60, 5, 1200, 2), 30)
        # Une seule lane : la répartition égale d'origine est conservée.
        self.assertEqual(auto.contract_tail_share(60, 2, 1200, 1), 20)

    def test_parallel_tail_does_not_cut_a_module_that_fits_the_lanes(self):
        seen: list[int] = []

        def execute(*args, **kwargs):
            seen.append(kwargs["timeout"])
            return auto.TestRun("contrats", [args[2][0]], 0, "OK", 0.0)

        with mock.patch.object(auto, "contract_order",
                               side_effect=lambda _, modules: modules), \
                mock.patch.object(auto, "run_tests", side_effect=execute), \
                mock.patch.object(auto, "save_test_log",
                                  side_effect=lambda run: run):
            auto.run_contract_modules(
                Path("."),
                ["tests.test_a", "tests.test_b", "tests.test_c", "tests.test_d"],
                end=time.monotonic() + 60, parallel=2)

        self.assertEqual(len(seen), 4)
        # Avant #8207 : 60 // (4, 3, 2, 1) = 15, 15, 20, 60 s. La part par
        # vague donne ~30 s aux deux premiers modules, qui tournent ensemble,
        # et la fin du reste aux suivants (20 s de tolérance d'horloge).
        self.assertGreaterEqual(min(seen), 20)


class StartedAndCutTest(unittest.TestCase):
    """Une série lancée puis coupée n'est pas une série non lancée."""

    def test_wall_cut_carries_the_global_duration_and_is_not_not_started(self):
        with mock.patch.object(auto, "project_python",
                               return_value=sys.executable), \
                mock.patch.object(auto, "python_env", return_value={}), \
                mock.patch.object(auto, "run_walled", return_value=None), \
                mock.patch.object(auto, "save_test_log",
                                  side_effect=lambda run: run):
            run = auto.run_tests(Path("."), "contrats", ["tests.test_a"],
                                 timeout=30, wall=7)

        self.assertIsNone(run.returncode)
        self.assertTrue(run.timed_out)
        self.assertEqual(run.deadline_cut, 7)
        self.assertEqual(run.not_started, "")
        self.assertTrue(run.inconclusive)
        self.assertEqual(
            run.motive(),
            "coupé par le délai global de la série après 7 s "
            "(série lancée, code non rendu)")

    def test_an_unexpected_code_stays_a_code(self):
        run = auto.TestRun("contrats", ["tests.test_a"], 5, "boom", 0.1)
        self.assertTrue(run.inconclusive)
        self.assertEqual(run.motive(), "code de retour 5")
        self.assertEqual(proof.reason(run), "code de retour 5")
        self.assertIsNone(run.deadline_cut)


if __name__ == "__main__":
    unittest.main()
