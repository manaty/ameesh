# SPDX-License-Identifier: AGPL-3.0-only
"""#987 : la garde qui relance un job hébergé annulé faute de runner.

Sans base : la décision pure de `scripts/ci-relance-hosted.py` sur des jobs
synthétiques au format de l'API GitHub (relevé de première source : job
115427733668 du run 38428931108 — `cancelled`, `runner_name` vide, aucune
étape), puis le contrat des deux fichiers de workflow.

La garde ne doit jamais noyer un échec réel, ni boucler sous une capacité qui
manque durablement : ces deux refus sont testés ici.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ci_relance_hosted", REPO / "scripts" / "ci-relance-hosted.py")
ci_relance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_relance)

TESTS = REPO / ".github" / "workflows" / "tests.yml"
GARDE = REPO / ".github" / "workflows" / "relance-hosted.yml"


def job(nom, conclusion, runner="", etapes=(), labels=()):
    """Un job au format de l'API (champs qui décident, seulement)."""
    return {"name": nom, "conclusion": conclusion, "runner_name": runner,
            "steps": [{"name": e} for e in etapes], "labels": list(labels)}


#: le job annulé faute de runner : jamais pris, aucune étape (signature #987)
MACOS_ANNULE = job("plateforme macOS", "cancelled", labels=["macos-26-intel"])
#: l'agrégat requis, rouge parce qu'il exige platform-macos == success
AGREGAT = job("tests", "failure", runner="ameesh-ci-1-1",
              etapes=["bilan des parts"],
              labels=["self-hosted", "linux", "ameesh-ci"])
PLAN = job("plan", "success", runner="ameesh-ci-2-1", etapes=["plan du run"],
           labels=["self-hosted", "linux", "ameesh-ci"])


class DecisionTest(unittest.TestCase):

    def test_job_annule_faute_de_runner_relance(self):
        relancer, raison = ci_relance.decision([PLAN, MACOS_ANNULE, AGREGAT], 1)
        self.assertTrue(relancer, raison)
        self.assertIn("plateforme macOS", raison)

    def test_echec_reel_a_cote_ne_relance_pas(self):
        psql = job("psql 1/1", "failure", runner="ameesh-ci-3-1",
                   etapes=["part 1"], labels=["self-hosted", "linux", "ameesh-ci"])
        relancer, raison = ci_relance.decision(
            [PLAN, MACOS_ANNULE, psql, AGREGAT], 1)
        self.assertFalse(relancer, raison)
        self.assertIn("psql 1/1", raison)

    def test_deuxieme_annulation_ne_relance_pas(self):
        # tentative 2 : une annulation qui se répète est un incident de
        # capacité, à voir, pas une boucle de relances
        relancer, raison = ci_relance.decision([PLAN, MACOS_ANNULE, AGREGAT], 2)
        self.assertFalse(relancer, raison)
        self.assertIn("tentative 2", raison)

    def test_job_auto_heberge_annule_n_est_pas_la_signature(self):
        # un job de la flotte ameesh annulé en file est notre saturation
        # (#824), pas la capacité hébergée de GitHub : la garde ne le relance pas
        psql = job("psql 1/1", "cancelled", labels=["self-hosted", "linux", "ameesh-ci"])
        relancer, raison = ci_relance.decision([PLAN, psql, AGREGAT], 1)
        self.assertFalse(relancer, raison)
        self.assertIn("aucun job hébergé", raison)

    def test_job_annule_avec_etapes_n_est_pas_la_signature(self):
        # un job pris par un runner puis annulé (concurrency) a des étapes :
        # ce n'est pas l'indisponibilité de runner
        annule = job("plateforme macOS", "cancelled", runner="macos-26-intel",
                     etapes=["Set up job"], labels=["macos-26-intel"])
        relancer, raison = ci_relance.decision([PLAN, annule, AGREGAT], 1)
        self.assertFalse(relancer, raison)

    def test_agregat_et_bilan_non_requis_ne_bloquent_pas(self):
        bilan = job("bilan des impactés", "failure", runner="ameesh-ci-1-2",
                    etapes=["leaks"], labels=["self-hosted", "linux", "ameesh-ci"])
        relancer, raison = ci_relance.decision(
            [PLAN, MACOS_ANNULE, AGREGAT, bilan], 1)
        self.assertTrue(relancer, raison)

    def test_aucun_job_annule_ne_relance_pas(self):
        relancer, raison = ci_relance.decision([PLAN, AGREGAT], 1)
        self.assertFalse(relancer, raison)
        self.assertIn("aucun job hébergé", raison)

    def test_jamais_pris(self):
        self.assertTrue(ci_relance.jamais_pris(MACOS_ANNULE))
        self.assertFalse(ci_relance.jamais_pris(
            job("x", "cancelled", runner="macos-26-intel")))
        self.assertFalse(ci_relance.jamais_pris(
            job("x", "cancelled", etapes=["Set up job"])))
        self.assertFalse(ci_relance.jamais_pris(
            job("x", "failure", labels=["macos-26-intel"])))

    def test_check_run_de_flux_n_est_pas_un_job(self):
        # les check runs « tests (flux) » des parts (L161c) apparaissent dans
        # la liste des jobs avec des étiquettes vides (relevé run 38377916850) :
        # sans étiquettes, ce n'est pas un job de workflow et ça ne décide rien
        flux = job("tests (flux) psql 1/1", "cancelled")
        self.assertFalse(ci_relance.job_reel(flux))
        self.assertFalse(ci_relance.jamais_pris(flux))

    def test_check_run_de_flux_annule_ne_bloque_pas_la_relance(self):
        # un check run de flux annulé à côté du job macOS annulé ne doit pas
        # être pris pour un échec réel : la relance reste possible
        flux = job("tests (flux) psycopg 2/4", "cancelled")
        relancer, raison = ci_relance.decision(
            [PLAN, MACOS_ANNULE, flux, AGREGAT], 1)
        self.assertTrue(relancer, raison)


