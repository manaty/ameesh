# SPDX-License-Identifier: AGPL-3.0-only
"""L161e — carte test → code mesurée.

`scripts/map.py` découpe le code en blocs à empreinte et fusionne les
mesures des parts (couverture par test, fichiers lus, tables touchées) en
carte `ameesh-carte/1` ; `scripts/map_measure.py` relève les lectures dans
chaque processus ; `tests/support.py` relève les tables par classe ;
`scripts/test-parts.py --map` lit les durées de la carte.
"""
from __future__ import annotations

import datetime
import gzip
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

from unittest import mock

from tests import support

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, REPO / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


carte = _charger("carte_l161e", "map.py")
mesure = _charger("map_measure_l161e", "map_measure.py")
parts = _charger("test_parts_l161e", "test-parts.py")

SOURCE = textwrap.dedent('''\
    """doc"""
    import os

    LIMITE = 3


    def f(x=LIMITE):
        y = x + 1
        return y


    class A:
        nom = "a"

        @staticmethod
        def m():
            return 1

        def m(self):
            return 2


    if os.name:
        def g():
            return 0
''')


class BlocsTest(unittest.TestCase):
    def test_lignes_attribuees_au_bon_bloc(self):
        infos, proprietaire = carte.blocks(SOURCE)
        self.assertEqual(sorted(infos), ["<module>", "A", "A.m", "A.m#2", "f", "g"])
        self.assertEqual(proprietaire[7], "<module>")  # ligne def : exécutée à l'import
        self.assertEqual(proprietaire[8], "f")
        self.assertEqual(proprietaire[13], "A")
        self.assertEqual(proprietaire[15], "A")  # décorateur
        self.assertEqual(proprietaire[17], "A.m")
        self.assertEqual(proprietaire[20], "A.m#2")
        self.assertEqual(proprietaire[24], "<module>")
        self.assertEqual(proprietaire[25], "g")
        self.assertEqual(infos["f"]["lignes"], [8, 9])

    def test_empreinte_sans_numeros_de_ligne(self):
        avant, _ = carte.blocks(SOURCE)
        decale, _ = carte.blocks("# commentaire\n\n" + SOURCE)
        self.assertEqual({k: v["empreinte"] for k, v in avant.items()},
                         {k: v["empreinte"] for k, v in decale.items()})

    def test_corps_change_seul_bloc_change(self):
        avant, _ = carte.blocks(SOURCE)
        apres, _ = carte.blocks(SOURCE.replace("return 2", "return 3"))
        changes = sorted(k for k in avant if avant[k]["empreinte"] != apres[k]["empreinte"])
        self.assertEqual(changes, ["A.m#2"])

    def test_constante_change_le_module(self):
        avant, _ = carte.blocks(SOURCE)
        apres, _ = carte.blocks(SOURCE.replace("LIMITE = 3", "LIMITE = 4"))
        changes = sorted(k for k in avant if avant[k]["empreinte"] != apres[k]["empreinte"])
        self.assertEqual(changes, ["<module>"])

    def test_ancetres(self):
        self.assertEqual(carte._ancestors("A.B.f#2"), ["A.B", "A"])
        self.assertEqual(carte._ancestors("f"), [])


class OutilsTest(unittest.TestCase):
    def test_module_du_contexte(self):
        self.assertEqual(carte.context_module("tests.test_x.Cl.test_y"), "test_x")
        self.assertEqual(carte.context_module("tests.test_x"), "test_x")
        self.assertEqual(carte.context_module("statique|tests.test_x.Cl"), "test_x")
        self.assertIsNone(carte.context_module(""))
        self.assertIsNone(carte.context_module("tests.support"))

    def test_chemin_relatif(self):
        self.assertEqual(carte.relative_path("/home/runner/work/a/a/src/ameesh/db.py"),
                         "src/ameesh/db.py")
        self.assertIsNone(carte.relative_path("/usr/lib/python3/x.py"))

    def test_tables_de_migration(self):
        sql = textwrap.dedent("""\
            -- create table commentee (x int);
            create table if not exists public.alpha (id int);
            alter table only beta add column x int;
            create unique index if not exists g_idx on gamma (x);
            create trigger t after insert on delta for each row execute function f();
            grant select, insert on epsilon, zeta to ameesh_superviseur;
            update eta set x = 1;
            """)
        self.assertEqual(carte.migration_tables(sql),
                         ["alpha", "beta", "delta", "epsilon", "eta", "gamma", "zeta"])

    def test_tables_touchees(self):
        avant = {"a": (1, 0), "b": (2, 2)}
        apres = {"a": (1, 0), "b": (3, 2), "c": (0, 1)}
        self.assertEqual(support.tables_touchees(avant, apres),
                         {"b": {"l": 1, "e": 0}, "c": {"l": 0, "e": 1}})


