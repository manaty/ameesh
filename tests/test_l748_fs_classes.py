# SPDX-License-Identifier: AGPL-3.0-only
"""#748 — la suite rejouée sur une autre classe de système de fichiers.

Le job `fs-class` de `.github/workflows/tests.yml` rejoue les parts psycopg du
plan avec `TMPDIR` sur l'autre classe de système de fichiers que celle du
runner (tmpfs ↔ disque), à la release (ou à la main) seulement : un test qui
ne tient que sur la classe ordinaire — horodatages, renommage entre
périphériques, inodes, blocs, montage — s'y révèle, là où la suite ordinaire
ne le voit pas. Ces témoins tiennent le contrat du workflow : le job existe,
il est borné, il ne tourne pas sur une PR ordinaire, il pose `TMPDIR` sur une
autre classe et le vérifie, il nettoie sa racine sur un hôte persistant, et le
check agrégé « tests » l'exige quand il a tourné.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


def _job(texte: str, nom: str) -> str:
    """Le bloc YAML du job `nom`, jusqu'au job suivant (même indentation)."""
    motif = r"\n  %s:\n(.*?)(?=\n  [a-z][a-z0-9-]*:\n|\Z)" % re.escape(nom)
    trouve = re.search(motif, texte, re.S)
    return trouve.group(1) if trouve else ""


class WorkflowFsClassTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.texte = WORKFLOW.read_text(encoding="utf-8")
        cls.job = _job(cls.texte, "fs-class")
        cls.agrege = _job(cls.texte, "tests")

    def test_job_existe_borne_et_nomme(self):
        self.assertTrue(self.job, "job fs-class absent de tests.yml")
        self.assertIn("name: autre système de fichiers", self.job)
        borne = re.search(r"timeout-minutes:\s*(\d+)", self.job)
        self.assertIsNotNone(borne, "job fs-class sans timeout-minutes")
        self.assertLessEqual(int(borne.group(1)), 60, "durée maximale non bornée")

    def test_rejoue_les_parts_psycopg_du_plan(self):
        self.assertIn("part: ${{ fromJSON(needs.plan.outputs.psycopg) }}", self.job)
        self.assertIn("AMEESH_DRIVER: psycopg", self.job)
        self.assertIn("test-parts.py run", self.job)
        self.assertIn("--driver psycopg", self.job)

    def test_release_seulement(self):
        # pas de rejeu sur une PR ordinaire : tête ou base `release/`, push sur
        # une branche de release, ou exécution à la main
        self.assertIn("needs.plan.outputs.code == 'true'", self.job)
        self.assertIn("github.event_name == 'workflow_dispatch'", self.job)
        self.assertIn("startsWith(github.ref_name, 'release/')", self.job)
        self.assertIn("startsWith(github.head_ref, 'release/')", self.job)
        self.assertIn("startsWith(github.base_ref, 'release/')", self.job)

    def test_autre_classe_posee_et_verifiee(self):
        # la classe par défaut est lue (stat -f), l'autre est exigée, et TMPDIR
        # de la part pointe dessus
        self.assertIn('defaut=$(stat -f -c %T "${TMPDIR:-/tmp}")', self.job)
        self.assertIn('[ "$classe" != "$defaut" ]', self.job)
        self.assertIn('echo "TMPDIR=$racine" >> "$GITHUB_ENV"', self.job)
        self.assertIn('echo "AMEESH_AUTRE_FS=$classe" >> "$GITHUB_ENV"', self.job)
        self.assertIn("exit 1", self.job, "classe introuvable : le job doit échouer, pas se taire")

    def test_racine_nettoyee_sur_hote_persistant(self):
        menage = re.search(r"\n      - name: ménage de la racine temporaire.*?(?=\n      - |\Z)",
                           self.job, re.S)
        self.assertIsNotNone(menage, "job fs-class sans ménage de sa racine temporaire")
        bloc = menage.group(0)
        self.assertIn("if: always()", bloc)
        self.assertIn('case "${TMPDIR:-}" in', bloc)
        self.assertIn('rm -rf -- "$TMPDIR"', bloc)
        # en dernier : une action Node (l'artefact) écrit dans `os.tmpdir()`,
        # qui suit `TMPDIR` ; la supprimer avant la ferait échouer
        self.assertGreater(self.job.index("ménage de la racine temporaire"),
                           self.job.index("flux en artefact"))

    def test_artefact_hors_du_bilan(self):
        # le bilan (L161f) agrège les flux `flux-*` : la part sur l'autre
        # système de fichiers ne doit pas y entrer une seconde fois
        self.assertIn("name: autre-fs-${{ matrix.part }}", self.job)
        self.assertNotIn("name: flux-", self.job)

    def test_agrege_exige_la_part_quand_elle_a_tourne(self):
        besoins = re.search(r"^    needs: \[(.*?)\]$", self.agrege, re.M)
        self.assertIsNotNone(besoins, "job tests sans liste needs")
        self.assertIn("fs-class", [nom.strip() for nom in besoins.group(1).split(",")])
        self.assertIn(
            'test "${{ needs.fs-class.result }}" = success '
            '|| test "${{ needs.fs-class.result }}" = skipped',
            self.agrege)
