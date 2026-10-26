# SPDX-License-Identifier: AGPL-3.0-only
"""#3801 — nommer l'unité probable d'un paquet rouge (carte d'impact).

Témoins : la règle pure (`attribute_tests` : test → fichiers → unité, listes
attendues, support importé, sinon non attribuable), le manifeste lu **à la
tête du run** (les recompositions v1/v2 gardent chacune le leur), les fichiers
d'une unité (`git diff <unit>^1 <unit>`, ou le détail du commit GitHub sans
clone), et la provenance de la carte (clone, cache, téléchargement, périmée).
Aucun retrait : un candidat n'est pas une culpabilité.
"""
from __future__ import annotations

import gzip
import json
import os
import pathlib
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from ameesh import packet_red


def card(**over):
    out = {"schema": "ameesh-carte/1", "commit": "c" * 40,
           "genere": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "blocs": {}, "donnees": {}, "dossiers": {}, "tables": {}}
    out.update(over)
    return out


def unit(lot, *files, sha="a" * 40):
    return {"lot": int(lot), "unit_sha": sha, "files": list(files)}


class AttributeTest(unittest.TestCase):
    def run_one(self, files, *, source="", card_data=None, tables=None, reads=None,
                dirs=None):
        data = card_data or card()
        if tables:
            data["tables"] = {name: ["test_a"] for name in tables}
        if reads:
            data["donnees"] = {name: ["test_a"] for name in reads}
        if dirs:
            data["dossiers"] = {name: ["test_a"] for name in dirs}
        return packet_red.attribute_tests(
            [{"test": "tests.test_a.T.test_1", "part": "p", "module": "test_a"}],
            [unit(101, *files)], packet_red.card_index(data),
            sources=lambda module: source)

    def test_fichier_du_test(self):
        out = self.run_one(["tests/test_a.py"])
        self.assertEqual(out[0]["status"], "probable")
        self.assertEqual(out[0]["units"][0]["lot"], 101)
        self.assertEqual(out[0]["units"][0]["evidence"][0]["kind"], "test")

    def test_module_couvert(self):
        data = card(blocs={"src/ameesh/x.py": {"f": {"modules": ["test_a"]}}})
        out = self.run_one(["src/ameesh/x.py"], card_data=data)
        self.assertEqual(out[0]["units"][0]["lot"], 101)
        self.assertEqual(out[0]["units"][0]["evidence"][0]["kind"], "covered")

    def test_liste_des_parts(self):
        out = self.run_one(["tests/parts/toujours.txt"],
                           reads=["tests/parts/toujours.txt"])
        self.assertEqual(out[0]["units"][0]["evidence"][0]["kind"], "list")

    def test_liste_des_roles(self):
        out = self.run_one(["deploy/sql/role-superviseur.sql"],
                           reads=["deploy/sql/role-superviseur.sql"])
        self.assertEqual(out[0]["units"][0]["evidence"][0]["kind"], "list")

    def test_migration_seulement_si_le_test_touche_une_table(self):
        files = ["src/ameesh/migrations/0490_account_force_expiry.sql"]
        without = self.run_one(files, dirs=["src/ameesh/migrations"])
        self.assertEqual(without[0]["status"], "unattributable")
        with_tables = self.run_one(files, dirs=["src/ameesh/migrations"],
                                   tables=["account_active"])
        self.assertEqual(with_tables[0]["units"][0]["evidence"][0]["kind"],
                         "migration")

    def test_support_importe(self):
        out = self.run_one(["tests/support.py"],
                           source="from .support import PgTestCase\n")
        self.assertEqual(out[0]["units"][0]["lot"], 101)
        self.assertEqual(out[0]["units"][0]["evidence"][0]["kind"], "support")

    def test_preuve_forte_ne_noie_pas_la_couverture(self):
        data = card(blocs={"src/ameesh/x.py": {"f": {"modules": ["test_a"]}}},
                    donnees={"tests/parts/toujours.txt": ["test_a"]})
        out = packet_red.attribute_tests(
            [{"test": "tests.test_a.T.test_1", "module": "test_a"}],
            [unit(101, "src/ameesh/x.py"), unit(102, "tests/parts/toujours.txt")],
            packet_red.card_index(data), sources=lambda module: "")
        self.assertEqual([row["lot"] for row in out[0]["units"]], [102])

    def test_non_attribuable(self):
        out = self.run_one(["src/ameesh/inconnu.py"])
        self.assertEqual(out[0]["status"], "unattributable")
        self.assertEqual(out[0]["units"], [])
        self.assertEqual(packet_red.evidence_line(out[0]), "non attribuable")

    def test_unite_sans_fichier_ne_conclut_pas(self):
        out = packet_red.attribute_tests(
            [{"test": "tests.test_a.T.t", "module": "test_a"}],
            [unit(101)], packet_red.card_index(card()),
            sources=lambda module: "")
        self.assertEqual(out[0]["status"], "unattributable")

    def test_trois_unites_au_plus(self):
        files = ["src/ameesh/%d.py" % i for i in range(5)]
        data = card(blocs={path: {"f": {"modules": ["test_a"]}} for path in files})
        out = packet_red.attribute_tests(
            [{"test": "tests.test_a.T.t", "module": "test_a"}],
            [unit(100 + i, path) for i, path in enumerate(files)],
            packet_red.card_index(data), sources=lambda module: "")
        self.assertEqual(len(out[0]["units"]), 3)
        self.assertEqual(out[0]["more"], 2)


