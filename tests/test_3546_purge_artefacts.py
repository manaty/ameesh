# SPDX-License-Identifier: AGPL-3.0-only
"""#3546 : quota d'artefacts de l'organisation — ménage des mesures à toute
issue du run `carte`, rétention explicite de chaque envoi, purge rejouable.

Sans réseau : le contrat des workflows (texte), puis la décision pure de
`scripts/purge-artefacts.py` sur des artefacts au format de l'API GitHub.
Relevé de première source (25/10 11:20Z, mesure #2376) : les quinze runs
`carte` des 24 h précédentes ont fini `cancelled`, et leurs mesures
(1,4 Gio) restaient la journée entière, le ménage ne tournant qu'après une
fusion réussie.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "purge_artefacts", REPO / "scripts" / "purge-artefacts.py")
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)

WORKFLOWS = REPO / ".github" / "workflows"
CARTE = (WORKFLOWS / "carte.yml").read_text(encoding="utf-8")


def _job(texte: str, nom: str) -> str:
    """Le bloc d'un job de premier niveau (jusqu'au job suivant)."""
    m = re.search(rf"^  {re.escape(nom)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", texte, re.M | re.S)
    assert m, f"job {nom} absent"
    return m.group(1)


def _envois() -> list[tuple[str, str, str | None]]:
    """(workflow, nom, retention-days) de chaque envoi d'artefact."""
    envois = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        texte = wf.read_text(encoding="utf-8")
        for m in re.finditer(r"uses: actions/upload-(?:pages-)?artifact@v\d+\n((?:\s{8,}.*\n)+)", texte):
            bloc = m.group(1)
            nom = re.search(r"^\s+name: (.+)$", bloc, re.M)
            ret = re.search(r"^\s+retention-days: (\d+)", bloc, re.M)
            envois.append((wf.name, nom.group(1).strip() if nom else "github-pages",
                           ret.group(1) if ret else None))
    return envois


class MenageMesuresTest(unittest.TestCase):

    def test_le_menage_tourne_a_toute_issue_meme_annulee(self):
        job = _job(CARTE, "menage-mesures")
        self.assertIn("needs: [mesure, carte]", job)
        self.assertRegex(job, r"\n    if: always\(\)")
        self.assertNotIn("cancelled()", job)
        self.assertIn("continue-on-error: true", job)
        self.assertIn("timeout-minutes:", job)
        # hors du pool : jamais en file derrière la mesure (#876)
        self.assertIn("runs-on: ubuntu-latest", job)

    def test_seul_le_menage_a_le_droit_d_ecrire(self):
        self.assertEqual(CARTE.count("actions: write"), 1)
        self.assertIn("actions: write", _job(CARTE, "menage-mesures"))
        self.assertNotIn("DELETE", _job(CARTE, "carte"))

    def test_les_mesures_ne_sont_gardees_que_pour_rejouer_la_fusion(self):
        job = _job(CARTE, "menage-mesures")
        self.assertIn("GARDER: ${{ github.event_name != 'pull_request' && "
                      "needs.mesure.result == 'success' && "
                      "needs.carte.result == 'failure' }}", job)
        self.assertIn('a["name"].startswith("mesure-")', job)

    def test_une_pr_de_fork_n_y_entre_pas(self):
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository",
                      _job(CARTE, "menage-mesures"))


class RetentionExpliciteTest(unittest.TestCase):

    def test_chaque_envoi_declare_sa_retention(self):
        envois = _envois()
        self.assertGreaterEqual(len(envois), 8)
        for wf, nom, ret in envois:
            self.assertIsNotNone(ret, f"{wf} : envoi {nom} sans retention-days")

    def test_la_purge_suit_la_retention_des_workflows(self):
        for wf, nom, ret in _envois():
            fam = purge.famille(re.sub(r"\$\{\{.*?\}\}", "1", nom))
            self.assertIsNotNone(fam, f"{wf} : famille {nom} absente de RETENTION_H")
            attendu = None if fam == "mesure" else int(ret) * 24
            self.assertEqual(purge.RETENTION_H[fam], attendu, f"{wf} : {nom}")


MAINTENANT = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)


def art(nom, heures, run=1, expire=False):
    cree = MAINTENANT - dt.timedelta(hours=heures)
    return {"id": 7, "name": nom, "expired": expire, "size_in_bytes": 10,
            "created_at": cree.strftime("%Y-%m-%dT%H:%M:%SZ"), "workflow_run": {"id": run}}


def decide(a, en_cours=(), garde=False, epargne=24):
    return purge.decision(a, MAINTENANT, en_cours=set(en_cours), epargne_h=epargne,
                          garde_mesures=lambda run_id: garde)


