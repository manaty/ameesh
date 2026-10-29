# SPDX-License-Identifier: AGPL-3.0-only
"""#4221 — défauts non bloquants du chantier #4217.

Deux N de la revue #4217 :
* un test rouge sur la tête mais **vert sur tous les préfixes** de
  l'empilement est instable : il est consigné non concluant, le pilote est
  prévenu et le paquet continue (canon §3), au lieu d'arrêter le paquet ;
* un `QueryCanceled`/`lock timeout` du **corps** d'un test qui se reproduit
  au rejeu seul, hors charge, est un rouge réel ; une erreur de fixture ou un
  QueryCanceled de charge restent non concluants.

Aucune base, aucune forge, aucun réseau.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"
HORAIRE = ROOT / "scripts" / "paquet-horaire.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_4221")
ph = _load_script(HORAIRE, "test_paquet_horaire_4221")

TARGET = "tests.test_target.T.test_value"

BODY_CANCEL = (
    "======================================================================\n"
    "ERROR: test_value (tests.test_target.T.test_value)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 1 test in 0.1s\n"
    "FAILED (errors=1)\n")
BODY_LOCK_TIMEOUT = (
    "======================================================================\n"
    "ERROR: test_value (tests.test_target.T.test_value)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "psycopg.errors.LockNotAvailable: canceling statement due to lock timeout\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 1 test in 0.1s\n"
    "FAILED (errors=1)\n")
FIXTURE_CANCEL = (
    "======================================================================\n"
    "ERROR: tearDownClass (tests.test_l56_role_executor.RoleExecutorTest)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 33 tests in 42.0s\n"
    "FAILED (errors=1)\n")


def _run(label: str, modules, returncode: int, output: str) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), returncode, output, 0.1)


class BodyCancelTest(unittest.TestCase):
    """`TestRun.body_cancel_tests` : corps de test contre fixture."""

    def test_querycanceled_du_corps_est_lu(self) -> None:
        run = _run("unité #1", ["tests.test_target"], 1, BODY_CANCEL)
        self.assertTrue(run.inconclusive)
        self.assertEqual(run.body_cancel_tests(), {TARGET})

    def test_lock_timeout_du_corps_est_lu(self) -> None:
        run = _run("unité #1", ["tests.test_target"], 1, BODY_LOCK_TIMEOUT)
        self.assertEqual(run.body_cancel_tests(), {TARGET})

    def test_fixture_n_est_pas_un_corps(self) -> None:
        run = _run("unité #1", ["tests.test_l56_role_executor"], 1,
                   FIXTURE_CANCEL)
        self.assertTrue(run.inconclusive)
        self.assertEqual(run.body_cancel_tests(), set())

    def test_setup_en_cancel_reste_une_fixture(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_value (tests.test_target.T.test_value)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"/usr/lib/python3.12/unittest/case.py\", line 574, in _callSetUp\n"
            "    self.setUp()\n"
            "  File \"/tmp/tests/test_target.py\", line 5, in setUp\n"
            "    self.prepare()\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = _run("unité #1", ["tests.test_target"], 1, output)
        self.assertTrue(run.inconclusive)
        self.assertEqual(run.body_cancel_tests(), set())

    def test_assertion_qui_cite_le_timeout_n_est_pas_un_cancel(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_value (tests.test_target.T.test_value)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "AssertionError: expected canceling statement due to statement timeout\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = _run("unité #1", ["tests.test_target"], 1, output)
        self.assertFalse(run.inconclusive)
        self.assertEqual(run.body_cancel_tests(), set())

    def test_valueerror_chainee_ne_devient_pas_un_cancel(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "ValueError: invalid application state\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = _run("unité #1", ["tests.test_x"], 1, output)
        self.assertFalse(run.inconclusive)
        self.assertEqual(run.body_cancel_tests(), set())


class FakeShell:
    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        self.calls.append(list(cmd))
        return ph.Result(0)


class ReplayLocallyTest(unittest.TestCase):
    """`paquet-horaire.replay_locally` : le rejeu seul, hors charge."""

    def replay(self, run) -> str:
        shell = FakeShell()
        ctx = ph.Context(repo=pathlib.Path("."), target="main",
                         project="ameesh", pilot="mesh-design",
                         actor="mesh-design", shell=shell)
        with mock.patch.object(ph.auto, "run_tests", return_value=run):
            return ph.replay_locally(ctx, "a" * 40, [TARGET])

    def test_body_cancel_reproduit_confirme_le_rouge(self) -> None:
        run = _run("rejeu", ["tests.test_target"], 1, BODY_CANCEL)
        self.assertEqual(self.replay(run), "")

    def test_body_lock_timeout_reproduit_confirme_le_rouge(self) -> None:
        run = _run("rejeu", ["tests.test_target"], 1, BODY_LOCK_TIMEOUT)
        self.assertEqual(self.replay(run), "")

    def test_body_cancel_non_reproduit_reste_non_concluant(self) -> None:
        run = _run("rejeu", ["tests.test_target"], 0,
                   "Ran 1 test in 0.1s\n\nOK")
        self.assertIn("vert en local", self.replay(run))

    def test_fixture_cancel_reste_non_concluante(self) -> None:
        run = _run("rejeu", ["tests.test_l56_role_executor"], 1,
                   FIXTURE_CANCEL)
        why = self.replay(run)
        self.assertIn("non concluant", why)

    def test_cancel_d_un_autre_test_ne_confirme_pas(self) -> None:
        run = _run("rejeu", ["tests.test_target"], 1, BODY_CANCEL)
        why = ph.replay_locally(
            ph.Context(repo=pathlib.Path("."), target="main",
                       project="ameesh", pilot="mesh-design",
                       actor="mesh-design", shell=FakeShell()),
            "a" * 40, ["tests.test_other.T.test_other"])
        self.assertIn("non concluant", why)


class FlakyLocalTest(unittest.TestCase):
    """#4221 : instable au rejeu des préfixes, le paquet continue."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4221-check-")
        self.addCleanup(self.tmp.cleanup)

    def check(self, *, prefix, own=None, attribute=None, own_failure=True,
              output=None):
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="b" * 40)
        own = list(own or ["tests.test_own"])
        neighbor = {"module": "tests.test_target", "reason": "carte",
                    "files": ["src/pkg/value.py"]}
        if output is None:
            output = ("FAIL: test_value (%s)\nAssertionError: 2 != 1\n"
                      % TARGET)
            if own_failure:
                output = ("FAIL: test_own (tests.test_own.T.test_own)\n"
                          "AssertionError: own\n" + output)
        neighbor_run = _run(
            "unité #1", own + ["tests.test_target"], 1, output)
        green = _run("contrats", ["tests.test_contrat"], 0,
                     "Ran 1 test in 0.1s\n\nOK")
        logs: list[str] = []
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=own), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=[neighbor_run, green]), \
                mock.patch.object(paquet_auto, "base_failures",
                                  return_value=set()):
            return paquet_auto.check_local_tests(
                pathlib.Path(self.tmp.name), composition, timeout=1,
                base_sha="a" * 40,
                log=logs.append,
                select=lambda *_a: ({1: [neighbor]}, "carte simulée"),
                attribute=attribute or (lambda *_a: {"status": "no-helper"}),
                prefix=prefix)

    def test_instable_est_consigne_sans_retrait(self) -> None:
        failure = {"test": TARGET, "module": "test_target"}
        runs, faulty, removals = self.check(
            prefix=lambda *_a, **_k: ({}, "", [failure]), own_failure=False)
        self.assertEqual((faulty, removals), ([], []))
        self.assertEqual(runs[0].flaky_tests, [TARGET])
        self.assertTrue(runs[0].inconclusive)
        self.assertFalse(runs[0].real_failure)
        self.assertIn("instable", runs[0].motive())
        self.assertIn("1 non concluant(s)",
                      paquet_auto._tests_summary(runs))

    def test_un_voisin_situe_reste_retire_a_cote_de_l_instable(self) -> None:
        # B1 cx2 : deux rouges voisins, l'un situé sur un préfixe, l'autre
        # instable ; le premier doit être retiré, le second consigné.
        real = "tests.test_target.T.test_real"
        flaky = "tests.test_target.T.test_flaky"
        output = ("FAIL: test_real (%s)\nAssertionError: real\n"
                  "FAIL: test_flaky (%s)\nAssertionError: flaky\n"
                  % (real, flaky))
        runs, faulty, removals = self.check(
            own_failure=False, output=output,
            prefix=lambda *_a, **_k: (
                {1: [{"test": real, "module": "test_target"}]}, "",
                [{"test": flaky, "module": "test_target"}]))
        self.assertEqual(faulty, [1])
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].proof, "prefixes")
        self.assertEqual(removals[0].tests, [real])
        self.assertTrue(any(run.flaky_tests == [flaky] for run in runs))

    def test_un_rouge_propre_reste_retire_a_cote_de_l_instable(self) -> None:
        failure = {"test": TARGET, "module": "test_target"}
        own_failure = {"test": "tests.test_own.T.test_own",
                       "module": "test_own"}
        runs, faulty, removals = self.check(
            prefix=lambda *_a, **_k: ({}, "", [failure]),
            attribute=lambda *_a: {"status": "ok", "tests": [
                {"test": own_failure["test"], "status": "probable",
                 "units": [{"lot": 1, "best": 9, "evidence": []}]}]})
        self.assertEqual(faulty, [1])
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertTrue(any(run.flaky_tests == [TARGET] for run in runs))
        self.assertIn("1 non concluant(s)",
                      paquet_auto._tests_summary(runs))


class NotifyFlakyTest(unittest.TestCase):
    def test_le_pilote_recoit_l_instable(self) -> None:
        with mock.patch.object(paquet_auto, "send_mail") as send:
            why = paquet_auto.notify_flaky(
                "h-20261028-42", 99, [TARGET], pilot="pilote",
                sender="mesh-design", log=lambda _line: None)
        self.assertEqual(why, "")
        recipient, lot, body, sender = send.call_args.args
        self.assertEqual((recipient, lot, sender), ("pilote", 99, "mesh-design"))
        self.assertIn("instable", body)
        self.assertIn("le paquet continue", body)
        self.assertIn(TARGET, body)


if __name__ == "__main__":
    unittest.main()
