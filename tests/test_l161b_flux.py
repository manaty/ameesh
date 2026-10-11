# SPDX-License-Identifier: AGPL-3.0-only
"""L161b — flux des résultats de tests (JSON lignes) et ordre simple.

`scripts/test-parts.py lancer --flux` écrit une ligne par test terminé
(`ameesh-test-event/1`) ; `--depuis` fait passer d'abord, dans chaque part,
les modules de test touchés par les fichiers changés ; `scripts/ci-flux.py
resume` en fait le résumé du job. La CI publie le flux en artefact.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parts = _charger("test_parts_l161b", "test-parts.py")
ciflux = _charger("ci_flux_l161b", "ci-flux.py")


class OrdreTest(unittest.TestCase):
    def setUp(self):
        self.dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dossier))
        modules = {
            "test_change": "x = 1\n",
            "test_pointe": "import ameesh.resources\n",
            "test_from": "from ameesh import (\n    canon,\n    resources,\n)\n",
            "test_chemin": "SCRIPT = 'scripts/version-only.py'\n",
            "test_nom": "lire('pilote-psql.txt')\n",
            "test_ambigu": "lire('README.md')\n",
            "test_voisin": "from ameesh import resources_bis\nimport ameesh.resourcesx\n",
            "test_reste": "pass\n",
        }
        for nom, texte in modules.items():
            (self.dossier / f"{nom}.py").write_text(texte, encoding="utf-8")
        self.mods = sorted(modules)

    def test_classes(self):
        changes = ["tests/test_change.py", "src/ameesh/resources.py",
                   "scripts/version-only.py", "tests/parts/pilote-psql.txt", "deploy/README.md"]
        classes = parts.classer(self.mods, changes, tests=self.dossier, ambigus={"README.md"})
        self.assertEqual(classes["test_change"], ("P0", "tests/test_change.py"))
        for m in ("test_pointe", "test_from"):
            self.assertEqual(classes[m], ("P1", "src/ameesh/resources.py"), m)
        self.assertEqual(classes["test_chemin"], ("P1", "scripts/version-only.py"))
        self.assertEqual(classes["test_nom"], ("P1", "tests/parts/pilote-psql.txt"))
        # un nom de fichier porté par plusieurs fichiers ne désigne rien
        self.assertEqual(classes["test_ambigu"], ("P3", ""))
        self.assertEqual(classes["test_voisin"], ("P3", ""))
        self.assertEqual(classes["test_reste"], ("P3", ""))

    def test_ordre_touches_d_abord_courts_d_abord(self):
        classes = {"a": ("P3", ""), "b": ("P1", "x"), "c": ("P1", "y"),
                   "d": ("P0", "tests/d.py"), "e": ("P3", "")}
        pesees = {"a": 1.0, "b": 50.0, "c": 5.0, "d": 90.0, "e": 0.5}
        # le reste garde l'ordre alphabétique, quel que soit son poids
        self.assertEqual(parts.ordonner(["a", "b", "c", "d", "e"], pesees, classes),
                         ["d", "c", "b", "a", "e"])
        # sans fichiers changés : l'ordre alphabétique d'aujourd'hui
        self.assertEqual(parts.ordonner(["e", "a", "b"], pesees, {}), ["a", "b", "e"])

    def test_diff_illisible_ordre_inchange(self):
        self.assertIsNone(parts.fichiers_changes("ref-qui-n-existe-pas", repo=self.dossier))


def _cas():
    """Cas d'exemple, hors de portée de la découverte des tests."""

    class _Exemples(unittest.TestCase):
        def test_a_ok(self):
            pass

        def test_b_echec(self):
            self.assertEqual(1, 2)

        def test_c_erreur(self):
            raise RuntimeError("boum")

        @unittest.skip("pas ici")
        def test_d_saute(self):
            pass

        def test_e_sous_test(self):
            for i in range(2):
                with self.subTest(i=i):
                    self.assertEqual(i, 0)


    class _ClasseCassee(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError("setUpClass cassé")

        def test_jamais(self):
            pass

    return _Exemples, _ClasseCassee


class FluxTest(unittest.TestCase):
    def _lancer(self, *classes_de_test):
        chemin = pathlib.Path(tempfile.mkdtemp()) / "flux.jsonl"
        flux = parts.Flux(chemin, {"depot": "manaty/ameesh", "part": "psql 1/1"})
        classes = {__name__.rsplit(".", 1)[-1]: ("P1", "src/ameesh/x.py")}
        cls = type("_R", (parts._Resultat,), {"flux": flux, "classes": classes})
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                                   for c in classes_de_test)
        unittest.TextTestRunner(stream=io.StringIO(), resultclass=cls).run(suite)
        flux.fermer()
        return [json.loads(l) for l in chemin.read_text(encoding="utf-8").splitlines()]

    def test_une_ligne_par_test(self):
        lignes = self._lancer(_cas()[0])
        statuts = {l["test"].rsplit(".", 1)[-1]: l["statut"] for l in lignes}
        self.assertEqual(statuts, {"test_a_ok": "succes", "test_b_echec": "echec",
                                   "test_c_erreur": "erreur", "test_d_saute": "saute",
                                   "test_e_sous_test": "echec"})
        for l in lignes:
            self.assertEqual((l["v"], l["schema"], l["type"]), (1, "ameesh-test-event/1", "test"))
            self.assertEqual((l["depot"], l["part"]), ("manaty/ameesh", "psql 1/1"))
            self.assertEqual((l["classe"], l["raison"]), ("P1", "src/ameesh/x.py"))
            self.assertEqual(l["module"], __name__.rsplit(".", 1)[-1])
            self.assertTrue(l["ts"].endswith("Z"))
            self.assertIsInstance(l["duree_s"], float)
        echec = next(l for l in lignes if l["statut"] == "echec")
        self.assertIn("AssertionError", echec["extrait"])
        self.assertEqual(echec["rejouer"], f"python -m unittest {echec['test']}")
        self.assertLessEqual(len(echec["extrait"].splitlines()), parts.EXTRAIT_LIGNES)
        self.assertNotIn("extrait", next(l for l in lignes if l["statut"] == "succes"))

    def test_erreur_de_setupclass(self):
        lignes = self._lancer(_cas()[1])
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["statut"], "erreur")
        self.assertIn("setUpClass cassé", lignes[0]["extrait"])
        self.assertNotIn("rejouer", lignes[0])