class DecisionTest(unittest.TestCase):

    def test_familles(self):
        self.assertEqual(purge.famille("mesure-psycopg-3"), "mesure")
        self.assertEqual(purge.famille("flux-psql-12"), "flux-psql")
        self.assertEqual(purge.famille("leaks"), "leaks")
        self.assertEqual(purge.famille("plan-mesure"), "plan-mesure")
        self.assertIsNone(purge.famille("inconnu-3"))

    def test_mesure_d_un_run_termine_supprimee_meme_recente(self):
        self.assertEqual(decide(art("mesure-psql-2", 2))[0], True)

    def test_un_run_en_cours_est_epargne(self):
        self.assertEqual(decide(art("mesure-psql-2", 2, run=9), en_cours={9}),
                         (False, "run en cours"))
        self.assertFalse(decide(art("flux-psql-1", 100, run=9), en_cours={9})[0])

    def test_mesures_gardees_pour_rejouer_la_fusion(self):
        self.assertFalse(decide(art("mesure-psql-2", 2), garde=True)[0])
        # au-delà d'un jour, la rétention d'origine les aurait expirées
        self.assertTrue(decide(art("mesure-psql-2", 25), garde=True)[0])

    def test_retention_par_famille(self):
        self.assertTrue(decide(art("flux-psycopg-3", 30))[0])
        self.assertFalse(decide(art("plan", 30))[0])
        self.assertTrue(decide(art("plan", 73))[0])
        self.assertFalse(decide(art("leaks", 14 * 24))[0])
        self.assertTrue(decide(art("leaks", 16 * 24))[0])

    def test_epargne_des_artefacts_jeunes(self):
        self.assertFalse(decide(art("flux-psql-1", 30), epargne=48)[0])

    def test_expire_et_inconnu_sont_laisses(self):
        self.assertEqual(decide(art("flux-psql-1", 30, expire=True)), (False, "déjà expiré"))
        self.assertEqual(decide(art("autre-chose", 300)), (False, "famille inconnue"))

    def test_regle_de_garde_du_workflow(self):
        jobs = [{"name": "mesure psycopg 1/8", "conclusion": "success"},
                {"name": "mesure psql 1/6", "conclusion": "success"},
                {"name": "carte", "conclusion": "failure"}]
        self.assertTrue(purge.mesures_gardees({"event": "schedule"}, jobs))
        self.assertFalse(purge.mesures_gardees({"event": "pull_request"}, jobs))
        jobs[1]["conclusion"] = "cancelled"
        self.assertFalse(purge.mesures_gardees({"event": "schedule"}, jobs))

    def test_bilan_par_famille(self):
        b = purge.bilan([art("mesure-psql-1", 1), art("mesure-psql-2", 1),
                         art("leaks", 1, expire=True)])
        self.assertEqual(b, {"mesure": (2, 20)})


class FauxGh:
    """`gh api` simulé : un run par identifiant, ses jobs de toutes les
    tentatives ; `filter=latest` (défaut de l'API) ne rend que la dernière."""

    def __init__(self, runs, jobs=()):
        self.runs, self.jobs, self.appels = runs, list(jobs), []

    def __call__(self, *args):
        url = next(a for a in args if a.startswith("repos/"))
        self.appels.append(url)
        run_id = int(url.split("/runs/")[1].split("/")[0].split("?")[0])
        if run_id not in self.runs:
            raise purge.subprocess.CalledProcessError(1, "gh", stderr="HTTP 404")
        run = self.runs[run_id]
        if "/jobs" not in url:
            return purge.json.dumps(run)
        jobs = self.jobs
        if "filter=all" not in url:
            jobs = [j for j in jobs if j["run_attempt"] == run["run_attempt"]]
        return "\n".join(purge.json.dumps(j) for j in jobs)


def _jobs_relance():
    """Tentative 1 : mesures vertes, carte en échec ; tentative 2 : la seule
    carte rejouée, encore en échec."""
    return [{"id": 1, "name": "mesure psycopg 1/8", "conclusion": "success", "run_attempt": 1},
            {"id": 2, "name": "mesure psql 1/6", "conclusion": "success", "run_attempt": 1},
            {"id": 3, "name": "carte", "conclusion": "failure", "run_attempt": 1},
            {"id": 4, "name": "carte", "conclusion": "failure", "run_attempt": 2}]


def decide_api(a, gh, en_cours=(), runs=None):
    runs = runs or purge.LecteurRuns("o/r", gh=gh)
    return purge.decision(a, MAINTENANT, en_cours=set(en_cours), epargne_h=24,
                          garde_mesures=runs.garde_mesures, run_termine=runs.termine)


