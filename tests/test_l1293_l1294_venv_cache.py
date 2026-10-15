# SPDX-License-Identifier: AGPL-3.0-only
"""#1293/#1294 : le venv de CI invalide est resauvé après reconstruction, et la
clé de cache distingue les runners hébergés des auto-hébergés.

Sans base : lecture du workflow `.github/workflows/tests.yml`.

#1293 — sur un cache-hit exact d'un venv devenu invalide, `actions/cache` ne
resauve pas (clé identique) : le venv était reconstruit à chaque exécution sur
ce runner. La restauration passe désormais par des clés partielles
(`restore-keys`), la clé primaire étant celle du run ; un venv restauré
invalide est reconstruit **puis resauvé** sous cette clé neuve, que l'exécution
suivante retrouve par `restore-keys`. Le sauvetage est conditionné à la
reconstruction : une restauration valide n'écrit rien.

#1294 — `runner.name` vaut « GitHub Actions N » sur un runner hébergé : les
clés étaient quasi uniques (peu de hits, quota pollué). L'identité de cache est
`hosted` sur un runner hébergé (toolcache commun `/opt/hostedtoolcache`),
`runner.name` sinon (un venv auto-hébergé porte un lien absolu vers son Python,
#932).
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

#: l'identité de cache du runner : les runners hébergés partagent `hosted`,
#: les auto-hébergés gardent leur nom (#932, #1294)
IDENTITE = "runner.environment == 'github-hosted' && 'hosted' || runner.name"


def _etape(debut: str) -> str:
    """Le bloc d'une étape, de son marqueur à l'étape suivante (indent 6)."""
    m = re.search(re.escape(debut) + r"[^\n]*\n(.*?)(?=\n      - |\Z)",
                  WORKFLOW, re.S)
    if m is None:
        raise AssertionError("étape introuvable : %s" % debut)
    return m.group(0)


class CacheVenvTest(unittest.TestCase):

    def test_identite_hebergee_partagee_auto_hebergee_par_runner(self):
        # #1294 : plus de clé bâtie sur le seul `runner.name`, variable sur un
        # runner hébergé ; l'identité ternaire est partout où une clé est bâtie.
        self.assertNotIn("${{ runner.os }}-${{ runner.name }}-py", WORKFLOW)
        for step in (_etape("      - id: venv"),
                     _etape("      - if: steps.deps.outputs.reconstruit")):
            self.assertIn(IDENTITE, step)
            self.assertIn("|| runner.name", step)

    def test_restauration_par_cle_partielle_du_run(self):
        # #1293 : la clé primaire est celle du run (unique) ; la restauration
        # d'un venv sauvé par une exécution précédente passe par restore-keys.
        step = _etape("      - id: venv")
        self.assertIn("uses: actions/cache/restore@v4", step)
        self.assertIn("path: .venv-ci", step)
        self.assertIn("github.run_id", step)
        self.assertIn("github.run_attempt", step)
        cle = re.search(r"key:\s*(venv-[^\n]+)", step)
        prefixe = re.search(r"restore-keys: \|\n\s*(venv-[^\n]+)", step)
        self.assertIsNotNone(cle, "clé primaire du venv introuvable")
        self.assertIsNotNone(prefixe, "restore-keys du venv introuvables")
        self.assertTrue(cle.group(1).startswith(prefixe.group(1)),
                        "la clé primaire doit prolonger le préfixe restauré")
        self.assertIn("github.run_id",
                      cle.group(1)[len(prefixe.group(1)):])

    def test_venv_invalide_reconstruit_puis_resauve(self):
        # #1293 : le pas « dépendances » valide le venv restauré, le
        # reconstruit sinon et le signale ; le sauvetage suit la
        # reconstruction, sous la clé du run retrouvée par restore-keys.
        deps = _etape("      - id: deps")
        self.assertNotIn("cache-hit", deps)
        self.assertIn("import psycopg, cryptography, psutil, yaml", deps)
        self.assertIn("rm -rf .venv-ci", deps)
        self.assertIn("python -m venv .venv-ci", deps)
        self.assertIn('echo "reconstruit=1" >> "$GITHUB_OUTPUT"', deps)
        self.assertLess(deps.index("rm -rf .venv-ci"),
                        deps.index('echo "reconstruit=1"'))
        sauve = _etape("      - if: steps.deps.outputs.reconstruit")
        self.assertIn("uses: actions/cache/save@v4", sauve)
        self.assertIn("if: steps.deps.outputs.reconstruit == '1'", sauve)
        self.assertIn("continue-on-error: true", sauve)
        self.assertIn("path: .venv-ci", sauve)
        cle_restauree = re.search(r"key:\s*(venv-[^\n]+)",
                                  _etape("      - id: venv")).group(1).strip()
        cle_sauvee = re.search(r"key:\s*(venv-[^\n]+)", sauve).group(1).strip()
        self.assertEqual(cle_restauree, cle_sauvee,
                         "le venv reconstruit doit être sauvé sous la clé du run")

    def test_plus_de_sauvetage_automatique_a_chaque_execution(self):
        # l'ancien `actions/cache@v4` sauvait à chaque clé primaire manquée :
        # avec une clé par run, il écrirait un cache par exécution. Le
        # sauvetage est donc une étape explicite, conditionnée.
        self.assertNotIn("uses: actions/cache@v4", WORKFLOW)
        self.assertIn("uses: actions/cache/restore@v4", WORKFLOW)
        self.assertIn("uses: actions/cache/save@v4", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
