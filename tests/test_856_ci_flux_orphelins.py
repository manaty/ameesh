# SPDX-License-Identifier: AGPL-3.0-only
"""#856 — les checks « tests (flux) » orphelins sont conclus par réconciliation.

Un runner arrêté (saturation #747) tue le publisher de `scripts/ci-flow.py
publish` *et* l'étape `close` : le check restait `in_progress` pour toujours et
la PR ne pouvait plus paraître verte. `sweep` relit l'état terminal réel du job
et de l'exécution qui ont publié le check et conclut `cancelled` — jamais
`success`, faute de ligne `fin`. L'exécution se lit dans le `details_url` quand
il porte `/actions/runs/<id>` ; sinon dans le `check_suite` du check, car
GitHub laisse l'URL par défaut `…/runs/<id_du_check>` sur les checks publiés
par un jeton d'exécution (cas réel constaté, #856). Aucun check dont
l'exécution est encore active, ni d'une autre tête, n'est touché.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ciflux = _charger("ci_flux_856", "ci-flow.py")

TETE = "a" * 40
AUTRE_TETE = "b" * 40
EXECUTION = 500
SUITE = 38436010127


def check(ident=11, name="tests (flux) psql 1/1", status="in_progress",
          execution=EXECUTION, details=True, suite=SUITE):
    """Un check « tests (flux) ».

    `details=True` : le `details_url` nomme l'exécution (`/actions/runs/<id>`).
    `details=False` : la forme par défaut réelle de GitHub
    (`…/runs/<id_du_check>`), que seule la `check_suite` éclaire."""
    lien = ("https://github.com/manaty/ameesh/actions/runs/%d" % execution
            if details else "https://github.com/manaty/ameesh/runs/%d" % ident)
    return {"id": ident, "name": name, "status": status, "details_url": lien,
            "check_suite": {"id": suite} if suite is not None else None,
            "output": {"title": "3 test(s) passés, en cours",
                       "summary": "Part psql 1/1 : en cours.",
                       "text": "<!-- ameesh-test-event/1 -->\n```jsonl\n```"}}


def run(status="completed"):
    return {"id": EXECUTION, "status": status}


def job(name="psql 1/1", status="completed", conclusion="cancelled"):
    return {"name": name, "status": status, "conclusion": conclusion}


class FauxGitHub:
    _listed = ciflux.GitHub._listed

    def __init__(self, checks=(), jobs=None, run_state=None, refus=False,
                 ready=True, tete=TETE, par_execution=None, par_suite=None,
                 suite_executions=None, refus_suite=False):
        self.checks = list(checks)
        self.job_list = jobs if jobs is not None else [job()]
        self.run_state = run_state if run_state is not None else run()
        self.par_execution = par_execution or {}
        self.par_suite = par_suite or {}
        self.suite_executions = ([EXECUTION] if suite_executions is None
                                 else suite_executions)
        self.refus = refus
        self.refus_suite = refus_suite
        self._ready = ready
        self.tete = tete
        self.lectures = []
        self.maj = []

    def ready(self):
        return self._ready

    def check_runs(self, tete):
        self.lectures.append(("checks", tete))
        if self.refus:
            raise ciflux.urllib.error.HTTPError("u", 500, "boom", {}, None)
        return self.checks

    def run(self, execution):
        if self.refus:
            raise ciflux.urllib.error.URLError("boom")
        return self.par_execution.get(execution, (self.run_state, self.job_list))[0]

    def jobs(self, execution):
        return self.par_execution.get(execution, (self.run_state, self.job_list))[1]

    def runs_of_suite(self, suite):
        self.lectures.append(("suite", suite))
        if self.refus or self.refus_suite:
            raise ciflux.urllib.error.URLError("boom")
        return self.par_suite.get(suite, [{"id": e} for e in self.suite_executions])

    def update(self, ident, output, conclusion=None):
        if self.refus:
            raise ciflux.urllib.error.URLError("boom")
        self.maj.append((ident, output, conclusion))


def args(sha=TETE):
    return argparse.Namespace(sha=sha)


class ReconciliationTest(unittest.TestCase):
    def test_execution_active_avec_job_terminal_conclut_cancelled(self):
        gh = FauxGitHub([check(details=False)], run_state=run("in_progress"))
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([item[2] for item in gh.maj], ["cancelled"])

    def test_execution_sans_statut_avec_job_terminal_non_conclue(self):
        gh = FauxGitHub([check(details=False)], run_state={"id": EXECUTION})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_check_suite_lit_toutes_les_pages_candidates(self):
        class Pages(ciflux.GitHub):
            def __init__(self):
                super().__init__(env={})
                self.paths = []

            def read(self, path):
                self.paths.append(path)
                if "page=2" in path:
                    return {"total_count": 101, "workflow_runs": [{"id": 101}]}
                return {"total_count": 101,
                        "workflow_runs": [{"id": n} for n in range(1, 101)]}

        gh = Pages()
        self.assertEqual([r["id"] for r in gh.runs_of_suite(SUITE)],
                         list(range(1, 102)))
        self.assertEqual(len(gh.paths), 2)

    def test_check_suite_page_manquante_non_conclue(self):
        gh = FauxGitHub([check(details=False)])
        gh.runs_of_suite = ciflux.GitHub.runs_of_suite.__get__(gh)
        gh.read = lambda path: (
            {"total_count": 2, "workflow_runs": []} if "page=2" in path
            else {"total_count": 2, "workflow_runs": [{"id": EXECUTION}]})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_check_suite_total_absent_non_conclu(self):
        gh = FauxGitHub([check(details=False)])
        gh.runs_of_suite = ciflux.GitHub.runs_of_suite.__get__(gh)
        gh.read = lambda path: {"workflow_runs": [{"id": EXECUTION}]}
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_reponse_jobs_absente_non_conclue(self):
        gh = FauxGitHub([check(details=False)])
        gh.jobs = ciflux.GitHub.jobs.__get__(gh)
        gh.read = lambda path: {}
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_jobs_page_manquante_non_conclue(self):
        gh = FauxGitHub([check(details=False)])
        gh.jobs = ciflux.GitHub.jobs.__get__(gh)
        gh.read = lambda path: (
            {"total_count": 2, "jobs": []} if "page=2" in path
            else {"total_count": 2, "jobs": [job()]})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_job_terminal_conclut_cancelled_jamais_success(self):
        gh = FauxGitHub([check()])
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(len(gh.maj), 1)
        ident, sortie, conclusion = gh.maj[0]
        self.assertEqual(ident, 11)
        self.assertEqual(conclusion, "cancelled")
        self.assertNotEqual(conclusion, "success")
        self.assertEqual(sortie["title"], "3 test(s) passés, en cours")
        self.assertIn("aucune ligne", sortie["summary"])
        self.assertIn("réconciliation", sortie["summary"])

    def test_job_terminal_success_conclut_quand_meme_cancelled(self):
        """Sans ligne `fin` lue, jamais `success` — même si le job est vert."""
        gh = FauxGitHub([check()], jobs=[job(conclusion="success")])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])

    def test_job_encore_actif_non_touche(self):
        """L'expiration d'un publisher vivant laisse le check ouvert."""
        gh = FauxGitHub([check()], jobs=[job(status="in_progress", conclusion=None)],
                        run_state=run("in_progress"))
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(gh.maj, [])

    def test_execution_active_sans_job_connu_non_touchee(self):
        gh = FauxGitHub([check()], jobs=[], run_state=run("in_progress"))
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_execution_terminale_sans_job_connu_conclut(self):
        gh = FauxGitHub([check()], jobs=[], run_state=run("completed"))
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])

    def test_check_deja_conclu_ignore(self):
        gh = FauxGitHub([check(status="completed")])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])
        self.assertEqual(gh.lectures, [("checks", TETE)])

    def test_check_d_un_autre_nom_ignore(self):
        gh = FauxGitHub([check(name="tests"), check(name="tests (flux)")])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_check_sans_execution_identifiable_non_touche(self):
        """Ni `/actions/runs/<id>` ni `check_suite` : aucune preuve, on ne juge pas."""
        gh = FauxGitHub([check(details=False, suite=None)])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_autre_tete_non_touchee(self):
        gh = FauxGitHub([check()])
        ciflux.sweep(args(sha=AUTRE_TETE), gh=gh)
        self.assertEqual(gh.lectures, [("checks", AUTRE_TETE)])

    def test_tete_par_defaut_celle_du_publisher(self):
        gh = FauxGitHub([check()], tete=AUTRE_TETE)
        ciflux.sweep(args(sha=""), gh=gh)
        self.assertEqual(gh.lectures, [("checks", AUTRE_TETE)])

    def test_api_en_erreur_ne_fait_pas_echouer(self):
        gh = FauxGitHub([check()], refus=True)
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(gh.maj, [])

    def test_sans_jeton_rien_est_lu(self):
        gh = FauxGitHub([check()], ready=False)
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(gh.lectures, [])
        self.assertEqual(gh.maj, [])

    def test_deux_checks_meme_execution_une_seule_lecture(self):
        gh = FauxGitHub([check(ident=11), check(ident=12, name="tests (flux) psql 2/2")],
                        jobs=[job(), job(name="psql 2/2")])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[0] for m in gh.maj], [11, 12])
        self.assertEqual(len(gh.lectures), 1)

    def test_autre_execution_active_non_touchee(self):
        """Seul l'orphelin de l'exécution terminale est conclu ; le check de
        l'exécution encore active qui porte le même nom reste ouvert."""
        ancienne = check(ident=11, execution=500)
        active = check(ident=12, execution=501)
        gh = FauxGitHub([ancienne, active], par_execution={
            500: (run("completed"), [job()]),
            501: (run("in_progress"), [job(status="in_progress", conclusion=None)]),
        })
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[0] for m in gh.maj], [11])
        self.assertEqual(gh.maj[0][2], "cancelled")


