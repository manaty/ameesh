# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 D + R1 — la carte n'est publiée que sur une mesure complète et couverte.

D — une part de mesure annulée (plafond, runner perdu) publie son artefact sans
`durees.json` : `scripts/map.py merge` la fusionnait silencieusement, et la
carte publiée perdait les durées de ses modules (la fusion n'hérite des cartes
précédentes que des échecs, pas des durées — constaté sur la carte `96a795fb2`,
248 durées psycopg et 0 psql). Le job `carte` refuse désormais de publier :
matrice de mesure non verte, part absente, `durees.json` absent ou vide.
`PARTS_MESURE` est le miroir de la matrice du workflow.

R1 — chaque part de mesure restaurait la carte du cache et recalculait son
découpage : deux jobs pouvaient lire deux cartes différentes (cache écrit
entre-temps par une PR ou main) et produire des parts disjointes, un module
omis et un autre doublé, toutes avec des `durees.json` non vides. Le job
`plan-mesure` lit maintenant la carte et calcule le découpage **une seule fois
par run** ; toutes les parts lisent ce plan (`run --plan`), et `merge --plan`
vérifie la couverture effective : aucun module du plan omis, doublé ou hors de
sa part.

Témoins : part absente, part annulée (artefact sans durées), durées vides,
mesure complète ; témoin K1/K2 du cache changé entre deux jobs ; couverture
effective (module omis, doublé, hors part) — plus le refus réel de la ligne de
commande.

Sans base : lecture du workflow, de `scripts/map.py` et de
`scripts/test-parts.py`.
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


def _job(nom: str) -> str:
    """Le texte du job `nom` de carte.yml (les jobs sont à deux espaces)."""
    m = re.search(rf"^  {re.escape(nom)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\s*$|\Z)",
                  WORKFLOW, re.M | re.S)
    if m is None:
        raise AssertionError(f"job {nom} introuvable dans carte.yml")
    return m.group(1)


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


carte = _charger("carte_1341", "map.py")
parts = _charger("parts_1341", "test-parts.py")

PLAN_K1 = {"parts": {"psycopg": [{"part": 1, "modules": ["a", "d"]},
                                 {"part": 2, "modules": ["b", "c"]}]}}


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
        # R1 : la carte est lue une seule fois, par le job `plan-mesure`.
        plan = _job("plan-mesure")
        self.assertIn("actions/cache/restore@v4", plan)
        self.assertIn('${CARTE:+--map "$CARTE"}', plan)

    def test_la_fusion_exige_une_matrice_verte(self):
        self.assertIn('if [ "$RESULTAT_MESURE" != "success" ]', WORKFLOW)
        self.assertIn("RESULTAT_MESURE: ${{ needs.mesure.result }}", WORKFLOW)

    def test_la_fusion_declare_les_parts_attendues(self):
        self.assertIn('--attendu "$PARTS_MESURE"', WORKFLOW)

    def test_rien_n_est_publie_si_la_fusion_echoue(self):
        # l'artefact `carte` et le cache ne s'écrivent qu'après une fusion
        # réussie : aucune étape de publication du job `carte` ne tourne sur
        # échec. Un ménage tolérant (`continue-on-error`) peut, lui, tourner
        # sur échec : il ne publie rien (quota d'artefacts).
        section = WORKFLOW.split("\n  carte:\n", 1)[1]
        etapes = section.split("    steps:\n", 1)[1]
        blocs = etapes.split("\n      - ")[1:]
        publication = [b for b in blocs
                       if "actions/upload-artifact" in b or "actions/cache/save" in b]
        self.assertTrue(publication, "aucune étape de publication dans le job carte")
        for bloc in publication:
            self.assertNotIn("if: always()", bloc,
                             "une étape de publication ne tourne pas sur échec")
        for bloc in blocs:
            if "if: always()" in bloc:
                self.assertIn("continue-on-error: true", bloc,
                              "seul un ménage tolérant tourne sur échec")
        self.assertIn("actions/cache/save@v4", etapes)


