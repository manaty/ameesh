# SPDX-License-Identifier: AGPL-3.0-only
"""#3754 : le parallélisme dans le job d'une part (`test-parts.py run
--parallel`), son découpage par unités et la fusion de son flux.

Le découpage par module ne suffisait pas : une part dont un module porte un
test de plusieurs minutes restait le chemin critique. Le processus parent
découpe donc la part en **classes** (ou fonctions de test seules), pondérées
par les durées par test de `durees.json` (`tests.<pilote>`), puis lance N
processus `run` sur des plans dérivés. Chaque processus a son schéma par
défaut ; le flux des tests est recopié en direct dans celui de la part, avec
un seul `debut`, un seul `impacted` et un seul `fin`.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _charger(nom: str, fichier: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(nom, SCRIPTS / fichier)
    module = importlib.util.module_from_spec(spec)
    sys.modules[nom] = module
    spec.loader.exec_module(module)
    return module


parts = _charger("parts_3754", "test-parts.py")
plan_mod = _charger("plan_3754", "test-plan.py")


class UnitesTest(unittest.TestCase):

    def test_une_classe_reste_une_unite(self):
        unites = parts._unites_de(["test_version", "test_site_art"])
        self.assertIn("tests.test_version.VersionTest", unites)
        self.assertIn("tests.test_site_art.SiteArtTest", unites)
        ids = [test_id for valeurs in unites.values() for test_id in valeurs]
        self.assertEqual(len(ids), len(set(ids)), "un test dans deux unités")

    def test_chaque_test_est_dans_une_unite(self):
        unites = parts._unites_de(["test_version"])
        ids = {test_id for valeurs in unites.values() for test_id in valeurs}
        self.assertTrue(any(test_id.endswith("test_version_is_semver") for test_id in ids))

    def test_un_test_lent_pese_sur_sa_classe(self):
        """La durée par test de `durees.json` prime sur le prorata du module."""
        ancien = parts.TABLE
        with tempfile.TemporaryDirectory() as tmp:
            table = pathlib.Path(tmp) / "durees.json"
            table.write_text(json.dumps({
                "psql": {"test_version": 10.0},
                "tests": {"psql": {
                    "tests.test_version.VersionTest.test_version_is_semver": 60.0,
                }},
            }), encoding="utf-8")
            parts.TABLE = table
            try:
                unites = parts._unites_de(["test_version"])
                pesees = parts._pesees_unites(unites, "psql", ["test_version"])
            finally:
                parts.TABLE = ancien
        self.assertGreaterEqual(pesees["tests.test_version.VersionTest"], 60.0)


class PoidsDuPlanTest(unittest.TestCase):
    """#3754 : la table régénérée depuis une ci-full réelle passe devant une
    carte sous couverture plus ancienne ; la carte plus récente garde la main."""

    def _poids(self, genere_table: str, genere_carte: str) -> float:
        ancien = plan_mod.parts.TABLE
        with tempfile.TemporaryDirectory() as tmp:
            table = pathlib.Path(tmp) / "durees.json"
            table.write_text(json.dumps({
                "_generated": genere_table,
                "psql": {"test_version": 100.0},
            }), encoding="utf-8")
            plan_mod.parts.TABLE = table
            try:
                carte = {
                    "genere": genere_carte,
                    "measure": {"surcout": {}},
                    "modules": {"test_version": {"durations": {"psql": 3.0}}},
                }
                return plan_mod.weights(["test_version"], "psql", carte)["test_version"]
            finally:
                plan_mod.parts.TABLE = ancien

    def test_la_table_fraiche_passe_devant_la_carte(self):
        self.assertEqual(self._poids("2026-10-26T20:00:00+00:00",
                                     "2026-10-26T09:00:00+00:00"), 100.0)

    def test_la_carte_plus_recente_garde_la_priorite(self):
        self.assertEqual(self._poids("2026-10-26T08:00:00+00:00",
                                     "2026-10-26T09:00:00+00:00"), 3.0)

    def test_une_carte_sans_fuseau_reste_comparable(self):
        # anciennes cartes : `genere` sans fuseau, lu comme UTC
        self.assertEqual(self._poids("2026-10-26T20:00:00+00:00",
                                     "2026-10-26T09:00:00"), 100.0)


    def test_une_classe_lourde_est_decoupee_en_tests(self):
        """Une classe au-dessus du seuil isole son test lent (503 s mesurées)."""
        lent = "tests.test_l108_executor_server.FencingTest.test_bail_perdu"
        rapide1 = "tests.test_l108_executor_server.FencingTest.test_autre_1"
        rapide2 = "tests.test_l108_executor_server.FencingTest.test_autre_2"
        unites = {"tests.test_l108_executor_server.FencingTest": [lent, rapide1, rapide2]}
        pesees = {"tests.test_l108_executor_server.FencingTest": 600.0}
        nouveaux, poids = parts._affiner_unites(unites, pesees, {lent: 500.0})
        self.assertEqual(set(nouveaux), {lent, rapide1, rapide2})
        self.assertEqual(poids[lent], 500.0)
        self.assertLess(poids[rapide1], 100.0)

    def test_une_classe_legere_reste_entiere(self):
        unites = {"tests.test_version.VersionTest": ["a", "b"]}
        nouveaux, _ = parts._affiner_unites(unites, {"tests.test_version.VersionTest": 20.0}, {})
        self.assertEqual(nouveaux, unites)


class WorkflowTest(unittest.TestCase):

    def test_les_parts_activent_deux_processus_et_pg_dump_17(self):
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(texte.count("--parallel 2"), 2)
        self.assertIn("postgresql-client-17", texte)
        self.assertIn("*pgdump17", texte)
        # Avis auditeur : l'installation du client 17 est un confort, le
        # gabarit se replie sur migrate() ; elle ne doit jamais faire tomber
        # le job (sudo, PGDG, verrou apt).
        bloc = re.search(r"- &pgdump17\n(.*?)(?=\n      - )", texte, re.S)
        self.assertIsNotNone(bloc)
        self.assertIn("continue-on-error: true", bloc.group(1))


class ProcessusTest(unittest.TestCase):

    def test_le_nombre_suit_la_charge_avec_plancher_et_plafond(self):
        self.assertEqual(parts.parallel_count(10.0, 2), 2)
        self.assertEqual(parts.parallel_count(451.0, 2), 2)
        self.assertEqual(parts.parallel_count(901.0, 2), 3)
        self.assertEqual(parts.parallel_count(1351.0, 2), 4)
        self.assertEqual(parts.parallel_count(9999.0, 2), 4)


class DuréesDepuisFluxTest(unittest.TestCase):

    def test_les_flux_donnent_modules_et_tests(self):
        """`durations --flows` régénère la table depuis une ci-full réelle."""
        ancien = parts.TABLE
        with tempfile.TemporaryDirectory() as tmp:
            table = pathlib.Path(tmp) / "durees.json"
            table.write_text(json.dumps({"psql": {"test_version": 1.0}}), encoding="utf-8")
            flux = pathlib.Path(tmp) / "flux.jsonl"
            flux.write_text("\n".join(json.dumps(e) for e in [
                {"type": "test", "module": "test_version",
                 "test": "tests.test_version.VersionTest.test_version_is_semver",
                 "duree_s": 12.0},
                {"type": "test", "module": "test_version",
                 "test": "tests.test_version.VersionTest.test_version_command_and_flag",
                 "duree_s": 3.0},
                {"type": "test", "module": "test_inconnu",
                 "test": "tests.test_inconnu.X.test_y", "duree_s": 9.0},
            ]), encoding="utf-8")
            parts.TABLE = table
            try:
                parts.durations(SimpleNamespace(driver="psql", files=[], flows=[str(flux)],
                                                min_test_s=5.0))
                data = json.loads(table.read_text(encoding="utf-8"))
                # Rejouer les mêmes flux remplace la table du pilote : les
                # durées par test ne s'additionnent pas deux fois.
                parts.durations(SimpleNamespace(driver="psql", files=[], flows=[str(flux)],
                                                min_test_s=5.0))
                rejoue = json.loads(table.read_text(encoding="utf-8"))
            finally:
                parts.TABLE = ancien
        self.assertEqual(rejoue["tests"]["psql"], data["tests"]["psql"])
        self.assertEqual(rejoue["psql"]["test_version"], 15.0)
        self.assertEqual(data["psql"]["test_version"], 15.0)
        self.assertEqual(data["tests"]["psql"],
                         {"tests.test_version.VersionTest.test_version_is_semver": 12.0})
        self.assertIn("_generated", data)

    def test_un_fichier_partiel_ne_marque_pas_la_table_fraiche(self):
        """`--files` (scripts/test-parallele.sh) n'est pas une ci-full : pas de
        `_generated`, sinon cette mesure partielle passerait devant la carte."""
        ancien = parts.TABLE
        with tempfile.TemporaryDirectory() as tmp:
            table = pathlib.Path(tmp) / "durees.json"
            table.write_text(json.dumps({"psql": {"test_version": 1.0}}), encoding="utf-8")
            mesure = pathlib.Path(tmp) / "psql-1.json"
            mesure.write_text(json.dumps({"psql": {"test_version": 7.0}}), encoding="utf-8")
            parts.TABLE = table
            try:
                parts.durations(SimpleNamespace(driver="psql", files=[str(mesure)],
                                                flows=None))
                data = json.loads(table.read_text(encoding="utf-8"))
            finally:
                parts.TABLE = ancien
        self.assertNotIn("_generated", data)
        self.assertEqual(data["psql"]["test_version"], 7.0)


class ParallelRunTest(unittest.TestCase):

    def _plan(self, tmp: pathlib.Path) -> pathlib.Path:
        modules = ["test_version", "test_site_art"]
        plan = {
            "schema": "ameesh-test-plan/1",
            "parts": {"psql": [{
                "part": 1, "of": 1, "label": "psql 1/1",
                "modules": modules, "estimate_s": 10.0, "pilot_list": True,
            }]},
            "classes": {m: ["P3", ""] for m in modules},
        }
        chemin = tmp / "plan.json"
        chemin.write_text(json.dumps(plan), encoding="utf-8")
        return chemin

    def test_le_flux_est_fusionne_une_seule_fois(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = pathlib.Path(tmp)
            plan = self._plan(racine)
            flux = racine / "flux.jsonl"
            env = dict(os.environ, AMEESH_REQUIRE_DB="0", AMEESH_BALANCE_INTERVAL="0")
            env.pop("AMEESH_CI_JOBS_PER_HOST", None)  # #3945 : pas de plafond d'hôte ici
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "test-parts.py"), "run",
                 "--plan", str(plan), "--part", "1", "--driver", "psql",
                 "--flow", str(flux), "--parallel", "2"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
            evenements = [json.loads(ligne) for ligne in
                          flux.read_text(encoding="utf-8").splitlines() if ligne.strip()]
            types = [e.get("type") for e in evenements]
            self.assertEqual(types.count("debut"), 1, types)
            self.assertEqual(types.count("fin"), 1, types)
            self.assertEqual(types.count("impacted"), 1, types)
            tests = [e for e in evenements if e.get("type") == "test"]
            self.assertGreaterEqual(len(tests), 2)
            self.assertTrue(all(e.get("module") in ("test_version", "test_site_art")
                                for e in tests))
            fin = [e for e in evenements if e.get("type") == "fin"][0]
            self.assertEqual(fin["statut"], "succes")
            self.assertEqual(sum(fin["comptes"].values()), len(tests))


if __name__ == "__main__":
    unittest.main()