class LegacyDetailsUrlTest(unittest.TestCase):
    """Le cas réel : GitHub laisse l'URL par défaut `…/runs/<id_du_check>`.

    Le `details_url` ne nomme alors pas l'exécution ; c'est le `check_suite` du
    check qui la retrouve, et la preuve de terminalité reste la même (celle du
    job et de l'exécution). Sans preuve, aucun check n'est conclu.
    """

    def test_url_par_defaut_resolue_par_le_check_suite(self):
        gh = FauxGitHub([check(details=False)], par_suite={SUITE: [{"id": 777}]})
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertIn(("suite", SUITE), gh.lectures)
        self.assertEqual([m[0] for m in gh.maj], [11])
        self.assertEqual(gh.maj[0][2], "cancelled")

    def test_check_suite_sans_execution_non_touche(self):
        gh = FauxGitHub([check(details=False)], suite_executions=[])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_check_suite_illisible_non_touche(self):
        gh = FauxGitHub([check(details=False)], refus_suite=True)
        self.assertEqual(ciflux.sweep(args(), gh=gh), 0)
        self.assertEqual(gh.maj, [])

    def test_execution_du_check_suite_encore_active_non_touchee(self):
        gh = FauxGitHub([check(details=False)], suite_executions=[501],
                        par_execution={501: (run("in_progress"),
                                             [job(status="in_progress", conclusion=None)])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_job_du_check_suite_encore_actif_non_touche(self):
        gh = FauxGitHub([check(details=False)], suite_executions=[501],
                        par_execution={501: (run("completed"),
                                             [job(status="in_progress", conclusion=None)])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_plusieurs_executions_du_check_suite_toutes_terminales_concluent(self):
        gh = FauxGitHub([check(details=False)], suite_executions=[500, 501],
                        par_execution={500: (run("completed"), [job()]),
                                       501: (run("completed"), [job()])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[2] for m in gh.maj], ["cancelled"])

    def test_une_execution_du_check_suite_encore_active_bloque_tout(self):
        """Toutes les exécutions candidates doivent être terminales."""
        gh = FauxGitHub([check(details=False)], suite_executions=[500, 501],
                        par_execution={500: (run("completed"), [job()]),
                                       501: (run("in_progress"),
                                             [job(status="in_progress", conclusion=None)])})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual(gh.maj, [])

    def test_deux_checks_meme_suite_une_seule_lecture(self):
        gh = FauxGitHub([check(ident=11, details=False),
                         check(ident=12, name="tests (flux) psql 2/2", details=False)],
                        jobs=[job(), job(name="psql 2/2")])
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[0] for m in gh.maj], [11, 12])
        self.assertEqual([l for l in gh.lectures if l[0] == "suite"], [("suite", SUITE)])

    def test_details_url_nommant_l_execution_prime_sur_la_suite(self):
        """Quand `details_url` nomme l'exécution, la suite n'est pas lue."""
        gh = FauxGitHub([check(details=True)], par_suite={SUITE: [{"id": 777}]})
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[0] for m in gh.maj], [11])
        self.assertNotIn(("suite", SUITE), gh.lectures)

    def test_suite_of(self):
        self.assertEqual(ciflux._suite_of(check(details=False)), SUITE)
        self.assertIsNone(ciflux._suite_of(check(details=False, suite=None)))
        self.assertIsNone(ciflux._suite_of({"check_suite": {"id": "x"}}))
        self.assertIsNone(ciflux._suite_of({}))


