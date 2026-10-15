# SPDX-License-Identifier: AGPL-3.0-only
"""#1410 — une tête de paquet ne lance qu'une seule suite complète.

Une poussée sur `integration/**` déclenchait le run `push` de tests.yml, et la
PR ouverte sur la même tête son run `pull_request` : deux suites complètes,
deux checks `tests` sur le même commit. Annuler l'un laissait l'agrégat rouge
de l'autre (incident #1342 : run push 38653682693 annulé, PR #1050 bloquée par
son `tests` en échec).

Le correctif est un delta de déclenchement (§12) : tests.yml ne se déclenche
plus sur `push integration/**` ; le portier `.github/workflows/paquet.yml`, lui
déclenché par ces poussées, lit les PR ouvertes (`scripts/test-plan.py
couverture`) et n'appelle la suite (`workflow_call`) que si aucune ne porte la
tête poussée exactement. La décision ne lit jamais un run : aucune réussite
n'est déduite de l'existence d'un autre run, le repli est « la suite tourne ».

Sans base ni réseau : décision pure, lecture d'API simulée, lecture des
workflows.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("test_plan_1410", REPO / "scripts" / "test-plan.py")
plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan)

WORKFLOWS = REPO / ".github" / "workflows"
TESTS = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
PAQUET = (WORKFLOWS / "paquet.yml").read_text(encoding="utf-8")

SHA = "a" * 40
AUTRE = "b" * 40


def _pr(number=1, head=SHA, base="main", state="open"):
    return {"number": number, "state": state,
            "head": {"sha": head, "ref": "integration/paquet-1"},
            "base": {"ref": base}}


def _sans_commentaires(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _bloc_on(text: str) -> str:
    """Les lignes du bloc `on:` (indentées), commentaires compris."""
    lignes = text.splitlines()
    debut = next(i for i, l in enumerate(lignes) if l.startswith("on:"))
    corps = []
    for ligne in lignes[debut + 1:]:
        if ligne.strip() and not ligne[0].isspace():
            break
        corps.append(ligne)
    return "\n".join(corps)


def _bloc_job(text: str, nom: str) -> str:
    """Le bloc du job `nom` (indentation de deux espaces)."""
    lignes = text.splitlines()
    debut = next(i for i, l in enumerate(lignes) if l.startswith("  %s:" % nom))
    fin = next((i for i in range(debut + 1, len(lignes))
                if lignes[i].startswith("  ") and not lignes[i].startswith("    ")),
               len(lignes))
    return "\n".join(lignes[debut:fin])


class Reponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CouvertureDecisionTest(unittest.TestCase):
    """La décision ne regarde que des PR : jamais un run."""

    def test_une_pr_ouverte_sur_la_tete_couvre(self):
        out = plan.couverture_decision([_pr(7)], SHA)
        self.assertTrue(out["covered"])
        self.assertEqual(out["pr"], 7)
        self.assertIn("couvre la tête", out["why"])

    def test_une_autre_tete_ne_couvre_pas(self):
        # nouvelle tête : la décision se reprend sur le sha exact
        out = plan.couverture_decision([_pr(7, head=AUTRE)], SHA)
        self.assertFalse(out["covered"])
        self.assertIn("aucune sur cette tête exacte", out["why"])

    def test_aucune_pr_ne_couvre_pas(self):
        out = plan.couverture_decision([], SHA)
        self.assertFalse(out["covered"])
        self.assertIn("aucune PR ouverte", out["why"])

    def test_une_pr_fermee_ne_couvre_pas(self):
        out = plan.couverture_decision([_pr(7, state="closed")], SHA)
        self.assertFalse(out["covered"])

    def test_les_bases_du_perimetre_couvrent(self):
        for base in ("main", "release/1.10", "integration/paquet-2"):
            out = plan.couverture_decision([_pr(7, base=base)], SHA)
            self.assertTrue(out["covered"], base)

    def test_une_base_hors_perimetre_ne_couvre_pas(self):
        # une PR dont la base ne déclenche pas tests.yml ne porte pas la suite
        for base in ("lot/xyz", "develop", ""):
            out = plan.couverture_decision([_pr(7, base=base)], SHA)
            self.assertFalse(out["covered"], base)
            self.assertIn("ne déclenche pas tests.yml", out["why"])

    def test_la_premiere_pr_du_perimetre_gagne(self):
        pulls = [_pr(1, head=AUTRE), _pr(2, base="lot/xyz"), _pr(3)]
        out = plan.couverture_decision(pulls, SHA)
        self.assertTrue(out["covered"])
        self.assertEqual(out["pr"], 3)


class PullsOpenTest(unittest.TestCase):

    def test_filtre_entetes_et_lecture(self):
        vus = []

        def opener(request, timeout=None):
            vus.append((request, timeout))
            return Reponse(json.dumps([_pr(7)]).encode("utf-8"))

        pulls = plan.pulls_open("manaty/ameesh", "integration/paquet-1",
                                "https://api.github.com", "jeton", opener=opener)
        self.assertEqual(pulls, [_pr(7)])
        request, timeout = vus[0]
        parsed = urllib.parse.urlparse(request.full_url)
        self.assertEqual(parsed.path, "/repos/manaty/ameesh/pulls")
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["state"], ["open"])
        self.assertEqual(query["head"], ["manaty:integration/paquet-1"])
        self.assertEqual(query["per_page"], ["100"])
        self.assertEqual(request.get_header("Authorization"), "Bearer jeton")
        self.assertEqual(timeout, 20)

    def test_un_payload_non_liste_est_une_erreur(self):
        def opener(request, timeout=None):
            return Reponse(b'{"message": "Not Found"}')

        with self.assertRaises(ValueError):
            plan.pulls_open("manaty/ameesh", "integration/paquet-1",
                            "https://api.github.com", "jeton", opener=opener)


class CouvertureCommandeTest(unittest.TestCase):
    """Le portier : repli sûr et sorties `covered`/`covered_pr`, toujours 0."""

    def _run(self, pulls=None, erreur=None, ref="refs/heads/integration/paquet-1",
             sha=SHA, repo="manaty/ameesh"):
        appels = []

        def faux(nom_repo, branche, api, token, opener=None):
            appels.append((nom_repo, branche, api, token))
            if erreur is not None:
                raise erreur
            return pulls if pulls is not None else []

        with tempfile.TemporaryDirectory() as tmp:
            sortie = pathlib.Path(tmp) / "github-output"
            resume = pathlib.Path(tmp) / "summary"
            with mock.patch.object(plan, "pulls_open", faux), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = plan.main(["couverture", "--repo", repo, "--ref", ref, "--sha", sha,
                                  "--github-output", str(sortie), "--summary", str(resume)])
            return code, sortie.read_text(encoding="utf-8"), resume.read_text(encoding="utf-8"), appels

    def test_pr_ouverte_sur_la_tete(self):
        code, sortie, resume, appels = self._run(pulls=[_pr(7)])
        self.assertEqual(code, 0)
        self.assertIn("covered=true", sortie)
        self.assertIn("covered_pr=7", sortie)
        self.assertIn("couverte", resume)
        self.assertEqual(appels[0][1], "integration/paquet-1")

    def test_tete_non_couverte(self):
        code, sortie, _, _ = self._run(pulls=[_pr(7, head=AUTRE)])
        self.assertEqual(code, 0)
        self.assertIn("covered=false", sortie)
        self.assertIn("covered_pr=\n", sortie)

    def test_api_illisible_replie_sur_la_suite(self):
        # une API illisible fait tourner la suite, jamais l'inverse
        code, sortie, resume, _ = self._run(erreur=urllib.error.URLError("boom"))
        self.assertEqual(code, 0)
        self.assertIn("covered=false", sortie)
        self.assertIn("la suite tourne", resume)

    def test_contexte_incomplet_replie_sur_la_suite(self):
        code, sortie, _, appels = self._run(repo="")
        self.assertEqual(code, 0)
        self.assertIn("covered=false", sortie)
        self.assertEqual(appels, [], "aucune lecture d'API sans contexte")

    def test_ref_hors_paquet_ne_lit_rien(self):
        code, sortie, _, appels = self._run(ref="refs/heads/main")
        self.assertEqual(code, 0)
        self.assertIn("covered=false", sortie)
        self.assertEqual(appels, [], "aucune lecture d'API hors integration/**")


class DeclenchementTest(unittest.TestCase):
    """Le delta de déclenchement, lu dans les deux workflows."""

    def test_tests_yml_ne_part_plus_sur_une_poussee_de_paquet(self):
        on = _sans_commentaires(_bloc_on(TESTS))
        self.assertNotIn("integration/**", on,
                         "une poussée de paquet ne doit plus déclencher tests.yml")
        self.assertIn('branches: [main, "release/**"]', on)

    def test_tests_yml_reste_appelable_et_requis(self):
        on = _sans_commentaires(_bloc_on(TESTS))
        self.assertIn("workflow_call:", on)
        self.assertIn("workflow_dispatch:", on)
        self.assertIn("pull_request:", on)
        # le check requis garde son job d'agrégat, nommé `tests`
        self.assertRegex(TESTS, r"(?m)^  tests:")

    def test_paquet_yml_declenche_la_suite_conditionnellement(self):
        on = _sans_commentaires(_bloc_on(PAQUET))
        self.assertIn("push:", on)
        self.assertIn('branches: ["integration/**"]', on)
        suite = _bloc_job(PAQUET, "suite")
        self.assertIn("uses: ./.github/workflows/tests.yml", suite)
        self.assertIn("needs: couverture", suite)
        self.assertIn("if: ${{ !cancelled() && needs.couverture.outputs.covered != 'true' }}",
                      suite,
                      "portier en échec : sa sortie vide fait tourner la suite ; "
                      "portier annulé (poussée plus récente) : la suite ne part pas")
        self.assertNotIn("if: always()", suite,
                         "#1429 : une annulation ne doit pas dispatcher de suite")

    def test_le_portier_decide_sur_les_pr_et_jamais_sur_un_run(self):
        gate = _bloc_job(PAQUET, "couverture")
        self.assertIn("scripts/test-plan.py couverture", gate)
        self.assertIn("pull-requests: read", gate)
        self.assertNotIn("checks: write", gate, "le portier n'écrit aucun check")
        self.assertNotIn("actions/runs", PAQUET)
        self.assertNotIn("gh run", PAQUET)

    def test_le_portier_ne_porte_jamais_le_check_requis(self):
        # le check du portier n'est jamais nommé `tests` : aucune validation
        # ne peut être déduite de l'existence d'un autre run.
        self.assertNotRegex(PAQUET, r"(?m)^  tests:")
        self.assertIn("name: couverture (PR ouverte sur cette tête)", PAQUET)

    def test_le_portier_est_borne_et_concurrence(self):
        # canon §3 : chaque job de CI porte une durée maximale
        self.assertIn("timeout-minutes: 5", _bloc_job(PAQUET, "couverture"))
        self.assertIn("cancel-in-progress: true", PAQUET)
        self.assertIn("group: paquet-${{ github.ref }}", PAQUET)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
