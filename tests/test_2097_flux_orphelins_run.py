# SPDX-License-Identifier: AGPL-3.0-only
"""#2097 — un run annulé avant l'agrégat ne laisse plus de check de flux ouvert.

Le balayage #856 vit dans le job agrégé `tests` de tests.yml. GitHub ne
démarre jamais un job encore en file quand l'exécution est annulée : `always()`
n'y change rien. Un run annulé avant l'agrégat — une poussée remplacée, une
annulation à la main — laissait donc les checks « tests (flux) <part> » dont
l'étape `close` n'avait pas tourné `in_progress` pour toujours (run
38917093257, tête 7c0b34b195b9 : deux checks sur cinq encore en cours) ;
aucun jeton utilisateur ne peut les conclure (403 « You must authenticate via
a GitHub App. » : ils appartiennent à l'application GitHub Actions), et le
relevé d'exploitation les lit comme une CI encore active.

Le correctif est une garde `workflow_run`
(.github/workflows/flux-orphelins.yml) qui rejoue le même balayage après la
conclusion du run, sur la tête du run terminé. Elle couvre les deux porteurs
de la suite : `tests` (push main ou release, dispatch, PR) et `paquet`
(workflow_call du portier #1410, dont l'exécution ne produit aucun événement
`tests`). Le balayage est celui de `scripts/ci-flow.py sweep` (#856) : jamais
`success` sans ligne `fin`, aucun check dont le job tourne encore ni d'une
autre tête, et il ne touche aucun check requis — la porte de fusion est
inchangée.

Sans base ni réseau : lecture du workflow et de son contrat, et le balayage
réel sur une API simulée.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
GARDE = WORKFLOWS / "flux-orphelins.yml"
TESTS = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
PAQUET = (WORKFLOWS / "paquet.yml").read_text(encoding="utf-8")


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ciflux = _charger("ci_flux_2097", "ci-flow.py")

#: la tête du run de l'incident (#2097, run 38917093257)
TETE = "7c0b34b195b9" + "0" * 28
RUN = 38917093257
CHECK_PSQL = 117027526121
CHECK_PSYCOPG = 117027588023
RUN_VIVANT = 38919999999
CHECK_AUTRE = 117027000000


class FauxGitHub:
    """L'API simulée : les checks d'une tête, l'état d'une exécution et ses jobs."""

    def __init__(self, checks, executions):
        self._checks = list(checks)
        #: {exécution: (exécution lue, jobs lus)}
        self._executions = executions
        self.maj: list[tuple[int, str]] = []

    def ready(self) -> bool:
        return True

    def check_runs(self, tete: str) -> list[dict]:
        return self._checks

    def run(self, execution: int) -> dict:
        return self._executions[execution][0]

    def jobs(self, execution: int) -> list[dict]:
        return self._executions[execution][1]

    # l'`external_id` #1393 nomme la tentative : le balayage relit l'exécution
    # sur `…/attempts/<n>`.
    def run_attempt(self, execution: int, tentative: int) -> dict:
        return self.run(execution)

    def jobs_attempt(self, execution: int, tentative: int) -> list[dict]:
        return self.jobs(execution)

    def update(self, ident: int, sortie: dict, conclusion: str) -> None:
        self.maj.append((ident, conclusion))


def flux(ident: int, nom: str, execution: int, statut: str = "in_progress") -> dict:
    return {"id": ident, "name": "tests (flux) %s" % nom, "status": statut,
            "external_id": "ameesh-ci-flow/%d/1" % execution,
            "output": {"title": "en cours", "summary": "Part %s : en cours." % nom}}


def job(nom: str, statut: str = "completed", conclusion: str = "cancelled") -> dict:
    return {"name": nom, "status": statut, "conclusion": conclusion}


class GardeFluxOrphelinsTest(unittest.TestCase):
    """Le contrat du fichier : un `workflow_run` après toute suite non verte."""

    def setUp(self):
        self.texte = GARDE.read_text(encoding="utf-8")

    def test_declenchee_apres_la_conclusion_des_deux_porteurs(self):
        # `tests` : push main ou release, dispatch, PR. `paquet` :
        # `workflow_call` du portier #1410 — l'exécution appelée ne produit
        # aucun événement `tests`, donc sans lui un paquet annulé garderait
        # ses orphelins.
        self.assertIn("workflow_run:", self.texte)
        self.assertIn('workflows: ["tests", "paquet"]', self.texte)
        self.assertIn("types: [completed]", self.texte)
        # les noms cités sont ceux des workflows : un renommage détacherait la
        # garde en silence.
        self.assertIn("name: tests", TESTS)
        self.assertIn("name: paquet", PAQUET)

    def test_balayage_de_la_tete_du_run_termine(self):
        self.assertIn("ci-flow.py sweep", self.texte)
        self.assertIn('--sha "${{ github.event.workflow_run.head_sha }}"', self.texte)
        # `GITHUB_SHA` d'un événement `workflow_run` est la branche par
        # défaut ; il ne doit jamais servir de tête à réconcilier.
        self.assertNotIn('--sha "${{ github.sha }}"', self.texte)
        self.assertNotIn("AMEESH_CI_TETE", self.texte)

    def test_ne_repasse_que_sur_une_conclusion_non_verte(self):
        # un run vert a déjà balayé dans son agrégat (`if: always()`) ;
        # `cancelled` (agrégat jamais démarré) et `failure`/`timed_out`
        # (balayage interrompu) peuvent laisser un orphelin.
        self.assertIn("github.event.workflow_run.conclusion != 'success'", self.texte)

    def test_garde_bornee(self):
        # canon §3 : chaque job porte son plafond, et le balayage est borné
        # sous ce plafond (un balayage lent s'arrête entre deux checks).
        self.assertIn("timeout-minutes:", self.texte)
        self.assertIn("timeout 480", self.texte)
        self.assertIn("--max-duration 420", self.texte)

    def test_hors_flotte_et_sur_le_code_de_la_branche_par_defaut(self):
        # la flotte saturée (#747) est la cause des orphelins : une garde qui
        # attendrait un runner de la flotte ne s'exécuterait pas. Le fichier
        # vit sur la branche par défaut ; `actions/checkout` y prend son code,
        # jamais celui de la PR (jeton `checks: write`, code de confiance).
        self.assertIn("runs-on: ubuntu-latest", self.texte)
        self.assertIn("actions/checkout@v4", self.texte)

    def test_jeton_minimal_et_aucune_publication(self):
        self.assertIn("checks: write", self.texte)
        self.assertIn("actions: read", self.texte)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.texte)
        # la garde ne fait que conclure : ni publisher ni close, et aucun
        # check nommé à la main (le check requis s'appelle `tests`).
        self.assertNotIn("ci-flow.py publish", self.texte)
        self.assertNotIn("ci-flow.py close", self.texte)
        self.assertNotIn("--name", self.texte)

    def test_gardes_d_une_meme_execution_serialisees(self):
        # un événement `workflow_run` peut être rejoué à la main (#1002) : les
        # gardes d'une même exécution ne se croisent pas, et aucune n'est
        # annulée par une autre.
        self.assertIn("group: flux-orphelins-${{ github.event.workflow_run.id }}",
                      self.texte)
        self.assertIn("cancel-in-progress: false", self.texte)

    def test_l_agregat_garde_son_propre_balayage(self):
        # la garde complète le balayage de l'agrégat, elle ne le remplace pas.
        self.assertIn("ci-flow.py sweep", TESTS)


class IncidentTest(unittest.TestCase):
    """Le scénario de l'incident : l'agrégat jamais démarré, le run terminal.

    Le balayage de l'agrégat ne tourne pas (job en file, annulé avec le run) ;
    c'est la garde d'après-run qui conclut, sur la tête du run terminé. Un
    check d'une exécution encore active n'est jamais touché."""

    def test_run_annule_avant_l_agregat_conclut_les_orphelins(self):
        faux = FauxGitHub(
            checks=[flux(CHECK_PSQL, "psql 2/3", RUN),
                    flux(CHECK_PSYCOPG, "psycopg 1/4", RUN),
                    flux(CHECK_AUTRE, "psql 1/6", RUN_VIVANT)],
            executions={
                RUN: ({"id": RUN, "status": "completed"},
                      [job("psql 2/3"), job("psycopg 1/4")]),
                RUN_VIVANT: ({"id": RUN_VIVANT, "status": "in_progress"},
                             [job("psql 1/6", "in_progress", None)]),
            })
        ciflux.sweep(argparse.Namespace(sha=TETE, max_duration=100), gh=faux)
        self.assertEqual([(CHECK_PSQL, "cancelled"), (CHECK_PSYCOPG, "cancelled")],
                         faux.maj)


if __name__ == "__main__":
    unittest.main()