class FauxGitHubPublication:
    """Le GitHub minimal de `publish` : création puis mises à jour."""

    def __init__(self):
        self.appels = []

    def ready(self):
        return True

    def create(self, nom, output):
        self.appels.append(("create", nom, output, None))
        return 7

    def update(self, ident, output, conclusion=None):
        self.appels.append(("update", ident, output, conclusion))


class ExpirationTest(unittest.TestCase):
    def test_publisher_expire_laisse_le_check_ouvert_puis_reconciliation(self):
        """Expiration d'un publisher vivant : pas de verdict ; le check reste
        ouvert et c'est la réconciliation, sur l'état terminal, qui conclut."""
        import json
        import tempfile

        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(dossier))
        flux = dossier / "flux.jsonl"
        flux.write_text(json.dumps({"v": 1, "type": "debut", "part": "p 1/1",
                                    "modules": ["test_a"]}) + "\n", encoding="utf-8")
        gh = FauxGitHubPublication()
        publier = argparse.Namespace(file=str(flux), name="tests (flux) p 1/1",
                                     interval=1.0, max_duration=0.0)
        self.assertEqual(ciflux.publish(publier, gh=gh, dormir=lambda _s: None,
                                        horloge=lambda: 0.0), 0)
        genres = [a[0] for a in gh.appels]
        self.assertEqual(genres, ["create"])  # aucune conclusion sans ligne fin
        # le temps passe, le publisher est tué : le job devient terminal
        laisse = check(ident=7, name="tests (flux) p 1/1")
        encore_vivant = FauxGitHub([laisse],
                                   jobs=[job(name="p 1/1", status="in_progress",
                                             conclusion=None)])
        ciflux.sweep(args(), gh=encore_vivant)
        self.assertEqual(encore_vivant.maj, [])
        # job terminal : le check est conclu cancelled
        terminal = FauxGitHub([laisse], jobs=[job(name="p 1/1")])
        ciflux.sweep(args(), gh=terminal)
        self.assertEqual([m[2] for m in terminal.maj], ["cancelled"])