class ModuleTest(unittest.TestCase):
    def test_module_du_nom_du_test(self):
        self.assertEqual(packet_red.module_of({"test": "tests.test_a.T.t"}), "test_a")
        self.assertEqual(packet_red.module_of(
            {"test": "setUpClass (tests.test_b.T)"}), "test_b")
        self.assertEqual(packet_red.module_of(
            {"test": "x", "module": "tests/test_c.py"}), "test_c")
        self.assertEqual(packet_red.module_of({"test": "sans module"}), "")


class ImportTest(unittest.TestCase):
    def test_imports_relatifs_et_paquet(self):
        text = ("from .support import PgTestCase\n"
                "from .test_canon import x\n"
                "from tests.support import PgTestCase\n"
                "from . import gabarit, support\n"
                "import tests.fixtures.mod\n")
        self.assertEqual(packet_red.imported_test_files(text),
                         {"tests/support.py", "tests/test_canon.py",
                          "tests/gabarit.py", "tests/fixtures.py"})


class GitRepo:
    """Un dépôt jetable : une base, une unité fusionnée, un manifeste à la tête."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="packet-red-")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "tests")
        self.write("README", "base\n")
        self.commit("base")
        self.base = self.rev("HEAD")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.path, *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def write(self, path, text):
        full = os.path.join(self.path, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        pathlib.Path(full).write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.rev("HEAD")

    def rev(self, ref):
        return self.git("rev-parse", ref)

    def unit(self, lot, files):
        branch = "unit-%d" % lot
        self.git("checkout", "-q", "-b", branch, self.base)
        for path, text in files.items():
            self.write(path, text)
        self.commit("unit %d" % lot)
        source = self.rev("HEAD")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "unit %d\n\nameesh-work: %d" % (lot, lot), branch)
        return source, self.rev("HEAD")

    def manifest(self, units):
        lines = ["# Paquet integration/paquet-h-test", "",
                 "## Composition (%d unité(s), %d lot(s))" % (len(units), len(units)), "",
                 "| unité | lot | source | commit |", "|---|---|---|---|"]
        for index, (lot, source, commit) in enumerate(units, 1):
            lines.append("| %d | #%d | `%s` | `%s` |" % (index, lot, source, commit))
        self.write("reviews/integration-paquet-h-test.md", "\n".join(lines) + "\n")
        return self.commit("manifeste")

    def close(self):
        subprocess.run(["rm", "-rf", self.path], check=False)


class CompositionTest(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepo()
        self.addCleanup(self.repo.close)

    def test_manifeste_a_la_tete_et_fichiers_de_l_unite(self):
        source, merge = self.repo.unit(101, {"src/ameesh/x.py": "x\n"})
        head = self.repo.manifest([(101, source, merge)])
        item = {"id": 1, "branch": "integration/paquet-h-test"}
        comp = packet_red.composition(None, item, head, checkout=self.repo.path)
        self.assertEqual(comp["source"], "manifest")
        self.assertEqual([row["lot"] for row in comp["members"]], [101])
        self.assertEqual(comp["members"][0]["unit_sha"], merge)
        units = packet_red.unit_files(comp["members"], checkout=self.repo.path)
        self.assertEqual(units[0]["files"], ["src/ameesh/x.py"])
        self.assertEqual(units[0]["files_from"], "git")

    def test_deux_recompositions_gardent_leur_manifeste(self):
        source1, merge1 = self.repo.unit(101, {"src/ameesh/un.py": "1\n"})
        head1 = self.repo.manifest([(101, source1, merge1)])
        source2, merge2 = self.repo.unit(102, {"src/ameesh/deux.py": "2\n"})
        head2 = self.repo.manifest([(102, source2, merge2)])
        item = {"id": 1, "branch": "integration/paquet-h-test"}
        first = packet_red.composition(None, item, head1, checkout=self.repo.path)
        second = packet_red.composition(None, item, head2, checkout=self.repo.path)
        self.assertEqual([row["lot"] for row in first["members"]], [101])
        self.assertEqual([row["lot"] for row in second["members"]], [102])

    def test_sans_manifeste_le_statut_le_dit(self):
        item = {"id": 1, "branch": "integration/paquet-h-test"}
        out = packet_red.attribute_run(
            None, item, {"head_sha": self.repo.base},
            [{"test": "tests.test_a.T.t", "module": "test_a"}],
            checkout=self.repo.path, card=card(), card_known=True)
        self.assertEqual(out["status"], "no-manifest")
        self.assertIn("manifeste", out["why"])

    def test_enregistrement_seulement_si_la_tete_est_la_meme(self):
        head = "c" * 40
        rows = [{"work_item_id": 101, "unit_sha": "a" * 40,
                 "source_sha": "b" * 40, "frozen_sha": "", "head_sha": head,
                 "recorded_ts": 1.0}]

        class Ops:
            def packet_members(self, **kwargs):
                return rows

        item = {"id": 1, "branch": "integration/paquet-h-test"}
        with mock.patch.object(packet_red.storage, "of",
                               return_value=types.SimpleNamespace(work=Ops())):
            same = packet_red.composition(None, item, head)
            other = packet_red.composition(None, item, "d" * 40)
        self.assertEqual(same["source"], "record")
        self.assertEqual(same["members"][0]["lot"], 101)
        self.assertEqual(other["members"], [])
        self.assertIn("pas pour la tête", other["why"])


class UnitFilesTest(unittest.TestCase):
    def test_api_sans_clone(self):
        class Api:
            def __call__(self, path):
                return {"files": [{"filename": "src/ameesh/api.py"},
                                  {"filename": "tests/test_api.py"}]}

        units = packet_red.unit_files(
            [{"lot": 7, "unit_sha": "b" * 40}], api=Api(), repo="manaty/ameesh")
        self.assertEqual(units[0]["files"], ["src/ameesh/api.py", "tests/test_api.py"])
        self.assertEqual(units[0]["files_from"], "github")

    def test_api_muette_ne_leve_pas(self):
        def api(path):
            raise RuntimeError("403")

        units = packet_red.unit_files(
            [{"lot": 7, "unit_sha": "b" * 40}], api=api, repo="manaty/ameesh")
        self.assertEqual(units[0]["files"], [])
        self.assertEqual(units[0]["files_from"], "")


class CarteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="packet-red-carte-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(data, stream)

    def test_carte_du_clone(self):
        path = os.path.join(self.tmp, "carte", "carte.json.gz")
        self.write(path, card())
        found, prov = packet_red.carte_for(None, "manaty/ameesh", repo=self.tmp)
        self.assertIsNotNone(found)
        self.assertEqual(prov["origin"], "local")
        self.assertEqual(prov["status"], "lue")

    def test_cache_de_l_hote(self):
        cfg = types.SimpleNamespace(state_dir=self.tmp)
        path = os.path.join(self.tmp, "packet-red", "carte-manaty-ameesh.json.gz")
        self.write(path, card())
        found, prov = packet_red.carte_for(cfg, "manaty/ameesh", download=False)
        self.assertIsNotNone(found)
        self.assertEqual(prov["origin"], "cache")

    def test_telechargement_et_cache(self):
        source = os.path.join(self.tmp, "download", "carte.json.gz")
        self.write(source, card())
        cfg = types.SimpleNamespace(state_dir=os.path.join(self.tmp, "state"))
        found, prov = packet_red.carte_for(
            cfg, "manaty/ameesh", fetch=lambda: (source, ""))
        self.assertIsNotNone(found)
        self.assertEqual(prov["origin"], "téléchargée")
        self.assertTrue(os.path.isfile(os.path.join(
            self.tmp, "state", "packet-red", "carte-manaty-ameesh.json.gz")))

    def test_carte_perimee_et_trop_vieille(self):
        old = time.time() - 3 * 24 * 3600
        stale = card(genere=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old)))
        path = os.path.join(self.tmp, "carte", "carte.json.gz")
        self.write(path, stale)
        found, prov = packet_red.carte_for(None, "manaty/ameesh", repo=self.tmp)
        self.assertIsNotNone(found)
        self.assertEqual(prov["status"], "périmée")
        too_old = time.time() - 8 * 24 * 3600
        self.write(path, card(genere=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(too_old))))
        found, prov = packet_red.carte_for(None, "manaty/ameesh", repo=self.tmp,
                                           download=False)
        self.assertIsNone(found)
        self.assertEqual(prov["status"], "absente")

    def test_schema_inconnu_absent(self):
        path = os.path.join(self.tmp, "carte", "carte.json.gz")
        self.write(path, {"schema": "autre/1"})
        found, prov = packet_red.carte_for(None, "manaty/ameesh", repo=self.tmp,
                                           download=False)
        self.assertIsNone(found)
        self.assertEqual(prov["status"], "absente")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepo()
        self.addCleanup(self.repo.close)
        self.source, self.merge = self.repo.unit(101, {"src/ameesh/x.py": "x\n"})
        self.head = self.repo.manifest([(101, self.source, self.merge)])
        self.item = {"id": 1, "branch": "integration/paquet-h-test"}
        self.run = {"head_sha": self.head}

    def test_nomme_l_unite_par_la_carte(self):
        data = card(blocs={"src/ameesh/x.py": {"f": {"modules": ["test_a"]}}})
        out = packet_red.attribute_run(
            None, self.item, self.run,
            [{"test": "tests.test_a.T.t", "part": "p", "module": "test_a"}],
            checkout=self.repo.path, card=data, card_prov={"status": "lue"},
            card_known=True)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["manifest"]["units"], 1)
        self.assertEqual(out["tests"][0]["units"][0]["lot"], 101)
        self.assertEqual(out["tests"][0]["units"][0]["evidence"][0]["kind"], "covered")
        self.assertIn("manifeste 1 unité", packet_red.note_of(out))

    def test_sans_carte_les_preuves_directes_restent(self):
        source2, merge2 = self.repo.unit(
            102, {"tests/test_a.py": "from .support import PgTestCase\n"})
        self.head = self.repo.manifest([(101, self.source, self.merge),
                                        (102, source2, merge2)])
        self.run = {"head_sha": self.head}
        # sans carte, le fichier du test reste une preuve lisible
        out = packet_red.attribute_run(
            None, self.item, self.run,
            [{"test": "tests.test_a.T.t", "module": "test_a"}],
            checkout=self.repo.path, card=None, card_known=True)
        self.assertEqual(out["status"], "no-card")
        self.assertEqual(out["tests"][0]["units"][0]["lot"], 102)
        self.assertEqual(out["tests"][0]["units"][0]["evidence"][0]["kind"], "test")

    def test_porteur_hors_paquet_ignore(self):
        out = packet_red.attribute_run(
            None, {"id": 1, "branch": "lot/1-x"}, self.run,
            [{"test": "tests.test_a.T.t", "module": "test_a"}],
            checkout=self.repo.path, card=card(), card_known=True)
        self.assertEqual(out["status"], "not-carrier")


class RecordedRunTest(unittest.TestCase):
    """Sans manifeste : la composition `work packet` et les fichiers à l'API."""

    def test_enregistrement_et_fichiers_api(self):
        head = "c" * 40
        rows = [{"work_item_id": 101, "unit_sha": "a" * 40,
                 "source_sha": "b" * 40, "frozen_sha": "", "head_sha": head,
                 "recorded_ts": 1.0}]

        class Ops:
            def packet_members(self, **kwargs):
                return rows

        class Api:
            def __call__(self, path):
                if path.startswith("repos/manaty/ameesh/commits/"):
                    return {"files": [{"filename": "src/ameesh/x.py"}]}
                return {}

        item = {"id": 1, "branch": "integration/paquet-h-test",
                "issue_ref": "manaty/ameesh#1"}
        data = card(blocs={"src/ameesh/x.py": {"f": {"modules": ["test_a"]}}},
                    commit=head)
        failures = [{"test": "tests.test_a.T.t", "part": "p", "module": "test_a"}]
        with mock.patch.object(packet_red.storage, "of",
                               return_value=types.SimpleNamespace(work=Ops())):
            out = packet_red.attribute_run(
                None, item, {"head_sha": head}, failures, api=Api(), card=data,
                card_prov={"commit": head}, card_known=True, checkout="")
        self.assertEqual(out["manifest"]["source"], "record")
        self.assertEqual(out["manifest"]["files_from"], ["github"])
        self.assertEqual(out["tests"][0]["units"][0]["lot"], 101)

    def test_sans_fichiers_le_statut_le_dit(self):
        head = "c" * 40
        rows = [{"work_item_id": 101, "unit_sha": "a" * 40,
                 "source_sha": "b" * 40, "frozen_sha": "", "head_sha": head,
                 "recorded_ts": 1.0}]

        class Ops:
            def packet_members(self, **kwargs):
                return rows

        item = {"id": 1, "branch": "integration/paquet-h-test",
                "issue_ref": "manaty/ameesh#1"}
        with mock.patch.object(packet_red.storage, "of",
                               return_value=types.SimpleNamespace(work=Ops())):
            out = packet_red.attribute_run(
                None, item, {"head_sha": head},
                [{"test": "tests.test_a.T.t", "module": "test_a"}],
                card=card(commit=head), card_prov={"commit": head},
                card_known=True, checkout="")
        self.assertEqual(out["status"], "no-files")
        self.assertEqual(out["tests"][0]["status"], "unattributable")
        self.assertIn("fichiers", out["why"])


class TextTest(unittest.TestCase):
    def test_ligne_et_note(self):
        entry = {"status": "probable", "more": 1,
                 "units": [{"lot": 3755, "evidence": [
                     {"kind": "migration",
                      "file": "src/ameesh/migrations/0490_account_force_expiry.sql"},
                     {"kind": "covered", "file": "src/ameesh/accounts.py"}]}]}
        text = packet_red.evidence_line(entry)
        self.assertIn("#3755", text)
        self.assertIn("migration 0490_account_force_expiry.sql", text)
        self.assertIn("module couvert src/ameesh/accounts.py", text)
        self.assertIn("et 1 autre", text)
        note = packet_red.note_of({"manifest": {"units": 14},
                                   "card": {"status": "lue", "commit": "c" * 40,
                                            "age_s": 7200}})
        self.assertIn("manifeste 14 unité(s)", note)
        self.assertIn("carte d'impact cccccccccccc (2h)", note)
        self.assertIn("aucun retrait", note)


if __name__ == "__main__":
    unittest.main()