class CliTest(unittest.TestCase):
    """Le mode hors ligne (--jobs) : ce que le workflow appelle à travers
    l'API, la décision se rejoue sans réseau."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = pathlib.Path(self.tmp.name) / "jobs.json"
        self.jobs.write_text(json.dumps(
            {"total_count": 3, "jobs": [PLAN, MACOS_ANNULE, AGREGAT]}),
            encoding="utf-8")
        self.resume = pathlib.Path(self.tmp.name) / "resume.md"

    def run_cli(self, argv):
        sortie = io.StringIO()
        env = {"GITHUB_STEP_SUMMARY": str(self.resume)}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(sortie):
            code = ci_relance.main(argv)
        return code, sortie.getvalue()

    def test_decision_hors_ligne_et_resume(self):
        code, sortie = self.run_cli(["--jobs", str(self.jobs), "--tentative", "1"])
        self.assertEqual(code, 0)
        self.assertIn("relancer", sortie)
        self.assertIn("relancer", self.resume.read_text(encoding="utf-8"))

    def test_conclusion_non_failure_ne_relance_pas(self):
        code, sortie = self.run_cli(
            ["--jobs", str(self.jobs), "--conclusion", "cancelled"])
        self.assertEqual(code, 0)
        self.assertIn("rien à relancer", sortie)

    def test_apply_exige_run_id(self):
        code, _ = self.run_cli(["--jobs", str(self.jobs), "--apply"])
        self.assertEqual(code, 2)

    def test_sans_source_la_garde_echoue(self):
        code, _ = self.run_cli(["--tentative", "1"])
        self.assertEqual(code, 2)


class WorkflowTest(unittest.TestCase):

    def test_macos_sur_image_x64_standard(self):
        # #987 : `macos-latest` (macOS 26 arm64) est la flotte qui a annulé le
        # job faute de runner ; l'image x64 standard garde le même OS et sort
        # de la contrainte de capacité arm64
        texte = TESTS.read_text(encoding="utf-8")
        self.assertIn("runs-on: macos-26-intel", texte)
        self.assertNotIn("runs-on: macos-latest", texte)

    def test_le_check_requis_exige_toujours_macos(self):
        # la garde ne remplace pas la porte : l'agrégat requis exige encore le
        # succès du job macOS (une annulation reste rouge tant qu'elle dure)
        texte = TESTS.read_text(encoding="utf-8")
        self.assertIn('test "${{ needs.platform-macos.result }}" = success', texte)

    def test_garde_declenchee_apres_la_conclusion_de_tests(self):
        # l'API refuse de rejouer un job tant que l'exécution tourne : la
        # reprise ne peut venir que d'un workflow_run « completed »
        texte = GARDE.read_text(encoding="utf-8")
        self.assertIn("workflow_run:", texte)
        self.assertIn('workflows: ["tests"]', texte)
        self.assertIn("types: [completed]", texte)
        self.assertIn("github.event.workflow_run.id", texte)
        self.assertIn("github.event.workflow_run.run_attempt", texte)
        self.assertIn("conclusion == 'failure'", texte)

    def test_garde_privilegiee_et_hors_flotte(self):
        # jeton minimal (actions: write), code de la branche par défaut, et un
        # runner hébergé : ni la flotte saturée (#824) ni macOS
        texte = GARDE.read_text(encoding="utf-8")
        self.assertIn("actions: write", texte)
        self.assertIn("contents: read", texte)
        self.assertIn("runs-on: ubuntu-latest", texte)
        self.assertIn("actions/checkout@v4", texte)
        self.assertIn("--apply", texte)
        self.assertIn("timeout-minutes: 5", texte)


if __name__ == "__main__":
    unittest.main()