class PaginationTest(unittest.TestCase):
    def test_check_runs_pagine(self):
        pages = {1: {"total_count": 3, "check_runs": [{"id": 1}, {"id": 2}]},
                 2: {"total_count": 3, "check_runs": [{"id": 3}]}}
        gh = ciflux.GitHub()
        gh.read = lambda chemin: pages[int(chemin.rsplit("page=", 1)[1])]
        self.assertEqual([c["id"] for c in gh.check_runs(TETE)], [1, 2, 3])

    def test_execution_lue_dans_le_details_url(self):
        self.assertEqual(ciflux._execution_of(check()), EXECUTION)
        self.assertIsNone(ciflux._execution_of(check(details=False)))


class WorkflowTest(unittest.TestCase):
    """Un bilan rouge ne saute pas la réconciliation (#856)."""

    def test_bilan_rouge_laisse_le_checkout_puis_le_sweep_tourner(self):
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        debut = texte.index("\n  tests:\n")
        suivant = re.compile(r"\n  [#\w]").search(texte, debut + 1)
        job = texte[debut:suivant.start() if suivant else len(texte)]
        bilan = job.index("- name: bilan des parts")
        sweep = job.index("- name: conclusion des checks de flux orphelins")
        self.assertIn("if: always()", job[sweep:],
                      "sweep doit tourner même quand le bilan est rouge")
        checkouts = [m.start() for m in re.finditer(r"- uses: actions/checkout@v4", job)]
        avant = [i for i in checkouts if i < sweep]
        self.assertTrue(avant, "sweep lit scripts/ci-flow.py : un checkout doit précéder")
        dernier = avant[-1]
        # La garde implicite success() saute une étape après un échec : soit le
        # checkout est placé avant le bilan, soit il lève la garde `if: always()`,
        # sinon sweep (en always()) tourne sans son script et ne conclut rien.
        self.assertTrue(dernier < bilan or "if: always()" in job[dernier:sweep],
                        "bilan rouge : le checkout doit rester `if: always()`")


if __name__ == "__main__":
    unittest.main()
