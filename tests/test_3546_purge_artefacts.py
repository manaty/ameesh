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


if __name__ == "__main__":
    unittest.main()