class RelanceTest(unittest.TestCase):
    """Relecture cx3 et dev3 de e2b4b0a2 : un run relancé pendant la purge."""

    def test_b1_mesure_d_un_run_relance_apres_le_cliche_est_gardee(self):
        # cliché en_cours vide, mesure d'une heure, le run relu est in_progress
        gh = FauxGh({1: {"event": "push", "status": "in_progress", "run_attempt": 2}},
                    [{"id": 3, "name": "carte", "conclusion": None, "run_attempt": 2}])
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh)[0], False)

    def test_b1_toute_famille_relit_le_statut_avant_de_supprimer(self):
        gh = FauxGh({1: {"event": "push", "status": "queued", "run_attempt": 2}})
        self.assertEqual(decide_api(art("flux-psql-1", 30), gh),
                         (False, "run relancé ou illisible à la relecture"))
        gh = FauxGh({})
        self.assertEqual(decide_api(art("flux-psql-1", 30), gh)[0], False)
        gh = FauxGh({1: {"event": "push", "status": "completed", "run_attempt": 1}})
        self.assertEqual(decide_api(art("flux-psql-1", 30), gh)[0], True)

    def test_b1_un_completed_lu_n_est_jamais_reutilise(self):
        # relecture cx3 de fb1f3860 : un même lecteur, deux mesures du run ;
        # la relance arrive entre les deux décisions
        gh = FauxGh({1: {"event": "push", "status": "completed", "run_attempt": 1}},
                    _jobs_relance()[:3])
        runs = purge.LecteurRuns("o/r", gh=gh)
        self.assertEqual(decide_api(art("mesure-psql-1", 1), gh, runs=runs),
                         (False, "gardée pour rejouer la fusion"))
        gh.runs[1] = {"event": "push", "status": "in_progress", "run_attempt": 2}
        gh.jobs.append({"id": 4, "name": "carte", "conclusion": None, "run_attempt": 2})
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh, runs=runs)[0], False)
        self.assertEqual(decide_api(art("flux-psql-1", 30), gh, runs=runs)[0], False)
        # une lecture du run par décision, aucune reprise du cache
        self.assertEqual(sum("/jobs" not in u for u in gh.appels), 3)

    def test_b1_relance_entre_la_lecture_du_run_et_celle_des_jobs(self):
        # run lu completed (tentative 1), jobs lus après la relance
        gh = FauxGh({1: {"event": "push", "status": "completed", "run_attempt": 1}},
                    _jobs_relance()[:3] + [{"id": 4, "name": "carte", "conclusion": None,
                                            "run_attempt": 2}])
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh),
                         (False, "gardée pour rejouer la fusion"))
        # une tentative plus récente déjà conclue, mais le run lu est l'ancien
        gh.jobs[-1]["conclusion"] = "success"
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh)[0], False)

    def test_b1_le_statut_se_relit_apres_les_jobs(self):
        # le run passe in_progress juste après la lecture des jobs : la
        # relecture d'avant suppression le voit
        class RelanceApresJobs(FauxGh):
            def __call__(self, *args):
                sortie = super().__call__(*args)
                if any("/jobs" in a for a in args):
                    self.runs[1] = {"event": "push", "status": "in_progress",
                                    "run_attempt": 2}
                return sortie
        jobs = _jobs_relance() + [{"id": 5, "name": "carte", "conclusion": "success",
                                   "run_attempt": 3}]
        gh = RelanceApresJobs({1: {"event": "push", "status": "completed",
                                   "run_attempt": 3}}, jobs)
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh),
                         (False, "run relancé ou illisible à la relecture"))

    def test_b2_relance_de_la_seule_carte_garde_les_mesures(self):
        # run_attempt=2 terminé : les mesures vertes sont de la tentative 1
        gh = FauxGh({1: {"event": "push", "status": "completed", "run_attempt": 2}},
                    _jobs_relance())
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh),
                         (False, "gardée pour rejouer la fusion"))
        self.assertTrue(any("/jobs?filter=all" in u for u in gh.appels))

    def test_b2_preuve_incomplete_garde(self):
        # l'API ne rend que la dernière tentative : aucun job de mesure
        self.assertTrue(purge.mesures_gardees(
            {"event": "push"}, [{"id": 4, "name": "carte", "conclusion": "failure",
                                 "run_attempt": 2}]))
        # mesures vertes sans carte : rien ne dit que la fusion a réussi
        self.assertTrue(purge.mesures_gardees(
            {"event": "push"}, [{"id": 1, "name": "mesure psql 1/6",
                                 "conclusion": "success", "run_attempt": 1}]))

    def test_b2_le_dernier_resultat_de_chaque_job_fait_foi(self):
        jobs = _jobs_relance() + [{"id": 5, "name": "carte", "conclusion": "success",
                                   "run_attempt": 3}]
        self.assertFalse(purge.mesures_gardees({"event": "push"}, jobs))
        gh = FauxGh({1: {"event": "push", "status": "completed", "run_attempt": 3}}, jobs)
        self.assertEqual(decide_api(art("mesure-psql-2", 1), gh)[0], True)


if __name__ == "__main__":
    unittest.main()
