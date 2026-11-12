# SPDX-License-Identifier: AGPL-3.0-only
"""#6981 — la relecture #3782 tolère les seuls atterrissages de coupure.

Constat h-20261111-11 (10:15Z) : le commit d'atterrissage 1.49.0 (10:03Z, §12 :
version + `docs/versions/*.md`, `[skip ci]`) suffit à lever `TargetMoved`, et
la composition repart (~12 min, deux fois par jour). Attendu :

1. une cible avancée par les seuls commits d'atterrissage garde la
   composition, aux relectures `ensure_reference` comme à celle du gel ; tout
   autre commit lève encore `TargetMoved` ;
2. seul un déplacement (`behind`, `diverged`) relance aussitôt : un refus
   `unknown` sort en 1 avec sa cause, sans épuiser les relances immédiates ;
3. un chargement en échec de `paquet-candidats.py` ne laisse pas de module à
   moitié chargé dans `sys.modules` ;
4. (#6867, point 1) des contrats rouges pendant lesquels la cible a bougé
   annoncent le rouge (rc 5), pas « cible avancée ».
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto
from tests.test_6891_reference_avant_tests import A, B, FakeGuard, composition


def red_contracts():
    run = paquet_auto.TestRun(
        "contrats", ["tests.test_x"], 1,
        "FAIL: test_x (tests.test_x.T.test_x)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        "  File \"tests/test_x.py\", line 3, in test_x\n"
        "AssertionError: 1 != 2\n\n"
        "Ran 1 test in 0.1s\n\nFAILED (failures=1)\n", 0.1)
    assert not run.green and not run.inconclusive
    return run


class AtterrissageToleréTest(unittest.TestCase):
    def test_atterrissage_seul_garde_la_composition(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, B])
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=True) as landing, \
                mock.patch.object(paquet_auto, "candidates_guard",
                                  side_effect=AssertionError("garde lue")):
            self.assertEqual(flux.run(), 0)
        landing.assert_any_call(mock.ANY, A, B)
        self.assertEqual(len(flux.calls("contracts")), 1)
        self.assertEqual(len(flux.calls("push")), 1)
        # La relecture du gel (seconde lecture) tolère le même décalage.
        self.assertEqual(flux.candidate_calls, 2)
        self.assertTrue(flux.candidate_kwargs[1].get("tolerate_landing"))

    def test_commit_de_code_leve_encore_target_moved(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, B])
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=False), \
                mock.patch.object(paquet_auto, "candidates_guard",
                                  return_value=FakeGuard()):
            with self.assertRaises(paquet_auto.TargetMoved):
                flux.run()
        self.assertEqual(flux.calls("contracts"), [])
        self.assertEqual(flux.calls("push"), [])


class RelectureDuGelTest(unittest.TestCase):
    """`compute_candidates(tolerate_landing=True)` : option et relecture."""

    def test_option_et_relecture_stricte(self):
        payload = {"schema": "ameesh-packet-candidates/1", "base_source": "option",
                   "candidates": [], "ready": [], "waiting": [], "errors": [],
                   "code_relation": "behind", "code_sha": A, "code_ref": B,
                   "code_ref_source": "project-target", "code_dirty": False}
        commands = []

        def run(cmd, **_kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps(payload).encode(), b"")

        tmp = Path(tempfile.mkdtemp(prefix="paquet-6981-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "scripts").mkdir()
        (tmp / "scripts" / "paquet-candidats.py").write_text("")
        with mock.patch.object(paquet_auto, "run_command", side_effect=run), \
                mock.patch.object(paquet_auto, "project_python",
                                  return_value=sys.executable), \
                mock.patch.object(paquet_auto, "cutoff_landing_only",
                                  return_value=True):
            paquet_auto.compute_candidates(tmp, "main", A, tmp / "p.txt",
                                           tolerate_landing=True)
        self.assertIn("--allow-code-mismatch", commands[-1])
        with mock.patch.object(paquet_auto, "run_command", side_effect=run), \
                mock.patch.object(paquet_auto, "project_python",
                                  return_value=sys.executable), \
                mock.patch.object(paquet_auto, "cutoff_landing_only",
                                  return_value=False):
            with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
                paquet_auto.compute_candidates(tmp, "main", A, tmp / "p.txt",
                                               tolerate_landing=True)
        self.assertIn("rc 2", str(caught.exception))
        # Sans tolérance, l'option n'est jamais passée.
        with mock.patch.object(paquet_auto, "run_command", side_effect=run), \
                mock.patch.object(paquet_auto, "project_python",
                                  return_value=sys.executable):
            paquet_auto.compute_candidates(tmp, "main", A, tmp / "p.txt")
        self.assertNotIn("--allow-code-mismatch", commands[-1])


class LandingMismatchTest(unittest.TestCase):
    def payload(self, **extra):
        out = {"code_relation": "behind", "code_sha": A, "code_ref": B,
               "code_ref_source": "project-target", "code_dirty": False}
        out.update(extra)
        return out

    def check(self, payload, landing=True):
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=landing):
            return paquet_auto.landing_mismatch(Path("."), A, payload)

    def test_atterrissage_seul_passe(self):
        self.assertEqual(self.check(self.payload()), "")

    def test_meme_reference_passe(self):
        self.assertEqual(self.check(self.payload(code_relation="same", code_ref=A),
                                    landing=False), "")

    def test_refus(self):
        cases = [
            self.payload(),                                  # commit de code
            self.payload(code_dirty=True),                   # arbre modifié
            self.payload(code_sha="c" * 40),                 # autre code que la base
            self.payload(code_relation="diverged"),
            self.payload(code_relation="unknown"),
        ]
        for index, payload in enumerate(cases):
            with self.subTest(payload=payload):
                self.assertTrue(self.check(payload, landing=index != 0))


class RefusInconnuTest(unittest.TestCase):
    def test_refus_unknown_sort_sans_relance(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, B])
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=False), \
                mock.patch.object(paquet_auto, "candidates_guard",
                                  return_value=FakeGuard(relation="unknown")):
            with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
                flux.run()
        self.assertNotIsInstance(caught.exception, paquet_auto.TargetMoved)
        self.assertIn("unknown", str(caught.exception))
        self.assertIn("pas de relance immédiate", str(caught.exception))
        self.assertEqual(flux.calls("contracts"), [])
        error = paquet_auto.PaquetAutoError(str(caught.exception))
        with mock.patch.object(paquet_auto, "compose_packet", side_effect=error):
            self.assertEqual(paquet_auto.main(["--name", "banc", "--carrier", "1"]), 1)

    def test_diverged_reste_un_deplacement(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, B])
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=False), \
                mock.patch.object(paquet_auto, "candidates_guard",
                                  return_value=FakeGuard(relation="diverged")):
            with self.assertRaises(paquet_auto.TargetMoved):
                flux.run()


class ContratsRougesTest(unittest.TestCase):
    def test_contrats_rouges_et_cible_avancee_annoncent_le_rouge(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, A, B], contract_result=red_contracts())
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=False), \
                mock.patch.object(paquet_auto, "candidates_guard",
                                  return_value=FakeGuard()), \
                mock.patch.object(paquet_auto, "record_contract_result"):
            with self.assertRaises(paquet_auto.CompositionRed) as caught:
                flux.run()
        self.assertIn("contrats rouges", str(caught.exception))
        self.assertEqual(flux.calls("push"), [])

    def test_contrats_verts_et_cible_avancee_relancent(self):
        flux = Flux(self, tests=[([green()], [], [])], compositions=[composition()],
                    fresh_bases=[A, A, B])
        with mock.patch.object(paquet_auto, "cutoff_landing_only",
                               return_value=False), \
                mock.patch.object(paquet_auto, "candidates_guard",
                                  return_value=FakeGuard()), \
                mock.patch.object(paquet_auto, "record_contract_result"):
            with self.assertRaises(paquet_auto.TargetMoved):
                flux.run()


class CandidatesGuardTest(unittest.TestCase):
    def test_echec_de_chargement_ne_laisse_pas_de_module(self):
        tmp = Path(tempfile.mkdtemp(prefix="paquet-6981-guard-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        broken = tmp / "paquet-candidats.py"
        broken.write_text("raise RuntimeError('chargement cassé')\n")
        fake_file = tmp / "paquet-auto.py"
        self.addCleanup(sys.modules.pop, "paquet_candidats_guard", None)
        sys.modules.pop("paquet_candidats_guard", None)
        with mock.patch.object(paquet_auto, "_CANDIDATES_GUARD", None), \
                mock.patch.object(paquet_auto, "__file__", str(fake_file)):
            with self.assertRaises(RuntimeError):
                paquet_auto.candidates_guard()
            self.assertNotIn("paquet_candidats_guard", sys.modules)
            self.assertIsNone(paquet_auto._CANDIDATES_GUARD)


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


class CutoffLandingOnlyGitTest(unittest.TestCase):
    """La preuve réelle sur un dépôt git : atterrissage seul, puis code."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="paquet-6981-git-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.env = env
        _git(self.repo, "init", "-q", env=env)
        (self.repo / "pyproject.toml").write_text('[project]\nversion = "1.49.0"\n')
        (self.repo / "src.py").write_text("x = 1\n")
        _git(self.repo, "add", ".", env=env)
        _git(self.repo, "commit", "-qm", "base", env=env)
        self.base = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "pyproject.toml").write_text('[project]\nversion = "1.50.0"\n')
        (self.repo / "docs" / "versions").mkdir(parents=True)
        (self.repo / "docs" / "versions" / "1.50.0.md").write_text("# 1.50.0\n")
        _git(self.repo, "add", ".", env=env)
        _git(self.repo, "commit", "-qm",
             "release: 1.50.0 [skip ci]\n\nameesh-release: 1.50.0", env=env)
        self.landing = _git(self.repo, "rev-parse", "HEAD")

    def test_atterrissage_seul(self):
        self.assertTrue(paquet_auto.cutoff_landing_only(self.repo, self.base,
                                                        self.landing))

    def test_commit_de_code_apres_atterrissage(self):
        (self.repo / "src.py").write_text("x = 2\n")
        _git(self.repo, "commit", "-qam", "code", env=self.env)
        code = _git(self.repo, "rev-parse", "HEAD")
        self.assertFalse(paquet_auto.cutoff_landing_only(self.repo, self.base, code))


if __name__ == "__main__":
    unittest.main()
