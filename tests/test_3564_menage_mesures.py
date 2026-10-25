# SPDX-License-Identifier: AGPL-3.0-only
"""#3564 : panier de #3546 — preuve d'annulation et garde du rejeu `carte`.

N1 : GitHub ne crée pas toujours un job `always()` encore en file après
l'annulation d'un run (run 40180614008 : créé ; run 38917093257 : jamais).
Le ménage des mesures est doublé par `menage-mesures.yml`, déclenché après
la conclusion de chaque run `carte` (`workflow_run`), qui applique la règle
de `scripts/purge-artefacts.py` aux seuls artefacts du run (`--run`).

N2 : le plafond de 60 min de `carte` conclut `cancelled`, pas `failure`
(runs 39223265159, 38985327391 : parts toutes vertes, fusion coupée) ; la
garde du rejeu le retient, dans le workflow comme dans le script.

Sans réseau : le texte des workflows et un `gh api` simulé.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "purge_artefacts", REPO / "scripts" / "purge-artefacts.py")
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)

WORKFLOWS = REPO / ".github" / "workflows"
CARTE = (WORKFLOWS / "carte.yml").read_text(encoding="utf-8")
MENAGE = (WORKFLOWS / "menage-mesures.yml").read_text(encoding="utf-8")


def _sans_commentaires(texte: str) -> str:
    return "\n".join(l for l in texte.splitlines() if not l.lstrip().startswith("#"))


def _jobs(carte: str, mesure: str = "success", attempt: int = 1) -> list[dict]:
    return [{"id": 1, "name": "mesure psycopg 1/8", "conclusion": mesure, "run_attempt": 1},
            {"id": 2, "name": "mesure psql 1/6", "conclusion": "success", "run_attempt": 1},
            {"id": 3, "name": "carte", "conclusion": carte, "run_attempt": attempt}]


class GardeDuRejeuTest(unittest.TestCase):
    """N2 : une fusion coupée par son plafond garde les mesures."""

    def test_le_workflow_garde_une_fusion_annulee(self):
        garde = re.search(r"GARDER: \$\{\{(.*?)\}\}", CARTE).group(1)
        self.assertIn("needs.carte.result == 'cancelled'", garde)
        self.assertIn("needs.carte.result == 'failure'", garde)
        self.assertIn("needs.mesure.result == 'success'", garde)
        self.assertIn("github.event_name != 'pull_request'", garde)

    def test_le_script_suit_la_meme_garde(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            self.assertTrue(purge.mesures_gardees({"event": "schedule"}, _jobs(conclusion)),
                            conclusion)
        for conclusion in ("success", "skipped"):
            self.assertFalse(purge.mesures_gardees({"event": "schedule"}, _jobs(conclusion)),
                             conclusion)

    def test_une_part_coupee_ne_garde_rien(self):
        # run 40177589953 : deux parts au plafond (cancelled), carte en échec
        self.assertFalse(purge.mesures_gardees({"event": "schedule"},
                                               _jobs("failure", mesure="cancelled")))
        self.assertFalse(purge.mesures_gardees({"event": "pull_request"},
                                               _jobs("cancelled")))


class MenageApresLeRunTest(unittest.TestCase):
    """N1 : le ménage ne dépend plus de la création du job après annulation."""

    def test_declenche_apres_la_conclusion_de_carte(self):
        corps = _sans_commentaires(MENAGE)
        self.assertRegex(corps, r"on:\n  workflow_run:\n    workflows: \[\"carte\"\]\n"
                                r"    types: \[completed\]\n")
        self.assertIn('name: carte\n', CARTE)
        # aucun autre déclencheur, aucun filtre sur la conclusion : un run
        # annulé, vert ou rouge passe par le même ménage
        self.assertNotIn("pull_request", corps)
        self.assertNotIn("if:", corps)

    def test_code_de_confiance_seulement(self):
        corps = _sans_commentaires(MENAGE)
        # checkout de la branche par défaut : ni ref ni dépôt de la PR
        self.assertIn("- uses: actions/checkout@v4\n", corps)
        self.assertNotIn("ref:", corps)
        self.assertNotIn("repository:", corps)
        self.assertNotIn("head_sha", corps)
        self.assertNotIn("head_branch", corps)

    def test_droits_minimaux_hors_du_pool(self):
        corps = _sans_commentaires(MENAGE)
        self.assertRegex(corps, r"\npermissions:\n  contents: read\n")
        self.assertIn("      actions: write", corps)
        self.assertNotIn("checks: write", corps)
        self.assertNotIn("contents: write", corps)
        self.assertIn("runs-on: ubuntu-latest", corps)
        self.assertRegex(corps, r"timeout-minutes: [1-5]\n")

    def test_la_regle_est_celle_du_script_pour_ce_seul_run(self):
        corps = _sans_commentaires(MENAGE)
        self.assertIn("python3 scripts/purge-artefacts.py", corps)
        self.assertIn('--run "${{ github.event.workflow_run.id }}"', corps)
        self.assertIn("--appliquer", corps)
        self.assertIn("group: menage-mesures-${{ github.event.workflow_run.id }}", corps)
        self.assertIn("cancel-in-progress: false", corps)


class PurgeDUnRunTest(unittest.TestCase):
    """`--run` : les seuls artefacts du run, sans relevé des runs en cours,
    et la relecture du statut avant chaque suppression."""

    def _main(self, run, jobs, artefacts):
        appels = []

        def gh(*args, essais=3):
            url = next(a for a in args if a.startswith("repos/"))
            appels.append((args[0] if args[0] == "-X" else "GET", url))
            if args[0] == "-X":
                return ""
            if url.endswith("/artifacts?per_page=100"):
                return "\n".join(purge.json.dumps(a) for a in artefacts)
            if "/jobs" in url:
                return "\n".join(purge.json.dumps(j) for j in jobs)
            return purge.json.dumps(run)

        with mock.patch.object(purge, "_gh", gh), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = purge.main(["--repo", "o/r", "--run", "42", "--appliquer"])
        return code, appels

    def _artefacts(self):
        maintenant = purge.dt.datetime.now(purge.dt.timezone.utc)
        cree = (maintenant - purge.dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [{"id": i, "name": nom, "expired": False, "size_in_bytes": 10,
                 "created_at": cree, "workflow_run": {"id": 42}}
                for i, nom in ((1, "mesure-psql-1"), (2, "mesure-psycopg-3"),
                               (3, "plan-mesure"), (4, "carte"))]

    def test_supprime_les_mesures_d_un_run_annule(self):
        run = {"event": "pull_request", "status": "completed", "run_attempt": 1}
        code, appels = self._main(run, _jobs("cancelled", mesure="cancelled"),
                                  self._artefacts())
        self.assertEqual(code, 0)
        lectures = [u for m, u in appels if m == "GET"]
        self.assertIn("repos/o/r/actions/runs/42/artifacts?per_page=100", lectures)
        self.assertFalse(any("actions/artifacts?" in u or "status=" in u for u in lectures))
        self.assertEqual(sorted(u for m, u in appels if m == "-X"),
                         ["repos/o/r/actions/artifacts/1", "repos/o/r/actions/artifacts/2"])

    def test_garde_les_mesures_d_une_fusion_coupee(self):
        run = {"event": "schedule", "status": "completed", "run_attempt": 1}
        _, appels = self._main(run, _jobs("cancelled"), self._artefacts())
        self.assertEqual([u for m, u in appels if m == "-X"], [])

    def test_un_run_relance_garde_tout(self):
        run = {"event": "push", "status": "in_progress", "run_attempt": 2}
        _, appels = self._main(run, _jobs("success"), self._artefacts())
        self.assertEqual([u for m, u in appels if m == "-X"], [])


if __name__ == "__main__":
    unittest.main()
