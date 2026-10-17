# SPDX-License-Identifier: AGPL-3.0-only
"""#1002 : la garde de relance hosted refuse un événement `workflow_run` périmé.

`GET actions/runs/<id>/jobs` lit la **dernière** tentative par défaut, et
`rerun-failed-jobs` vise la tentative courante : un helper rejoué à la main
(événement tentative 1) après une relance (état courant tentative 2) lirait la
tentative 2 et demanderait une tentative 3. Les témoins montent une API GitHub
simulée : l'état de l'exécution fait foi, et aucun POST n'est émis quand
l'événement est périmé. La décision pure est testée séparément.

Sans base ni réseau : `urllib.request.urlopen` est remplacé par la fausse API.
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
import urllib.request
from unittest import mock

from tests import support  # noqa: F401  (L142 : environnement isolé)

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ci_relance_hosted_1002", REPO / "scripts" / "ci-relance-hosted.py")
ci_relance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_relance)

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


class Reponse:
    """Le strict nécessaire d'une réponse `urlopen` (contexte + corps)."""

    def __init__(self, donnees):
        self.corps = json.dumps(donnees).encode("utf-8")

    def read(self):
        return self.corps

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class API:
    """API GitHub simulée : état de l'exécution, jobs, POST de relance.

    Le POST `rerun-failed-jobs` fait passer l'exécution à la tentative
    suivante, en cours — comme le vrai service : c'est ce qui distingue le
    rejeu du helper (événement tentative 1) de l'état courant.
    """

    def __init__(self, tentative=1, statut="completed", jobs=None):
        self.tentative = tentative
        self.statut = statut
        self.jobs = list(jobs if jobs is not None else [PLAN, MACOS_ANNULE, AGREGAT])
        self.appels: list[tuple[str, str]] = []

    def urlopen(self, req, timeout=None):
        self.appels.append((req.method, req.full_url))
        chemin = req.full_url.split("/repos/manaty/ameesh/", 1)[1]
        if chemin.endswith("/rerun-failed-jobs"):
            self.tentative += 1
            self.statut = "in_progress"
            return Reponse({})
        if "/jobs" in chemin:
            return Reponse({"total_count": len(self.jobs), "jobs": self.jobs})
        return Reponse({"id": 38443445528, "run_attempt": self.tentative,
                        "status": self.statut, "conclusion": "failure"})

    @property
    def posts(self) -> list[str]:
        return [url for methode, url in self.appels if methode == "POST"]

    @property
    def lectures_jobs(self) -> list[str]:
        return [url for methode, url in self.appels
                if methode == "GET" and "/jobs" in url]


class EvenementPerimeTest(unittest.TestCase):
    """La fonction pure : ce que l'événement doit décrire."""

    def test_evenement_1_etat_courant_2_est_perime(self):
        refus = ci_relance.evenement_perime(
            1, {"id": 42, "run_attempt": 2, "status": "completed"})
        self.assertIsNotNone(refus)
        self.assertIn("périmé", refus)
        self.assertIn("tentative 2", refus)

    def test_evenement_accorde_a_l_etat_n_est_pas_perime(self):
        self.assertIsNone(ci_relance.evenement_perime(
            1, {"id": 42, "run_attempt": 1, "status": "completed"}))

    def test_execution_non_terminee_est_refusee(self):
        # un rejeu manuel a démarré entre l'événement et la garde : rien ne
        # doit être relancé sur une tentative en cours
        refus = ci_relance.evenement_perime(
            2, {"id": 42, "run_attempt": 2, "status": "in_progress"})
        self.assertIsNotNone(refus)
        self.assertIn("non terminée", refus)

    def test_tentative_courante_illisible_est_refusee(self):
        refus = ci_relance.evenement_perime(
            1, {"id": 42, "status": "completed"})
        self.assertIsNotNone(refus)
        self.assertIn("inconnue", refus)


class GardeApiTest(unittest.TestCase):
    """Le script complet, sur l'API simulée (`--run-id --apply`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resume = pathlib.Path(self.tmp.name) / "resume.md"
        self.api = API()
        self.env = {
            "GH_TOKEN": "jeton-de-test",
            "GITHUB_REPOSITORY": "manaty/ameesh",
            "GITHUB_API_URL": "https://api.github.test",
            "GITHUB_STEP_SUMMARY": str(self.resume),
        }

    def lancer(self, tentative="1"):
        sortie = io.StringIO()
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(urllib.request, "urlopen", self.api.urlopen), \
                contextlib.redirect_stdout(sortie):
            code = ci_relance.main(
                ["--run-id", "38443445528", "--tentative", tentative,
                 "--conclusion", "failure", "--apply"])
        return code, sortie.getvalue()

    def test_evenement_1_etat_courant_2_ne_relance_pas(self):
        # le helper est rejoué après la relance : l'événement (tentative 1)
        # ne décrit plus l'état courant (tentative 2)
        self.api.tentative = 2
        code, sortie = self.lancer()
        self.assertEqual(code, 0, sortie)
        self.assertIn("périmé", sortie)
        self.assertEqual(self.api.posts, [])
        # et la lecture des jobs de la tentative 2 est évitée : elle aurait
        # servi à décider sur la mauvaise tentative
        self.assertEqual(self.api.lectures_jobs, [])

    def test_relance_nominale_puis_helper_rejoue(self):
        code, sortie = self.lancer()          # tentative 1 -> relance
        self.assertEqual(code, 0, sortie)
        self.assertIn("relance demandée", sortie)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.tentative, 2)
        # le helper est rejoué avec le même événement (tentative 1) : l'état
        # courant est 2, plus rien n'est demandé
        self.api.appels.clear()
        code, sortie = self.lancer()
        self.assertEqual(code, 0, sortie)
        self.assertIn("périmé", sortie)
        self.assertEqual(self.api.posts, [])

    def test_deuxieme_annulation_ne_relance_pas(self):
        # l'événement de la tentative 2 est à jour : la décision refuse, sans
        # POST
        self.api.tentative = 2
        code, sortie = self.lancer(tentative="2")
        self.assertEqual(code, 0, sortie)
        self.assertIn("tentative 2", sortie)
        self.assertEqual(self.api.posts, [])

    def test_echec_reel_ne_relance_pas(self):
        self.api.jobs = [PLAN, MACOS_ANNULE, AGREGAT,
                         job("psql 1/1", "failure", runner="ameesh-ci-3-1",
                             etapes=["part 1"],
                             labels=["self-hosted", "linux", "ameesh-ci"])]
        code, sortie = self.lancer()
        self.assertEqual(code, 0, sortie)
        self.assertIn("échec réel", sortie)
        self.assertEqual(self.api.posts, [])


class WorkflowTest(unittest.TestCase):
    """Le contrat du workflow : sérialiser les gardes d'une même exécution."""

    def test_groupe_de_concurrence_par_execution(self):
        texte = GARDE.read_text(encoding="utf-8")
        self.assertIn("concurrency:", texte)
        self.assertIn("group: relance-hosted-${{ github.event.workflow_run.id }}",
                      texte)
        self.assertIn("cancel-in-progress: false", texte)


if __name__ == "__main__":
    unittest.main()