class ResumeTest(unittest.TestCase):
    def test_resume(self):
        dossier = pathlib.Path(tempfile.mkdtemp())
        f = dossier / "flux.jsonl"
        lignes = [
            {"v": 1, "type": "debut", "part": "psycopg 2/4", "modules": ["test_a", "test_b"],
             "classes": {"test_a": "P0", "test_b": "P3"}},
            {"v": 1, "type": "test", "test": "tests.test_a.T.test_x", "statut": "succes",
             "duree_s": 0.2},
            {"v": 1, "type": "test", "test": "tests.test_b.T.test_y", "statut": "echec",
             "duree_s": 1.5, "extrait": "AssertionError: 1 != 2",
             "rejouer": "python -m unittest tests.test_b.T.test_y"},
            {"v": 1, "type": "fin", "statut": "echec", "duree_s": 75.0},
        ]
        f.write_text("".join(json.dumps(l) + "\n" for l in lignes) + '{"v": 1, "ty',
                     encoding="utf-8")
        texte = ciflux.resume([str(f), str(dossier / "absent.jsonl")])
        self.assertIn("### Tests psycopg 2/4 : rouge, 1 min 15 s", texte)
        self.assertIn("1 echec, 1 succes ; 1 ligne(s) illisible(s)", texte)
        self.assertIn("`test_a` (P0)", texte)
        self.assertIn("`python -m unittest tests.test_b.T.test_y`", texte)
        self.assertIn("AssertionError: 1 != 2", texte)
        self.assertIn("absent.jsonl` absent", texte)

    def test_inacheve(self):
        f = pathlib.Path(tempfile.mkdtemp()) / "flux.jsonl"
        f.write_text(json.dumps({"v": 1, "type": "debut", "part": "psql 1/1"}) + "\n",
                     encoding="utf-8")
        self.assertIn("inachevée", ciflux.resume([str(f)]))


class WorkflowTest(unittest.TestCase):
    def test_parts_ecrivent_et_publient_le_flux(self):
        texte = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for pilote in ("psycopg", "psql"):
            debut = texte.index(f"\n  {pilote}:\n")
            fin = re.compile(r"\n  [#\w]").search(texte, debut + 1).start()
            job = texte[debut:fin]
            self.assertIn("fetch-depth: 2", job, pilote)
            self.assertIn('--depuis HEAD^1 --flux "$RUNNER_TEMP/flux.jsonl"', job, pilote)
            self.assertIn(f"name: flux-{pilote}-${{{{ matrix.part }}}}", job, pilote)
            self.assertIn("actions/upload-artifact@v4", job, pilote)
            # résumé (ancre &resume, reprise par *resume) et artefact, même en échec
            self.assertRegex(job, r"- [&*]resume\b", pilote)
            self.assertIn("if: always()\n        uses: actions/upload-artifact@v4", job, pilote)
        self.assertIn('ci-flux.py resume "$RUNNER_TEMP/flux.jsonl" >> "$GITHUB_STEP_SUMMARY"',
                      texte)


if __name__ == "__main__":
    unittest.main()
