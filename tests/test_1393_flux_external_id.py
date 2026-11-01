# SPDX-License-Identifier: AGPL-3.0-only
"""#1393 — le sweep identifie l'exécution publiante par son `external_id`.

`publish` pose à la création du check `ameesh-ci-flow/<exécution>/<tentative>`
(variables GitHub du job). Le sweep le lit avant le `details_url` et le
`check_suite` : ce repli peut nommer une *autre* exécution de `tests.yml` sur la
même tête et faire conclure un check encore vivant. Une tentative nommée est
relue sur elle-même (`…/attempts/<n>`) : le check d'une tentative passée se
conclut sur l'état de *cette* tentative, pas sur celui de la tentative courante
d'un rerun. Le balayage est borné (`--max-duration`, défaut 100 s ; l'étape CI
l'entoure de `timeout 120`), et ne conclut jamais `success` ni `skipped`.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import pathlib
import re
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ciflux = _charger("ci_flux_1393", "ci-flow.py")

TETE = "a" * 40
EXECUTION, AUTRE_EXECUTION = 500, 501
SUITE = 38436010127


def check(ident=11, name="tests (flux) psql 1/1", status="in_progress",
          external=None, execution=None, suite=SUITE, external_id=None):
    """Un check « tests (flux) ».

    `external` : le couple (exécution, tentative) posé dans l'`external_id`.
    `execution` : celle que nomme le `details_url` (None = URL par défaut de
    GitHub, qui ne nomme pas l'exécution)."""
    if execution is None:
        lien = "https://github.com/manaty/ameesh/runs/%d" % ident
    else:
        lien = "https://github.com/manaty/ameesh/actions/runs/%d" % execution
    if external_id is None and external is not None:
        external_id = ciflux.EXTERNAL_PREFIX + "%d/%d" % external
    return {"id": ident, "name": name, "status": status, "details_url": lien,
            "external_id": external_id,
            "check_suite": {"id": suite} if suite is not None else None,
            "output": {"title": "1 test(s) passé(s), en cours",
                       "summary": "Part psql 1/1 : en cours.",
                       "text": "<!-- ameesh-test-event/1 -->\n```jsonl\n```"}}


def run(status="completed"):
    return {"id": EXECUTION, "status": status}


def job(name="psql 1/1", status="completed", conclusion="cancelled"):
    return {"name": name, "status": status, "conclusion": conclusion}


def args(**champs):
    return argparse.Namespace(sha=TETE, **champs)


class FauxGitHub:
    """Le GitHub minimal du sweep : exécutions, tentatives et suite de checks."""

    def __init__(self, checks=(), executions=None, tentatives=None,
                 suite_executions=None, refus_attempt=False):
        self.checks = list(checks)
        self.executions = executions or {}
        self.tentatives = tentatives or {}
        self.suite_executions = list(suite_executions or [])
        self.refus_attempt = refus_attempt
        self.lectures = []
        self.maj = []

    def ready(self):
        return True

    def check_runs(self, tete):
        self.lectures.append(("checks", tete))
        return self.checks

    def run(self, execution):
        self.lectures.append(("run", execution))
        return self.executions[execution][0]

    def jobs(self, execution):
        self.lectures.append(("jobs", execution))
        return self.executions[execution][1]

    def run_attempt(self, execution, tentative):
        self.lectures.append(("attempt", execution, tentative))
        if self.refus_attempt:
            raise ciflux.urllib.error.URLError("boom")
        return self.tentatives[(execution, tentative)][0]

    def jobs_attempt(self, execution, tentative):
        self.lectures.append(("jobs-attempt", execution, tentative))
        if self.refus_attempt:
            raise ciflux.urllib.error.URLError("boom")
        return self.tentatives[(execution, tentative)][1]

    def runs_of_suite(self, suite):
        self.lectures.append(("suite", suite))
        return [{"id": execution} for execution in self.suite_executions]

    def update(self, ident, output, conclusion=None):
        self.maj.append((ident, output, conclusion))


class ExternalIdTest(unittest.TestCase):
    """L'identité posée à la création du check, et relue par le sweep."""

    def _github(self, **env):
        base = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": "manaty/ameesh", "GITHUB_SHA": TETE}
        base.update(env)
        return ciflux.GitHub(env=base)

    def _create(self, gh):
        appels = []

        def call(methode, chemin, corps):
            appels.append((methode, chemin, corps))
            return {"id": 7}

        gh.call = call
        self.assertEqual(gh.create("tests (flux) psql 1/1", {"title": "t"}), 7)
        return appels[0][2]

    def test_external_id_pose_a_la_creation(self):
        corps = self._create(self._github(GITHUB_RUN_ID="500", GITHUB_RUN_ATTEMPT="2"))
        self.assertEqual(corps["external_id"], "ameesh-ci-flow/500/2")

    def test_tentative_absente_vaut_un(self):
        corps = self._create(self._github(GITHUB_RUN_ID="500"))
        self.assertEqual(corps["external_id"], "ameesh-ci-flow/500/1")

    def test_started_at_utc_a_la_creation(self):
        # #5298 : sans `started_at`, la forge peut dater le check de l'heure
        # locale du runner ; la lecture `filter=latest` garde alors le check
        # de l'attempt précédent et son rouge masque le vert de la relance.
        avant = time.time() - 5.0
        corps = self._create(self._github(GITHUB_RUN_ID="500", GITHUB_RUN_ATTEMPT="2"))
        stamp = datetime.datetime.fromisoformat(
            str(corps["started_at"]).replace("Z", "+00:00"))
        self.assertIsNotNone(stamp.tzinfo)
        self.assertGreaterEqual(stamp.timestamp(), avant)
        self.assertLessEqual(stamp.timestamp(), time.time() + 5.0)

    def test_sans_numero_execution_aucun_external_id(self):
        corps = self._create(self._github())
        self.assertNotIn("external_id", corps)

    def test_lecture_de_l_external_id(self):
        self.assertEqual(ciflux._external_of(check(external=(500, 2))), (500, 2))
        self.assertEqual(ciflux._external_of(check(external=(500, 1))), (500, 1))

    def test_external_id_absent_ou_illisible_rend_none(self):
        for texte in ("", None, "autre/500/1", "ameesh-ci-flow/", "ameesh-ci-flow/500",
                      "ameesh-ci-flow/500/2/3", "ameesh-ci-flow/x/2",
                      "ameesh-ci-flow/0/2", "ameesh-ci-flow/500/0"):
            self.assertIsNone(ciflux._external_of(check(external_id=texte)), texte)


class SweepIdentiteTest(unittest.TestCase):
    """Le sweep juge l'exécution — et la tentative — nommées par le check."""

    def test_check_vivant_d_une_execution_active_non_conclu(self):
        """Le repli `check_suite` nommait une autre exécution, terminale : le
        check de l'exécution encore vivante était conclu à tort."""
        gh = FauxGitHub([check(external=(AUTRE_EXECUTION, 1))],
                        tentatives={(AUTRE_EXECUTION, 1): (
                            run("in_progress"),
                            [job(status="in_progress", conclusion=None)])},
                        executions={EXECUTION: (run("completed"), [job()])},
                        suite_executions=[EXECUTION])
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(gh.maj, [])
        self.assertIn(("attempt", AUTRE_EXECUTION, 1), gh.lectures)
        self.assertNotIn(("suite", SUITE), gh.lectures)
        self.assertNotIn(("run", EXECUTION), gh.lectures)

    def test_tentative_passee_conclue_quand_la_courante_tourne(self):
        """Le check de la tentative 1 est terminal : la tentative 2 qui tourne
        encore ne le rouvre pas."""
        gh = FauxGitHub([check(external=(EXECUTION, 1))],
                        tentatives={(EXECUTION, 1): (run("completed"), [job()])},
                        executions={EXECUTION: (
                            run("in_progress"),
                            [job(status="in_progress", conclusion=None)])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])
        self.assertNotIn(("run", EXECUTION), gh.lectures)

    def test_tentative_courante_active_non_conclue(self):
        gh = FauxGitHub([check(external=(EXECUTION, 2))],
                        tentatives={(EXECUTION, 2): (
                            run("in_progress"),
                            [job(status="in_progress", conclusion=None)]),
                            (EXECUTION, 1): (run("completed"), [job()])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_tentative_terminale_sans_job_connu_conclut(self):
        gh = FauxGitHub([check(external=(EXECUTION, 1))],
                        tentatives={(EXECUTION, 1): (run("completed"), [])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])

    def test_tentative_active_sans_job_connu_non_conclue(self):
        gh = FauxGitHub([check(external=(EXECUTION, 1))],
                        tentatives={(EXECUTION, 1): (run("in_progress"), [])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_reponse_de_tentative_illisible_non_conclue(self):
        gh = FauxGitHub([check(external=(EXECUTION, 1))], refus_attempt=True)
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(gh.maj, [])

    def test_external_id_prime_sur_le_details_url(self):
        gh = FauxGitHub([check(external=(AUTRE_EXECUTION, 1), execution=EXECUTION)],
                        tentatives={(AUTRE_EXECUTION, 1): (
                            run("in_progress"),
                            [job(status="in_progress", conclusion=None)])},
                        executions={EXECUTION: (run("completed"), [job()])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])
        self.assertIn(("attempt", AUTRE_EXECUTION, 1), gh.lectures)
        self.assertNotIn(("run", EXECUTION), gh.lectures)

    def test_external_id_illisible_retombe_sur_le_details_url(self):
        """Les checks publiés avant #1393 n'ont pas d'`external_id` exploitable :
        le repli d'origine reste appliqué."""
        ancien = check(execution=EXECUTION, external_id="forme inconnue")
        gh = FauxGitHub([ancien], executions={EXECUTION: (run("completed"), [job()])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])
        self.assertIn(("run", EXECUTION), gh.lectures)

    def test_deux_checks_meme_tentative_une_seule_lecture(self):
        gh = FauxGitHub([check(ident=11, external=(EXECUTION, 1)),
                         check(ident=12, name="tests (flux) psql 2/2",
                               external=(EXECUTION, 1))],
                        tentatives={(EXECUTION, 1): (
                            run("completed"), [job(), job(name="psql 2/2")])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[0] for m in gh.maj], [11, 12])
        self.assertEqual([l for l in gh.lectures if l[0] == "attempt"],
                         [("attempt", EXECUTION, 1)])

    def test_jamais_success_ni_skipped_meme_sur_job_vert(self):
        gh = FauxGitHub([check(external=(EXECUTION, 1))],
                        tentatives={(EXECUTION, 1): (
                            run("completed"), [job(conclusion="success")])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])
        for _, _, conclusion in gh.maj:
            self.assertNotIn(conclusion, ("success", "skipped"))


class BudgetTest(unittest.TestCase):
    """#1393 : le balayage s'arrête à son budget, sans conclure à l'aveugle."""

    def test_budget_epuise_laisse_les_checks_suivants(self):
        horloge = iter([0.0, 0.0, 200.0, 200.0])
        gh = FauxGitHub([check(ident=11, external=(EXECUTION, 1)),
                         check(ident=12, name="tests (flux) psql 2/2",
                               external=(EXECUTION, 1))],
                        tentatives={(EXECUTION, 1): (
                            run("completed"), [job(), job(name="psql 2/2")])})
        self.assertEqual(ciflux.sweep(args(max_duration=100.0), gh=gh,
                                      horloge=lambda: next(horloge, 200.0)), 0)
        self.assertEqual([m[0] for m in gh.maj], [11])

    def test_budget_par_defaut_borne(self):
        self.assertEqual(ciflux.SWEEP_MAX_DURATION, 100.0)


class WorkflowTest(unittest.TestCase):
    """L'étape CI du sweep est bornée et ne peut pas rougir le check requis."""

    def _etape(self) -> str:
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        debut = texte.index("- name: conclusion des checks de flux orphelins")
        suivant = texte.find("\n      - ", debut)
        return texte[debut:suivant if suivant != -1 else len(texte)]

    def test_etape_bornee(self):
        etape = self._etape()
        self.assertIn("timeout 120", etape)
        self.assertIn("--max-duration 100", etape)

    def test_etape_ne_rougit_jamais_le_verdict_requis(self):
        etape = self._etape()
        self.assertIn("continue-on-error: true", etape)
        self.assertIn("if: always()", etape)
        # le job requis garde son plafond de 5 min, aucun continue-on-error au
        # niveau du job (il masquerait le verdict « tests »).
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        job = texte[texte.index("\n  tests:\n"):]
        entete = job[:job.index("    steps:")]
        self.assertIn("timeout-minutes: 5", entete)
        self.assertNotIn("continue-on-error", entete)


if __name__ == "__main__":
    unittest.main()