class PlanUniqueTest(unittest.TestCase):
    """R1 : un seul plan par run, consommé par toutes les parts."""

    def test_le_plan_est_calcule_une_fois_par_run(self):
        plan = _job("plan-mesure")
        self.assertIn("scripts/test-parts.py plan", plan)
        self.assertIn('--parts "$PARTS_MESURE"', plan)
        self.assertIn("tests/parts/pilote-psql.txt", plan)
        self.assertIn("name: plan-mesure", plan)
        self.assertNotIn("strategy:", plan, "le plan ne doit pas être une matrice")

    def test_les_parts_ne_relisent_pas_la_carte(self):
        mesure = _job("mesure")
        self.assertIn("needs: plan-mesure", mesure)
        self.assertIn("name: plan-mesure", mesure)
        self.assertIn("scripts/test-parts.py run --plan", mesure)
        self.assertNotIn("actions/cache/restore", mesure,
                         "chaque part ne doit plus restaurer la carte elle-même")
        self.assertNotIn("--map", mesure,
                         "le découpage de la part vient du plan, pas d'une carte relue")

    def test_la_fusion_verifie_la_couverture_du_plan(self):
        fusion = _job("carte")
        self.assertIn("name: plan-mesure", fusion)
        self.assertIn("--plan plan/plan.json", fusion)