class ReleveTest(unittest.TestCase):
    """Le hook d'audit, appelé directement (sans être installé)."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.dossier = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        self.addCleanup(shutil.rmtree, self.dossier)
        for nom in ("deploy/sql/role.sql", "src/m.py", "docs/a.md"):
            chemin = pathlib.Path(self.repo, nom)
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text("x")
        self.releve = mesure.Logbook(self.dossier, repo=self.repo)
        ancien = os.environ.get(mesure.ENV_MEASURE_CONTEXT)
        os.environ[mesure.ENV_MEASURE_CONTEXT] = "tests.test_x.Cl.test_y"
        self.addCleanup(lambda: os.environ.__setitem__(mesure.ENV_MEASURE_CONTEXT, ancien)
                        if ancien is not None else os.environ.pop(mesure.ENV_MEASURE_CONTEXT, None))

    def faits(self) -> list[dict]:
        if not os.path.exists(self.releve.chemin):
            return []
        with open(self.releve.chemin, encoding="utf-8") as fh:
            return [json.loads(ligne) for ligne in fh]

    def test_lectures_retenues(self):
        r = self.repo
        self.releve.hook("open", (os.path.join(r, "deploy/sql/role.sql"), "r", os.O_RDONLY))
        self.releve.hook("open", (os.path.join(r, "deploy/sql/role.sql"), "r", os.O_RDONLY))
        self.releve.hook("os.listdir", (os.path.join(r, "deploy/sql"),))
        self.assertEqual(self.faits(), [
            {"c": "tests.test_x.Cl.test_y", "f": "deploy/sql/role.sql"},
            {"c": "tests.test_x.Cl.test_y", "d": "deploy/sql"}])

    def test_lectures_ignorees(self):
        r = self.repo
        self.releve.hook("open", (os.path.join(r, "src/m.py"), "r", os.O_RDONLY))  # code
        self.releve.hook("open", (os.path.join(r, "docs/a.md"), "w", os.O_WRONLY))  # écriture
        self.releve.hook("open", (os.path.join(r, "absent.txt"), "r", os.O_RDONLY))
        self.releve.hook("open", ("/etc/hostname", "r", os.O_RDONLY))  # hors dépôt
        self.releve.hook("open", (3, "r", os.O_RDONLY))  # descripteur
        self.releve.hook("exec", (None,))
        self.assertEqual(self.faits(), [])

    def test_sous_processus_sans_mesure(self):
        env_complet = {"COVERAGE_PROCESS_START": "rc", mesure.ENV_MEASURE_CONTEXT: "c"}
        self.releve.hook("subprocess.Popen", (None, [sys.executable, "-m", "ameesh.cli"],
                                              None, env_complet))
        self.releve.hook("subprocess.Popen", (None, ["git", "status"], None, {}))
        self.releve.hook("subprocess.Popen", (None, [sys.executable, "-m", "ameesh.cli", "x"],
                                              None, {"PATH": "/bin"}))
        self.releve.hook("subprocess.Popen", (None, ["python3", "-c", "1"], None, None))
        self.assertEqual(self.faits(), [{"c": "tests.test_x.Cl.test_y",
                                         "sans_mesure": f"{sys.executable} -m ameesh.cli"}])

    def test_fichiers_en_argument(self):
        sql = os.path.join(self.repo, "deploy/sql/role.sql")
        self.releve.hook("subprocess.Popen", ("psql", ["psql", "-v", "role=x", "-f", sql],
                                              None, None))
        self.releve.hook("subprocess.Popen", ("x", ["x", "--file=deploy/sql/role.sql",
                                                    "src/m.py", "absent"], self.repo, None))
        self.assertEqual(self.faits(), [{"c": "tests.test_x.Cl.test_y",
                                         "f": "deploy/sql/role.sql"}])

    def test_venv_ignore(self):
        venv = pathlib.Path(self.repo, ".venv-carte/lib/site-packages/p/METADATA")
        venv.parent.mkdir(parents=True)
        venv.write_text("x")
        self.releve.hook("open", (str(venv), "r", os.O_RDONLY))
        self.assertEqual(self.faits(), [])

    def test_ligne_pth_inerte_sans_mesure(self):
        env = {k: v for k, v in os.environ.items() if k != mesure.ENV_MEASURE_DIR}
        sortie = subprocess.run([sys.executable, "-c", mesure.pth_line() +
                                 "import sys; print(hasattr(sys, '_ameesh_carte'))"],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(sortie.stdout.strip(), "False", sortie.stderr)


class FusionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dossier = pathlib.Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.dossier)
        part = cls.dossier / "mesure-psycopg-1"
        (part / "lectures").mkdir(parents=True)
        (part / "tables").mkdir()
        (part / "lectures" / "1-a.jsonl").write_text("\n".join(json.dumps(x) for x in [
            {"c": "tests.test_cluster.RoleTest.test_role", "f": "deploy/sql/role-superviseur.sql"},
            {"c": "tests.test_migrations.MTest", "d": "src/ameesh/migrations"},
            {"c": "tests.test_l1.T.test_a", "sans_mesure": "python -m ameesh.cli"},
            {"c": "", "f": "pyproject.toml"},
        ]) + "\n{\"c\": coupée")
        (part / "tables" / "9.jsonl").write_text(json.dumps(
            {"classe": "tests.test_l1.T", "tables": {"agent_mailbox": {"l": 1, "e": 2}}}) + "\n")
        (part / "durees.json").write_text(json.dumps({"psycopg": {"test_l1": 4.0}}))
        (part / "flux.jsonl").write_text(json.dumps(
            {"type": "test", "module": "test_l1", "test": "tests.test_l1.T.test_a",
             "statut": "echec", "ts": "2026-10-11T03:00:00.000Z"}) + "\n")
        cls.maintenant = datetime.datetime(2026, 10, 11, 4, tzinfo=datetime.timezone.utc)
        precedente = {"modules": {
            "test_l1": {"echecs": [{"ts": "2026-10-01T00:00:00+00:00", "test": "vieux"}]},
            "test_l2": {"echecs": [{"ts": "2026-09-01T00:00:00+00:00", "test": "périmé"}]}}}
        cls.c = carte.merge([str(part)], "abc", precedente, cls.maintenant,
                                 table_durees={"psycopg": {"test_l1": 2.0}})

    def test_contenu(self):
        c = self.c
        self.assertEqual(c["schema"], "ameesh-carte/1")
        self.assertEqual(c["donnees"], {"deploy/sql/role-superviseur.sql": ["test_cluster"]})
        self.assertEqual(c["dossiers"], {"src/ameesh/migrations": ["test_migrations"]})
        self.assertEqual(c["tables"], {"agent_mailbox": ["test_l1"]})
        self.assertEqual(c["mesure"]["sous_processus_sans_mesure"],
                         {"test_l1": ["python -m ameesh.cli"]})
        self.assertEqual(c["mesure"]["surcout"], {"psycopg": 2.0})
        # L148 stage 6 : la mesure est émise dans les deux graphies, une release.
        self.assertEqual(c["measure"], c["mesure"])
        self.assertIn("work_items", c["migrations"]["src/ameesh/migrations/0053_work_item_migration.sql"])
        self.assertIn("src/ameesh/radar.py", c["blocs"])
        self.assertIn("src/ameesh/radar.py::github_slug", c["sans_test"])  # rien mesuré ici

    def test_echecs_sur_quatorze_jours(self):
        modules = self.c["modules"]
        self.assertEqual([e["test"] for e in modules["test_l1"]["echecs"]],
                         ["vieux", "tests.test_l1.T.test_a"])
        self.assertEqual(modules["test_l1"]["durees"], {"psycopg": 4.0})
        # L148 stage 6 : les durées sortent aussi en anglais, mêmes valeurs.
        self.assertEqual(modules["test_l1"]["durations"], {"psycopg": 4.0})
        self.assertNotIn("test_l2", modules)

    def test_ecrire_lire_et_durees_pour_les_parts(self):
        chemin = self.dossier / "carte.json.gz"
        carte.write(self.c, str(chemin))
        self.assertEqual(carte.read(str(chemin))["commit"], "abc")
        self.assertEqual(parts.map_durations(chemin), {"psycopg": {"test_l1": 4.0}})
        texte = carte.show(self.c, ["test_cluster", "table:agent_mailbox", "inconnu.txt"])
        self.assertIn("lit deploy/sql/role-superviseur.sql", texte)
        self.assertIn("table:agent_mailbox : test_l1", texte)
        self.assertIn("inconnu.txt : inconnu de la carte", texte)
        with gzip.open(chemin, "wt") as f:
            f.write('{"schema": "autre"}')
        with self.assertRaises(ValueError):
            carte.read(str(chemin))
        self.assertEqual(parts.map_durations(self.dossier / "absente.json.gz"), {})


def _coverage_disponible() -> bool:
    try:
        import coverage  # noqa: F401
    except ImportError:
        return False
    return True


#: #1690 : même sonde, gardée sous son nom d'origine pour ses témoins.
_coverage_installe = _coverage_disponible


class CouvertureTronqueeTest(unittest.TestCase):
    """#1257 : une couverture tronquée (processus tué pendant l'écriture de sa
    donnée) ne fait plus échouer la fusion de la carte ; elle est ignorée et
    nommée, et les autres parts restent mesurées."""

    def _part(self, nom: str = "mesure-psycopg-1") -> pathlib.Path:
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        (dossier / nom / "cov").mkdir(parents=True)
        return dossier / nom

    def _sqlite_tronque(self) -> bytes:
        """Un fichier sqlite valide de plusieurs pages, tronqué de la dernière :
        l'en-tête annonce N pages, N−1 sont écrites (trace de l'incident #1257 :
        processus tué pendant l'écriture, `database disk image is malformed`)."""
        import sqlite3
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        chemin = dossier / "base.sqlite"
        con = sqlite3.connect(chemin)
        with con:
            con.execute("create table t (x integer)")
            con.executemany("insert into t values (?)", [(i,) for i in range(2000)])
        con.close()
        octets = chemin.read_bytes()
        self.assertGreater(len(octets), 4096)
        return octets[:-4096]

    #: la lecture d'un fichier de couverture passe par le paquet `coverage`
    #: (scripts/map.py, `_coverage`) : sans lui, ces cas sont sautés plutôt que
    #: rouges — le pilote psql de la suite complète ne l'installe pas.
    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_tronquee_ignoree_et_nommee(self):
        part = self._part()
        (part / "cov" / ".coverage.tronquee").write_bytes(self._sqlite_tronque())
        carte_ = carte.merge([str(part)], "abc")  # avant #1257 : DataError, fusion morte
        ignorees = carte_["mesure"]["couverture_ignoree"]
        self.assertEqual(len(ignorees), 1)
        self.assertEqual(ignorees[0]["part"], "mesure-psycopg-1")
        self.assertEqual(ignorees[0]["fichier"], ".coverage.tronquee")
        self.assertIn("tronqué", ignorees[0]["raison"])
        self.assertEqual(carte_["mesure"]["processus"], 0)
        resume = carte.summary(carte_)
        self.assertIn("illisible", resume)
        self.assertIn(".coverage.tronquee", resume)

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_entete_absent(self):
        part = self._part()
        (part / "cov" / ".coverage.pas-sqlite").write_bytes(b"pas du sqlite")
        carte_ = carte.merge([str(part)], "abc")
        ignorees = carte_["mesure"]["couverture_ignoree"]
        self.assertEqual(len(ignorees), 1)
        self.assertEqual(ignorees[0]["part"], "mesure-psycopg-1")
        self.assertEqual(ignorees[0]["fichier"], ".coverage.pas-sqlite")
        # le diagnostic d'en-tête nomme la panne, le message de coverage suit
        self.assertIn("en-tête sqlite absent", ignorees[0]["raison"])

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_couverture_vide_ignoree_et_nommee(self):
        """R1 B3 #1257 : un fichier de 0 octet (processus tué avant d'écrire sa
        mesure) se lit sans erreur et ne porte aucune mesure ; il est une perte
        conservatrice nommée, pas un processus mesuré à zéro. Le témoin sain de
        la même part reste mesuré."""
        from coverage import CoverageData
        part = self._part()
        vide = part / "cov" / ".coverage.vide"
        vide.write_bytes(b"")
        valide = CoverageData(basename=str(part / "cov" / ".coverage.valide"))
        valide.set_context("tests.test_l1.T.test_a")
        valide.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {1}})
        valide.write()
        carte_ = carte.merge([str(part)], "abc")
        ignorees = carte_["mesure"]["couverture_ignoree"]
        self.assertEqual(len(ignorees), 1)
        self.assertEqual(ignorees[0]["part"], "mesure-psycopg-1")
        self.assertEqual(ignorees[0]["fichier"], ".coverage.vide")
        self.assertIn("vide (0 octet)", ignorees[0]["raison"])
        self.assertEqual(carte_["mesure"]["processus"], 1)  # seule la valide
        modules = {m for blocs in carte_["blocs"].values()
                   for info in blocs.values() for m in info["modules"]}
        self.assertIn("test_l1", modules)
        self.assertIn(".coverage.vide", carte.summary(carte_))
        # témoin du témoin : sans le fichier vide, la part reste mesurée
        # normalement (aucune perte, un processus) — le contrôle sain.
        vide.unlink()
        saine = carte.merge([str(part)], "abc")
        self.assertEqual(saine["mesure"]["couverture_ignoree"], [])
        self.assertEqual(saine["mesure"]["processus"], 1)

    def test_aucune_couverture_ignoree_sans_corruption(self):
        carte_ = carte.merge([str(self._part())], "abc")
        self.assertEqual(carte_["mesure"]["couverture_ignoree"], [])
        self.assertNotIn("illisible", carte.summary(carte_))

    def _base_page_64k(self) -> pathlib.Path:
        """Une base sqlite saine en pages de 64 Kio : le champ d'en-tête
        « page_size » vaut 1 pour 65536 octets (format sqlite)."""
        import sqlite3
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        chemin = dossier / "base-64k.sqlite"
        con = sqlite3.connect(chemin)
        with con:
            con.execute("create table t (x integer)")
            con.executemany("insert into t values (?)", [(i,) for i in range(2000)])
        con.execute("pragma page_size=65536")
        con.execute("vacuum")
        self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
        self.assertEqual(con.execute("pragma page_size").fetchone()[0], 65536)
        con.close()
        self.assertEqual(int.from_bytes(chemin.read_bytes()[16:18], "big"), 1)
        return chemin

    def test_base_saine_page_64k_non_rejetee(self):
        """R1 #1257 : une base légitime en 64 Kio n'est pas un fichier tronqué
        (l'ancienne lecture du champ 1 la rejetait comme 1 page)."""
        self.assertIsNone(carte._entete_sqlite(str(self._base_page_64k())))

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_sqlite_sans_schema_coverage(self):
        """Second fichier illisible de l'artefact du run 38485229713 : tables de
        coverage présentes, schéma vide (aucune version) — le repli DataError
        l'écarte au lieu de faire échouer la carte."""
        import sqlite3

        from coverage import CoverageData
        part = self._part()
        chemin = part / "cov" / ".coverage.sans-schema"
        donnees = CoverageData(basename=str(chemin))
        donnees.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {1}})
        donnees.write()
        con = sqlite3.connect(chemin)
        with con:
            con.execute("delete from coverage_schema")
            con.execute("delete from meta")
        con.close()
        carte_ = carte.merge([str(part)], "abc")
        ignorees = carte_["mesure"]["couverture_ignoree"]
        self.assertEqual(len(ignorees), 1)
        self.assertIn("DataError", ignorees[0]["raison"])
        self.assertEqual(carte_["mesure"]["processus"], 0)

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_valide_lue_tronquee_ignoree(self):
        from coverage import CoverageData
        part = self._part()
        valide = CoverageData(basename=str(part / "cov" / ".coverage.valide"))
        valide.set_context("tests.test_l1.T.test_a")
        valide.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {1}})
        valide.write()
        (part / "cov" / ".coverage.tronquee").write_bytes(self._sqlite_tronque())
        carte_ = carte.merge([str(part)], "abc")
        self.assertEqual(carte_["mesure"]["processus"], 1)
        self.assertEqual(len(carte_["mesure"]["couverture_ignoree"]), 1)
        modules = {m for blocs in carte_["blocs"].values()
                   for info in blocs.values() for m in info["modules"]}
        self.assertIn("test_l1", modules)  # la part valide reste mesurée

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_couverture_valide_page_64k_mesuree(self):
        """R1 #1257 : une couverture légitime en pages de 64 Kio (champ
        d'en-tête = 1, format sqlite) reste mesurée au lieu d'être écartée."""
        import sqlite3
        from coverage import CoverageData
        chemin = self._base_page_64k()
        donnees = CoverageData(basename=str(chemin))
        donnees.set_context("tests.test_l161e.T.test_64k")
        donnees.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {4}})
        donnees.write()
        con = sqlite3.connect(chemin)
        con.execute("pragma page_size=65536")
        con.execute("vacuum")
        con.close()
        self.assertEqual(int.from_bytes(chemin.read_bytes()[16:18], "big"), 1)
        part = self._part()
        shutil.copy(chemin, part / "cov" / ".coverage.64k")
        carte_ = carte.merge([str(part)], "abc")
        self.assertEqual(carte_["mesure"]["couverture_ignoree"], [])
        self.assertEqual(carte_["mesure"]["processus"], 1)
        modules = {m for blocs in carte_["blocs"].values()
                   for info in blocs.values() for m in info["modules"]}
        self.assertIn("test_l161e", modules)

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_panne_apres_lecture_ecarte_sans_partiel(self):
        """R1 #1257 : une panne après `read()` (ici contexts_by_lineno) écarte
        le fichier et nomme la panne ; rien de sa contribution partielle n'est
        versé dans la carte, les autres fichiers restent mesurés."""
        from coverage import CoverageData
        from coverage.exceptions import DataError

        part = self._part()
        casse = CoverageData(basename=str(part / "cov" / ".coverage.casse"))
        casse.set_context("tests.test_l1.T.test_casse")
        casse.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {1, 2}})
        casse.write()
        valide = CoverageData(basename=str(part / "cov" / ".coverage.valide"))
        valide.set_context("tests.test_l2.T.test_valide")
        valide.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {3}})
        valide.write()

        original = CoverageData.contexts_by_lineno

        def partielle(self, mesure):
            donnees = original(self, mesure)
            if ".coverage.casse" not in str(getattr(self, "_basename", "")):
                return donnees

            class PremiereSeule(dict):
                def items(self):
                    for index, element in enumerate(dict.items(self)):
                        if index:
                            raise DataError("panne simulée en cours d'exploitation")
                        yield element

            return PremiereSeule(donnees)

        CoverageData.contexts_by_lineno = partielle
        self.addCleanup(setattr, CoverageData, "contexts_by_lineno", original)

        carte_ = carte.merge([str(part)], "abc")
        self.assertEqual(carte_["mesure"]["processus"], 1)  # seule la part valide
        ignorees = carte_["mesure"]["couverture_ignoree"]
        self.assertEqual(len(ignorees), 1)
        self.assertEqual(ignorees[0]["fichier"], ".coverage.casse")
        self.assertIn("DataError", ignorees[0]["raison"])
        modules = {m for blocs in carte_["blocs"].values()
                   for info in blocs.values() for m in info["modules"]}
        self.assertIn("test_l2", modules)
        self.assertNotIn("test_l1", modules)  # aucune ligne partielle versée
