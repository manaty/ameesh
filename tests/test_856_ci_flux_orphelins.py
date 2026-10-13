# SPDX-License-Identifier: AGPL-3.0-only
"""#856 — les checks « tests (flux) » orphelins sont conclus par réconciliation.

Un runner arrêté (saturation #747) tue le publisher de `scripts/ci-flow.py
publish` *et* l'étape `close` : le check restait `in_progress` pour toujours et
la PR ne pouvait plus paraître verte. `sweep` relit l'état terminal réel du job
et de l'exécution qui ont publié le check (par son `details_url`) et conclut
`cancelled` — jamais `success`, faute de ligne `fin`. Aucun check actif, d'une
autre tête ou d'une autre tentative n'est touché.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
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


def check(ident=11, name="tests (flux) psql 1/1", status="in_progress",
          execution=EXECUTION, details=True):
    lien = "https://github.com/manaty/ameesh/actions/runs/%d" % execution
    return {"id": ident, "name": name, "status": status,
            "details_url": lien if details else "",
            "output": {"title": "3 test(s) passés, en cours",
                       "summary": "Part psql 1/1 : en cours.",
                       "text": "<!-- ameesh-test-event/1 -->\n```jsonl\n```"}}


def run(status="completed"):
    return {"id": EXECUTION, "status": status}


def job(name="psql 1/1", status="completed", conclusion="cancelled"):
    return {"name": name, "status": status, "conclusion": conclusion}


class FauxGitHub:
    def __init__(self, checks=(), jobs=None, run_state=None, refus=False,
                 ready=True, tete=TETE, par_execution=None):
        self.checks = list(checks)
        self.job_list = jobs if jobs is not None else [job()]
        self.run_state = run_state if run_state is not None else run()
        self.par_execution = par_execution or {}
        self.refus = refus
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

    def update(self, ident, output, conclusion=None):
        if self.refus:
            raise ciflux.urllib.error.URLError("boom")
        self.maj.append((ident, output, conclusion))


def args(sha=TETE):
    return argparse.Namespace(sha=sha)


class ReconciliationTest(unittest.TestCase):
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
        gh = FauxGitHub([check(details=False)])
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

    def test_autre_tentative_active_non_touchee(self):
        """Seul l'orphelin de la tentative terminale est conclu ; le check de
        la tentative active qui porte le même nom reste ouvert."""
        ancienne = check(ident=11, execution=500)
        active = check(ident=12, execution=501)
        gh = FauxGitHub([ancienne, active], par_execution={
            500: (run("completed"), [job()]),
            501: (run("in_progress"), [job(status="in_progress", conclusion=None)]),
        })
        ciflux.sweep(args(), gh=gh)
        self.assertEqual([m[0] for m in gh.maj], [11])
        self.assertEqual(gh.maj[0][2], "cancelled")


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


if __name__ == "__main__":
    unittest.main()
