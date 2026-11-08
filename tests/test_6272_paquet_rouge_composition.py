# SPDX-License-Identifier: AGPL-3.0-only
"""#6272 — un rouge de composition n'ouvre pas de lot d'empreinte §16.

Un paquet rouge **par son contenu** (contrat, test d'une unité, contrôle de
gel, types clients) n'est pas une panne du composeur : `paquet-auto.py` sort
en `COMPOSITION_RED_RC`, `paquet-horaire.py` prévient le pilote et sort du
même code sans ouvrir d'empreinte `work fingerprint`, et l'unité systemd
accepte ce code (`SuccessExitStatus=3 5`). Une panne réelle du composeur
reste rc 1 et garde son empreinte.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


auto = _load("test_paquet_auto_6272", ROOT / "scripts" / "paquet-auto.py")
hourly = _load("test_paquet_horaire_6272", ROOT / "scripts" / "paquet-horaire.py")


class ExitCodeTest(unittest.TestCase):
    def test_code_distinct_et_miroir(self):
        self.assertEqual(auto.COMPOSITION_RED_RC, 5)
        self.assertEqual(hourly.COMPOSITION_RED_RC, auto.COMPOSITION_RED_RC)
        for other in (0, 1, 2, auto.IN_PROGRESS_RC, auto.PAUSED_RC,
                      auto.PUBLISH_REFUSED, hourly.STALE_RC):
            self.assertNotEqual(auto.COMPOSITION_RED_RC, other)

    def test_hierarchie(self):
        self.assertTrue(issubclass(auto.CompositionRed, auto.PaquetAutoError))
        self.assertTrue(issubclass(auto.AttributionUnreadable,
                                   auto.CompositionRed))
        self.assertTrue(issubclass(hourly.CompositionRed, hourly.HoraireError))

    def test_paquet_auto_rend_5_pour_un_rouge_de_composition(self):
        with mock.patch.object(auto, "compose_packet",
                               side_effect=auto.CompositionRed("contrat rouge")):
            code = auto.main(["--name", "banc", "--carrier", "1"])
        self.assertEqual(code, auto.COMPOSITION_RED_RC)

    def test_paquet_auto_rend_1_pour_une_panne_du_composeur(self):
        with mock.patch.object(auto, "compose_packet",
                               side_effect=auto.PaquetAutoError("poussée refusée")):
            code = auto.main(["--name", "banc", "--carrier", "1"])
        self.assertEqual(code, 1)


class ContractRedTest(unittest.TestCase):
    """Un contrat rouge non attribuable est un rouge du contenu, pas une panne."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-6272-")
        self.addCleanup(self.tmp.cleanup)
        self.worktree = pathlib.Path(self.tmp.name)
        self.composition = auto.Composition(
            units=[auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)

    def check(self):
        green = auto.TestRun(
            "unité #1", ["tests.test_unit"], 0,
            "Ran 1 test in 0.1s\n\nOK", 0.1)
        red = auto.TestRun(
            "contrats", ["tests.test_cluster"], 1,
            "FAIL: test_x (tests.test_cluster.T.test_x)\n"
            "AssertionError: x\n", 0.1)

        def attribute(_worktree, _composition, _failures):
            return {"status": "ok", "tests": [{
                "test": "tests.test_cluster.T.test_x",
                "status": "unattributable", "units": []}]}

        with mock.patch.object(auto, "unit_test_modules",
                               return_value=["tests.test_unit"]), \
                mock.patch.object(auto, "contract_modules",
                                  return_value=["tests.test_cluster"]), \
                mock.patch.object(auto, "run_tests", side_effect=[green, red]):
            return auto.check_local_tests(
                self.worktree, self.composition, timeout=1,
                log=lambda _line: None, attribute=attribute)

    def test_contrat_rouge_leve_composition_red(self):
        with self.assertRaises(auto.CompositionRed) as caught:
            self.check()
        self.assertIn("contrat rouges", str(caught.exception))

    def test_attribution_illisible_est_une_composition_rouge(self):
        error = auto.AttributionUnreadable("préfixe illisible",
                                           tests=["tests.test_x"])
        self.assertIsInstance(error, auto.CompositionRed)
        self.assertEqual(error.tests, ["tests.test_x"])


class FreezeChecksRedTest(unittest.TestCase):
    """Un contrôle de gel rouge est un rouge du contenu (#5872)."""

    def compose(self, *, freeze, clients=(True, "types clients : à jour")):
        tmp = tempfile.TemporaryDirectory(prefix="paquet-6272-gel-")
        self.addCleanup(tmp.cleanup)
        worktree = pathlib.Path(tmp.name) / "wt"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text("")
        composition = auto.Composition(
            units=[auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)
        args = argparse.Namespace(
            repo=str(ROOT), name="banc-6272", target="main", carrier=99,
            base=None, local=True, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="mesh-design", sender="mesh-design", contracts_budget=10)
        with mock.patch.object(auto, "validate_target"), \
                mock.patch.object(auto, "validate_branch"), \
                mock.patch.object(auto, "PackageLock") as lock, \
                mock.patch.object(auto, "fresh_base", return_value="a" * 40), \
                mock.patch.object(auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(auto, "compute_candidates",
                                  return_value=(
                                      [auto.Candidate(1, "a" * 40)],
                                      {"ready": [1], "waiting": []})), \
                mock.patch.object(auto, "compose_candidates",
                                  return_value=composition), \
                mock.patch.object(auto, "run_freeze_checks",
                                  return_value=freeze), \
                mock.patch.object(auto, "check_client_types",
                                  return_value=clients), \
                mock.patch.object(auto, "remove_worktree"):
            lock.return_value.acquire.return_value = True
            lock.return_value.describe.return_value = "banc"
            return auto.compose_packet(args)

    def test_freeze_checks_rouge_leve_composition_red(self):
        with self.assertRaises(auto.CompositionRed) as caught:
            self.compose(freeze=(1, "contrat rouge", 0.1))
        self.assertIn("freeze-checks rouge", str(caught.exception))

    def test_freeze_checks_hors_arbre_reste_une_panne(self):
        # rc 2 = hors d'un arbre ameesh : panne du composeur, pas un rouge.
        with self.assertRaises(auto.PaquetAutoError) as caught:
            self.compose(freeze=(2, "hors d'un arbre ameesh", 0.1))
        self.assertNotIsInstance(caught.exception, auto.CompositionRed)

    def test_types_clients_perimes_levent_composition_red(self):
        with self.assertRaises(auto.CompositionRed) as caught:
            self.compose(freeze=(0, "ok", 0.1),
                         clients=(False, "types clients périmés"))
        self.assertIn("types clients rouges", str(caught.exception))


class FakeShell:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.mails: list[tuple[str, str]] = []

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        self.calls.append(list(cmd))
        if cmd[0] == "agent-mail":
            self.mails.append((cmd[2], input_text))
        return hourly.Result(0)


class TickRedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.shell = FakeShell()
        self.ctx = hourly.Context(
            repo=ROOT, target="main", project="ameesh", pilot="mesh-design",
            actor="mesh-design", shell=self.shell, python="python3")

    def test_after_compose_rc_5_rapporte_sans_empreinte(self):
        # #6272 : la marque d'échec reste (le tic suivant reprend), le pilote
        # reçoit le rouge, mais `work fingerprint` n'est pas appelé.
        with self.assertRaisesRegex(hourly.CompositionRed, "paquet-auto rc 5"):
            hourly.after_compose(
                self.ctx, 4000, "h-20261108-04",
                hourly.Result(5, "paquet-auto : tests de contrat rouges\n", ""),
                hourly.Attempt(hourly.COMPOSITION, 1, ""))
        notes = [call[call.index("note") + 2] for call in self.shell.calls
                 if call[:3] == ["ameesh", "work", "note"]]
        self.assertTrue(
            any(note.startswith(hourly.MARK_FAILED) and note.endswith("rc=5")
                for note in notes), notes)
        self.assertEqual([to for to, _ in self.shell.mails], ["mesh-design"])
        self.assertFalse([call for call in self.shell.calls
                          if call[:3] == ["ameesh", "work", "fingerprint"]])
        self.assertIn("aucun lot d'empreinte", self.shell.mails[0][1])

    def test_main_sort_en_5_sans_alerte(self):
        clone = tempfile.TemporaryDirectory(prefix="paquet-6272-main-")
        self.addCleanup(clone.cleanup)
        (pathlib.Path(clone.name) / ".git").mkdir()
        zone = mock.Mock(timezone="Europe/Paris")
        with mock.patch.object(hourly, "pose_git_identity"), \
                mock.patch("ameesh.config.load", return_value=None), \
                mock.patch("ameesh.mechanics.configured", return_value=zone), \
                mock.patch.object(hourly, "run",
                                  side_effect=hourly.CompositionRed(
                                      "paquet-auto rc 5 pour h-x")), \
                mock.patch.object(hourly, "alert_tick") as alert, \
                mock.patch.object(hourly, "Shell", return_value=self.shell):
            rc = hourly.main(["tick", "--repo", clone.name])
        self.assertEqual(rc, hourly.COMPOSITION_RED_RC)
        self.assertEqual(alert.call_count, 0)

    def test_main_panne_reste_1_avec_alerte(self):
        clone = tempfile.TemporaryDirectory(prefix="paquet-6272-main-")
        self.addCleanup(clone.cleanup)
        (pathlib.Path(clone.name) / ".git").mkdir()
        zone = mock.Mock(timezone="Europe/Paris")
        with mock.patch.object(hourly, "pose_git_identity"), \
                mock.patch("ameesh.config.load", return_value=None), \
                mock.patch("ameesh.mechanics.configured", return_value=zone), \
                mock.patch.object(hourly, "run",
                                  side_effect=hourly.HoraireError(
                                      "paquet-auto rc 1 pour h-x")), \
                mock.patch.object(hourly, "alert_tick") as alert, \
                mock.patch.object(hourly, "Shell", return_value=self.shell):
            rc = hourly.main(["tick", "--repo", clone.name])
        self.assertEqual(rc, 1)
        self.assertEqual(alert.call_count, 1)


if __name__ == "__main__":
    unittest.main()