class _FauxCoverageException(Exception):
    pass


class _FauxDataError(_FauxCoverageException):
    pass


class _FauxCoverageData:
    """CoverageData minimal : `.coverage.coupee` est illisible, le reste vide."""

    def __init__(self, basename=None):
        self.basename = str(basename)

    def read(self):
        if self.basename.endswith(".coverage.coupee"):
            raise _FauxDataError("file is not a database")

    def measured_contexts(self):
        return {"tests.test_l1.T.test_a"}

    def measured_files(self):
        return []

    def contexts_by_lineno(self, fichier):
        return {}


class ArtefactIncompletTest(unittest.TestCase):
    """#1690 : un artefact de mesure illisible est signalé, jamais compté."""

    @staticmethod
    def _faux_module():
        faux = types.ModuleType("coverage")
        faux.CoverageData = _FauxCoverageData
        faux.CoverageException = _FauxCoverageException
        # #1257 x #1690 : l'union lit l'exception dans `coverage.exceptions`
        # (chemin déjà utilisé par #1257) ; la doublure l'expose donc aussi.
        exceptions = types.ModuleType("coverage.exceptions")
        exceptions.CoverageException = _FauxCoverageException
        exceptions.DataError = _FauxDataError
        faux.exceptions = exceptions
        faux.__version__ = "faux"
        return faux

    def _merge(self, dossiers: list[pathlib.Path]) -> dict:
        faux = self._faux_module()
        with mock.patch.dict(sys.modules, {"coverage": faux,
                                           "coverage.exceptions": faux.exceptions}):
            return carte.merge([str(d) for d in dossiers], "abc")

    def test_artefact_illisible_signale_et_non_compte(self):
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        coupee = dossier / "mesure-psycopg-3"
        (coupee / "cov").mkdir(parents=True)
        (coupee / "cov" / ".coverage.coupee").write_bytes(b"tronque")
        lisible = dossier / "mesure-psycopg-1"
        (lisible / "cov").mkdir(parents=True)
        (lisible / "cov" / ".coverage.bon").write_bytes(b"vide")
        c = self._merge([coupee, lisible])
        mesure = c["mesure"]
        self.assertEqual(mesure["processus"], 1)   # le lisible seul compte
        self.assertEqual([i["fichier"] for i in mesure["artefacts_incomplets"]],
                         [os.path.join("cov", ".coverage.coupee")])
        self.assertIn("FauxDataError", mesure["artefacts_incomplets"][0]["erreur"])
        self.assertIn(".coverage.coupee", carte.summary(c))

    @unittest.skipUnless(_coverage_installe(),
                         "coverage requis pour un artefact réellement tronqué")
    def test_fichier_coverage_tronque_reel(self):
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        part = dossier / "mesure-psycopg-3"
        (part / "cov").mkdir(parents=True)
        (part / "cov" / ".coverage").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        c = carte.merge([str(part)], "abc")
        mesure = c["mesure"]
        self.assertEqual(mesure["processus"], 0)
        self.assertEqual([i["fichier"] for i in mesure["artefacts_incomplets"]],
                         [os.path.join("cov", ".coverage")])
        self.assertIn(".coverage", carte.summary(c))

    def test_artefact_vide_signale_sans_lecture(self):
        """#1707 : 0 octet est écarté avant l'import de coverage lui-même."""
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        part = dossier / "mesure-psycopg-4"
        (part / "cov").mkdir(parents=True)
        vide = part / "cov" / ".coverage"
        vide.write_bytes(b"")
        with mock.patch.dict(sys.modules, {"coverage": None}):
            c = carte.merge([str(part)], "abc")
        mesure = c["mesure"]
        self.assertEqual(mesure["processus"], 0)
        self.assertEqual([i["fichier"] for i in mesure["artefacts_incomplets"]],
                         [os.path.join("cov", ".coverage")])
        self.assertIn("0 octet", mesure["artefacts_incomplets"][0]["erreur"])
        self.assertIn(".coverage", carte.summary(c))
        self.assertEqual(vide.stat().st_size, 0)  # read() aurait écrit le schéma

    @unittest.skipUnless(_coverage_installe(),
                         "coverage requis pour un artefact réellement vide")
    def test_fichier_coverage_vide_reel_non_compte(self):
        """#1707 : un .coverage de 0 octet n'est pas un processus mesuré."""
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        part = dossier / "mesure-psycopg-4"
        (part / "cov").mkdir(parents=True)
        vide = part / "cov" / ".coverage"
        vide.write_bytes(b"")
        c = carte.merge([str(part)], "abc")
        mesure = c["mesure"]
        self.assertEqual(mesure["processus"], 0)
        self.assertEqual([i["fichier"] for i in mesure["artefacts_incomplets"]],
                         [os.path.join("cov", ".coverage")])
        self.assertEqual(vide.stat().st_size, 0)  # la lecture n'a pas eu lieu
    @unittest.skipUnless(_coverage_installe(),
                         "coverage requis pour suivre la connexion SQLite")
    def test_artefact_illisible_ferme_la_connexion_sqlite(self):
        # #1716 (accessoire de la relecture dev4) : un `read()` en échec
        # laisse la connexion SQLite ouverte dans `CoverageData._dbs` (le
        # `with` de coverage n'est jamais entré) ; la fusion la ferme au lieu
        # de la laisser au ramasse-miettes.
        import coverage

        fermetures = []

        class Suivi(coverage.CoverageData):
            def close(self, force: bool = False) -> None:
                fermetures.append(force)
                return super().close(force=force)

        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        part = dossier / "mesure-psycopg-3"
        (part / "cov").mkdir(parents=True)
        (part / "cov" / ".coverage").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        with mock.patch.object(coverage, "CoverageData", Suivi):
            c = carte.merge([str(part)], "abc")
        self.assertEqual([i["fichier"] for i in c["mesure"]["artefacts_incomplets"]],
                         [os.path.join("cov", ".coverage")])
        self.assertEqual(fermetures, [True])


