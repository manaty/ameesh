# SPDX-License-Identifier: AGPL-3.0-only
"""L940 : le check requis `tests` rougit quand une part est en échec ou annulée.

Sous `bash -e`, `test A || { test B && test C; }` ne fait pas sortir le script
quand A est faux : l'échec est dans une liste `&&`, exemptée de `-e`, le groupe
rend 1 et le script continue. Le job `tests` — seul check exigé par la
protection de main — concluait donc SUCCESS avec une part psql en échec ou
annulée (production : run 38357567337, job tests 115330451235, log
« psql : cancelled (parts [1]) », SUCCESS).

Ce contrat rejoue le script exact du job, substitué pour chaque scénario, sous
`bash -e` : aucun accès base, aucune suite.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
_STEP = "      - name: bilan des parts\n        run: |\n"
_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def job_script() -> str:
    """Le `run` de l'étape « bilan des parts » du job `tests`, dé-indenté."""
    text = WORKFLOW.read_text(encoding="utf-8")
    try:
        start = text.index(_STEP) + len(_STEP)
    except ValueError:  # pragma: no cover - dépend du fichier
        raise AssertionError("étape « bilan des parts » introuvable dans tests.yml")
    lines = []
    for line in text[start:].splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:] if line.startswith("          ") else "")
    script = "\n".join(lines) + "\n"
    assert "needs.psql.result" in script, "script extrait vide ou déplacé"
    return script


def scenario(**over: str) -> str:
    """Le script avec chaque expression remplacée ; refuse une expression inconnue."""
    values = {
        "needs.plan.result": "success",
        "needs.plan.outputs.version_only": "false",
        "needs.plan.outputs.docs_only": "false",
        "needs.plan.outputs.psycopg": "[1, 2]",
        "needs.plan.outputs.psql": "[1]",
        "needs.doc-readers.result": "success",
        "needs.psycopg.result": "success",
        "needs.psql.result": "success",
        "needs.platform-macos.result": "success",
        "needs.version.result": "success",
        "github.event_name": "pull_request",
    }
    values.update(over)

    def repl(match):
        expr = match.group(1)
        if expr not in values:
            raise AssertionError("expression non couverte par le contrat : %s" % expr)
        return values[expr]

    return _EXPR.sub(repl, job_script())


def run_script(script: str) -> subprocess.CompletedProcess:
    """Le script exact sous `bash -e` (le shell des étapes GitHub)."""
    return subprocess.run(["bash", "-e", "-c", script], cwd=REPO,
                          capture_output=True, text=True)


class AggregateGuardTest(unittest.TestCase):

    def test_le_contrat_couvre_toutes_les_expressions(self):
        # scenario() lève si une expression du workflow n'a pas de valeur
        self.assertIn("psql    : success", scenario())

    def test_vert_quand_tout_reussit(self):
        self.assertEqual(run_script(scenario()).returncode, 0)

    def test_psql_en_echec_rougit_le_job(self):
        done = run_script(scenario(**{"needs.psql.result": "failure"}))
        self.assertNotEqual(done.returncode, 0, done.stdout)

    def test_psql_annule_rougit_le_job(self):
        done = run_script(scenario(**{"needs.psql.result": "cancelled"}))
        self.assertNotEqual(done.returncode, 0, done.stdout)

    def test_psql_saute_sans_part_reste_legitime(self):
        done = run_script(scenario(**{"needs.psql.result": "skipped",
                                      "needs.plan.outputs.psql": "[]"}))
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_psql_saute_avec_des_parts_rougit_le_job(self):
        done = run_script(scenario(**{"needs.psql.result": "skipped"}))
        self.assertNotEqual(done.returncode, 0, done.stdout)

    def test_psycopg_en_echec_rougit_le_job(self):
        done = run_script(scenario(**{"needs.psycopg.result": "failure"}))
        self.assertNotEqual(done.returncode, 0, done.stdout)

    def test_version_sautee_sur_une_pr_rougit_le_job(self):
        done = run_script(scenario(**{"needs.version.result": "skipped"}))
        self.assertNotEqual(done.returncode, 0, done.stdout)

    def test_version_sautee_sur_main_reste_legitime(self):
        done = run_script(scenario(**{"needs.version.result": "skipped",
                                      "github.event_name": "push"}))
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_version_en_echec_rougit_le_job(self):
        done = run_script(scenario(**{"needs.version.result": "failure"}))
        self.assertNotEqual(done.returncode, 0, done.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