class CouvertureEffectiveTest(unittest.TestCase):
    """`map.couverture_incomplete` : aucun module omis, doublé ou hors part."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _part(self, nom: str, modules):
        dossier = self.racine / nom
        dossier.mkdir(parents=True, exist_ok=True)
        (dossier / "durees.json").write_text(
            json.dumps({"psycopg": {m: 1.0 for m in modules}}), encoding="utf-8")

    def _check(self):
        dossiers = [str(p) for p in sorted(self.racine.iterdir())]
        return carte.couverture_incomplete(dossiers, PLAN_K1)

    def test_couverture_complete(self):
        self._part("mesure-psycopg-1", ["a", "d"])
        self._part("mesure-psycopg-2", ["b", "c"])
        self.assertEqual([], self._check())

    def test_module_omis(self):
        self._part("mesure-psycopg-1", ["a", "d"])
        self._part("mesure-psycopg-2", ["c"])
        problemes = self._check()
        self.assertTrue(any("module du plan non mesuré : b" in p for p in problemes),
                        problemes)

    def test_module_double(self):
        # le témoin K1/K2 : la part 2 a été calculée sur une autre carte que la
        # part 1, `a` est mesuré deux fois et `b` n'est mesuré nulle part.
        self._part("mesure-psycopg-1", ["a", "d"])
        self._part("mesure-psycopg-2", ["a", "c"])
        problemes = self._check()
        self.assertTrue(any("mesuré dans plusieurs parts" in p and "a" in p
                            for p in problemes), problemes)
        self.assertTrue(any("module du plan non mesuré : b" in p for p in problemes),
                        problemes)

    def test_module_hors_de_sa_part(self):
        self._part("mesure-psycopg-1", ["a", "d", "x"])
        self._part("mesure-psycopg-2", ["b", "c"])
        problemes = self._check()
        self.assertIn("mesure-psycopg-1 : module mesuré hors de sa part : x", problemes)

    def test_part_mesuree_absente_du_plan(self):
        self._part("mesure-psycopg-1", ["a", "d"])
        self._part("mesure-psycopg-2", ["b", "c"])
        self._part("mesure-psycopg-3", ["e"])
        problemes = self._check()
        self.assertIn("mesure-psycopg-3 : part mesurée absente du plan", problemes)


class TemoinCacheChangeTest(unittest.TestCase):
    """R1 : deux cartes K1/K2, un seul plan — aucun module omis ni doublé."""

    #: le témoin de la R1 : mêmes modules, poids différents entre deux jobs
    K1 = {"a": 8.0, "b": 7.0, "c": 6.0, "d": 5.0}
    K2 = {"a": 7.0, "b": 8.0, "c": 6.0, "d": 5.0}

    def test_le_decoupage_change_avec_la_carte(self):
        # A0 ≠ A1 du même run : le partage dépend bien de la carte lue.
        self.assertNotEqual(parts.split(self.K1, 2), parts.split(self.K2, 2))

    def test_sans_plan_unique_un_module_est_omis_et_un_double(self):
        # comportement d'avant R1 : la part 1 lit K1, la part 2 lit K2 (cache
        # écrit entre les deux jobs) -> `b` omis, `a` doublé, durées non vides.
        part1 = parts.split(self.K1, 2)[0]
        part2 = parts.split(self.K2, 2)[1]
        self.assertNotIn("b", part1 + part2)
        self.assertEqual(2, (part1 + part2).count("a"))

    def test_avec_le_plan_unique_la_couverture_est_exacte(self):
        # le plan est calculé une fois (K1) et toutes les parts le lisent.
        decoupage = parts.split(self.K1, 2)
        vus = [m for part in decoupage for m in part]
        self.assertEqual(sorted(self.K1), sorted(vus))
        self.assertEqual(len(vus), len(set(vus)), "aucun module doublé")


class PlanMesureTest(unittest.TestCase):
    """La commande `test-parts.py plan` : les parts attendues, une fois."""

    def _plan(self, *noms: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "plan.json"
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "test-parts.py"), "plan",
                 "--out", str(out), "--parts", " ".join(noms)],
                capture_output=True, text=True, cwd=REPO)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(out.read_text(encoding="utf-8"))

    def test_le_plan_partitionne_les_modules(self):
        plan = self._plan("mesure-psycopg-1", "mesure-psycopg-2")
        entrees = plan["parts"]["psycopg"]
        self.assertEqual([1, 2], [e["part"] for e in entrees])
        self.assertEqual([2, 2], [e["of"] for e in entrees])
        vus = [m for e in entrees for m in e["modules"]]
        self.assertEqual(len(vus), len(set(vus)), "aucun module doublé")
        self.assertEqual(set(parts.modules()), set(vus))

    def test_le_plan_psql_suit_la_liste_pilote(self):
        plan = self._plan("mesure-psql-1", "mesure-psql-2")
        pilote = parts.read_list(REPO / "tests" / "parts" / "pilote-psql.txt",
                                 parts.modules())
        vus = [m for e in plan["parts"]["psql"] for m in e["modules"]]
        self.assertEqual(sorted(pilote), sorted(vus))
        self.assertTrue(all(e["pilot_list"] for e in plan["parts"]["psql"]))

    def test_le_plan_refuse_une_part_inconnue(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "plan.json"
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "test-parts.py"), "plan",
                 "--out", str(out), "--parts", "mesure-psycopg-1 mesure-psycopg-3"],
                capture_output=True, text=True, cwd=REPO)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("non contiguës", proc.stderr)


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

    def test_le_cli_refuse_une_couverture_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = pathlib.Path(tmp)
            part = racine / "mesure-psycopg-1"
            part.mkdir()
            (part / "durees.json").write_text(
                json.dumps({"psycopg": {"a": 1.0, "d": 1.0}}), encoding="utf-8")
            plan = racine / "plan.json"
            plan.write_text(json.dumps(PLAN_K1), encoding="utf-8")
            sortie = racine / "carte.json.gz"
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "map.py"), "merge",
                 "--output", str(sortie), "--plan", str(plan),
                 "--attendu", "mesure-psycopg-1", str(part)],
                capture_output=True, text=True, cwd=REPO)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertFalse(sortie.exists(), "aucune carte ne doit être publiée")
            self.assertIn("::error::couverture incomplète", proc.stdout)
            self.assertIn("module du plan non mesuré : b", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
