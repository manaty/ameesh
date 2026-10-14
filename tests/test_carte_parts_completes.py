# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 D — la carte n'est publiée que sur une mesure complète.

Une part de mesure annulée (plafond, runner perdu) publie son artefact sans
`durees.json` : `scripts/map.py merge` la fusionnait silencieusement, et la
carte publiée perdait les durées de ses modules (la fusion n'hérite des
cartes précédentes que des échecs, pas des durées — constaté sur la carte
`96a795fb2`, 248 durées psycopg et 0 psql). Le job `carte` refuse désormais
de publier : matrice de mesure non verte, part absente, `durees.json` absent
ou vide. `PARTS_MESURE` est le miroir de la matrice du workflow.

Témoins : part absente, part annulée (artefact sans durées), durées vides,
mesure complète — plus le refus réel de la ligne de commande.

Sans base : lecture du workflow et de `scripts/map.py`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")
INCLUDE = re.compile(r"^\s*-\s*\{\s*pilote:\s*(\w+),\s*part:\s*(\d+),\s*parts:\s*(\d+)", re.M)
MIROIR = re.compile(r"^(\s*)PARTS_MESURE:\s*>-\s*$\n((?:\1[ \t]+\S.*\n?)+)", re.M)


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


carte = _charger("carte_1341", "map.py")


class MiroirDesPartsTest(unittest.TestCase):
    """Le workflow et le garde-fou ne peuvent pas diverger en silence."""

    def test_le_miroir_couvre_la_matrice_de_mesure(self):
        matrice = sorted(f"mesure-{p}-{n}" for p, n, _ in INCLUDE.findall(WORKFLOW))
        self.assertEqual(len(matrice), 6, "matrice de mesure inattendue")
        m = MIROIR.search(WORKFLOW)
        self.assertIsNotNone(m, "PARTS_MESURE introuvable dans carte.yml")
        miroir = sorted(m.group(2).split())
        self.assertEqual(matrice, miroir,
                         "PARTS_MESURE doit être le miroir exact de la matrice mesure")

    def test_la_mesure_psql_est_decoupee(self):
        # B : une seule part de 45 modules pesait 38 min de mur pour 40 de
        # plafond, annulée deux fois.
        psql = [(p, n, of) for p, n, of in INCLUDE.findall(WORKFLOW) if p == "psql"]
        self.assertEqual(psql, [("psql", "1", "2"), ("psql", "2", "2")])

    def test_le_decoupage_sous_mesure_lit_la_carte(self):
        # A : le surcoût de la mesure varie de 1,2× à 9,3× selon les modules ;
        # le découpage sous mesure ne peut pas venir de la table statique.
        self.assertIn("actions/cache/restore@v4", WORKFLOW)
        self.assertIn('${CARTE:+--map "$CARTE"}', WORKFLOW)

    def test_la_fusion_exige_une_matrice_verte(self):
        self.assertIn('if [ "$RESULTAT_MESURE" != "success" ]', WORKFLOW)
        self.assertIn("RESULTAT_MESURE: ${{ needs.mesure.result }}", WORKFLOW)

    def test_la_fusion_declare_les_parts_attendues(self):
        self.assertIn('--attendu "$PARTS_MESURE"', WORKFLOW)

    def test_rien_n_est_publie_si_la_fusion_echoue(self):
        # l'artefact `carte` et le cache ne s'écrivent qu'après une fusion
        # réussie : aucune étape du job `carte` ne tourne sur échec.
        section = WORKFLOW.split("\n  carte:\n", 1)[1]
        etapes = section.split("    steps:\n", 1)[1]
        self.assertNotIn("if: always()", etapes)
        self.assertIn("actions/cache/save@v4", etapes)


class PartsIncompletesTest(unittest.TestCase):
    """Les quatre témoins du garde-fou (`map.parts_incompletes`)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _part(self, nom: str, durees=None, flux: bool = False):
        dossier = self.racine / nom
        dossier.mkdir(parents=True, exist_ok=True)
        if durees is not None:
            (dossier / "durees.json").write_text(json.dumps(durees), encoding="utf-8")
        if flux:
            (dossier / "flux.jsonl").write_text('{"type": "debut"}\n', encoding="utf-8")
        return str(dossier)

    def _check(self, attendus):
        dossiers = [str(p) for p in sorted(self.racine.iterdir())]
        return carte.parts_incompletes(dossiers, attendus)

    def test_mesure_complete(self):
        self._part("mesure-psycopg-1", {"psycopg": {"test_a": 12.0}})
        self._part("mesure-psql-1", {"psql": {"test_b": 3.0}})
        self.assertEqual([], self._check(["mesure-psycopg-1", "mesure-psql-1"]))

    def test_part_absente(self):
        self._part("mesure-psycopg-1", {"psycopg": {"test_a": 12.0}})
        problemes = self._check(["mesure-psycopg-1", "mesure-psql-1"])
        self.assertEqual(len(problemes), 1)
        self.assertIn("mesure-psql-1 : artefact absent", problemes[0])

    def test_part_annulee_artefact_sans_durees(self):
        # le cas réel psy1 du run 38610188898 : 18 Mo d'artefact, pas de durees.json
        self._part("mesure-psycopg-1", None, flux=True)
        problemes = self._check(["mesure-psycopg-1"])
        self.assertEqual(len(problemes), 1)
        self.assertIn("aucune durée écrite", problemes[0])

    def test_durees_vides(self):
        self._part("mesure-psycopg-1", {})
        problemes = self._check(["mesure-psycopg-1"])
        self.assertEqual(len(problemes), 1)
        self.assertIn("durées vides", problemes[0])

    def test_durees_illisibles(self):
        dossier = self.racine / "mesure-psycopg-1"
        dossier.mkdir(parents=True)
        (dossier / "durees.json").write_text("{", encoding="utf-8")
        problemes = self._check(["mesure-psycopg-1"])
        self.assertEqual(len(problemes), 1)
        self.assertIn("illisible", problemes[0])


class MergeRefuseTest(unittest.TestCase):
    """La ligne de commande refuse et n'écrit aucune carte."""

    def test_le_cli_refuse_une_part_manquante(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = pathlib.Path(tmp)
            part = racine / "mesure-psycopg-1"
            part.mkdir()
            (part / "durees.json").write_text('{"psycopg": {"test_a": 1.0}}', encoding="utf-8")
            sortie = racine / "carte.json.gz"
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "map.py"), "merge",
                 "--output", str(sortie), "--attendu", "mesure-psycopg-1 mesure-psql-1",
                 str(part)],
                capture_output=True, text=True, cwd=REPO)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertFalse(sortie.exists(), "aucune carte ne doit être publiée")
            self.assertIn("::error::mesure incomplète : mesure-psql-1", proc.stdout)
            self.assertIn("artefact absent", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
