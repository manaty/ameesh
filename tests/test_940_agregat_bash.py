# SPDX-License-Identifier: AGPL-3.0-only
"""L940/#3251/#3449 : le check requis `tests` ne verdit jamais muet.

#940 (13/10/2026) : une part psql annulée concluait `tests` SUCCESS sans
qu'aucun test n'ait tourné — un vert muet, fusionné. La garde bash d'alors
exigeait `success` de chaque part, ce qui a rendu rouge toute annulation
interne.

#3251 (décision owner #128512, 23/10, option a) déplace la frontière : un
non-concluant **à l'intérieur** d'un agrégat qui a conclu n'est pas bloquant
(canon §3), mais l'agrégat doit le dire. #3449 la borne : les familles Linux
planifiées (`psycopg`, `psql`) sont la preuve ; leur annulation est une
preuve absente, jamais un vert. L'étape « bilan des parts » appelle
`scripts/ci-agregat.py`, qui :

* rougit `tests` sur un **échec** prouvé, une part sautée qui devait tourner,
  un plan refusé ou une **famille Linux planifiée annulée/absente**
  (inchangé pour l'échec, #940 ; #3449 pour la preuve absente) ;
* laisse `tests` vert sur l'**annulation d'une part non requise** (macOS,
  autre système de fichiers) et publie le check de détail
  `tests (bilan des parts)` en `neutral`, que fire/radar consignent ;
* échoue si une annulation non requise ne peut pas être consignée (jeton en
  lecture seule, API en panne) : jamais de SUCCESS muet.

Ce contrat vérifie l'étape du workflow (chaque résultat passé au classifieur,
plus aucune garde bash) et rejoue les issues qui font #940 : l'échec réel
rougit, l'annulation non requise ne verdit pas sans consignation.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
_STEP = "      - name: bilan des parts\n"
_RUN = "        run: |\n"
_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _charger():
    spec = importlib.util.spec_from_file_location(
        "ci_agregat_l940", REPO / "scripts" / "ci-agregat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agregat = _charger()


def step_block() -> str:
    """Le texte exact de l'étape « bilan des parts » (nom, env, run)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    try:
        start = text.index(_STEP)
    except ValueError:  # pragma: no cover - dépend du fichier
        raise AssertionError("étape « bilan des parts » introuvable dans tests.yml")
    end = text.index("\n      - ", start + len(_STEP)) + 1
    return text[start:end]


def job_script() -> str:
    """Le `run` de l'étape, dé-indenté (les `env:` éventuels sont sautés)."""
    block = step_block()
    try:
        start = block.index(_RUN) + len(_RUN)
    except ValueError:  # pragma: no cover - dépend du fichier
        raise AssertionError("bloc `run: |` introuvable dans l'étape « bilan des parts »")
    lines = []
    for line in block[start:].splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:] if line.startswith("          ") else "")
    script = "\n".join(lines) + "\n"
    assert "ci-agregat.py" in script, "script extrait vide ou déplacé"
    return script


class WorkflowContractTest(unittest.TestCase):
    """L'étape passe les résultats au classifieur, sans garde bash parallèle."""

    #: chaque résultat lu par le classifieur, et l'argument qui le porte
    ARGS = {
        "github.event_name": "--event",
        "github.run_id": "--run-id",
        "needs.plan.result": "--plan",
        "needs.version.result": "--version",
        "needs.plan.outputs.version_only": "--version-only",
        "needs.plan.outputs.docs_only": "--docs-only",
        "needs.doc-readers.result": "--doc-readers",
        "needs.psycopg.result": "--psycopg",
        "needs.plan.outputs.psycopg": "--psycopg-parts",
        "needs.psql.result": "--psql",
        "needs.plan.outputs.psql": "--psql-parts",
        "inputs.psql-full": "--psql-full",
        "needs.platform-macos.result": "--plateforme-macos",
        "needs.fs-class.result": "--autre-fs",
    }

    #: les sorties de plan (JSON) sont citées en simple, le reste en double
    SINGLE = {"needs.plan.outputs.psycopg", "needs.plan.outputs.psql"}

    def test_chaque_resultat_est_passe_au_classifieur(self):
        script = job_script()
        for expr, flag in self.ARGS.items():
            quote = "'" if expr in self.SINGLE else '"'
            with self.subTest(expr=expr):
                self.assertIn("%s %s${{ %s }}%s" % (flag, quote, expr, quote), script)

    def test_l_etape_publie_le_detail(self):
        script = job_script()
        self.assertIn("--publish", script)
        self.assertIn("GH_TOKEN", step_block())

    def test_plus_aucune_garde_bash_ne_classe_les_parts(self):
        # #940 : les gardes `test ... = success || { ... }` sous bash -e
        # avalaient l'échec de la branche gauche du `&&`. Le classifieur les
        # remplace : aucune comparaison de résultat de job ne reste en shell.
        script = job_script()
        for expr in self.ARGS:
            if expr.startswith("needs."):
                self.assertNotRegex(script, r'test\s+"\$?\{\{\s*%s\s*\}\}"'
                                    % re.escape(expr))
        self.assertNotIn("|| {", script)

    def test_chaque_expression_du_script_est_couverte(self):
        seen = set(_EXPR.findall(job_script()))
        self.assertEqual(seen - set(self.ARGS), set())

    def test_le_script_du_bilan_est_present_avant_l_etape(self):
        # #3251 : le bilan exécute `scripts/ci-agregat.py`, donc le dépôt doit
        # être extrait avant l'étape. Sans ce checkout, un runner neuf n'a pas
        # le script : l'agrégat requis rougit sur tout run.
        text = WORKFLOW.read_text(encoding="utf-8")
        job = text[text.index("\n  tests:\n"):]
        before = job[:job.index(_STEP)]
        self.assertIn("- uses: actions/checkout@v4", before)

    def test_un_run_annule_rougit_avant_le_bilan(self):
        # Décision #128512 : un run annulé n'a pas conclu du tout. L'étape
        # `if: cancelled()` échoue explicitement avant le bilan, sinon un run
        # annulé pourrait publier un `tests` vert à partir des parts déjà
        # conclues.
        text = WORKFLOW.read_text(encoding="utf-8")
        job = text[text.index("\n  tests:\n"):]
        start = job.index("- name: run annulé")
        self.assertLess(start, job.index(_STEP))
        block = job[start:job.index("\n      - ", start)]
        self.assertIn("if: cancelled()", block)
        self.assertIn("exit 1", block)