def _mesure_possible() -> bool:
    try:
        import coverage  # noqa: F401
    except ImportError:
        return False
    return carte.pth_installed()


@unittest.skipUnless(_mesure_possible(), "coverage et scripts/map.py installer requis")
class BoutEnBoutTest(unittest.TestCase):
    """Une part sous mesure, sous-processus compris (CLI lancée par le test)."""

    def test_part_mesuree(self):
        dossier = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dossier)
        liste = pathlib.Path(dossier, "liste.txt")
        liste.write_text("test_bin_launchers\n")
        env = {k: v for k, v in os.environ.items()
               if k not in (mesure.ENV_MEASURE_DIR, mesure.ENV_MEASURE_CONTEXT, "COVERAGE_PROCESS_START")}
        env.update(PYTHONPATH=str(REPO / "src"), AMEESH_REQUIRE_DB="0")
        m = os.path.join(dossier, "m")
        os.makedirs(m)
        sortie = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "test-parts.py"), "run", "--parts", "1",
             "--part", "1", "--driver", "psycopg", "--list", str(liste), "--measure", m,
             "--output-durations", os.path.join(m, "durees.json")],
            env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(sortie.returncode, 0, sortie.stderr[-2000:])
        c = carte.merge([m], "abc")
        # ameesh.main n'est exécuté que dans les sous-processus des lanceurs
        self.assertEqual(c["blocs"]["src/ameesh/main.py"]["main"]["modules"],
                         ["test_bin_launchers"])
        self.assertGreaterEqual(c["mesure"]["processus"], 2)
        self.assertEqual(c["mesure"]["processus_sans_contexte"], 0)


if __name__ == "__main__":
    unittest.main()
