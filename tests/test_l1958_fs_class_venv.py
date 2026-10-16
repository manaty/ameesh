# SPDX-License-Identifier: AGPL-3.0-only
"""#1958 : fs-class partage le venv de psycopg — même clé, même contrat.

Le job `fs-class` (release seulement) rejoue les parts psycopg sur une autre
classe de système de fichiers. Il construit ou restaure le même `.venv-ci` que
le job `psycopg` : même runner, même Python, même `pyproject.toml` ; la classe
de `TMPDIR` n'est posée qu'après l'étape venv, elle ne change pas son contenu.
Deux exigences pour que ce partage soit sain :

* la clé de cache et son préfixe de restauration sont ceux de psycopg, sinon
  chaque job reconstruit son venv et le contrôle de l'un ne couvre pas ce que
  l'autre a déposé dans le cache ;
* l'installation et le contrôle d'import sont une seule et même étape, ancrée
  sur celle de psycopg (`&deps_venv`) et reprise par fs-class : le raccord
  #1511 (pyyaml) s'applique alors aux deux jobs, sans seconde liste qui
  divergerait.

Témoin sans base : lecture de `.github/workflows/tests.yml`.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def _job(nom: str) -> str:
    """Le bloc YAML du job `nom`, jusqu'au job suivant (même indentation)."""
    trouve = re.search(r"\n  %s:\n(.*?)(?=\n  [a-z][a-z0-9-]*:\n|\Z)" % re.escape(nom),
                       WORKFLOW, re.S)
    if trouve is None:
        raise AssertionError("job introuvable : %s" % nom)
    return trouve.group(1)


def _cle_venv(job: str) -> tuple[str, str]:
    """(clé restaurée, préfixe de restauration) de l'étape venv d'un job."""
    restaure = re.search(r"- id: venv\n.*?key:\s*(venv-[^\n]+)", job, re.S)
    if restaure is None:
        raise AssertionError("étape venv sans clé de cache")
    prefixe = re.search(r"restore-keys: \|\n\s*(venv-[^\n]+)", job)
    if prefixe is None:
        raise AssertionError("étape venv sans restore-keys")
    return restaure.group(1).strip(), prefixe.group(1).strip()


class FsClassVenvTest(unittest.TestCase):

    def test_fs_class_partage_la_cle_du_venv_psycopg(self):
        cle_psycopg, prefixe_psycopg = _cle_venv(_job("psycopg"))
        cle_fs, prefixe_fs = _cle_venv(_job("fs-class"))
        self.assertEqual(cle_fs, cle_psycopg,
                         "fs-class doit restaurer le venv de psycopg, pas le sien")
        self.assertEqual(prefixe_fs, prefixe_psycopg,
                         "même préfixe de restauration dans les deux jobs")
        self.assertTrue(cle_fs.startswith(prefixe_fs),
                        "la clé primaire doit prolonger le préfixe restauré")
        # #1257 : la clé porte le besoin `coverage` ; fs-class en hérite.
        self.assertIn("-psycopg-cov-", cle_fs)

    def test_fs_class_resauve_sous_la_meme_cle(self):
        fs = _job("fs-class")
        cle_fs, _ = _cle_venv(fs)
        sauve = re.search(
            r"- if: steps\.deps\.outputs\.reconstruit == '1'.*?key:\s*(venv-[^\n]+)",
            fs, re.S)
        self.assertIsNotNone(sauve, "fs-class sans sauvetage du venv reconstruit")
        self.assertEqual(sauve.group(1).strip(), cle_fs)

    def test_fs_class_reprend_l_etape_de_controle_de_psycopg(self):
        ancre = re.search(r"run: &(\w+) \|\n(.*?)(?=\n      - )", _job("psycopg"), re.S)
        self.assertIsNotNone(ancre, "l'étape « dépendances » de psycopg n'est pas ancrée")
        nom, corps = ancre.group(1), ancre.group(2)
        # l'étape partagée installe et contrôle la même liste
        self.assertIn("import psycopg, cryptography, psutil", corps)
        self.assertIn("'coverage>=7.10'", corps)
        self.assertIn("'psycopg[binary]>=3.1'", corps)
        fs = _job("fs-class")
        self.assertIn("run: *%s" % nom, fs,
                      "fs-class doit reprendre l'étape ancrée, pas sa propre liste")
        self.assertNotIn(".venv-ci/bin/python -c", fs,
                         "le contrôle d'import de fs-class doit être celui de psycopg")


if __name__ == "__main__":
    unittest.main()