class SilentSuccessTest(unittest.TestCase):
    """#940 : une annulation ne verdit jamais sans consignation publiée."""

    class FakeGh:
        def __init__(self, fail=False):
            self.fail, self.created = fail, []

        def create(self, name, conclusion, title, summary):
            if self.fail:
                raise agregat.AgregatError("jeton en lecture seule")
            self.created.append((name, conclusion))
            return 1

    def run_main(self, rules, gh=None, jobs=None) -> int:
        args = ["--event", "pull_request", "--plan", "success",
                "--version", "success", "--version-only", "false",
                "--docs-only", "false", "--doc-readers", "skipped",
                "--psycopg", "success", "--psycopg-parts", "[1]",
                "--publish"]
        args += rules
        # Les tests qui n'exercent pas le comptage des jobs n'appellent pas
        # l'API : une lecture vide est un repli valide (`jobs=[]`), et évite
        # de dépendre de `GITHUB_RUN_ID` quand la suite tourne dans Actions.
        return agregat.main(args, gh=gh, jobs=[] if jobs is None else jobs)

    def test_annulation_non_requise_consignee_publie_neutral_et_ne_rougit_pas(self):
        gh = self.FakeGh()
        self.assertEqual(self.run_main(["--psql", "success", "--psql-parts", "[1]",
                                        "--plateforme-macos", "cancelled"],
                                       gh=gh), 0)
        self.assertEqual(gh.created, [(agregat.DETAIL_CHECK, "neutral")])

    def test_toutes_les_parts_annulees_rougissent_et_publient_failure(self):
        # #128512 : aucune part réussie = preuve absente. Le check requis
        # rougit, même si la consignation est possible ; le détail le dit.
        gh = self.FakeGh()
        self.assertEqual(self.run_main(
            ["--psycopg", "cancelled", "--psql", "cancelled",
             "--psql-parts", "[1]"], gh=gh,
            jobs=[{"name": "psycopg 1/1", "conclusion": "cancelled"},
                  {"name": "psql 1/1", "conclusion": "cancelled"}]), 1)
        self.assertEqual(gh.created, [(agregat.DETAIL_CHECK, "failure")])

    def test_annulation_non_requise_non_consignable_rougit(self):
        # #940 : on ne peut pas verdir quand l'annulation ne peut pas être
        # consignée. Une PR de fourche sans `checks: write` reste rouge.
        self.assertEqual(self.run_main(["--psql", "success", "--psql-parts", "[1]",
                                        "--plateforme-macos", "cancelled"],
                                       gh=self.FakeGh(fail=True)), 2)

    def test_annulation_d_une_famille_linux_rougit_et_publie_failure(self):
        # #3449 : macOS vert ne rachète pas 0/1 psql réussie.
        gh = self.FakeGh()
        self.assertEqual(self.run_main(
            ["--psql", "cancelled", "--psql-parts", "[1]",
             "--plateforme-macos", "success"], gh=gh,
            jobs=[{"name": "psql 1/1", "conclusion": "cancelled"}]), 1)
        self.assertEqual(gh.created, [(agregat.DETAIL_CHECK, "failure")])

    def test_echec_reel_rougit_et_publie_failure(self):
        gh = self.FakeGh()
        self.assertEqual(self.run_main(["--psql", "failure", "--psql-parts", "[1]"],
                                       gh=gh), 1)
        self.assertEqual(gh.created, [(agregat.DETAIL_CHECK, "failure")])

    def test_vert_complet_publie_success(self):
        gh = self.FakeGh()
        self.assertEqual(self.run_main(["--psql", "success", "--psql-parts", "[1]"],
                                       gh=gh), 0)
        self.assertEqual(gh.created, [(agregat.DETAIL_CHECK, "success")])

    def test_vert_sans_publication_reste_vert(self):
        # Une PR de fourche n'a pas `checks: write` : sans annulation, rien
        # ne doit être consigné, la publication en échec ne rougit pas.
        self.assertEqual(self.run_main(["--psql", "success", "--psql-parts", "[1]"],
                                       gh=self.FakeGh(fail=True)), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
