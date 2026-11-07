# SPDX-License-Identifier: AGPL-3.0-only
"""#6157 — la mesure planifiée de la carte n'est due que dans sa fenêtre.

Constat (pilote, 07/11) : GitHub démarre le cron 03:30Z de carte.yml avec six
à huit heures de retard (run 42364733479 à 10:37Z : 11 des 12 runners pendant
que la ci-full du paquet h-20261107-13 attendait). Témoins : la décision du
garde (scripts/carte-fenetre.py), son câblage dans carte.yml (job `fenetre`
hébergé, `plan-mesure` qui en dépend, `carte` qui ne rougit pas sur une mesure
sautée, plafond de runners) et la minuterie d'hôte qui dispatche à 03:30Z.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")

_spec = importlib.util.spec_from_file_location(
    "carte_fenetre", REPO / "scripts" / "carte-fenetre.py")
fenetre = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fenetre)

UTC = dt.timezone.utc


def a(heure: str) -> dt.datetime:
    return dt.datetime.fromisoformat("2026-11-07T%s:00+00:00" % heure)


def job(nom: str) -> str:
    m = re.search(r"(?ms)^  %s:\n(.*?)(?=^  [a-z][a-z0-9-]*:\s*$|\Z)" % re.escape(nom),
                  WORKFLOW.split("\njobs:\n", 1)[1])
    assert m is not None, "job %s introuvable" % nom
    return m.group(1)


class DecisionTest(unittest.TestCase):

    def decider(self, **kw):
        base = dict(evenement="schedule", planifie=False, maintenant=a("03:31"),
                    derniere_carte=a("03:31") - dt.timedelta(hours=24),
                    paquet_actif=False, autre_carte=False)
        base.update(kw)
        return fenetre.decider(**base)

    def test_le_cron_en_retard_est_saute(self):
        # les heures réelles du constat : 10:37Z, 11:24Z
        for heure in ("10:37", "11:24", "07:00"):
            with self.subTest(heure=heure):
                mesurer, raison = self.decider(maintenant=a(heure))
                self.assertFalse(mesurer)
                self.assertIn("hors fenêtre", raison)

    def test_la_minuterie_dans_sa_fenetre_mesure(self):
        mesurer, _ = self.decider(evenement="workflow_dispatch", planifie=True)
        self.assertTrue(mesurer)
        self.assertTrue(self.decider(maintenant=a("00:00"))[0])
        self.assertTrue(self.decider(maintenant=a("06:59"))[0])

    def test_la_minuterie_hors_fenetre_est_sautee(self):
        # rattrapage `Persistent=true` d'un hôte rallumé en pleine journée
        mesurer, _ = self.decider(evenement="workflow_dispatch", planifie=True,
                                  maintenant=a("14:00"))
        self.assertFalse(mesurer)

    def test_un_paquet_en_file_saute_la_mesure(self):
        mesurer, raison = self.decider(paquet_actif=True)
        self.assertFalse(mesurer)
        self.assertIn("paquet", raison)

    def test_une_carte_fraiche_ou_une_autre_mesure_rendent_pas_due(self):
        self.assertFalse(self.decider(derniere_carte=a("01:00"))[0])
        self.assertFalse(self.decider(autre_carte=True)[0])
        # 13 h : due
        self.assertTrue(self.decider(
            derniere_carte=a("03:31") - dt.timedelta(hours=13))[0])

    def test_lecture_impossible_ne_bloque_pas(self):
        # non concluant (canon §3) : la fenêtre seule borne la mesure
        self.assertTrue(self.decider(derniere_carte=None, paquet_actif=None,
                                     autre_carte=None)[0])

    def test_dispatch_manuel_et_pr_mesurent_toujours(self):
        for kw in (dict(evenement="workflow_dispatch", planifie=False),
                   dict(evenement="pull_request")):
            with self.subTest(**kw):
                self.assertTrue(self.decider(maintenant=a("11:00"), paquet_actif=True,
                                             autre_carte=True, **kw)[0])

    def test_detection_des_executions(self):
        runs = [{"id": 1, "path": ".github/workflows/carte.yml", "head_branch": "main"},
                {"id": 2, "path": ".github/workflows/paquet.yml",
                 "head_branch": "integration/paquet-h-20261107-13"}]
        self.assertTrue(fenetre.paquet_actif(runs))
        self.assertFalse(fenetre.paquet_actif(runs[:1]))
        self.assertFalse(fenetre.autre_carte(runs, "1"), "le garde ne se compte pas")
        self.assertTrue(fenetre.autre_carte(runs, "9"))

    def test_main_ecrit_la_sortie_sans_api_pour_une_pr(self):
        with tempfile.TemporaryDirectory() as d:
            sortie = pathlib.Path(d) / "out"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(sortie)}), \
                    mock.patch.object(fenetre.GitHub, "get",
                                      side_effect=AssertionError("pas d'API")):
                self.assertEqual(fenetre.main(["--event", "pull_request"]), 0)
            self.assertEqual(sortie.read_text(), "mesurer=true\n")

    def test_main_api_en_panne_garde_la_fenetre(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as d:
            sortie = pathlib.Path(d) / "out"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(sortie)}), \
                    mock.patch.object(fenetre.GitHub, "get",
                                      side_effect=urllib.error.URLError("x")):
                fenetre.main(["--event", "schedule", "--maintenant", "2026-11-07T10:37:00Z"])
            self.assertEqual(sortie.read_text(), "mesurer=false\n")


class CablageTest(unittest.TestCase):

    def test_le_garde_tourne_sur_un_runner_heberge(self):
        corps = job("fenetre")
        self.assertIn("runs-on: ubuntu-latest", corps)
        self.assertIn("actions: read", corps)
        self.assertIn("mesurer: ${{ steps.garde.outputs.mesurer }}", corps)
        self.assertIn('echo "mesurer=true" >> "$GITHUB_OUTPUT"', corps)

    def test_le_plan_depend_de_la_fenetre(self):
        corps = job("plan-mesure")
        self.assertIn("needs: fenetre", corps)
        self.assertRegex(corps, r"(?m)^    if: needs\.fenetre\.outputs\.mesurer == 'true' && ")

    def test_une_mesure_sautee_ne_rougit_pas_la_carte(self):
        self.assertIn("needs.mesure.result != 'skipped'", job("carte"))

    def test_plafond_de_runners(self):
        m = re.search(r"(?m)^      max-parallel: (\d+)$", job("mesure"))
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 8)

    def test_dispatch_planifie_et_cron_de_secours(self):
        self.assertRegex(WORKFLOW, r"(?m)^      planifie:\n")
        self.assertIn('- cron: "30 3 * * *"', WORKFLOW)
        self.assertIn("      - scripts/carte-fenetre.py", WORKFLOW)

    def test_minuterie_d_hote(self):
        timer = (REPO / "deploy/systemd/ameesh-carte.timer").read_text(encoding="utf-8")
        service = (REPO / "deploy/systemd/ameesh-carte.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 03:30:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("gh workflow run carte.yml", service)
        self.assertIn("-f planifie=true", service)
        self.assertNotIn("manaty", service, "rien de propre à un client en dur")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
